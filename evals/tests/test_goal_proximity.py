"""Unit tests for M4 Goal Proximity (evals/goal_proximity.py).

Run: python -m pytest evals/tests/test_goal_proximity.py
Or directly: python evals/tests/test_goal_proximity.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from evals.goal_proximity import (  # noqa: E402
    agent_text,
    extract_macros,
    goal_proximity,
    score_run,
    words_to_number,
)


# --- words_to_number ------------------------------------------------------

def test_words_to_number_units_and_tens():
    assert words_to_number("six") == 6
    assert words_to_number("ninety-six") == 96
    assert words_to_number("forty two") == 42


def test_words_to_number_hundreds_colloquial():
    assert words_to_number("five hundred forty") == 540
    assert words_to_number("eleven hundred forty") == 1140
    assert words_to_number("two hundred") == 200


def test_words_to_number_thousands():
    assert words_to_number("one thousand one hundred forty") == 1140
    assert words_to_number("two thousand") == 2000


def test_words_to_number_digits_and_filler():
    assert words_to_number("540") == 540
    assert words_to_number("forty grams of") == 40  # filler ignored
    assert words_to_number("grams of protein") is None  # no number


# --- extract_macros -------------------------------------------------------

def test_extract_macros_spoken():
    text = (
        "The tofu stir-fry has twenty-six grams of protein, "
        "five hundred calories, fifty-eight grams of carbs, and eighteen grams of fat."
    )
    assert extract_macros(text) == {
        "calories": 500, "protein_g": 26, "carbs_g": 58, "fat_g": 18,
    }


def test_extract_macros_digits():
    text = "About 540 calories and 42 g protein."
    got = extract_macros(text)
    assert got["calories"] == 540
    assert got["protein_g"] == 42


def test_extract_macros_uses_last_mention():
    # First a rejected option, then the final pick — we want the final numbers.
    text = (
        "The pizza is seven hundred forty calories. "
        "Instead, the burrito bowl is five hundred forty calories."
    )
    assert extract_macros(text)["calories"] == 540


# --- goal_proximity -------------------------------------------------------

def test_goal_proximity_perfect():
    target = {"calories": 1140, "protein_g": 96, "carbs_g": 93, "fat_g": 42}
    assert goal_proximity(dict(target), target) == 1.0


def test_goal_proximity_partial():
    target = {"calories": 1000, "protein_g": 100}
    # 10% off on calories, 20% off on protein -> mean error 0.15 -> 0.85
    rec = {"calories": 1100, "protein_g": 80}
    assert goal_proximity(rec, target) == 0.85


def test_goal_proximity_clamps_each_term():
    target = {"calories": 1000, "protein_g": 100}
    rec = {"calories": 5000, "protein_g": 100}  # calories 400% off -> clamped to 1.0
    # errors: 1.0 (cal), 0.0 (protein) -> mean 0.5 -> 0.5
    assert goal_proximity(rec, target) == 0.5


def test_goal_proximity_needs_calories_plus_one():
    target = {"calories": 1000, "protein_g": 100}
    assert goal_proximity({"protein_g": 100}, target) is None  # no calories
    assert goal_proximity({"calories": 1000}, target) is None  # calories alone


# --- agent_text -----------------------------------------------------------

def test_agent_text_filters_caller():
    transcript = [
        {"role": "user", "content": "I have ninety-six grams of protein left."},
        {"role": "assistant", "content": "Try the tofu stir-fry, twenty-six grams of protein."},
    ]
    txt = agent_text(transcript)
    assert "tofu stir-fry" in txt
    assert "ninety-six" not in txt  # caller's number must not leak in


def test_agent_text_speaker_key_and_string():
    assert "hello" in agent_text([{"speaker": "agent", "text": "hello there"}])
    assert agent_text("plain transcript string") == "plain transcript string"
    assert agent_text({"weird": "shape"}) == ""


# --- score_run (end to end) ----------------------------------------------

def test_score_run_end_to_end():
    target = {"calories": 1140, "protein_g": 96, "carbs_g": 93, "fat_g": 42}
    transcript = [
        {"role": "user", "content": "What's for dinner?"},
        {"role": "assistant", "content": (
            "Go with the sheet-pan chicken and veg — about eleven hundred forty calories, "
            "ninety-six grams of protein, ninety-three grams of carbs, forty-two grams of fat. "
            "That lands you right on target."
        )},
    ]
    result = score_run(transcript, target)
    assert result is not None
    assert result["score"] == 1.0
    assert result["parsed"]["calories"] == 1140


def test_score_run_returns_none_when_no_macros():
    target = {"calories": 1140, "protein_g": 96}
    transcript = [{"role": "assistant", "content": "Welcome back! How's the day going?"}]
    assert score_run(transcript, target) is None


# --- direct runner --------------------------------------------------------

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
