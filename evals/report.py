"""Turn a Cekura result payload into a shareable markdown report.

Pure transform: no network. Feed it a result dict (from CekuraClient.get_result or
a saved JSON) plus the loaded config, and it emits a grouped markdown report —
pass rates, per-family metric rollup, failures grouped by root cause, an STT/WER
section, and dashboard links.

CLI:
    python -m evals.report --result-json path/to/result.json [--out report.md]
    python -m evals.report --result-id 591149 --out report.md   # needs CEKURA_API_KEY
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
WER_RE = re.compile(r"WER:\s*([\d.]+)%")


# --------------------------------------------------------------------------
# Config helpers
# --------------------------------------------------------------------------


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path) as fh:
        return yaml.safe_load(fh)


def _family_by_metric_id(config: dict) -> dict[int, str]:
    out: dict[int, str] = {}
    for family, metrics in config.get("metrics", {}).items():
        for m in metrics:
            out[m["id"]] = family
    return out


def _bucket_by_scenario_id(config: dict) -> dict[int, str]:
    out: dict[int, str] = {}
    for bucket, scenarios in config.get("scenarios", {}).items():
        for s in scenarios:
            out[s["id"]] = bucket
    return out


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RunSummary:
    run_id: int
    scenario_id: int
    scenario_name: str
    bucket: str
    status: str
    evaluation_status: str
    ended_reason: str
    duration: str
    passed: bool
    is_concurrency_failure: bool
    metrics: dict[str, dict]  # metric name -> {score, score_normalized, explanation}

    @property
    def expected_outcome_score(self) -> float | None:
        m = self.metrics.get("Expected Outcome")
        return m.get("score_normalized") if m else None

    @property
    def wer_pct(self) -> float | None:
        m = self.metrics.get("Transcription Accuracy")
        if not m:
            return None
        match = WER_RE.search(str(m.get("explanation", "")))
        return float(match.group(1)) if match else None

    @property
    def avg_latency_ms(self) -> float | None:
        m = self.metrics.get("Latency (in ms)")
        return m.get("score") if m else None

    @property
    def dead_air(self) -> bool:
        m = self.metrics.get("Infrastructure Issues")
        return bool(m) and m.get("score_normalized") == 0


def parse_run(run: dict, bucket_by_scenario: dict[int, str]) -> RunSummary:
    metrics = {
        m["name"]: {
            "score": m.get("score"),
            "score_normalized": m.get("score_normalized"),
            "explanation": m.get("explanation"),
        }
        for m in run.get("evaluation", {}).get("metrics", [])
    }
    ended = (run.get("metadata") or {}).get("ended_reason", "") or ""
    return RunSummary(
        run_id=run["id"],
        scenario_id=run.get("scenario"),
        scenario_name=run.get("scenario_name", "?"),
        bucket=bucket_by_scenario.get(run.get("scenario"), "other"),
        status=run.get("status", "?"),
        evaluation_status=run.get("evaluation_status", "?"),
        ended_reason=ended,
        duration=run.get("duration", "00:00"),
        passed=run.get("evaluation_status") == "success",
        is_concurrency_failure=ended == "pipecat-agent-concurrency-limit-reached",
        metrics=metrics,
    )


def classify_failure(r: RunSummary) -> str:
    """Bucket a failed run by most-actionable root cause."""
    if r.is_concurrency_failure:
        return "Infrastructure: Pipecat concurrency limit (not an agent bug)"
    if r.status not in ("completed", "evaluating"):
        return "Did not complete"
    if r.dead_air:
        return "Dead air / latency (agent silent >10s)"
    rep = r.metrics.get("Unnecessary Repetition Score", {})
    if rep.get("score_normalized") is not None and rep["score_normalized"] < 3:
        return "Stuck loop / unnecessary repetition"
    return "Task outcome missed (see expected-outcome detail)"


# --------------------------------------------------------------------------
# Aggregation + rendering
# --------------------------------------------------------------------------


def _dashboard_link(config: dict, result_id: int, run_id: int, label: str) -> str:
    url = config["cekura"]["dashboard_result_url"].format(
        project_id=config["cekura"]["project_id"], result_id=result_id, run_id=run_id
    )
    return f"[{label}]({url})"


def _family_rollup(runs: list[RunSummary], config: dict) -> list[tuple[str, str]]:
    """Average normalized score per metric family across runs that have it."""
    fam_by_name = {
        m["name"]: fam
        for fam, ms in config.get("metrics", {}).items()
        for m in ms
    }
    buckets: dict[str, list[float]] = {}
    for r in runs:
        for name, m in r.metrics.items():
            fam = fam_by_name.get(name)
            val = m.get("score_normalized")
            if fam and isinstance(val, (int, float)):
                buckets.setdefault(fam, []).append(float(val))
    rows = []
    for fam, vals in sorted(buckets.items()):
        rows.append((fam, f"{statistics.mean(vals):.2f} (n={len(vals)})"))
    return rows


def _emoji(r: RunSummary) -> str:
    if r.is_concurrency_failure:
        return "🚫"
    if r.status not in ("completed", "evaluating"):
        return "⏳"
    return "✅" if r.passed else "❌"


def render(
    result: dict,
    config: dict,
    runs_detail: list[dict] | None = None,
    result_id_by_run: dict[int, int] | None = None,
) -> str:
    bucket_by_scenario = _bucket_by_scenario_id(config)
    runs = [parse_run(r, bucket_by_scenario) for r in result.get("runs", {}).values()]
    runs.sort(key=lambda r: (r.bucket, r.scenario_name))
    result_id = result["id"]
    # Per-run result_id for dashboard links (differs per wave when merged).
    rid_map = result_id_by_run or {r.run_id: result_id for r in runs}

    def link(r: RunSummary, label: str) -> str:
        return _dashboard_link(config, rid_map.get(r.run_id, result_id), r.run_id, label)
    scored = [r for r in runs if r.status in ("completed", "evaluating") and not r.is_concurrency_failure]
    passes = [r for r in scored if r.passed]
    wer_vals = [r.wer_pct for r in scored if r.wer_pct is not None]
    lat_vals = [r.avg_latency_ms for r in scored if r.avg_latency_ms is not None]
    dead = [r for r in scored if r.dead_air]
    concurrency_fails = [r for r in runs if r.is_concurrency_failure]

    lines: list[str] = []
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append("<!-- CEKURA-REPORT-START -->")
    lines.append(f"# Cekura Quality Report — {result.get('agent_name', 'agent')}")
    lines.append("")
    lines.append(f"- **Result:** {result.get('name')} (`{result_id}`) · generated {ts}")
    lines.append(f"- **Status:** {result.get('status')} · "
                 f"scored {len(scored)}/{len(runs)} runs · "
                 f"**{len(passes)}/{len(scored)} passed**"
                 + (f" ({100*len(passes)/len(scored):.0f}%)" if scored else ""))
    lines.append(f"- **Connection:** Pipecat Cloud (`{config['pipecat_cloud']['agent_name']}`) via pipecat_v2")
    if concurrency_fails:
        lines.append(f"- ⚠️ **{len(concurrency_fails)} run(s) never started** "
                     f"(`pipecat-agent-concurrency-limit-reached`) — exceeded the 10-agent cap; "
                     f"re-run these in a later wave.")
    lines.append("")

    # Headline
    lines.append("## Headline")
    lines.append("")
    if lat_vals:
        lines.append(f"- ⏱️ **Latency is the dominant problem:** mean per-turn agent latency "
                     f"**{statistics.mean(lat_vals)/1000:.1f}s** across scored runs "
                     f"(worst run avg {max(lat_vals)/1000:.1f}s). "
                     f"{len(dead)}/{len(scored)} runs tripped Infrastructure Issues (silent >10s).")
    if wer_vals:
        lines.append(f"- 🎙️ **STT is healthy:** mean transcription WER "
                     f"**{statistics.mean(wer_vals):.1f}%** — not the bottleneck.")
    lines.append("")

    # Scenario table
    lines.append("## Results by scenario")
    lines.append("")
    lines.append("| Scenario | Bucket | Outcome | Latency | WER | Dead air | Link |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in runs:
        lat = f"{r.avg_latency_ms/1000:.1f}s" if r.avg_latency_ms else "—"
        wer = f"{r.wer_pct:.0f}%" if r.wer_pct is not None else "—"
        da = "⚠️" if r.dead_air else ("—" if r.status in ("completed", "evaluating") else "")
        lines.append(f"| {r.scenario_name} | {r.bucket} | {_emoji(r)} {r.evaluation_status} "
                     f"| {lat} | {wer} | {da} | {link(r, 'view')} |")
    lines.append("")

    # Failures grouped by cause
    failed = [r for r in scored if not r.passed]
    if failed:
        lines.append("## Failures by root cause")
        lines.append("")
        groups: dict[str, list[RunSummary]] = {}
        for r in failed:
            groups.setdefault(classify_failure(r), []).append(r)
        for cause, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            names = " · ".join(link(r, r.scenario_name) for r in rs)
            lines.append(f"### ❌ {cause} ({len(rs)})")
            lines.append(f"{names}")
            eo = next((r.metrics.get("Expected Outcome") for r in rs if r.metrics.get("Expected Outcome")), None)
            if eo and isinstance(eo.get("explanation"), list):
                proof = next((e for e in eo["explanation"] if str(e).startswith("❌")), None)
                if proof:
                    lines.append(f"> {proof}")
            lines.append("")

    # Metric family rollup
    rollup = _family_rollup(scored, config)
    if rollup:
        lines.append("## Metric family rollup (avg normalized score)")
        lines.append("")
        lines.append("| Family | Avg score |")
        lines.append("|---|---|")
        for fam, val in rollup:
            lines.append(f"| {fam} | {val} |")
        lines.append("")

    # Next steps
    lines.append("## Next steps")
    lines.append("")
    if dead or (lat_vals and statistics.mean(lat_vals) > 3000):
        lines.append("1. **Cut per-turn latency** — the agent narrates each tool step "
                     "(`add_to_order` → `get_order_summary` → `set_delivery_details`) with a "
                     "round-trip each. Batch tool calls / trim narration; consider a holding "
                     "phrase so silence isn't dead air. This is the Phase 4 prompt-optimization target.")
    if concurrency_fails:
        lines.append("2. **Concurrency** — keep waves ≤10 or raise the Pipecat cap "
                     "(`pcc deploy --max-agents N`); re-run the skipped scenarios.")
    lines.append("<!-- CEKURA-REPORT-END -->")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a Cekura result as a markdown report.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--result-json", type=Path, help="Path to a saved result JSON.")
    src.add_argument("--result-id", type=int, help="Fetch this result via the API (needs CEKURA_API_KEY).")
    ap.add_argument("--out", type=Path, default=None, help="Output path (default: evals/reports/<result>_<ts>.md)")
    args = ap.parse_args()

    config = load_config()
    if args.result_json:
        result = json.loads(args.result_json.read_text())
    else:
        from evals.cekura_client import CekuraClient

        result = CekuraClient().get_result(args.result_id)

    md = render(result, config)
    out = args.out or (
        Path(__file__).resolve().parent
        / "reports"
        / f"result_{result['id']}_{datetime.now(timezone.utc):%Y%m%d_%H%M}.md"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
