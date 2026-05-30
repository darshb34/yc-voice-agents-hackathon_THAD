"""CSV/JSON-backed meal + restaurant catalog for the Tetris Nutrition Coach.

Loads two datasets shipped with the build and normalizes them into one meal
shape the tools consume:

  * ``data/healthy_meal_recipes.json`` — 12 cook-at-home recipes (source="recipe")
  * ``data/sf_94122_menus.csv``        — 85 SF restaurant dishes (source="restaurant")

Normalization unifies macros, diet compatibility, allergens (canonical tokens),
slots, and a spoken-friendly "where to find it" string. ``find_meals`` filters by
diet (compat), allergies (hard safety filter), slot, craving, and source, then
ranks by fit to the remaining budget. ``search_restaurants`` is the eat-out path,
drawn from the SF dataset (deterministic — no live API).

This is the swap-to-real-API boundary: replace the loaders and everything above
keeps working.
"""

from __future__ import annotations

import csv
import json
import os
from functools import lru_cache

from nutrition import rank_by_fit

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
RECIPES_PATH = os.path.join(DATA_DIR, "healthy_meal_recipes.json")
MENUS_PATH = os.path.join(DATA_DIR, "sf_94122_menus.csv")

DIET_STYLES = ("vegan", "vegetarian", "pescatarian")  # omnivore matches everything

# --- allergen normalization --------------------------------------------------
# Map free-text allergen mentions to the canonical tokens the intake tool records
# (peanut, tree_nut, dairy, egg, fish, shellfish, soy, gluten, sesame, corn, ...).
_ALLERGEN_RULES = [
    ("tree_nut", ("tree nut", "treenut", "almond", "coconut", "pine nut", "cashew",
                  "walnut", "pecan", "hazelnut", "pistachio")),
    ("shellfish", ("shellfish", "shrimp", "crab", "prawn", "oyster", "clam",
                   "lobster", "mussel", "scallop", "crawfish")),
    ("fish", ("fish", "anchovy", "salmon", "tuna", "cod")),
    ("dairy", ("dairy", "milk", "cheese", "parmesan", "butter", "cream",
               "yogurt", "feta", "mozzarella")),
    ("egg", ("egg",)),
    ("soy", ("soy", "tofu", "edamame", "tamari")),
    ("sesame", ("sesame", "tahini")),
    ("gluten", ("gluten", "wheat", "barley", "rye")),
    ("corn", ("corn",)),
    ("peanut", ("peanut",)),
    ("pork", ("pork",)),
]


def normalize_allergens(raw: object) -> list[str]:
    """Turn an allergen field (list or comma-string) into canonical tokens.

    Order-stable, de-duplicated. ``shellfish`` is detected before ``fish`` so
    "shellfish" doesn't also register as "fish".
    """
    if isinstance(raw, str):
        parts = [p for p in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        parts = list(raw)
    else:
        parts = []
    text = " , ".join(str(p).lower() for p in parts)
    found: list[str] = []
    for token, needles in _ALLERGEN_RULES:
        if token == "fish" and "shellfish" in text and not any(
            n in text for n in ("fish stock", "fish sauce", "anchovy", "salmon", "tuna", "cod")
        ) and "fish" not in text.replace("shellfish", ""):
            continue  # "shellfish" only — don't double-count as fish
        if any(n in text for n in needles) and token not in found:
            found.append(token)
    return found


# --- diet inference (restaurant dishes lack explicit veg/pesc flags) ----------
_MEAT_WORDS = (
    "beef", "steak", "chicken", "pork", "bacon", "ham", "lamb", "turkey", "duck",
    "quail", "sausage", "carnitas", "chorizo", "andouille", "prosciutto", "meatball",
    "burger", "gyro", "shawarma", "prime rib", "pho tai", "bun thit", "banh mi",
    "shaking beef", "carne", "joe's special",
)
_SEAFOOD_WORDS = (
    "fish", "crab", "shrimp", "prawn", "oyster", "clam", "tuna", "salmon", "poke",
    "ceviche", "cod", "sand dab", "calamari", "squid", "scallop", "mussel", "sushi",
    "nigiri", "sashimi", "hamachi", "cioppino", "anchovy", "dungeness", "seafood",
    "chowder", "crawfish",
)


def _diets_for_restaurant(name: str, desc: str, cuisine: str, is_vegan: bool) -> list[str]:
    """Infer which diet styles a restaurant dish satisfies. Conservative against
    false-vegetarian: any meat keyword => omnivore-only."""
    hay = f"{name} {desc} {cuisine}".lower()
    if is_vegan:
        return ["vegan", "vegetarian", "pescatarian"]
    has_meat = any(w in hay for w in _MEAT_WORDS)
    has_seafood = any(w in hay for w in _SEAFOOD_WORDS)
    if has_meat:
        return []  # omnivore-only (matched via the "omnivore matches all" rule)
    if has_seafood:
        return ["pescatarian"]
    return ["vegetarian", "pescatarian"]  # no animal-flesh keywords -> treat as vegetarian


# --- slot inference -----------------------------------------------------------
_BREAKFAST_WORDS = (
    "breakfast", "brunch", "pancake", "egg", "omelet", "frittata", "benedict",
    "yogurt", "oat", "granola", "toast", "croissant", "beignet", "pastry", "muffin",
    "kouign", "morning bun", "coffee", "espresso", "latte", "soda bread",
    "french toast", "hash brown", "snowy plover",
)


def _slots_for(name: str, desc: str, calories: int) -> list[str]:
    hay = f"{name} {desc}".lower()
    if any(w in hay for w in _BREAKFAST_WORDS):
        return ["breakfast"]
    if calories and calories < 250:
        return ["snack"]
    return ["lunch", "dinner"]


# --- loaders ------------------------------------------------------------------

def _load_recipes() -> list[dict]:
    with open(RECIPES_PATH) as f:
        data = json.load(f)
    meals = []
    for r in data.get("recipes", []):
        n = r["nutrition_per_serving"]
        macros = {k: n.get(k, 0) for k in ("calories", "protein_g", "carbs_g", "fat_g")}
        diets = [d for d in DIET_STYLES if r.get(f"is_{d}")]
        meals.append({
            "name": r["name"],
            "source": "recipe",
            "macros": macros,
            "diets": diets,
            "contains": normalize_allergens(r.get("allergens")),
            "tags": [t.lower() for t in r.get("main_ingredients", [])]
                    + [r.get("diet_category", "").lower()],
            "where_to_find": f"a home recipe (about {r.get('total_min', '?')} minutes)",
            "slots": _slots_for(r["name"], r.get("why_healthy", ""), macros["calories"]),
            "restaurant": None,
            "cuisine": "home cooking",
            "price": None,
            "why_healthy": r.get("why_healthy"),
        })
    return meals


def _yn(val: str) -> bool:
    return str(val).strip().upper().startswith("Y")


def _load_menus() -> list[dict]:
    meals = []
    with open(MENUS_PATH, newline="") as f:
        for row in csv.DictReader(f):
            name = row["Menu Item"].strip()
            desc = row.get("Description", "")
            cuisine = row.get("Cuisine Type", "").strip()
            try:
                macros = {
                    "calories": int(float(row["Estimated Calories"])),
                    "protein_g": int(float(row["Estimated Protein (g)"])),
                    "carbs_g": int(float(row["Estimated Carbs (g)"])),
                    "fat_g": int(float(row["Estimated Fat (g)"])),
                }
            except (ValueError, KeyError):
                continue
            is_vegan = _yn(row.get("Is Vegan (Y/N)", "N"))
            restaurant = row["Restaurant Name"].strip()
            try:
                price = float(row.get("Approx Price (USD)") or 0) or None
            except ValueError:
                price = None
            tags = [cuisine.lower(), "restaurant"]
            if _yn(row.get("Is Healthy (Y/N)", "N")):
                tags.append("healthy")
            meals.append({
                "name": name,
                "source": "restaurant",
                "macros": macros,
                "diets": _diets_for_restaurant(name, desc, cuisine, is_vegan),
                "contains": normalize_allergens(row.get("Allergy Content")),
                "tags": tags,
                "where_to_find": f"{restaurant} ({row.get('Address', 'San Francisco')})",
                "slots": _slots_for(name, desc, macros["calories"]),
                "restaurant": restaurant,
                "cuisine": cuisine,
                "price": price,
                "address": row.get("Address"),
            })
    return meals


@lru_cache(maxsize=1)
def get_catalog() -> tuple[dict, ...]:
    """Full normalized catalog (recipes + restaurant dishes), loaded once."""
    return tuple(_load_recipes() + _load_menus())


def _matches(meal: dict, diet_style: str, allergy_set: set, slot: str, src: str, crave: str) -> bool:
    if allergy_set & set(meal["contains"]):
        return False  # safety: never suggest a stated allergen (hard filter)
    if diet_style and diet_style != "omnivore" and diet_style not in meal["diets"]:
        return False
    if slot and slot not in meal["slots"]:
        return False
    if src and meal["source"] != src:
        return False
    if crave:
        haystack = meal["name"].lower() + " " + " ".join(meal["tags"]) + " " + (meal.get("cuisine") or "").lower()
        if crave not in haystack:
            return False
    return True


def find_meals(
    slot: str | None = None,
    craving: str | None = None,
    source_type: str | None = None,
    diet_style: str | None = None,
    allergies: list | None = None,
    remaining: dict | None = None,
    max_results: int = 5,
) -> list[dict]:
    """Filter the catalog and rank best-fit first. Single funnel for recommendations.

    Allergies are a hard safety filter (any overlap excludes the meal). Diet must
    be compatible ("omnivore"/None matches all). ``craving`` is matched against the
    meal name, tags, and cuisine. Ranked by fit to ``remaining`` (see nutrition).
    """
    allergy_set = set()
    for a in allergies or []:
        allergy_set.update(normalize_allergens([a]))
    diet = (diet_style or "").strip().lower()
    src = (source_type or "").strip().lower()
    crave = (craving or "").strip().lower()
    slot_f = (slot or "").strip().lower()

    matches = [m for m in get_catalog() if _matches(m, diet, allergy_set, slot_f, src, crave)]
    ranked = rank_by_fit(matches, remaining)
    return [dict(m) for m in ranked[: max(1, max_results)]]


def search_restaurants(
    location: str | None = None,
    cuisine: str | None = None,
    diet_style: str | None = None,
    allergies: list | None = None,
    remaining: dict | None = None,
    max_results: int = 4,
) -> list[dict]:
    """Eat-out path: restaurant dishes near the member (SF dataset), filtered by
    cuisine/diet/allergies and ranked by fit. Each result names the dish, its
    restaurant + address, and macros. Deterministic (no live API).
    """
    kw = {
        "source_type": "restaurant",
        "diet_style": diet_style,
        "allergies": allergies,
        "remaining": remaining,
        "max_results": max_results,
    }
    results = find_meals(craving=cuisine, **kw)
    if not results and cuisine:
        # No dish matched that cuisine keyword — relax it (but keep the diet and
        # allergy filters intact; those are never relaxed) so we still surface
        # nearby options instead of going silent.
        results = find_meals(craving=None, **kw)
    # Shape for the eat-out tool: lead with restaurant + dish.
    return [
        {
            "restaurant": m["restaurant"],
            "dish": m["name"],
            "address": m.get("address"),
            "cuisine": m.get("cuisine"),
            "macros": m["macros"],
            "price": m.get("price"),
        }
        for m in results
    ]


def get_meal(name: str) -> dict | None:
    """Look up a single catalog meal by name, case-insensitively."""
    key = (name or "").strip().lower()
    for m in get_catalog():
        if m["name"].lower() == key:
            return dict(m)
    return None
