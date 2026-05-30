#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Mock backend data + data-access layer for the Tetris Nutrition voice coach.

This is the ONE file to change when wiring the coach to the real Tetris API: the
bot's tool functions never touch the
``MEALS`` / ``KNOWN_MEMBERS`` dicts directly — they only call the helper
functions at the bottom (``get_meals``, ``get_meal``, ``find_meals``,
``calc_targets``). Swap those bodies for ``GET /v1/meals`` /
``POST /v1/meals/search`` / ``GET /v1/members/{id}/targets`` and the tools are
unchanged.

Conventions:
- Macros are ``{calories, protein_g, carbs_g, fat_g}``.
- Meal names are lowercased on lookup.
- ``diets`` lists the diet styles a meal is compatible with.
- ``contains`` lists allergens / excludable ingredients (lowercase), screened
  against the member's allergies and restrictions.
- Member phone numbers are E.164 to match Twilio's ``from_number``.
"""

# --- Catalog -----------------------------------------------------------------

MEALS = {
    "grilled chicken power bowl": {
        "macros": {"calories": 520, "protein_g": 48, "carbs_g": 42, "fat_g": 16},
        "source": "ready-to-eat",
        "where_to_find": "Sweetgreen or any salad spot — grilled chicken, quinoa, greens",
        "tags": ["high-protein", "quick", "gluten-free", "lunch", "dinner"],
        "diets": ["omnivore", "pescatarian", "paleo"],
        "contains": ["chicken"],
    },
    "salmon teriyaki plate": {
        "macros": {"calories": 610, "protein_g": 40, "carbs_g": 55, "fat_g": 22},
        "source": "restaurant",
        "where_to_find": "Most Japanese spots — ask for the sauce on the side",
        "tags": ["high-protein", "dinner"],
        "diets": ["omnivore", "pescatarian"],
        "contains": ["fish", "soy", "gluten"],
    },
    "tofu veggie stir-fry": {
        "macros": {"calories": 430, "protein_g": 26, "carbs_g": 38, "fat_g": 18},
        "source": "recipe",
        "where_to_find": "20-min recipe — tofu, broccoli, peppers, tamari, brown rice",
        "tags": ["vegan", "high-protein", "quick", "lunch", "dinner"],
        "diets": ["vegan", "vegetarian", "omnivore", "pescatarian"],
        "contains": ["soy"],
    },
    "greek yogurt and berries": {
        "macros": {"calories": 220, "protein_g": 20, "carbs_g": 24, "fat_g": 5},
        "source": "ready-to-eat",
        "where_to_find": "Any grocery — 2% Greek yogurt with a handful of berries",
        "tags": ["high-protein", "quick", "vegetarian", "snack", "breakfast"],
        "diets": ["vegetarian", "omnivore", "pescatarian"],
        "contains": ["dairy"],
    },
    "steak and sweet potato": {
        "macros": {"calories": 640, "protein_g": 46, "carbs_g": 40, "fat_g": 30},
        "source": "recipe",
        "where_to_find": "Recipe — sirloin, roasted sweet potato, asparagus",
        "tags": ["high-protein", "paleo", "dinner"],
        "diets": ["omnivore", "paleo"],
        "contains": ["beef"],
    },
    "egg-white veggie scramble": {
        "macros": {"calories": 310, "protein_g": 28, "carbs_g": 12, "fat_g": 16},
        "source": "recipe",
        "where_to_find": "Recipe — egg whites, spinach, mushrooms, a little feta",
        "tags": ["high-protein", "keto", "vegetarian", "breakfast"],
        "diets": ["vegetarian", "omnivore", "keto", "pescatarian"],
        "contains": ["eggs", "dairy"],
    },
    "chipotle chicken burrito bowl": {
        "macros": {"calories": 700, "protein_g": 45, "carbs_g": 65, "fat_g": 24},
        "source": "restaurant",
        "where_to_find": "Chipotle — chicken, brown rice, black beans, fajita veg, salsa",
        "tags": ["high-protein", "quick", "lunch", "dinner"],
        "diets": ["omnivore", "pescatarian"],
        "contains": ["chicken"],
    },
    "lentil quinoa salad": {
        "macros": {"calories": 400, "protein_g": 22, "carbs_g": 52, "fat_g": 12},
        "source": "ready-to-eat",
        "where_to_find": "Whole Foods hot bar or a 15-min recipe",
        "tags": ["vegan", "high-fiber", "quick", "lunch"],
        "diets": ["vegan", "vegetarian", "omnivore", "pescatarian"],
        "contains": [],
    },
    "protein oatmeal": {
        "macros": {"calories": 380, "protein_g": 30, "carbs_g": 45, "fat_g": 8},
        "source": "recipe",
        "where_to_find": "Recipe — oats, a scoop of whey, banana, cinnamon",
        "tags": ["high-protein", "breakfast"],
        "diets": ["vegetarian", "omnivore", "pescatarian"],
        "contains": ["dairy", "gluten"],
    },
    "turkey and avocado wrap": {
        "macros": {"calories": 480, "protein_g": 34, "carbs_g": 40, "fat_g": 20},
        "source": "ready-to-eat",
        "where_to_find": "Most cafes or delis — whole-wheat wrap, turkey, avocado",
        "tags": ["high-protein", "quick", "lunch"],
        "diets": ["omnivore"],
        "contains": ["turkey", "gluten"],
    },
    "shrimp poke bowl": {
        "macros": {"calories": 540, "protein_g": 38, "carbs_g": 62, "fat_g": 12},
        "source": "restaurant",
        "where_to_find": "Any poke spot — shrimp, rice, edamame, cucumber",
        "tags": ["high-protein", "lunch", "dinner"],
        "diets": ["pescatarian", "omnivore"],
        "contains": ["shellfish", "soy"],
    },
    "peanut butter banana toast": {
        "macros": {"calories": 420, "protein_g": 14, "carbs_g": 52, "fat_g": 16},
        "source": "recipe",
        "where_to_find": "Recipe — whole-grain toast, peanut butter, banana",
        "tags": ["quick", "breakfast", "snack"],
        "diets": ["vegan", "vegetarian", "omnivore", "pescatarian"],
        "contains": ["peanuts", "gluten"],
    },
    "cottage cheese bowl": {
        "macros": {"calories": 240, "protein_g": 26, "carbs_g": 14, "fat_g": 8},
        "source": "ready-to-eat",
        "where_to_find": "Any grocery — cottage cheese, cherry tomatoes, pepper",
        "tags": ["high-protein", "snack", "quick"],
        "diets": ["vegetarian", "omnivore", "pescatarian"],
        "contains": ["dairy"],
    },
    "black bean tacos": {
        "macros": {"calories": 520, "protein_g": 22, "carbs_g": 68, "fat_g": 16},
        "source": "recipe",
        "where_to_find": "Recipe — corn tortillas, black beans, slaw, salsa",
        "tags": ["vegan", "high-fiber", "dinner"],
        "diets": ["vegan", "vegetarian", "omnivore"],
        "contains": [],
    },
}

# --- Known members (replaces KNOWN_CUSTOMERS) --------------------------------
# Add your own E.164 number here to demo the returning-member phone path.
KNOWN_MEMBERS = {
    "+14155551234": {
        "name": "Alex",
        "profile": {
            "goal": "cut",
            "diet_style": "omnivore",
            "allergies": ["shellfish"],
            "restrictions": [],
            "activity_level": "moderate",
            "targets": {"calories": 2100, "protein_g": 180, "carbs_g": 190, "fat_g": 60},
        },
        "today": {"remaining": {"calories": 1580, "protein_g": 132, "carbs_g": 148, "fat_g": 44}},
    },
    "+14155555678": {
        "name": "Jordan",
        "profile": {
            "goal": "bulk",
            "diet_style": "vegetarian",
            "allergies": ["peanuts"],
            "restrictions": [],
            "activity_level": "very_active",
            "targets": {"calories": 3000, "protein_g": 190, "carbs_g": 340, "fat_g": 90},
        },
        "today": {"remaining": {"calories": 3000, "protein_g": 190, "carbs_g": 340, "fat_g": 90}},
    },
}

# --- Target calculation constants --------------------------------------------

_MACRO_KEYS = ("calories", "protein_g", "carbs_g", "fat_g")
# Mifflin–St Jeor activity multipliers (TDEE = BMR * factor).
_ACTIVITY_FACTORS = {
    "sedentary": 1.2,
    "light": 1.375,
    "moderate": 1.55,
    "active": 1.725,
    "very_active": 1.9,
}
# Goal calorie adjustment applied to TDEE / maintenance.
_GOAL_ADJUST = {"cut": 0.82, "maintain": 1.0, "bulk": 1.12}
# Maintenance-calorie heuristic used when biometrics aren't given.
_HEURISTIC_MAINTAIN = {
    "sedentary": 2000,
    "light": 2250,
    "moderate": 2500,
    "active": 2800,
    "very_active": 3100,
}
# Protein as a fraction of calories for the heuristic path (higher on a cut).
_PROTEIN_FRACTION = {"cut": 0.35, "maintain": 0.30, "bulk": 0.28}


# --- Data-access layer (the swap-to-real-API boundary) -----------------------


def get_meals() -> dict:
    """Return the full meal catalog. Replace with ``GET /v1/meals``."""
    return MEALS


def get_meal(name: str) -> dict | None:
    """Look up one meal by (case-insensitive) name. Replace with the real lookup."""
    if not name:
        return None
    return MEALS.get(name.strip().lower())


# Canonical allergen / ingredient classes mapped to the words members actually say.
# Both the member's stated terms AND each meal's `contains` tokens are normalized
# through this, so "egg"/"eggs", "milk"/"dairy", "shrimp"/"shellfish",
# "wheat"/"gluten", "nuts"/"peanuts" all screen correctly. This is the safety gate,
# so it errs toward excluding (e.g. "nuts" implies both peanuts and tree nuts).
_ALLERGEN_SYNONYMS = {
    "dairy": {"dairy", "milk", "lactose", "cheese", "yogurt", "yoghurt", "cream",
              "butter", "whey", "casein"},
    "eggs": {"eggs", "egg"},
    "peanuts": {"peanuts", "peanut", "groundnut", "groundnuts", "nuts", "nut"},
    "tree nuts": {"tree nuts", "tree nut", "nuts", "nut", "almond", "almonds",
                  "cashew", "cashews", "walnut", "walnuts", "pecan", "pecans"},
    "shellfish": {"shellfish", "shrimp", "prawn", "prawns", "crab", "lobster",
                  "crayfish", "clam", "clams", "oyster", "oysters", "scallop", "scallops"},
    "fish": {"fish", "salmon", "tuna", "cod", "tilapia", "anchovy", "anchovies",
             "sardine", "sardines"},
    "soy": {"soy", "soya", "soybean", "soybeans", "edamame", "tofu", "tamari"},
    "gluten": {"gluten", "wheat", "bread", "flour", "barley", "rye", "pasta"},
    "beef": {"beef", "steak", "red meat"},
    "chicken": {"chicken", "poultry"},
    "turkey": {"turkey", "poultry"},
    "pork": {"pork", "bacon", "ham"},
}

# Words that qualify a restriction ("no dairy", "gluten-free", "avoid shellfish")
# and should be dropped before matching the ingredient itself.
_RESTRICTION_QUALIFIERS = {"no", "not", "avoid", "without", "free", "of", "from"}


def _normalize_term(term: str) -> str:
    """Strip qualifiers ('no', 'avoid', 'free', ...) across spaces/hyphens/underscores."""
    cleaned = term.strip().lower().replace("_", " ").replace("-", " ")
    tokens = [t for t in cleaned.split() if t not in _RESTRICTION_QUALIFIERS]
    return " ".join(tokens).strip()


def _canonical_classes(term: str) -> set[str]:
    """Map a (normalized) ingredient/allergen word to the canonical class(es) it implies.

    Returns an empty set for terms we can't map (e.g. "halal", "kosher") — those can't
    be enforced against the catalog's ingredient tokens, so the coach must disclose it
    rather than imply compliance.
    """
    t = _normalize_term(term)
    classes = {canon for canon, syns in _ALLERGEN_SYNONYMS.items() if t == canon or t in syns}
    if not classes and t.endswith("s"):  # generic de-pluralization fallback
        sing = t[:-1]
        classes = {canon for canon, syns in _ALLERGEN_SYNONYMS.items() if sing in syns}
    return classes


def _excluded(meal: dict, allergies, restrictions) -> bool:
    """True if a meal violates a stated allergy or restriction.

    Member terms and the meal's ``contains`` tokens are both mapped to canonical
    allergen/ingredient classes before comparison, so natural phrasing ("egg",
    "milk", "shrimp", "nuts", "wheat") screens the right meals. SAFETY-CRITICAL —
    this is the allergen gate, so it errs toward excluding.
    """
    avoid: set[str] = set()
    for term in list(allergies or []) + list(restrictions or []):
        avoid |= _canonical_classes(term)
    if not avoid:
        return False
    meal_classes: set[str] = set()
    for c in meal.get("contains", []):
        meal_classes |= _canonical_classes(c) or {c.strip().lower()}
    return bool(avoid & meal_classes)


def _fit_score(name: str, meal: dict, remaining, craving, slot) -> float:
    """Deterministic fit score: higher is a better recommendation.

    Rewards protein density and meals that help close the remaining protein gap;
    penalizes meals that blow the remaining calorie budget; bonuses for matching a
    craving or meal slot. Kept pure/deterministic so Cekura's macro evaluators are
    reproducible.
    """
    m = meal["macros"]
    cal = m["calories"]
    prot = m["protein_g"]
    tags = [t.lower() for t in meal.get("tags", [])]

    score = (prot / cal * 100.0) if cal else 0.0  # protein per 100 kcal
    if remaining and remaining.get("calories") is not None:
        rem_cal = remaining["calories"]
        rem_prot = remaining.get("protein_g", 0)
        if rem_cal >= 0:
            if cal > rem_cal:
                score -= (cal - rem_cal) * 0.05  # going over budget is bad
            score += min(prot, max(rem_prot, 0)) * 0.05  # help hit protein
        else:
            score -= cal * 0.02  # already over: prefer the lightest options
    if craving:
        haystack = (name + " " + " ".join(tags)).lower()
        if craving.strip().lower() in haystack:
            score += 40.0
    if slot and slot.strip().lower() in tags:
        score += 8.0
    return score


def find_meals(
    *,
    remaining: dict | None = None,
    diet_style: str | None = None,
    allergies=None,
    restrictions=None,
    craving: str | None = None,
    source_type: str | None = None,
    slot: str | None = None,
    max_results: int = 5,
) -> list[dict]:
    """Rank meals by fit to remaining macros + diet/allergy/craving/source filters.

    Hard filters: diet-style compatibility, allergen/restriction exclusion, and
    source_type. Soft ranking via ``_fit_score``. Replace the body with
    ``POST /v1/meals/search``; the return shape is what the tools read.

    Returns a list of ``{name, calories, protein_g, carbs_g, fat_g, source,
    where_to_find, tags}`` dicts.
    """
    scored = []
    ds = diet_style.strip().lower() if diet_style else None
    st = source_type.strip().lower() if source_type else None
    for name, meal in get_meals().items():
        if ds and ds not in [d.lower() for d in meal.get("diets", [])]:
            continue
        if _excluded(meal, allergies, restrictions):
            continue
        if st and meal.get("source") != st:
            continue
        scored.append((name, meal, _fit_score(name, meal, remaining, craving, slot)))

    scored.sort(key=lambda x: x[2], reverse=True)
    limit = max(1, min(max_results, 5))
    out = []
    for name, meal, _ in scored[:limit]:
        m = meal["macros"]
        out.append(
            {
                "name": name,
                "calories": m["calories"],
                "protein_g": m["protein_g"],
                "carbs_g": m["carbs_g"],
                "fat_g": m["fat_g"],
                "source": meal["source"],
                "where_to_find": meal["where_to_find"],
                "tags": meal["tags"],
            }
        )
    return out


def calc_targets(profile: dict) -> dict:
    """Compute daily calorie + macro targets from a profile.

    Uses Mifflin–St Jeor when sex/age/height/weight are present, otherwise a
    goal×activity calorie heuristic. Macro split: protein from bodyweight
    (~2.0 g/kg) or a goal-based fraction, fat ~27% of calories, carbs the
    remainder. Replace with ``GET /v1/members/{id}/targets``.

    Returns ``{calories, protein_g, carbs_g, fat_g, basis}``.
    """
    bio = profile.get("biometrics") or {}
    goal = (profile.get("goal") or "maintain").strip().lower()
    activity = (profile.get("activity_level") or "moderate").strip().lower()
    factor = _ACTIVITY_FACTORS.get(activity, 1.55)
    goal_mult = _GOAL_ADJUST.get(goal, 1.0)

    sex = (bio.get("sex") or "").strip().lower()
    age = bio.get("age")
    height_cm = bio.get("height_cm")
    weight_kg = bio.get("weight_kg")

    if weight_kg and height_cm and age and sex in ("male", "female"):
        bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + (5 if sex == "male" else -161)
        calories = bmr * factor * goal_mult
        protein_g = 2.0 * weight_kg  # ~0.9 g/lb
        basis = "mifflin_st_jeor"
    else:
        calories = _HEURISTIC_MAINTAIN.get(activity, 2500) * goal_mult
        protein_g = calories * _PROTEIN_FRACTION.get(goal, 0.30) / 4
        basis = "heuristic"

    calories = round(calories)
    protein_g = round(protein_g)
    fat_g = round(calories * 0.27 / 9)
    carbs_g = max(round((calories - protein_g * 4 - fat_g * 9) / 4), 0)
    return {
        "calories": calories,
        "protein_g": protein_g,
        "carbs_g": carbs_g,
        "fat_g": fat_g,
        "basis": basis,
    }
