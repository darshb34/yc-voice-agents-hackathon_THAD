# Field & Flower — Cekura automated testing

Automated evaluation of the Nemotron voice agent (`bot-nemotron.py`) using Cekura.
This directory holds the eval config and (Phase 3+) the pipeline that runs the suite,
collects results, and writes reports.

## How it connects

```
Cekura cloud ──(scenarios_run_pipecat_v2)──▶ Pipecat Cloud "flower-bot" ──▶ NVIDIA STT + Nemotron LLM + Gradium TTS
```

The bot is **deployed on Pipecat Cloud** (`pc deploy` from `server/`). Cekura starts a
Daily/WebRTC session against it — no tunnel, no localhost. Both sides connect outbound
to Daily, so there's no NAT/firewall problem. (A local tunnel does **not** work: Cekura
has no SmallWebRTC client and WebRTC media won't cross an HTTP tunnel.)

## What's set up (Phase 1)

- **Agent** `18027` "Field & Flower (Nemotron)" in project `5636`, wired with
  `transcript_provider=pipecat` + `pipecat_agent_name=flower-bot`.
- **12 scenarios** in folder "Phase 1 - Field and Flower suite": 6 happy-path,
  3 edge-case, 3 STT-stress. See `config.yaml` for IDs and buckets.
- **11 metrics** across 4 families (task success, tool-calling, conversational
  quality, STT accuracy) + latency/pitch. Auto-attached to every scenario.

All IDs live in [`config.yaml`](./config.yaml).

## Re-deploying the bot

From `server/` after any bot change:

```bash
uv run pcc secrets set flower-bot-secrets --file .env --skip   # if .env changed
uv run pcc deploy --yes
```

## The pipeline (Phase 3)

Three modules in this directory:

- **`cekura_client.py`** — thin REST client (needs `CEKURA_API_KEY`). Standalone, so
  the pipeline runs from a terminal/cron without Claude or the MCP (and dodges the
  OAuth token expiry).
- **`report.py`** — pure transform: result JSON → grouped markdown report (pass
  rates, per-family rollup, failures grouped by root cause, STT/WER, latency,
  dashboard links). No network; testable on saved JSON.
- **`run.py`** — orchestrator: select scenarios → submit in **waves of `concurrency`**
  (≤10, the Pipecat cap) → poll each wave → merge → write one report.

### Setup

```bash
pip install -r evals/requirements.txt
export CEKURA_API_KEY=...   # dashboard.cekura.ai → Settings → API Keys (org-scoped)
```

### Run

```bash
# from repo root
python -m evals.run                       # full 12-scenario suite (2 waves: 10 + 2)
python -m evals.run --bucket stt_stress   # just the STT-stress scenarios
python -m evals.run --scenarios 272706 272716
python -m evals.report --result-id 591149 # re-render a report for an existing result
python -m evals.report --result-json evals/reports/raw/result_591149.json  # from saved JSON
```

Reports land in `evals/reports/<ts>.md`; raw result JSON in `evals/reports/raw/`.
Each scenario is a full voice call (~3–5 min today due to Nemotron latency), so a
full run burns credits + time — run deliberately.

## Known issue surfaced in Phase 0

The bot has severe inter-turn latency / dead air (10–25s gaps; simulated caller asked
"are you still there?" 5×). The **Infrastructure Issues** metric (fails if the agent is
silent >10s) will flag this across the suite. Likely cause: sequential Nemotron-120B
tool-call round-trips + step-by-step narration. This is the prime target for Phase 4
(prompt optimization).
