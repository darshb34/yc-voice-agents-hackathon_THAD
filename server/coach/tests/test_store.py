"""Unit tests for coach.store (single-user persistence + seeding).

Run: python -m pytest server/coach/tests/test_store.py
Or:  python server/coach/tests/test_store.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # server/coach

import store  # noqa: E402


def _fresh_dir():
    d = tempfile.mkdtemp(prefix="coachtest_")
    os.environ["COACH_DATA_DIR"] = d
    os.environ.pop("COACH_SEED_JSON", None)
    return d


def test_new_user_has_no_profile():
    _fresh_dir()
    assert store.load_profile() is None
    state = store.load_state()
    assert state["is_returning"] is False
    assert state["profile"]["goal"] is None
    assert state["log"]["meals"] == []


def test_save_and_load_profile_roundtrip():
    _fresh_dir()
    profile = store.blank_profile()
    profile.update({"name": "Sam", "goal": "cut", "diet_style": "vegetarian",
                    "allergies": ["peanut"], "activity_level": "moderate",
                    "targets": {"calories": 1870, "protein_g": 140, "carbs_g": 187, "fat_g": 62}})
    store.save_profile(profile)
    loaded = store.load_profile()
    assert loaded["name"] == "Sam" and loaded["goal"] == "cut"
    assert loaded["targets"]["calories"] == 1870
    assert store.load_state()["is_returning"] is True  # has goal + targets


def test_append_meal_accumulates_today():
    _fresh_dir()
    store.append_meal({"name": "greek yogurt parfait", "slot": "breakfast",
                       "macros": {"calories": 320, "protein_g": 24, "carbs_g": 38, "fat_g": 8}})
    store.append_meal({"name": "lentil quinoa salad", "slot": "lunch",
                       "macros": {"calories": 410, "protein_g": 20, "carbs_g": 56, "fat_g": 12}})
    log = store.load_today_log()
    assert len(log["meals"]) == 2
    assert log["meals"][0]["name"] == "greek yogurt parfait"


def test_seed_from_env_hydrates_returning_member():
    _fresh_dir()
    os.environ["COACH_SEED_JSON"] = json.dumps({
        "member": {"name": "Sam Rivera", "goal": "cut", "diet_style": "vegetarian",
                   "allergies": ["peanut"], "restrictions": [], "activity_level": "moderate"},
        "daily_targets": {"calories": 1870, "protein_g": 140, "carbs_g": 187, "fat_g": 62},
        "today_logs": [
            {"name": "greek yogurt parfait", "slot": "breakfast", "calories": 320,
             "protein_g": 24, "carbs_g": 38, "fat_g": 8},
            {"name": "lentil quinoa salad", "slot": "lunch", "calories": 410,
             "protein_g": 20, "carbs_g": 56, "fat_g": 12},
        ],
    })
    state = store.load_state()
    assert state["is_returning"] is True
    assert state["profile"]["diet_style"] == "vegetarian"
    assert state["profile"]["allergies"] == ["peanut"]
    assert state["profile"]["targets"]["calories"] == 1870
    assert len(state["log"]["meals"]) == 2
    assert state["log"]["meals"][1]["macros"]["protein_g"] == 20
    os.environ.pop("COACH_SEED_JSON", None)


def test_seed_with_biometrics():
    _fresh_dir()
    os.environ["COACH_SEED_JSON"] = json.dumps({
        "member": {"name": "Alex", "goal": "maintain", "activity_level": "moderate",
                   "biometrics": {"sex": "male", "age": 31, "weight_kg": 84.0, "height_cm": 180.0}},
        "daily_targets": {"calories": 2806, "protein_g": 210, "carbs_g": 281, "fat_g": 94},
    })
    p = store.load_state()["profile"]
    assert p["weight_kg"] == 84.0 and p["sex"] == "male"
    os.environ.pop("COACH_SEED_JSON", None)


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
