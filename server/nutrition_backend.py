#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Mock backend data for the Tetris Nutrition Coach demo.

This is the **swap-to-real-API boundary**: every tool in ``coach_core.py``
touches data only through the access helpers here (``get_meals``, ``get_meal``,
``find_meals``, ``calc_targets``). Replacing these dicts with calls to a real
Tetris API later is a one-file change.

``MEALS`` spans all diet styles, all three source types
(restaurant | recipe | ready-to-eat), and all slots. Each meal carries:
    macros (calories, protein_g, carbs_g, fat_g), source (one of the three
    types), where_to_find (spoken-aloud locator), slot, tags (free-form,
    matched against cravings), diets (compatible diet styles), and contains
    (allergen screen — names are matched against the member's stated allergies).

``KNOWN_MEMBERS`` (phone → existing profile) replaces the flower shop's
``KNOWN_CUSTOMERS`` for the returning-member-over-phone demo. Phone numbers are
stored in E.164 format (e.g. ``+14155551234``) to match Twilio's ``from_number``.

``calc_targets`` and ``_fit_score`` are kept **pure and deterministic** so the
Cekura macro-math evaluator is reproducible.

``search_restaurants`` is the one impure helper: it queries a live web service
for real nearby restaurants. It uses the Google Places API when
``GOOGLE_PLACES_API_KEY`` is set, otherwise falls back to OpenStreetMap's
Nominatim (free, no key required).
"""

import os

import aiohttp
from loguru import logger

# --- Catalog -----------------------------------------------------------------
#
# diets compatibility: every meal lists ALL diet styles it satisfies. "omnivore"
# matches everything (handled in find_meals, not listed here). A vegan meal also
# lists "vegetarian" since it satisfies that constraint too.

MEALS = {
    "greek yogurt parfait": {
        "macros": {"calories": 320, "protein_g": 24, "carbs_g": 38, "fat_g": 8},
        "source": "ready-to-eat",
        "where_to_find": "the fridge aisle at most grocery stores",
        "slot": "breakfast",
        "tags": ["sweet", "quick", "high-protein", "yogurt", "berries"],
        "diets": ["vegetarian"],
        "contains": ["dairy"],
    },
    "veggie tofu scramble": {
        "macros": {"calories": 290, "protein_g": 22, "carbs_g": 14, "fat_g": 16},
        "source": "recipe",
        "where_to_find": "a ten-minute cook at home",
        "slot": "breakfast",
        "tags": ["savory", "warm", "high-protein", "tofu", "veggies"],
        "diets": ["vegetarian", "vegan"],
        "contains": ["soy"],
    },
    "steel-cut oats with almond butter": {
        "macros": {"calories": 380, "protein_g": 12, "carbs_g": 54, "fat_g": 14},
        "source": "recipe",
        "where_to_find": "a five-minute cook at home",
        "slot": "breakfast",
        "tags": ["sweet", "warm", "filling", "oats", "carby"],
        "diets": ["vegetarian", "vegan"],
        "contains": ["tree_nut"],
    },
    "grilled chicken burrito bowl": {
        "macros": {"calories": 540, "protein_g": 42, "carbs_g": 52, "fat_g": 16},
        "source": "restaurant",
        "where_to_find": "Chipotle or any fast-casual Mexican spot",
        "slot": "lunch",
        "tags": ["savory", "filling", "high-protein", "chicken", "mexican"],
        "diets": [],
        "contains": [],
    },
    "salmon poke bowl": {
        "macros": {"calories": 480, "protein_g": 34, "carbs_g": 46, "fat_g": 16},
        "source": "restaurant",
        "where_to_find": "a poke counter or the prepared-foods case",
        "slot": "lunch",
        "tags": ["fresh", "fish", "rice", "high-protein", "hawaiian"],
        "diets": ["pescatarian"],
        "contains": ["fish", "soy"],
    },
    "lentil quinoa salad": {
        "macros": {"calories": 410, "protein_g": 20, "carbs_g": 56, "fat_g": 12},
        "source": "ready-to-eat",
        "where_to_find": "the grab-and-go salad case",
        "slot": "lunch",
        "tags": ["fresh", "fiber", "lentils", "quinoa", "light"],
        "diets": ["vegetarian", "vegan"],
        "contains": [],
    },
    "turkey avocado wrap": {
        "macros": {"calories": 450, "protein_g": 32, "carbs_g": 40, "fat_g": 18},
        "source": "ready-to-eat",
        "where_to_find": "any deli or convenience cooler",
        "slot": "lunch",
        "tags": ["savory", "quick", "turkey", "avocado", "sandwich"],
        "diets": [],
        "contains": ["gluten"],
    },
    "sheet-pan chicken and veg": {
        "macros": {"calories": 520, "protein_g": 46, "carbs_g": 30, "fat_g": 22},
        "source": "recipe",
        "where_to_find": "a one-pan cook at home",
        "slot": "dinner",
        "tags": ["savory", "warm", "high-protein", "chicken", "roasted"],
        "diets": ["paleo"],
        "contains": [],
    },
    "baked cod with sweet potato": {
        "macros": {"calories": 470, "protein_g": 38, "carbs_g": 44, "fat_g": 12},
        "source": "recipe",
        "where_to_find": "a twenty-minute cook at home",
        "slot": "dinner",
        "tags": ["fresh", "fish", "lean", "cod", "sweet potato"],
        "diets": ["pescatarian"],
        "contains": ["fish"],
    },
    "steak fajita plate": {
        "macros": {"calories": 620, "protein_g": 44, "carbs_g": 38, "fat_g": 32},
        "source": "restaurant",
        "where_to_find": "most sit-down Mexican restaurants",
        "slot": "dinner",
        "tags": ["savory", "hearty", "steak", "beef", "mexican"],
        "diets": ["paleo"],
        "contains": [],
    },
    "tofu stir-fry with brown rice": {
        "macros": {"calories": 500, "protein_g": 26, "carbs_g": 58, "fat_g": 18},
        "source": "recipe",
        "where_to_find": "a fifteen-minute cook at home",
        "slot": "dinner",
        "tags": ["savory", "warm", "tofu", "veggies", "rice"],
        "diets": ["vegetarian", "vegan"],
        "contains": ["soy"],
    },
    "cottage cheese with pineapple": {
        "macros": {"calories": 180, "protein_g": 22, "carbs_g": 16, "fat_g": 3},
        "source": "ready-to-eat",
        "where_to_find": "the dairy case",
        "slot": "snack",
        "tags": ["sweet", "quick", "high-protein", "cottage cheese", "fruit"],
        "diets": ["vegetarian"],
        "contains": ["dairy"],
    },
    "protein shake": {
        "macros": {"calories": 220, "protein_g": 30, "carbs_g": 12, "fat_g": 5},
        "source": "ready-to-eat",
        "where_to_find": "a bottle from the cooler or a quick blend at home",
        "slot": "snack",
        "tags": ["sweet", "quick", "high-protein", "shake", "post-workout"],
        "diets": ["vegetarian"],
        "contains": ["dairy"],
    },
    "apple with peanut butter": {
        "macros": {"calories": 270, "protein_g": 8, "carbs_g": 30, "fat_g": 14},
        "source": "ready-to-eat",
        "where_to_find": "anywhere — fruit bowl plus a jar",
        "slot": "snack",
        "tags": ["sweet", "crunchy", "fruit", "peanut butter", "filling"],
        "diets": ["vegetarian", "vegan"],
        "contains": ["peanut"],
    },
    "hummus and veggie cup": {
        "macros": {"calories": 200, "protein_g": 7, "carbs_g": 22, "fat_g": 10},
        "source": "ready-to-eat",
        "where_to_find": "the grab-and-go snack cooler",
        "slot": "snack",
        "tags": ["savory", "fresh", "crunchy", "hummus", "veggies"],
        "diets": ["vegetarian", "vegan"],
        "contains": [],
    },
}

# Add your own number here to demo the returning-member (skip-intake) path over
# a live phone call. ``profile`` mirrors the per-call state's profile shape so it
# can be loaded straight in. ``targets`` is pre-computed from that profile.
KNOWN_MEMBERS = {
    "+14155551234": {
        "name": "Alex",
        "profile": {
            "goal": "cut",
            "diet_style": "omnivore",
            "allergies": [],
            "restrictions": [],
            "activity_level": "moderate",
            "sex": "male",
            "age": 31,
            "weight_kg": 84.0,
            "height_cm": 180.0,
        },
    },
    "+14155555678": {
        "name": "Jordan",
        "profile": {
            "goal": "maintain",
            "diet_style": "vegetarian",
            "allergies": ["shellfish"],
            "restrictions": [],
            "activity_level": "light",
            "sex": "female",
            "age": 28,
            "weight_kg": 63.0,
            "height_cm": 167.0,
        },
    },
}

# Activity multipliers for Mifflin–St Jeor TDEE.
_ACTIVITY_FACTORS = {
    "sedentary": 1.2,
    "light": 1.375,
    "moderate": 1.55,
    "active": 1.725,
    "very_active": 1.9,
}

# Fallback maintenance calories by activity when biometrics are unknown.
_HEURISTIC_BASE = {
    "sedentary": 1800,
    "light": 2000,
    "moderate": 2200,
    "active": 2500,
    "very_active": 2800,
}

# Goal calorie adjustments.
_GOAL_ADJUST = {"cut": 0.85, "maintain": 1.0, "bulk": 1.12}


def _macros_from_calories(calories: float) -> dict:
    """Split a calorie target into a deterministic 30/40/30 protein/carb/fat
    macro split (4/4/9 kcal per gram)."""
    return {
        "calories": int(round(calories)),
        "protein_g": int(round(calories * 0.30 / 4)),
        "carbs_g": int(round(calories * 0.40 / 4)),
        "fat_g": int(round(calories * 0.30 / 9)),
    }


def calc_targets(profile: dict) -> dict:
    """Compute daily macro targets from a member profile. Pure & deterministic.

    Uses Mifflin–St Jeor when sex, age, weight_kg, and height_cm are all
    present; otherwise falls back to an activity-based heuristic. Either way the
    TDEE is scaled by a goal adjustment (cut −15% / maintain 0 / bulk +12%) and
    split 30/40/30 across protein/carbs/fat.

    Args:
        profile: Member profile dict. Reads goal, activity_level, and the
            optional biometrics sex/age/weight_kg/height_cm.

    Returns:
        Macro target dict {calories, protein_g, carbs_g, fat_g}.
    """
    activity = (profile.get("activity_level") or "moderate").lower()
    factor = _ACTIVITY_FACTORS.get(activity, 1.55)

    sex = profile.get("sex")
    age = profile.get("age")
    weight_kg = profile.get("weight_kg")
    height_cm = profile.get("height_cm")

    if sex and age and weight_kg and height_cm:
        # Mifflin–St Jeor basal metabolic rate, then TDEE.
        bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age
        bmr += 5 if str(sex).lower().startswith("m") else -161
        tdee = bmr * factor
    else:
        tdee = _HEURISTIC_BASE.get(activity, 2200)

    goal = (profile.get("goal") or "maintain").lower()
    calories = tdee * _GOAL_ADJUST.get(goal, 1.0)
    return _macros_from_calories(calories)


def get_meals() -> dict:
    """Return the full meal catalog (name → meal dict)."""
    return MEALS


def get_meal(name: str) -> dict | None:
    """Look up a single meal by name, case-insensitively. None if not found."""
    return MEALS.get((name or "").strip().lower())


def _fit_score(meal: dict, remaining: dict | None) -> float:
    """How well a meal fits the remaining macro budget. Lower is better. Pure.

    Going over the remaining calorie budget is penalized heavily; leaving some
    room is penalized lightly; protein is rewarded. Deterministic so the macro
    evaluator can reproduce the ranking.
    """
    m = meal["macros"]
    score = 0.0
    rem_cal = (remaining or {}).get("calories")
    if rem_cal is not None:
        if m["calories"] > rem_cal:
            score += (m["calories"] - rem_cal) * 3.0  # over budget — bad
        else:
            score += (rem_cal - m["calories"]) * 0.5  # leaves room — mild
    score -= m["protein_g"] * 2.0  # reward protein
    return score


def find_meals(
    slot: str | None = None,
    craving: str | None = None,
    source_type: str | None = None,
    diet_style: str | None = None,
    allergies: list | None = None,
    remaining: dict | None = None,
    max_results: int = 5,
) -> list:
    """Filter and rank meals. Single funnel for every recommendation tool.

    Args:
        slot: "breakfast" | "lunch" | "dinner" | "snack" to restrict the slot.
        craving: free-text matched against each meal's name and tags.
        source_type: "restaurant" | "recipe" | "ready-to-eat" filter.
        diet_style: member's diet style; meals must be compatible. "omnivore"
            (or None) matches everything.
        allergies: allergen names; any meal whose ``contains`` intersects this
            list is excluded (safety-critical, hard filter).
        remaining: remaining macro budget, used to rank by ``_fit_score``.
        max_results: cap on returned meals.

    Returns:
        List of {"name": ..., **meal} dicts, best fit first, capped.
    """
    allergy_set = {a.strip().lower() for a in (allergies or [])}
    diet = (diet_style or "").strip().lower()
    src = (source_type or "").strip().lower()
    crave = (craving or "").strip().lower()

    results = []
    for name, meal in MEALS.items():
        if slot and meal["slot"] != slot.strip().lower():
            continue
        if src and meal["source"] != src:
            continue
        if diet and diet != "omnivore" and diet not in meal["diets"]:
            continue
        if allergy_set & {c.lower() for c in meal["contains"]}:
            continue  # never suggest a meal containing a stated allergen
        if crave:
            haystack = name + " " + " ".join(meal["tags"])
            if crave not in haystack:
                continue
        results.append((name, meal))

    results.sort(key=lambda nm: _fit_score(nm[1], remaining))
    return [{"name": name, **meal} for name, meal in results[: max(1, max_results)]]


# --- Live restaurant search --------------------------------------------------

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_GOOGLE_PLACES_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
_OSM_USER_AGENT = "TetrisNutritionCoach/1.0 (hackathon demo)"


async def _search_google_places(session, term: str, location: str, max_results: int) -> list:
    """Google Places Text Search (used when GOOGLE_PLACES_API_KEY is set)."""
    params = {
        "query": f"{term} restaurants in {location}",
        "type": "restaurant",
        "key": os.environ["GOOGLE_PLACES_API_KEY"],
    }
    async with session.get(_GOOGLE_PLACES_URL, params=params) as resp:
        data = await resp.json()
    out = []
    for p in data.get("results", [])[:max_results]:
        out.append(
            {
                "name": p.get("name"),
                "address": p.get("formatted_address"),
                "rating": p.get("rating"),
                "source": "online",
            }
        )
    return out


def _osm_address(tags: dict) -> str:
    """Build a readable address from OSM addr:* tags."""
    parts = [
        " ".join(p for p in (tags.get("addr:housenumber"), tags.get("addr:street")) if p),
        tags.get("addr:city"),
    ]
    return ", ".join(p for p in parts if p)


async def _search_osm(session, term: str, location: str, max_results: int) -> list:
    """Free, no-key restaurant search: Nominatim geocodes the location, then the
    Overpass API finds nearby ``amenity=restaurant`` POIs. Nominatim alone is a
    geocoder and won't return businesses, hence the two steps."""
    headers = {"User-Agent": _OSM_USER_AGENT}

    # 1. Geocode the location to a lat/lon.
    geo_params = {"q": location, "format": "json", "limit": 1}
    async with session.get(_NOMINATIM_URL, params=geo_params, headers=headers) as resp:
        geo = await resp.json()
    if not geo:
        return []
    lat, lon = geo[0]["lat"], geo[0]["lon"]

    # 2. Overpass: restaurants within 3 km of that point.
    query = (
        f"[out:json][timeout:12];"
        f'node["amenity"="restaurant"](around:3000,{lat},{lon});'
        f"out body 40;"
    )
    async with session.post(_OVERPASS_URL, data={"data": query}, headers=headers) as resp:
        data = await resp.json()

    elements = [e for e in data.get("elements", []) if e.get("tags", {}).get("name")]

    # Prefer places whose name or cuisine matches the keyword; fall back to all.
    kw = term.strip().lower()
    if kw and kw not in ("healthy", "restaurant"):
        matched = [
            e
            for e in elements
            if kw in e["tags"]["name"].lower() or kw in e["tags"].get("cuisine", "").lower()
        ]
        elements = matched or elements

    out = []
    for e in elements[:max_results]:
        tags = e["tags"]
        out.append(
            {
                "name": tags["name"],
                "address": _osm_address(tags),
                "cuisine": tags.get("cuisine", "").replace("_", " ").replace(";", ", "),
                "source": "online",
            }
        )
    return out


async def search_restaurants(
    location: str, query: str | None = None, max_results: int = 4
) -> list:
    """Search live for real restaurants near a location.

    Uses Google Places when ``GOOGLE_PLACES_API_KEY`` is set, otherwise OSM
    Nominatim. Returns ``[]`` on any error or no match (the caller turns that
    into a spoken fallback). This is the swap point for a different provider.

    Args:
        location: City / neighborhood / address to search near (required).
        query: Optional cuisine or keyword (e.g. "sushi", "healthy", "salad").
        max_results: Cap on returned restaurants (kept small for voice).

    Returns:
        List of {"name", "address", "source", optional "rating"} dicts.
    """
    term = (query or "healthy").strip()
    loc = (location or "").strip()
    if not loc:
        return []
    max_results = max(1, min(max_results, 5))
    timeout = aiohttp.ClientTimeout(total=20)  # OSM path makes two sequential calls
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if os.getenv("GOOGLE_PLACES_API_KEY"):
                return await _search_google_places(session, term, loc, max_results)
            return await _search_osm(session, term, loc, max_results)
    except Exception as e:
        logger.error(f"Restaurant search failed: {e}")
        return []
