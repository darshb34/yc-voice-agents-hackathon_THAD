"""Unit tests for coach.catalog (CSV/JSON-backed catalog + safety filters).

Run: python -m pytest server/coach/tests/test_catalog.py
Or:  python server/coach/tests/test_catalog.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # server/coach

from catalog import (  # noqa: E402
    find_meals,
    get_catalog,
    get_meal,
    normalize_allergens,
    search_restaurants,
)


def test_catalog_loads_both_sources():
    cat = get_catalog()
    sources = {m["source"] for m in cat}
    assert sources == {"recipe", "restaurant"}
    # Range-based so it's robust to dataset growth (datasets have been expanded).
    assert sum(m["source"] == "recipe" for m in cat) >= 12
    assert sum(m["source"] == "restaurant" for m in cat) >= 70


def test_normalize_allergens_tokens():
    assert normalize_allergens("Wheat, Soy, Garlic, Sesame") == ["soy", "sesame", "gluten"]
    assert normalize_allergens(["Tree nut (coconut)"]) == ["tree_nut"]
    assert normalize_allergens("Dairy (parmesan in pesto)") == ["dairy"]


def test_normalize_allergens_shellfish_not_double_counted_as_fish():
    assert normalize_allergens("Shellfish, Dairy, Garlic") == ["shellfish", "dairy"]
    # but an explicit fish + shellfish list keeps both
    toks = normalize_allergens("Peanut, Egg, Fish, Shellfish, Soy")
    assert "fish" in toks and "shellfish" in toks and "peanut" in toks


def test_allergy_is_hard_filter_fish():
    results = find_meals(diet_style="omnivore", allergies=["fish"], max_results=50)
    assert results, "should still return non-fish meals"
    assert all("fish" not in m["contains"] for m in results)


def test_allergy_is_hard_filter_peanut():
    results = find_meals(allergies=["peanut"], max_results=50)
    assert all("peanut" not in m["contains"] for m in results)


def test_vegan_diet_only_returns_vegan_meals():
    results = find_meals(diet_style="vegan", max_results=50)
    assert results
    assert all("vegan" in m["diets"] for m in results)


def test_vegetarian_never_returns_meat_dish():
    # No result for a vegetarian should be omnivore-only (diets == []).
    results = find_meals(diet_style="vegetarian", max_results=80)
    assert all(m["diets"] for m in results)
    assert all("vegetarian" in m["diets"] for m in results)


def test_slot_filter_excludes_breakfast_for_lunch():
    results = find_meals(slot="lunch", max_results=80)
    assert all("lunch" in m["slots"] for m in results)
    assert all(m["slots"] != ["breakfast"] for m in results)


def test_craving_matches_cuisine():
    results = find_meals(craving="mexican", max_results=20)
    assert results
    for m in results:
        hay = m["name"].lower() + " " + " ".join(m["tags"]) + " " + (m.get("cuisine") or "").lower()
        assert "mexican" in hay


def test_search_restaurants_shape_and_safety():
    results = search_restaurants(location="Inner Sunset", cuisine="sushi", max_results=4)
    assert results
    for r in results:
        assert {"restaurant", "dish", "macros"} <= set(r)
    # vegan + allergy safety carries through
    vegan = search_restaurants(diet_style="vegan", allergies=["soy"], max_results=10)
    # (may be empty if no vegan soy-free SF dish, but must never include soy)
    for r in vegan:
        m = get_meal(r["dish"])
        assert m and "soy" not in m["contains"]


def test_find_meals_ranks_high_protein_under_budget_first():
    remaining = {"calories": 700, "protein_g": 55, "carbs_g": 60, "fat_g": 22}
    results = find_meals(diet_style="omnivore", remaining=remaining, max_results=5)
    assert results
    # top result should be reasonably high protein (fit_score rewards protein)
    assert results[0]["macros"]["protein_g"] >= 25


def test_get_meal_by_name():
    m = get_meal("Ahi Tuna Poke Bowl")
    assert m and m["source"] == "recipe" and "fish" in m["contains"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
