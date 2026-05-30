"""M4 — Goal Proximity: deterministic measure of how close the agent's recommended
meal lands to the member's *remaining* macro target.

Why deterministic instead of an LLM judge: for the seeded returning-member
scenarios (G/H/I) we KNOW the remaining macro budget (the scenario's ``target:``
in config.yaml), and macro arithmetic should be exact. LLM judges are flaky at
arithmetic, so we parse the macros the agent actually spoke — numbers are read
aloud in words ("five hundred forty calories") — and compute the closeness
ourselves. Reproducible, no network.

Pure module. The risky parts (spoken-number parsing, macro extraction, the score)
are unit-tested in ``evals/tests/test_goal_proximity.py``.
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------
# Spoken-number parsing
# --------------------------------------------------------------------------

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}

# Filler words allowed between a macro keyword and its number ("grams of").
_FILLER = {
    "grams", "gram", "g", "of", "about", "around", "roughly", "approximately",
    "a", "an", "is", "are", "has", "have", "with", "the", "that", "s", "its",
}


def words_to_number(text: str) -> int | None:
    """Convert a spoken-number phrase (or digits) to an int.

    Handles digit forms, tens-ones ("ninety-six"), hundreds/thousands, and the
    colloquial "eleven hundred forty" == 1140. Returns None if no number token
    is present. Non-number tokens are ignored, so "forty grams of" -> 40.
    """
    cleaned = text.lower().replace("-", " ")
    tokens = re.findall(r"[a-z]+|\d+", cleaned)
    total = 0
    current = 0
    have = False
    for tok in tokens:
        if tok.isdigit():
            current += int(tok)
            have = True
        elif tok in _UNITS:
            current += _UNITS[tok]
            have = True
        elif tok in _TENS:
            current += _TENS[tok]
            have = True
        elif tok == "hundred":
            current = (current or 1) * 100
            have = True
        elif tok == "thousand":
            total += (current or 1) * 1000
            current = 0
            have = True
        # any other token is ignored (e.g. "grams", "of")
    return (total + current) if have else None


def _is_number_token(tok: str) -> bool:
    return words_to_number(tok) is not None


def _number_before(text: str, end: int) -> int | None:
    """Find the number phrase immediately preceding position ``end`` in ``text``.

    Walks backwards from the keyword, skipping filler ("grams", "of") until it
    reaches the contiguous number phrase, then parses it. Stops at the first
    non-number, non-filler token so earlier numbers don't bleed in.
    """
    tokens = re.findall(r"[\w-]+", text[:end])
    picked: list[str] = []
    seen_num = False
    for tok in reversed(tokens):
        t = tok.lower()
        if _is_number_token(t):
            picked.append(tok)
            seen_num = True
        elif not seen_num and t in _FILLER:
            continue
        else:
            break
    if not seen_num:
        return None
    return words_to_number(" ".join(reversed(picked)))


# Macro keyword patterns (matched case-insensitively).
_MACRO_PATTERNS = {
    "calories": re.compile(r"calor", re.I),
    "protein_g": re.compile(r"protein", re.I),
    "carbs_g": re.compile(r"carb", re.I),
    "fat_g": re.compile(r"\bfat", re.I),
}


def extract_macros(text: str) -> dict[str, int]:
    """Extract macro values the agent stated in ``text``.

    For each macro keyword (calories/protein/carbs/fat), takes the number that
    immediately precedes the *last* mention of that keyword (the final
    recommendation tends to be stated last). Returns only macros it could parse.
    """
    out: dict[str, int] = {}
    for macro, pat in _MACRO_PATTERNS.items():
        last_val: int | None = None
        for m in pat.finditer(text):
            val = _number_before(text, m.start())
            if val is not None:
                last_val = val
        if last_val is not None:
            out[macro] = last_val
    return out


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

_MACRO_KEYS = ("calories", "protein_g", "carbs_g", "fat_g")


def goal_proximity(rec: dict, target: dict) -> float | None:
    """Closeness of a recommended meal's macros to the remaining target.

    ``1 - mean(|rec - target| / target)`` over the macros present in both, each
    term clamped to [0, 1] so a wildly-off macro can't drag the score negative.
    Returns None unless calories plus at least one other macro are available
    (a lone macro is too weak to score).

    Args:
        rec: parsed recommendation macros, e.g. {"calories": 520, "protein_g": 42}.
        target: the remaining macro budget the recommendation should hit.

    Returns:
        Score in [0, 1] (higher = closer), or None if not enough data.
    """
    keys = [
        k for k in _MACRO_KEYS
        if isinstance(target.get(k), (int, float)) and target.get(k)
        and isinstance(rec.get(k), (int, float))
    ]
    if "calories" not in keys or len(keys) < 2:
        return None
    errs = [min(abs(rec[k] - target[k]) / target[k], 1.0) for k in keys]
    return round(1.0 - sum(errs) / len(errs), 3)


# --------------------------------------------------------------------------
# Transcript helpers
# --------------------------------------------------------------------------

_AGENT_ROLES = {"assistant", "agent", "bot", "ai", "system", "model"}
_TEXT_KEYS = ("content", "text", "message", "transcript", "value")


def agent_text(transcript_object: object) -> str:
    """Concatenate the AGENT's turns from a Cekura ``transcript_object``.

    Defensive about shape: accepts a list of turn dicts with a role/speaker key
    and a text key, or a plain string. Returns only the agent-under-test's words
    (not the simulated caller's), so caller-spoken numbers don't contaminate the
    macro parse. Returns "" if it can't identify agent turns.
    """
    if isinstance(transcript_object, str):
        return transcript_object
    if not isinstance(transcript_object, list):
        return ""
    parts: list[str] = []
    for turn in transcript_object:
        if not isinstance(turn, dict):
            continue
        role = str(
            turn.get("role") or turn.get("speaker") or turn.get("from") or ""
        ).lower()
        if role and role not in _AGENT_ROLES:
            continue  # a labelled non-agent turn (the caller) — skip
        text = next(
            (turn[k] for k in _TEXT_KEYS if isinstance(turn.get(k), str)), ""
        )
        if text and (role in _AGENT_ROLES or not role):
            parts.append(text)
    return " ".join(parts)


def score_run(transcript_object: object, target: dict) -> dict | None:
    """End-to-end M4 for one run: agent text -> parsed macros -> proximity.

    Returns {"score", "parsed", "target"} or None if it couldn't score.
    """
    text = agent_text(transcript_object)
    if not text:
        return None
    parsed = extract_macros(text)
    score = goal_proximity(parsed, target)
    if score is None:
        return None
    return {"score": score, "parsed": parsed, "target": target}
