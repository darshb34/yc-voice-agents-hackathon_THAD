"""Deterministic nutrition math for the Tetris Nutrition Coach.

Ported from the proven hackathon backend (Mifflin–St Jeor TDEE, 30/40/30 macro
split, fit-scoring) and extended with the *compositional* helpers that power the
"get me as close to my goal as possible" behavior: proximity scoring, ranking a
set of options by closeness, and concrete spoken "tweak" suggestions (less oil,
swap, add a side) to close a macro gap.

Pure + deterministic (no I/O), so it's reproducible and unit-testable — the same
property the eval's M4 Goal Proximity relies on.
"""

from __future__ import annotations

MACRO_KEYS = ("calories", "protein_g", "carbs_g", "fat_g")

# Mifflin–St Jeor activity multipliers.
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


def macros_from_calories(calories: float) -> dict:
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

    Uses Mifflin–St Jeor when sex, age, weight_kg, and height_cm are all present;
    otherwise an activity-based heuristic. TDEE is scaled by a goal adjustment
    (cut −15% / maintain 0 / bulk +12%) and split 30/40/30.
    """
    activity = (profile.get("activity_level") or "moderate").lower()
    factor = _ACTIVITY_FACTORS.get(activity, 1.55)

    sex = profile.get("sex")
    age = profile.get("age")
    weight_kg = profile.get("weight_kg")
    height_cm = profile.get("height_cm")

    if sex and age and weight_kg and height_cm:
        bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age
        bmr += 5 if str(sex).lower().startswith("m") else -161
        tdee = bmr * factor
    else:
        tdee = _HEURISTIC_BASE.get(activity, 2200)

    goal = (profile.get("goal") or "maintain").lower()
    return macros_from_calories(tdee * _GOAL_ADJUST.get(goal, 1.0))


def remaining_macros(targets: dict | None, meals: list[dict]) -> dict | None:
    """targets − sum(logged meal macros). None until targets exist. Values may go
    negative — surfacing an over-budget day honestly is the point."""
    if not targets:
        return None
    remaining = {k: targets.get(k, 0) for k in MACRO_KEYS}
    for meal in meals:
        macros = meal.get("macros", meal)
        for k in MACRO_KEYS:
            remaining[k] -= macros.get(k, 0) or 0
    return remaining


def fit_score(macros: dict, remaining: dict | None) -> float:
    """How well a meal fits the remaining macro budget. Lower is better. Pure.

    Going over the remaining calorie budget is penalized heavily; leaving room is
    penalized lightly; protein is rewarded. Deterministic.
    """
    score = 0.0
    rem_cal = (remaining or {}).get("calories")
    cals = macros.get("calories", 0)
    if rem_cal is not None:
        if cals > rem_cal:
            score += (cals - rem_cal) * 3.0  # over budget — bad
        else:
            score += (rem_cal - cals) * 0.5  # leaves room — mild
    score -= macros.get("protein_g", 0) * 2.0  # reward protein
    return score


def rank_by_fit(items: list[dict], remaining: dict | None) -> list[dict]:
    """Return items sorted best-fit first by ``fit_score`` on each item's macros.

    Each item must carry a ``macros`` dict. Stable sort; does not mutate inputs.
    """
    return sorted(items, key=lambda it: fit_score(it.get("macros", {}), remaining))


def proximity(macros: dict, target: dict | None) -> float | None:
    """How close a meal/plan lands to a macro target. 1.0 = exact.

    ``1 − mean(|macro − target| / target)`` over the macros where the target is
    positive, each term clamped to [0, 1]. Returns None if there's no positive
    target to score against (e.g. already over budget on everything).
    """
    if not target:
        return None
    keys = [k for k in MACRO_KEYS if isinstance(target.get(k), (int, float)) and target.get(k, 0) > 0]
    if not keys:
        return None
    errs = [min(abs(macros.get(k, 0) - target[k]) / target[k], 1.0) for k in keys]
    return round(1.0 - sum(errs) / len(errs), 3)


def suggest_tweaks(macros: dict, remaining: dict | None) -> list[str]:
    """Spoken-friendly modifications to bring a meal closer to the remaining budget.

    Returns phrases the coach can offer ("less oil", "swap the rice", "add a side
    of protein"). Empty if the meal already fits well. Diet-agnostic — the coach
    adapts the wording to the member's diet/allergies.
    """
    if not remaining:
        return []
    tweaks: list[str] = []
    cal_r = remaining.get("calories")
    if cal_r is not None and cal_r > 0 and macros.get("calories", 0) > cal_r * 1.15:
        tweaks.append("it runs a little big for what's left, so a half portion or skipping a side keeps it in range")
    fat_r = remaining.get("fat_g")
    if fat_r is not None and macros.get("fat_g", 0) > max(fat_r, 0):
        tweaks.append("ask them to go light on the oil, or put any sauce or dressing on the side")
    carb_r = remaining.get("carbs_g")
    if carb_r is not None and macros.get("carbs_g", 0) > max(carb_r, 0):
        tweaks.append("go easy on the rice or bread, or swap some of it for extra vegetables")
    prot_r = remaining.get("protein_g")
    if prot_r is not None and prot_r > 0 and macros.get("protein_g", 0) < prot_r * 0.5:
        tweaks.append("add a side of protein to round it out")
    return tweaks


def pick_closest(options: list[dict], remaining: dict | None) -> list[dict]:
    """Rank candidate options by closeness to the remaining budget, annotating each.

    Ranked by ``proximity`` (overall macro closeness — the honest answer to "which
    gets me closest to my goal"), best first, with ``fit_score`` as a tie-break.
    Each returned option is a shallow copy with added ``fit_score``, ``proximity``,
    and ``tweaks``. This is the engine behind the compositional "which gets me
    closest, and how do I tweak it" coaching.
    """
    annotated = []
    for opt in options:
        macros = opt.get("macros", {})
        a = dict(opt)
        a["fit_score"] = round(fit_score(macros, remaining), 2)
        a["proximity"] = proximity(macros, remaining)
        a["tweaks"] = suggest_tweaks(macros, remaining)
        annotated.append(a)
    # Highest proximity first (None -> worst); break ties by best fit_score.
    annotated.sort(key=lambda a: (-(a["proximity"] if a["proximity"] is not None else -1.0), a["fit_score"]))
    return annotated
