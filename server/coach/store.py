"""Single-user persistence for the Tetris Nutrition Coach.

The hackathon deployment serves exactly ONE user, so we don't key by caller ID —
there's one durable profile and one rolling daily log:

  * ``profile.json``        — durable across days (goal, diet, allergies, activity,
                              biometrics, computed daily targets).
  * ``log_<YYYY-MM-DD>.json`` — that day's logged meals. A new calendar day starts
                              empty automatically (different filename).

Boot behavior this enables (see ``load_state``): first call ever -> no profile ->
onboard + persist; every later call -> profile loaded -> SKIP onboarding; same-day
later call -> today's log loaded -> recommendations sized to what's *left*.

Seeding for deterministic eval: set ``COACH_SEED_JSON`` (the test-profile shape
from docs/PERSISTENCE_CONTRACT.md) and the state is written on boot. The data dir
is ``COACH_DATA_DIR`` (default: ``<module>/data/coach_state``), so the harness or
a temp dir can redirect it.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date

logger = logging.getLogger("coach.store")

_PROFILE_FIELDS = (
    "name", "goal", "diet_style", "allergies", "restrictions", "activity_level",
    "sex", "age", "weight_kg", "height_cm", "targets",
)


def _data_dir() -> str:
    return os.environ.get(
        "COACH_DATA_DIR",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "coach_state"),
    )


def _profile_path() -> str:
    return os.path.join(_data_dir(), "profile.json")


def _log_path(day: str | None = None) -> str:
    return os.path.join(_data_dir(), f"log_{day or date.today().isoformat()}.json")


def blank_profile() -> dict:
    """A fresh, empty profile (new caller, pre-intake)."""
    return {
        "name": None, "goal": None, "diet_style": None,
        "allergies": [], "restrictions": [], "activity_level": None,
        "sex": None, "age": None, "weight_kg": None, "height_cm": None,
        "targets": None,
    }


def load_profile() -> dict | None:
    """Load the durable profile, or None if the user hasn't onboarded yet."""
    path = _profile_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        profile = blank_profile()
        profile.update({k: data.get(k, profile[k]) for k in _PROFILE_FIELDS})
        return profile
    except Exception as e:
        logger.error(f"Failed to load profile: {e}")
        return None


def save_profile(profile: dict) -> None:
    """Persist (overwrite) the durable profile."""
    try:
        os.makedirs(_data_dir(), exist_ok=True)
        with open(_profile_path(), "w") as f:
            json.dump({k: profile.get(k) for k in _PROFILE_FIELDS}, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save profile: {e}")


def load_today_log() -> dict:
    """Load today's meal log ({"date", "meals"}). Empty if nothing logged today."""
    path = _log_path()
    if not os.path.exists(path):
        return {"date": date.today().isoformat(), "meals": []}
    try:
        with open(path) as f:
            data = json.load(f)
        return {"date": data.get("date", date.today().isoformat()), "meals": data.get("meals", [])}
    except Exception as e:
        logger.error(f"Failed to load today's log: {e}")
        return {"date": date.today().isoformat(), "meals": []}


def save_today_log(log: dict) -> None:
    try:
        os.makedirs(_data_dir(), exist_ok=True)
        with open(_log_path(), "w") as f:
            json.dump({"date": log.get("date", date.today().isoformat()), "meals": log.get("meals", [])}, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save today's log: {e}")


def append_meal(meal: dict) -> dict:
    """Append a logged meal to today's log and persist. Returns the updated log.

    ``meal`` shape: {"name", "slot", "macros": {calories, protein_g, carbs_g, fat_g}}.
    """
    log = load_today_log()
    log["meals"].append(meal)
    save_today_log(log)
    return log


def _seed_from_env() -> bool:
    """If COACH_SEED_JSON is set, write the seeded profile + today's log. Idempotent
    per process via a sentinel. Returns True if it seeded. See PERSISTENCE_CONTRACT."""
    raw = os.environ.get("COACH_SEED_JSON")
    if not raw:
        return False
    try:
        seed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"COACH_SEED_JSON is not valid JSON: {e}")
        return False

    member = seed.get("member", {})
    profile = blank_profile()
    for k in ("name", "goal", "diet_style", "allergies", "restrictions", "activity_level"):
        if member.get(k) is not None:
            profile[k] = member[k]
    bio = member.get("biometrics")
    if isinstance(bio, dict):
        for k in ("sex", "age", "weight_kg", "height_cm"):
            if bio.get(k) is not None:
                profile[k] = bio[k]
    if seed.get("daily_targets"):
        profile["targets"] = seed["daily_targets"]
    save_profile(profile)

    meals = []
    for m in seed.get("today_logs", []):
        meals.append({
            "name": m.get("name", "meal"),
            "slot": m.get("slot"),
            "macros": {k: m.get(k, 0) for k in ("calories", "protein_g", "carbs_g", "fat_g")},
        })
    save_today_log({"date": date.today().isoformat(), "meals": meals})
    logger.info(f"Seeded single-user state from COACH_SEED_JSON ({len(meals)} meals logged today)")
    return True


def load_state(seed: bool = True) -> dict:
    """Hydrate per-call state at boot.

    Returns {"profile", "log", "is_returning"}: ``profile`` is the loaded durable
    profile or a blank one; ``log`` is today's meal log; ``is_returning`` is True
    when the user has already onboarded (profile has computed targets).
    """
    if seed:
        _seed_from_env()
    profile = load_profile()
    return {
        "profile": profile or blank_profile(),
        "log": load_today_log(),
        "is_returning": bool(profile and profile.get("targets") and profile.get("goal")),
    }
