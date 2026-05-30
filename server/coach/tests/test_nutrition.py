"""Unit tests for coach.nutrition.

Run: python -m pytest server/coach/tests/test_nutrition.py
Or:  python server/coach/tests/test_nutrition.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # server/coach

from nutrition import (  # noqa: E402
    calc_targets,
    fit_score,
    macros_from_calories,
    pick_closest,
    proximity,
    rank_by_fit,
    remaining_macros,
    suggest_tweaks,
)


def test_macros_split_30_40_30():
    m = macros_from_calories(1870)
    assert m == {"calories": 1870, "protein_g": 140, "carbs_g": 187, "fat_g": 62}


def test_calc_targets_heuristic_cut_moderate():
    # No biometrics -> heuristic base 2200 * 0.85 (cut) = 1870. Matches the seeded
    # returning-member profile used in the eval suite.
    t = calc_targets({"goal": "cut", "activity_level": "moderate"})
    assert t == {"calories": 1870, "protein_g": 140, "carbs_g": 187, "fat_g": 62}


def test_calc_targets_mifflin_with_biometrics():
    t = calc_targets({
        "goal": "maintain", "activity_level": "moderate",
        "sex": "male", "age": 31, "weight_kg": 84.0, "height_cm": 180.0,
    })
    # BMR = 10*84 + 6.25*180 - 5*31 + 5 = 1815; TDEE = *1.55 = 2813.25; maintain.
    assert t["calories"] == 2813


def test_remaining_macros_subtracts_logs():
    targets = {"calories": 1870, "protein_g": 140, "carbs_g": 187, "fat_g": 62}
    meals = [
        {"macros": {"calories": 320, "protein_g": 24, "carbs_g": 38, "fat_g": 8}},
        {"macros": {"calories": 410, "protein_g": 20, "carbs_g": 56, "fat_g": 12}},
    ]
    assert remaining_macros(targets, meals) == {
        "calories": 1140, "protein_g": 96, "carbs_g": 93, "fat_g": 42,
    }


def test_remaining_can_go_negative():
    targets = {"calories": 500, "protein_g": 40, "carbs_g": 50, "fat_g": 20}
    meals = [{"macros": {"calories": 700, "protein_g": 30, "carbs_g": 60, "fat_g": 40}}]
    rem = remaining_macros(targets, meals)
    assert rem["calories"] == -200 and rem["fat_g"] == -20


def test_remaining_none_without_targets():
    assert remaining_macros(None, []) is None


def test_fit_score_prefers_protein_and_penalizes_over_budget():
    remaining = {"calories": 600, "protein_g": 50, "carbs_g": 60, "fat_g": 20}
    lean_high_protein = {"calories": 520, "protein_g": 46, "carbs_g": 30, "fat_g": 22}
    over_budget = {"calories": 900, "protein_g": 30, "carbs_g": 80, "fat_g": 40}
    assert fit_score(lean_high_protein, remaining) < fit_score(over_budget, remaining)


def test_rank_by_fit_orders_best_first():
    remaining = {"calories": 600, "protein_g": 50, "carbs_g": 60, "fat_g": 20}
    items = [
        {"name": "huge", "macros": {"calories": 1000, "protein_g": 20, "carbs_g": 100, "fat_g": 50}},
        {"name": "fit", "macros": {"calories": 560, "protein_g": 44, "carbs_g": 40, "fat_g": 18}},
    ]
    assert rank_by_fit(items, remaining)[0]["name"] == "fit"


def test_proximity_exact_and_partial():
    target = {"calories": 1000, "protein_g": 100, "carbs_g": 100, "fat_g": 30}
    assert proximity(dict(target), target) == 1.0
    # only positive-target keys count; 10% off calories alone
    assert proximity({"calories": 1100, "protein_g": 100, "carbs_g": 100, "fat_g": 30}, target) == 0.975


def test_proximity_none_when_no_positive_target():
    assert proximity({"calories": 500}, {"calories": 0, "protein_g": 0}) is None


def test_suggest_tweaks_flags_over_fat_and_carbs():
    remaining = {"calories": 700, "protein_g": 50, "carbs_g": 40, "fat_g": 15}
    macros = {"calories": 680, "protein_g": 30, "carbs_g": 70, "fat_g": 30}  # over fat + carbs
    tweaks = suggest_tweaks(macros, remaining)
    assert any("oil" in t for t in tweaks)
    assert any("rice or bread" in t for t in tweaks)


def test_suggest_tweaks_empty_when_fits():
    remaining = {"calories": 700, "protein_g": 40, "carbs_g": 80, "fat_g": 30}
    macros = {"calories": 500, "protein_g": 40, "carbs_g": 50, "fat_g": 18}
    assert suggest_tweaks(macros, remaining) == []


def test_pick_closest_ranks_by_proximity():
    remaining = {"calories": 600, "protein_g": 50, "carbs_g": 60, "fat_g": 20}
    options = [
        {"name": "spot on", "macros": {"calories": 600, "protein_g": 48, "carbs_g": 58, "fat_g": 19}},
        {"name": "too big", "macros": {"calories": 1200, "protein_g": 60, "carbs_g": 140, "fat_g": 50}},
        {"name": "too small", "macros": {"calories": 200, "protein_g": 15, "carbs_g": 20, "fat_g": 8}},
    ]
    ranked = pick_closest(options, remaining)
    assert ranked[0]["name"] == "spot on"  # closest by overall macro proximity
    assert "proximity" in ranked[0] and "tweaks" in ranked[0]
    assert ranked[0]["proximity"] > 0.9


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
