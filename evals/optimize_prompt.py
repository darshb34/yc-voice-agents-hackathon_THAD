"""Phase 4b — prompt-optimization loop (Cekura improve-prompt).

Improves the bot's SYSTEM PROMPT from failing runs. This is the behavioral/quality
loop (e.g. "didn't resolve 'next Tuesday' to a date", "never listed specials",
one-question-at-a-time style) — NOT latency, which is a code problem (see
the latency loop). Run this AFTER latency is fixed, so calls complete and the
failure signal is about behavior rather than dead-air.

Engine: Cekura's improve-prompt (the same thing the `cekura-self-improving-agent`
skill drives). Feed it the current system prompt + up to 3 failing run IDs; it
returns a revised prompt. We diff it for human review, then (on approval) promote
it to the canonical prompt file and the bot picks it up on redeploy.

Canonical prompt lives in `evals/prompts/system_instruction.current.txt` with
`{today}` / `{caller_context}` placeholders the bot fills at call time.

Two ways to run:

  # Human-in-the-loop: you (or Claude via MCP) saved Cekura's suggestion to a file
  python -m evals.optimize_prompt --candidate-file evals/prompts/suggestion.txt
  python -m evals.optimize_prompt --candidate-file evals/prompts/suggestion.txt --apply

  # Autonomous (needs CEKURA_API_KEY; REST paths flagged in cekura_client):
  python -m evals.optimize_prompt --run-ids 3199500 3199503 3199504
"""

from __future__ import annotations

import argparse
import difflib
import time
from datetime import datetime, timezone
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
CURRENT_PROMPT = PROMPTS_DIR / "system_instruction.current.txt"
# Keys Cekura's improve-prompt response might carry the revised prompt under.
_CANDIDATE_KEYS = ("improved_prompt", "suggested_prompt", "new_prompt", "prompt", "result")


def read_current() -> str:
    return CURRENT_PROMPT.read_text()


def render_diff(current: str, candidate: str) -> str:
    diff = difflib.unified_diff(
        current.splitlines(keepends=True),
        candidate.splitlines(keepends=True),
        fromfile="system_instruction.current.txt",
        tofile="system_instruction.candidate.txt",
    )
    return "".join(diff)


def _extract_candidate(payload: dict) -> str:
    """Best-effort pull of the revised prompt text from an improve-prompt payload."""
    for key in _CANDIDATE_KEYS:
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val
    raise SystemExit(
        f"Could not find a prompt in improve-prompt response (keys: {list(payload)}). "
        "Inspect the payload and extend _CANDIDATE_KEYS."
    )


def fetch_candidate_via_api(run_ids: list[int], poll: int = 10, timeout: int = 300) -> str:
    """Autonomous path: call Cekura improve-prompt and poll for the revised prompt."""
    from evals.cekura_client import CekuraClient

    client = CekuraClient()
    started = client.improve_prompt(read_current(), run_ids)
    # Some tenants return the result inline; others return a progress handle.
    try:
        return _extract_candidate(started)
    except SystemExit:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        progress = client.get_improve_prompt_progress()
        if progress.get("status") in ("completed", "success", "done"):
            return _extract_candidate(progress)
        time.sleep(poll)
    raise SystemExit("improve-prompt timed out without returning a prompt.")


def save_artifacts(current: str, candidate: str) -> tuple[Path, Path]:
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    cand_path = PROMPTS_DIR / f"candidate_{ts}.txt"
    diff_path = PROMPTS_DIR / f"diff_{ts}.patch"
    cand_path.write_text(candidate)
    diff_path.write_text(render_diff(current, candidate))
    return cand_path, diff_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate / review / apply a prompt improvement.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--candidate-file", type=Path, help="A revised prompt to review (from Cekura).")
    src.add_argument("--run-ids", type=int, nargs="+", help="Failing run IDs → fetch suggestion via API.")
    ap.add_argument("--apply", action="store_true", help="Promote the candidate to the canonical prompt file.")
    args = ap.parse_args()

    current = read_current()
    candidate = (
        args.candidate_file.read_text() if args.candidate_file else fetch_candidate_via_api(args.run_ids)
    )

    cand_path, diff_path = save_artifacts(current, candidate)
    diff = render_diff(current, candidate)
    print(diff or "(no textual change)")
    print(f"\nCandidate: {cand_path}\nDiff:      {diff_path}")

    if args.apply:
        CURRENT_PROMPT.write_text(candidate)
        print(f"\n✅ Promoted candidate to {CURRENT_PROMPT}.")
        print("Next: ensure bot-nemotron.py loads this prompt (Phase 4b wiring), then `pcc deploy`, then re-run the suite.")
    else:
        print("\nReview the diff. Re-run with --apply to promote it.")


if __name__ == "__main__":
    main()
