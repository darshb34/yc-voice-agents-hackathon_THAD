# Cekura Self-Improvement Loop — Design

> Naming note: the platform is **Cekura** (sometimes mis-heard as "Secura"). All
> code, secrets, and the dashboard use the spelling `cekura`.

This document describes the **closed-loop, human-gated behavioral self-improvement
system** wired up around the Tetris Nutrition Coach voice agent. Its job is
narrow: turn failing eval runs into a concrete, reviewable prompt revision, and
do it without humans writing the prompt diff by hand.

It is intentionally **not** a fine-tuning loop, a RAG-update loop, or a runtime
agentic critic — it is a build-time prompt-optimization loop driven by Cekura's
`improve-prompt` engine (the same engine that powers the
`cekura-self-improving-agent` skill).

## TL;DR

```
                  ┌──────────────────────────────────────────────┐
                  │   server/coach_prompt.py                     │
                  │   build_system_instruction(caller_context)   │  ← SINGLE SOURCE OF TRUTH
                  └────────────────────┬─────────────────────────┘
                                       │ deployed via `pc deploy` to
                                       ▼
                  Pipecat Cloud agent  "flower-bot"  (Tetris Coach inside)
                                       │
                                       │  Daily/WebRTC sessions
                                       ▼
   ┌──────────────────────  Cekura cloud  ───────────────────────────┐
   │   evals/run.py    →  trigger_pipecat_v2 (waves of ≤10)          │
   │                   →  wait_for_result   (poll every 20s)         │
   │                   →  raw JSON dumped to evals/reports/raw/      │
   │   evals/report.py →  markdown report:                           │
   │                       · pass rates by bucket                    │
   │                       · failures grouped by root cause          │
   │                       · M4 Goal Proximity (deterministic)       │
   │                       · per-run dashboard links                 │
   └────────────────────────────┬───────────────────────────────────-┘
                                │  pick ≤3 failing run IDs
                                ▼
   ┌─────────────────  evals/optimize_prompt.py  ─────────────────────┐
   │  CekuraClient.improve_prompt(current_prompt, run_ids)            │
   │      → (poll get_improve_prompt_progress until done)             │
   │      → candidate_<ts>.txt   +   diff_<ts>.patch                  │
   │      ↓ (human review of the diff)                                │
   │  --apply  →  evals/prompts/system_instruction.current.txt        │
   │             (the canonical prompt the harness edits)             │
   └──────────────────────────────┬──────────────────────────────────-┘
                                  │ promote into server/coach_prompt.py
                                  │ then `pc deploy`
                                  ▼
                            ↻ re-run suite
```

## Why this shape (design principles)

1. **One file changes per iteration.** The persona lives in
   `server/coach_prompt.py::build_system_instruction` — a single function that
   takes only `caller_context` and returns the full system string. The
   optimization harness mirrors it in `evals/prompts/system_instruction.current.txt`
   so the only artifact that ever changes is one text file. Diffs are short,
   reviewable, and bisectable.

2. **Human-in-the-loop by default.** `optimize_prompt.py` writes a candidate
   and a unified diff to `evals/prompts/`, prints the diff, and **does
   nothing else** unless you re-invoke with `--apply`. A misbehaving suggestion
   from Cekura cannot silently ship.

3. **No MCP dependency in the pipeline.** Cekura is also driven from Claude
   Code via an MCP + skills bundle, but the OAuth token expires often. The eval
   pipeline uses a standalone REST client (`evals/cekura_client.py`) with an
   org-scoped API key (`CEKURA_API_KEY`). This makes the loop runnable from a
   terminal, a CI job, or a cron — not just from an interactive Claude session.

4. **Wave batching respects upstream limits.** Pipecat Cloud caps a deployed
   agent at ~10 concurrent sessions; exceeding it yields
   `pipecat-agent-concurrency-limit-reached` runs that look like agent failures
   but aren't. `run.py` chunks scenarios into waves of `run.concurrency`
   (config.yaml, default 3 during dev, 10 cap) and waits for each wave before
   submitting the next.

5. **Reporting is a pure transform.** `report.py` takes a result JSON + the
   config and emits markdown — no network calls. This means a saved
   `evals/reports/raw/result_*.json` can be re-rendered offline (useful for
   triage and unit tests), and the renderer can be tested without burning
   Cekura credits.

6. **Deterministic where possible (M4 Goal Proximity).** LLM judges are flaky
   at arithmetic. For the seeded returning-member scenarios (G/H/I) we know the
   *remaining* macro budget from the test profile, so
   `evals/goal_proximity.py` parses the macros the agent actually *spoke*
   ("five hundred forty calories" → 540) and scores closeness in pure Python.
   Result: reproducible, no API call, unit-tested.

7. **Two loops, not one.** Behavior issues (didn't resolve "next Tuesday",
   never listed specials, asked stacked questions) → the prompt loop in this
   doc. Latency / dead-air issues (Nemotron 120B sequential tool round-trips)
   → a code-level loop, not a prompt loop. They share the eval suite but the
   *fix path* is different. Don't try to prompt-engineer your way out of a
   2-second tool round-trip.

## The five components

### 1. `evals/config.yaml` — the contract

Single source of truth for:

- **Cekura IDs**: organization, project, agent (`18069 — Tetris Nutrition Coach`),
  personality (`693`), folder.
- **Pipecat target**: org + agent name (`flower-bot`, kept from the original
  flower-shop deploy; the running code is the coach).
- **Run policy**: `frequency`, `concurrency`, `poll_interval_secs`, `timeout_secs`.
- **Prompt source**: `prompt_source: "server/coach_prompt.py::build_system_instruction"`
  — declarative pointer to the one file the loop edits.
- **Metrics**: 11 Cekura project metrics across 4 families (task success,
  tool-calling, conversational quality, STT, performance) + 3 custom
  recall/adaptation metrics. M4 Goal Proximity is *not* listed here because it
  is computed locally in `report.py`.
- **Scenarios**: grouped into buckets (`onboarding`, `recommend`, `safety`,
  `adapt`, `guidance`, `summary`, `returning`). Returning scenarios carry a
  `test_profile` ID (the seeded single-user state Cekura injects) and
  optionally a `target:` (remaining macro budget — ground truth for M4).

### 2. `evals/cekura_client.py` — the REST layer

Thin wrapper. Three responsibilities:

- **Trigger**: `trigger_pipecat_v2(scenario_ids, frequency, name)` — POST to
  `/test_framework/v1/scenarios/run_scenarios_pipecat_v2/`. Returns a result
  payload with an `id`.
- **Collect**: `get_result(result_id)` and `wait_for_result(...)` which polls
  until status is terminal (`completed` or `failed`) or the timeout elapses.
  Timeout returns the **partial** result instead of raising, so a slow run
  still produces a report for what finished.
- **Improve**: `improve_prompt(prompt, run_ids[:3])` +
  `get_improve_prompt_progress()` — mirrors the `runs_improve_prompt_create`
  MCP tool. Hard cap of 3 run IDs (Cekura's limit).

Auth is `X-CEKURA-API-KEY` from `CEKURA_API_KEY`. No OAuth, no refresh.

### 3. `evals/run.py` — the orchestrator

```
select_scenarios(config, buckets, ids)         # filter / explicit
    │
    ▼
_chunk(scenario_ids, concurrency)              # waves of ≤10
    │
    ▼  per wave:
client.trigger_pipecat_v2(wave)                # one Cekura result_id per wave
client.wait_for_result(result_id, poll, timeout)
write raw JSON to evals/reports/raw/result_<id>.json
merge wave's runs into a single merged result
remember run_id → result_id (for dashboard links in the report)
    │
    ▼
render(merged, config, result_id_by_run=rid_by_run)
write evals/reports/report_<ts>.md
```

The `result_id_by_run` map matters because when a suite spans multiple waves,
each run lives under a *different* result, so the per-run dashboard link
needs the right `result_id` for the URL pattern in `config.cekura.dashboard_result_url`.

### 4. `evals/report.py` — the renderer

Pure function `render(result, config, runs_detail=None, result_id_by_run=None) -> str`.
Sections in order:

1. **Header** — result name, ID, generation timestamp, status, pass count, deploy target.
2. **Headline** — surfaces the dominant problem in plain English (latency vs STT
   vs behavior). The agent in this repo has chronic dead air, so the headline
   has flagged that across every run we've run so far.
3. **Results by scenario** — one row per run, with bucket, ✅/❌/🚫/⏳, latency,
   WER, dead-air flag, M4 Goal Proximity %, and a dashboard link.
4. **Goal Proximity (M4)** — only renders when `runs_detail` (per-run
   transcripts) is passed in and at least one scenario has a `target:` budget.
   Calls `goal_proximity.score_run(transcript_object, target)` per run.
5. **Failures by root cause** — `classify_failure(r)` buckets failures into:
   - *Infrastructure: Pipecat concurrency limit* — not an agent bug, re-queue.
   - *Did not complete* — call dropped, runtime error, etc.
   - *Dead air / latency* — Infrastructure-Issues metric tripped (silent >10s).
   - *Stuck loop / unnecessary repetition* — Unnecessary-Repetition < 3.
   - *Task outcome missed* — the catch-all behavioral failure.
   The order matters: an infra failure is reported as infra even if it would
   *also* trip behavior metrics, so a flaky cap doesn't masquerade as a
   regression.
6. **Metric family rollup** — average normalized score per family, computed
   only over runs that produced a numeric score.
7. **Next steps** — auto-suggestions based on what's tripping (latency
   recommendation when mean latency > 3s; concurrency note when any wave
   overflowed).

The report is wrapped in `<!-- CEKURA-REPORT-START -->` / `<!-- ...END -->`
markers for downstream tools (and so you can extract just the report block
when pasting into an issue).

### 5. `evals/optimize_prompt.py` — the improvement step

This is "the loop" proper. Two modes:

**Human-in-the-loop (default during dev):**

```bash
# Cekura (or you, via MCP) wrote a suggestion to a file
python -m evals.optimize_prompt --candidate-file evals/prompts/suggestion.txt
# review the printed diff, then promote:
python -m evals.optimize_prompt --candidate-file evals/prompts/suggestion.txt --apply
```

**Autonomous (CI / cron):**

```bash
# pass up to 3 failing run IDs; Cekura analyzes transcripts + metric failures
python -m evals.optimize_prompt --run-ids 3199500 3199503 3199504
# (then review the printed diff and re-run with --apply when satisfied)
```

Mechanics:

1. Read `evals/prompts/system_instruction.current.txt` — the canonical prompt
   the harness operates on.
2. Get a candidate, either from `--candidate-file` (offline) or by calling
   `CekuraClient.improve_prompt(prompt, run_ids[:3])` and then polling
   `get_improve_prompt_progress()` until it returns a payload containing one
   of `improved_prompt | suggested_prompt | new_prompt | prompt | result`.
3. Write artifacts to `evals/prompts/`:
   - `candidate_<utc_ts>.txt` — the proposed new prompt verbatim.
   - `diff_<utc_ts>.patch` — unified diff vs the current prompt.
4. Print the diff to stdout for inspection.
5. **Only if `--apply`** is passed: overwrite `system_instruction.current.txt`.

After `--apply`, the human still has to:

- Sync the change into `server/coach_prompt.py::build_system_instruction`
  (this is the file the bot actually loads; the harness file is the canonical
  copy for the loop).
- Redeploy: `uv run pcc secrets set flower-bot-secrets --file .env --skip`
  (only if `.env` changed) then `uv run pcc deploy --yes`.
- Re-run the suite (`python -m evals.run`) to verify the failures are gone
  and nothing else regressed.

This explicit gate is deliberate: prompt edits can introduce subtle
regressions (e.g. a new "never apologize" rule that breaks the safety-script
disclaimer requirement), so the loop is **not** allowed to auto-promote.

## End-to-end flow

```
1. python -m evals.run                          # full suite or a bucket
                                                # → evals/reports/report_<ts>.md
                                                # → evals/reports/raw/result_*.json

2. Read the report → identify 1–3 failing runs that share a root cause
   (use the "Failures by root cause" section, not the per-run table).

3. python -m evals.optimize_prompt --run-ids <r1> <r2> <r3>
                                                # → evals/prompts/candidate_<ts>.txt
                                                # → evals/prompts/diff_<ts>.patch

4. Inspect the diff. Sanity-check: did Cekura preserve safety rules?
   Did it keep the "one question at a time" guidance? Did it drop the
   tool-calling discipline section?

5. python -m evals.optimize_prompt --candidate-file evals/prompts/candidate_<ts>.txt --apply
                                                # → promotes to system_instruction.current.txt

6. Edit server/coach_prompt.py::build_system_instruction to match.
   (Today this is manual; a future improvement is to make coach_prompt.py
   read the canonical .txt at import time, so step 6 disappears.)

7. cd server && uv run pcc deploy --yes         # redeploy to Pipecat Cloud

8. python -m evals.run                          # verify: failures resolved?
                                                # any new regressions?
   ↻ loop back to step 2 if not yet healthy.
```

## What the loop is *not*

- **Not a runtime self-improver.** The bot in production reads a frozen
  prompt. Improvements are build-time and reviewed.
- **Not a fine-tuning loop.** Underlying model weights (Nemotron 3 Super 120B,
  or GPT-4.1 in the alternate config) are untouched. We change behavior by
  changing instructions.
- **Not a RAG-tuning loop.** The catalog/restaurant data flows through tools,
  not prompts. The loop doesn't edit those.
- **Not a latency loop.** Cekura's `improve-prompt` is for *behavior*. If the
  Infrastructure-Issues metric is failing because of dead air, the fix is in
  the bot code (batching tool calls, holding phrases, switching LLM), not in
  the prompt. Run the prompt loop only after latency is acceptable, so the
  failure signal is about *what was said*, not *how long it took to say it*.

## Files at a glance

| File                                         | Role                                                                 |
|----------------------------------------------|----------------------------------------------------------------------|
| `server/coach_prompt.py`                     | The persona — single file the loop edits.                            |
| `evals/prompts/system_instruction.current.txt` | Canonical prompt copy the optimization harness reads/writes.       |
| `evals/config.yaml`                          | Cekura IDs, scenarios, metrics, run policy, prompt source.           |
| `evals/cekura_client.py`                     | Standalone REST client (no MCP / no OAuth).                          |
| `evals/run.py`                               | Orchestrator — waves, polling, raw JSON, report.                     |
| `evals/report.py`                            | Pure-transform markdown renderer.                                    |
| `evals/goal_proximity.py`                    | M4 — deterministic macro-arithmetic scorer.                          |
| `evals/optimize_prompt.py`                   | The improvement step — `improve-prompt` + diff + gated `--apply`.    |
| `evals/reports/`                             | Rendered markdown reports + `raw/` JSON dumps.                       |
| `evals/prompts/candidate_*.txt`              | Auto-saved candidate prompts (one per `improve-prompt` invocation).  |
| `evals/prompts/diff_*.patch`                 | Unified diff of each candidate vs the then-current prompt.           |

## Open improvements (not yet implemented)

- **Auto-sync `coach_prompt.py` ←→ `system_instruction.current.txt`.** Today
  these are kept in sync by hand. Making `build_system_instruction` read the
  `.txt` at module import would collapse step 6 of the flow.
- **Suite gate on `--apply`.** Today `--apply` only promotes the file. A
  natural next step is to refuse to promote unless a clean re-run of the
  affected scenarios passes.
- **Metric-targeted improve.** `CekuraClient.improve_prompt` already accepts
  `category_ids` / `workflow_metric_ids` — wiring those through
  `optimize_prompt.py` would let the human steer Cekura at a specific failure
  family (e.g. "fix tool-calling, leave conversational quality alone").
- **Per-iteration archive.** Snapshot `system_instruction.current.txt` to
  `evals/prompts/history/<ts>.txt` on every `--apply`, so a regression can be
  rolled back with one file copy.
