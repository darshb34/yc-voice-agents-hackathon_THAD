#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Per-member persistence for the Tetris Nutrition Coach.

A registered member is keyed by phone number. Each member's profile, meal log,
and a trimmed conversation transcript are stored as one JSON file under
``data/members/``. On a returning call (recognized by caller ID, or when the
member gives their number) we reload that record so intake is skipped and the
coach can pick up where it left off.

This is a deliberately simple file store — the swap point for a real database.
``data/members/`` is git-ignored (it holds user data).
"""

import json
import os
import re
from datetime import datetime, timezone

from loguru import logger

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data", "members")

# How much conversation to retain / replay so files and recaps stay bounded.
_MAX_TRANSCRIPT = 60
_RECAP_TURNS = 6


def _key(member_id: str | int | None) -> str:
    """Normalize a phone number to a filesystem-safe key (digits only).
    Accepts ints too — the LLM sometimes passes the phone as a number."""
    return re.sub(r"\D", "", str(member_id or ""))


def _path(member_id: str | None) -> str | None:
    key = _key(member_id)
    return os.path.join(_DATA_DIR, f"p_{key}.json") if key else None


def load_member(member_id: str | None) -> dict | None:
    """Load a saved member record, or None if there's no file / it's unreadable.

    Returns a dict with keys: member_id, name, profile, log, transcript, updated_at.
    """
    path = _path(member_id)
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load member {member_id}: {e}")
        return None


def save_member(
    member_id: str | None,
    profile: dict,
    log: dict,
    transcript: list,
    name: str | None = None,
) -> None:
    """Persist (overwrite) a member's profile, meal log, and recent transcript."""
    path = _path(member_id)
    if not path:
        logger.warning("save_member called with no usable member_id — skipping")
        return
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        record = {
            "member_id": member_id,
            "name": name or profile.get("name"),
            "profile": profile,
            "log": log,
            "transcript": transcript[-_MAX_TRANSCRIPT:],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(path, "w") as f:
            json.dump(record, f, indent=2)
        logger.info(f"Saved member history: {path}")
    except Exception as e:
        logger.error(f"Failed to save member {member_id}: {e}")


def extract_transcript(messages: list) -> list:
    """Reduce raw LLM-context messages to a clean [{role, content}] transcript.

    Keeps only user/assistant turns with real text — drops the system prompt,
    tool calls/results, and the internal greeting-kickoff instruction.
    """
    out = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role not in ("user", "assistant"):
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        text = content.strip()
        if text.startswith(("A member just called", "A returning member", "A customer")):
            continue  # internal greeting instruction, not real dialogue
        out.append({"role": role, "content": text[:500]})
    return out


def build_recap(record: dict | None) -> str:
    """Build a short, single-line recap of the last few turns for context.

    Meant to be handed to the model as background (not read aloud verbatim).
    """
    if not record:
        return ""
    turns = (record.get("transcript") or [])[-_RECAP_TURNS:]
    if not turns:
        return ""
    lines = [
        f"{'Member' if m['role'] == 'user' else 'You'}: {m['content']}" for m in turns
    ]
    return " | ".join(lines)
