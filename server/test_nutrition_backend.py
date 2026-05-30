#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Validation tests for the Tetris nutrition coach backend + tool schemas.

Pure/deterministic checks (no network) so they double as the reproducible basis
for Cekura's macro-math and allergy-safety evaluators.

Run: uv run python test_nutrition_backend.py
"""

import importlib.util
from pathlib import Path

from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams

import nutrition_backend as nb


def test_diet_filter():
    vegan = nb.find_meals(diet_style="vegan")
    assert vegan, "expected vegan options"
    for m in vegan:
        assert "vegan" in nb.get_meal(m["name"])["diets"], f"{m['name']} not vegan-compatible"


def test_allergy_exclusion_is_hard():
    # shellfish-allergic member must never be offered the shrimp poke bowl
    res = nb.find_meals(diet_style="omnivore", allergies=["shellfish"])
    assert "shrimp poke bowl" not in [m["name"] for m in res]
    # peanut allergy excludes peanut butter toast
    res2 = nb.find_meals(allergies=["peanuts"])
    assert "peanut butter banana toast" not in [m["name"] for m in res2]


def test_allergy_synonyms_and_singulars_screen_meals():
    # Natural phrasings must screen the right meals, not just exact catalog tokens.
    assert nb._excluded(nb.get_meal("egg-white veggie scramble"), ["egg"], None)  # singular
    assert nb._excluded(nb.get_meal("greek yogurt and berries"), ["milk"], None)  # milk -> dairy
    assert nb._excluded(nb.get_meal("shrimp poke bowl"), ["shrimp"], None)  # shrimp -> shellfish
    assert nb._excluded(nb.get_meal("salmon teriyaki plate"), ["salmon"], None)  # salmon -> fish
    assert nb._excluded(nb.get_meal("protein oatmeal"), ["wheat"], None)  # wheat -> gluten
    assert nb._excluded(nb.get_meal("peanut butter banana toast"), ["nuts"], None)  # nuts -> peanuts
    # End-to-end: an egg-allergic member is never offered the egg scramble...
    assert "egg-white veggie scramble" not in [m["name"] for m in nb.find_meals(allergies=["egg"])]
    # ...and a milk-allergic member gets zero dairy meals back.
    for m in nb.find_meals(allergies=["milk"]):
        classes = {c for tok in nb.get_meal(m["name"])["contains"] for c in nb._canonical_classes(tok)}
        assert "dairy" not in classes, m["name"]


def test_restriction_phrasing_variants():
    yogurt = nb.get_meal("greek yogurt and berries")
    for phrasing in ["no dairy", "no-dairy", "dairy-free", "avoid dairy", "without dairy", "no_dairy", "dairy"]:
        assert nb._excluded(yogurt, None, [phrasing]), phrasing
    # "no red meat" maps to the beef class
    assert nb._excluded(nb.get_meal("steak and sweet potato"), None, ["no red meat"])


def test_restriction_normalization():
    # "gluten-free" must screen out anything containing gluten
    res = nb.find_meals(restrictions=["gluten-free"])
    for m in res:
        assert "gluten" not in nb.get_meal(m["name"])["contains"], f"{m['name']} has gluten"
    # "no pork" phrasing should not error and should screen pork
    assert isinstance(nb.find_meals(restrictions=["no pork"]), list)


def test_source_filter_and_cap():
    res = nb.find_meals(source_type="restaurant", max_results=10)
    assert res and all(nb.get_meal(m["name"])["source"] == "restaurant" for m in res)
    assert len(nb.find_meals(max_results=10)) <= 5, "must hard-cap at 5 options"


def test_calc_targets_heuristic():
    t = nb.calc_targets({"goal": "cut", "activity_level": "moderate"})
    assert t["basis"] == "heuristic"
    assert 1500 < t["calories"] < 2500, t
    macro_cals = t["protein_g"] * 4 + t["carbs_g"] * 4 + t["fat_g"] * 9
    assert abs(macro_cals - t["calories"]) <= 60, (macro_cals, t)


def test_calc_targets_mifflin():
    t = nb.calc_targets(
        {
            "goal": "maintain",
            "activity_level": "moderate",
            "biometrics": {"sex": "male", "age": 30, "height_cm": 180, "weight_kg": 80},
        }
    )
    assert t["basis"] == "mifflin_st_jeor"
    assert t["protein_g"] == 160, t  # 2.0 g/kg * 80
    assert 2400 < t["calories"] < 3200, t


def test_remaining_macros_and_state():
    bot = _load_bot()
    state = bot.new_call_state()
    assert bot.remaining_macros(state) is None  # no targets yet
    assert bot.profile_missing(state) == ["goal", "diet_style", "activity_level"]

    state["profile"]["targets"] = {"calories": 2000, "protein_g": 150, "carbs_g": 200, "fat_g": 60}
    rem = bot.remaining_macros(state)
    assert rem["calories"] == 2000 and rem["meals_logged"] == 0

    state["log"]["meals"].append({"calories": 500, "protein_g": 40, "carbs_g": 50, "fat_g": 15})
    rem = bot.remaining_macros(state)
    assert rem["calories"] == 1500 and rem["protein_g"] == 110 and rem["meals_logged"] == 1

    # Going over budget yields negative remaining (drives "adapt when life happens")
    state["log"]["meals"].append({"calories": 1800, "protein_g": 60, "carbs_g": 120, "fat_g": 70})
    assert bot.remaining_macros(state)["calories"] < 0


def test_known_member_baseline():
    # Alex (cut) has eaten ~520 cal already; remaining should match the fixture.
    member = nb.KNOWN_MEMBERS["+14155551234"]
    t = member["profile"]["targets"]
    rem = member["today"]["remaining"]
    bot = _load_bot()
    state = bot.new_call_state()
    state["profile"]["targets"] = t
    state["consumed_baseline"] = {k: t[k] - rem.get(k, t[k]) for k in bot.MACRO_KEYS}
    assert bot.remaining_macros(state)["calories"] == rem["calories"]


def test_tool_schema_supports_list_params():
    async def set_restrictions(
        params: FunctionCallParams,
        allergies: list[str] | None = None,
        restrictions: list[str] | None = None,
    ) -> None:
        """Record allergies and restrictions.

        Args:
            allergies: Foods the member is allergic to.
            restrictions: Lifestyle or religious restrictions.
        """
        ...

    fs = ToolsSchema(standard_tools=[set_restrictions]).standard_tools[0]
    prop = fs.properties["allergies"]
    # Optional list[str] renders as anyOf: [{array of strings}, {null}].
    variants = prop.get("anyOf") or [prop]
    assert any(
        v.get("type") == "array" and v.get("items", {}).get("type") == "string" for v in variants
    ), prop


def _load_bot():
    spec = importlib.util.spec_from_file_location(
        "bot_nemotron", Path(__file__).parent / "bot-nemotron.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
        passed += 1
    print(f"\n{passed}/{len(tests)} tests passed")
