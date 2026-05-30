"""Run the Cekura eval suite end-to-end and write a markdown report.

Pipeline: select scenarios from config → submit in waves of `concurrency`
(Pipecat Cloud caps at 10 concurrent sessions) → poll each wave to completion →
merge results → render one report with per-run dashboard links.

Needs CEKURA_API_KEY (org-scoped key from dashboard.cekura.ai → Settings → API Keys).

CLI:
    python -m evals.run                      # full suite
    python -m evals.run --bucket stt_stress  # one bucket
    python -m evals.run --scenarios 272706 272716
    python -m evals.run --frequency 2 --name "nightly"
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from evals.cekura_client import CekuraClient
from evals.report import load_config, render

EVALS_DIR = Path(__file__).resolve().parent
RAW_DIR = EVALS_DIR / "reports" / "raw"


def select_scenarios(config: dict, buckets: list[str] | None, ids: list[int] | None) -> list[int]:
    """Resolve which scenario IDs to run from explicit ids, bucket filter, or all."""
    if ids:
        return ids
    out: list[int] = []
    for bucket, scenarios in config.get("scenarios", {}).items():
        if buckets and bucket not in buckets:
            continue
        out.extend(s["id"] for s in scenarios)
    return out


def _chunk(items: list[int], size: int) -> list[list[int]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def run_suite(
    config: dict,
    scenario_ids: list[int],
    frequency: int,
    run_name: str,
    client: CekuraClient | None = None,
) -> tuple[dict, dict[int, int]]:
    """Submit in waves, poll each, and return a merged result + run_id→result_id map."""
    client = client or CekuraClient()
    run_cfg = config.get("run", {})
    concurrency = int(run_cfg.get("concurrency", 10))
    poll = int(run_cfg.get("poll_interval_secs", 20))
    timeout = int(run_cfg.get("timeout_secs", 1800))

    waves = _chunk(scenario_ids, concurrency)
    merged_runs: dict[str, dict] = {}
    rid_by_run: dict[int, int] = {}
    first_result_id: int | None = None
    agent_name: str | None = None
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    for i, wave in enumerate(waves, 1):
        name = run_name if len(waves) == 1 else f"{run_name} (wave {i}/{len(waves)})"
        print(f"[wave {i}/{len(waves)}] submitting {len(wave)} scenarios…")
        started = client.trigger_pipecat_v2(wave, frequency=frequency, name=name)
        result_id = started["id"]
        first_result_id = first_result_id or result_id
        print(f"[wave {i}/{len(waves)}] result {result_id} running — polling…")
        result = client.wait_for_result(result_id, poll, timeout)
        agent_name = agent_name or result.get("agent_name")
        (RAW_DIR / f"result_{result_id}.json").write_text(json.dumps(result, indent=2))
        for run_id_str, run in result.get("runs", {}).items():
            merged_runs[run_id_str] = run
            rid_by_run[run["id"]] = result_id
        print(f"[wave {i}/{len(waves)}] done — {len(result.get('runs', {}))} runs collected.")

    merged = {
        "id": first_result_id,
        "name": run_name,
        "agent_name": agent_name or "agent",
        "status": "completed",
        "runs": merged_runs,
    }
    return merged, rid_by_run


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the Cekura eval suite and write a report.")
    ap.add_argument("--bucket", action="append", help="Limit to bucket(s): happy_path, edge_case, stt_stress")
    ap.add_argument("--scenarios", type=int, nargs="+", help="Explicit scenario IDs")
    ap.add_argument("--frequency", type=int, default=1, help="Runs per scenario (default 1)")
    ap.add_argument("--name", default=None, help="Result name")
    args = ap.parse_args()

    config = load_config()
    scenario_ids = select_scenarios(config, args.bucket, args.scenarios)
    if not scenario_ids:
        raise SystemExit("No scenarios selected.")
    name = args.name or f"Eval run {datetime.now(timezone.utc):%Y-%m-%d %H:%M}"

    print(f"Running {len(scenario_ids)} scenario(s): {scenario_ids}")
    merged, rid_by_run = run_suite(config, scenario_ids, args.frequency, name)

    md = render(merged, config, result_id_by_run=rid_by_run)
    out = EVALS_DIR / "reports" / f"report_{datetime.now(timezone.utc):%Y%m%d_%H%M}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    print(f"\nWrote report: {out}")


if __name__ == "__main__":
    main()
