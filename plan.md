# Tetris Nutrition Voice Coach — YC Voice Agents Hackathon

## Context

We're competing in the **Voice Agents Hackathon** (Cekura + Daily, w/ NVIDIA + AWS; one day, submissions 6:00 PM, winner 8:30 PM). The prize that matters: a **guaranteed YC interview**, plus NVIDIA/AWS judges' prizes.

The judges' explicit framing — *"We aren't just looking for the best-sounding voice; we are looking for the best **system**."* They want a **continuous feedback loop** across four themes:

1. **Build & Customize** — leverage NVIDIA-accelerated SOTA **open-weights** models (Nemotron) on AWS.
2. **Deploy at Scale** — Pipecat infra + Twilio telephony, network/latency optimization.
3. **Simulate & Evaluate** — Cekura automated testing, "move beyond vibes."
4. **Auto-Improve** — **evaluation data flows back into the agent** to improve it over time.

Our entry: a **Tetris Nutrition voice coach** built on the repo's Nemotron starter, wrapped in an **auto-improvement harness** that turns Cekura eval failures into agent fixes and re-tests — the literal thing the judges ask for. Tetris's product hook ("a meal plan that *adapts when life happens*") maps perfectly onto a voice agent that re-optimizes macros live when a member skips/swaps a meal, and onto a system that auto-adapts to its own eval results.

**Locked-in decisions (from Darshan):** Full coach (intake + daily recommend + adapt) · Mock data but architected for a real Tetris API · **Nemotron-only** stack · Full deploy incl. Twilio phone. Team: Darshan, Harshit (@deepmind11), Albert (@albertnew2012).

---

## Winning thesis → what we build for each theme

| Theme | Our concrete deliverable |
|---|---|
| Build & Customize | End-to-end NVIDIA stack: Nemotron Speech Streaming STT + Nemotron-3-Super-120B LLM. Custom `VLLMOpenAILLMService` TTFB instrumentation already measures time-to-first-*spoken*-word ([nemotron_llm.py](server/nemotron_llm.py)). |
| Deploy at Scale | Pipecat Cloud + Twilio phone number; report real Nemotron latency (TTFB/turn metrics); Krisp telephony filter already wired ([pcc-deploy.toml](server/pcc-deploy.toml)). |
| Simulate & Evaluate | Cekura suite of 10–20 evaluators tuned to a nutrition coach (macro-math correctness, deviation handling, allergy safety, medical disclaimer, voice-UX). |
| **Auto-Improve (centerpiece)** | A harness that runs Cekura → parses failing transcripts → proposes & applies a `coach_prompt.py` edit → re-runs → keeps the change only if pass-rate improves. Demo the **before/after pass-rate delta**. |

---

## Part 0 — Run the starter bot first (README walkthrough)

Goal: prove the provided Nemotron bot runs locally before we modify anything.

1. `cd server && cp .env.example .env`, fill `GRADIUM_API_KEY` (free signup at gradium.ai; event credits). Add the README V2 Nemotron endpoints (currently blank in `.env.example`):
   - `NVIDIA_ASR_URL=ws://44.241.251.184:8080`
   - `NEMOTRON_LLM_URL=http://nemotron-fleet-alb-1322439314.us-west-2.elb.amazonaws.com/v1`
   - `NEMOTRON_LLM_MODEL=nvidia/nemotron-3-super`
2. **Connectivity pre-check** (these endpoints are NVIDIA's event-hosted fleet — may only be live during the hackathon):
   - `curl -s $NEMOTRON_LLM_URL/models | python -m json.tool` → expect a `nvidia/nemotron-3-super` id.
   - one-shot `POST /v1/chat/completions` smoke test (8 tokens).
   - ASR WebSocket reachability (`wscat -c ws://44.241.251.184:8080`, look for `{"type":"ready"}`).
3. `uv sync` then `ENV=local uv run bot-nemotron.py` → open http://localhost:7860 → Connect.
4. **GPT fallback** if the Nemotron fleet is unreachable now: `ENV=local uv run bot-gpt.py` (needs `OPENAI_API_KEY` + `GRADIUM_API_KEY`). Same flower-shop logic; just confirms the pipeline runs.

---

## Part 1 — Build the Tetris Nutrition Coach

### File strategy — extract a shared core (avoid prompt drift)
The GPT and Nemotron bots are byte-identical except imports + the two STT/LLM constructors. We refactor the shared body into modules so the prompt/tools live **once** (critical: the auto-improve loop will rewrite the prompt repeatedly — it must edit one file):

- **`server/coach_core.py`** (new) — `run_bot()`, tool closures, per-call state, `bot()` entry + transport branches, `get_call_info()`. Model-agnostic: takes a `build_services(system_instruction) -> (stt, llm, tts)` callback. Distilled from [bot-nemotron.py](server/bot-nemotron.py) `run_bot`/`bot`.
- **`server/coach_prompt.py`** (new) — `build_system_instruction(caller_context)` + `build_caller_context(member)`. **Single source of truth for the persona** (this is what the auto-improve harness edits).
- **`server/nutrition_backend.py`** (new, replaces [mock_backend.py](server/mock_backend.py)) — `MEALS`, `KNOWN_MEMBERS`, and a data-access layer (`get_meals`, `get_meal`, `find_meals`, `calc_targets`) — the **swap-to-real-API boundary**.
- **`server/bot.py`** (new — **Dockerfile target**) — ~30-line Nemotron shim: defines `build_services()` (NVidia STT + `VLLMOpenAILLMService` + Gradium TTS) and exposes `bot`. Mirrors [bot-nemotron.py:354-397](server/bot-nemotron.py#L354-L397).
- **`server/bot-gpt.py`** (edit) — becomes a thin GPT dev-fallback shim reusing the same three modules (Gradium STT + `OpenAIResponsesLLMService`). Lets us A/B Nemotron vs GPT in Cekura with one-file swap.

> Lower-risk alternative if time is tight: copy `bot-nemotron.py` → `bot.py`, edit in place, accept GPT/Nemotron prompt drift. Recommended path is the shared module because the auto-improve loop depends on one editable prompt file.

### Conversation state (replaces `order = {...}`)
Per-call dict: `profile` (goal cut/maintain/bulk · diet_style · allergies · restrictions · activity_level · optional biometrics · derived `targets`) + `log.meals[]` + `member_id`. A pure `remaining_macros(state)` helper = targets − sum(logged). Remaining is allowed to go **negative** (over-ate) — surfacing that honestly is the "adapts when life happens" moment.

### Tool functions (replace the 7 flower tools; register via `llm.register_direct_function` + `ToolsSchema`)
- **Intake (granular setters — one field per turn):** `set_goal`, `set_diet_style`, `set_restrictions` (allergies+restrictions, safety-critical), `set_activity_level`, `set_biometrics` (optional), then `compute_targets` → reads targets back for confirmation. Each setter returns `missing[]` so the model knows when intake is done.
- **Daily recommend:** `recommend_meals(slot?, craving?, source_type?, max_results≤5)` → ranked meals fitting remaining macros + diet/allergy filters; each result leads with name + macros + where-to-find.
- **Adapt:** `log_meal(name|macros, slot?)` (write; recomputes remaining) → `reoptimize_day(note?)` (re-plan remaining meals to close the gap; over-budget-aware).
- **Cross-phase:** `get_summary()` (targets/logged/remaining) · `end_call()` — **copy verbatim** from [bot-nemotron.py:271-281](server/bot-nemotron.py#L271-L281) (EndTaskFrame UPSTREAM + `run_llm=False`).

All tools touch data only through `find_meals` / `get_meal` / `calc_targets` → real Tetris API later = one-file change.

### Mock backend (`nutrition_backend.py`)
`MEALS` (~12–16 entries spanning all diets + all 3 source types {restaurant|recipe|ready-to-eat} + slots): each = `macros{calories,protein_g,carbs_g,fat_g}`, `source`, `where_to_find`, `tags`, `diets[]` (compat), `contains[]` (allergen screen). `KNOWN_MEMBERS` (phone → existing profile + today's remaining macros) replaces `KNOWN_CUSTOMERS` for the returning-caller-over-phone demo. `calc_targets` = Mifflin–St Jeor (activity factors 1.2–1.9) × goal adjust (cut −15% / maintain 0 / bulk +12%), else goal/activity heuristic; protein/fat/carb split. Keep `_fit_score`/`calc_targets` **pure & deterministic** so Cekura's macro-math evaluator is reproducible.

### System prompt rewrite (`coach_prompt.py`)
Preserve the starter's voice-UX rules (1–2 sentences/turn · ONE question at a time · no filler · spoken-aloud, numbers in words, no markdown/emoji · ≤5 named options · say goodbye before `end_call`). Add: conversational intake flow, name-led meal options with where-to-find, the `log_meal`→`reoptimize_day` deviation loop ("no problem — that burger put you ~300 over on fat, let's keep dinner lean"), and a **safety block** (not a doctor; medical/pregnancy/ED → general guidance + see-a-doctor/RD disclaimer; never violate stated allergies). New `on_client_connected` greeting + new-vs-returning `caller_context`.

### Config + deploy edits
- **[.env.example](server/.env.example):** add `NVIDIA_ASR_URL`, `NEMOTRON_LLM_URL`, `NEMOTRON_LLM_MODEL`, `NEMOTRON_LLM_API_KEY=EMPTY`, `NEMOTRON_ENABLE_THINKING=false`.
- **[pcc-deploy.toml](server/pcc-deploy.toml):** `agent_name = "tetris-coach"`, `secret_set = "tetris-coach-secrets"`.
- **[Dockerfile](server/Dockerfile) — critical fix:** current COPY references a non-existent `bot.py` and **omits `nemotron_llm.py` + `nvidia_stt.py`**, so a Nemotron deploy is impossible today. Replace lines 19–20 to COPY `bot.py`, `coach_core.py`, `coach_prompt.py`, `nutrition_backend.py`, **`nemotron_llm.py`, `nvidia_stt.py`**.

---

## Part 2 — Cekura evaluation suite

Install plugin (README): `/plugin marketplace add cekura-ai/cekura-skills` → `/plugin install cekura@cekura-skills`. Connect agent with **provider = Pipecat**. Run `/cekura-report`.

Evaluators (10–20) tuned to a nutrition coach:
- **Macro-math correctness** (headline) — scripted persona eats meals with known macros; assert spoken "remaining" = targets − logged (deterministic).
- **Deviation handling** — "I skipped lunch / ate a 900-cal burrito" → `log_meal`+`reoptimize_day` fired, plan changed.
- **Allergy safety** (hard-fail) — shellfish/peanut persona, never suggest an allergen meal.
- **Diet adherence** — vegan persona, no animal products suggested.
- **Intake completeness + one-question-per-turn.**
- **Medical-disclaimer / safety** — pregnancy/ED persona → disclaimer present, no extreme-restriction advice.
- **Voice-UX** — ≤2 sentences, numbers in words, ≤5 options, no markdown.
- **Graceful close** — goodbye before `end_call`.

---

## Part 3 — Auto-Improvement harness (the centerpiece that wins)

This is the theme the judges weight most. Build a closed loop that makes eval data improve the agent:

1. **Run** Cekura via `/cekura-report` (or its MCP/API) → collect per-evaluator pass/fail + failing transcripts.
2. **Diagnose** — an agent reads failing transcripts, clusters root causes (e.g. "suggested shellfish to an allergic persona", "math off by a logged snack"), and proposes a **targeted edit** to `coach_prompt.py` (or a `find_meals`/`calc_targets` fix for logic failures).
3. **Apply** the smallest change on a branch/worktree.
4. **Re-evaluate** — re-run the same Cekura suite.
5. **Gate** — keep the change only if overall pass-rate (or the targeted evaluator) improves; else revert. Log every iteration's score.

Implementation: orchestrate with a **Workflow** (fan-out evaluators → diagnose → patch → re-eval → gate) and/or the `/loop` skill to run rounds until pass-rate plateaus. Persist an `improvement_log.md` (round, failing evaluators, hypothesis, diff, before/after score). **Demo artifact = the pass-rate climb chart + the auto-generated diffs.** Lead the judge pitch with: *"our deviation-handling evaluator went 4/10 → 9/10 across three automated rounds, no human edits."*

---

## Part 4 — Deploy: Pipecat Cloud + Twilio (full, incl. phone)

1. `uv tool install pipecat-ai-cli && pc cloud auth login`; `pc cloud organizations list` (get ORG).
2. `pc cloud secrets set tetris-coach-secrets --file .env` (Nemotron URLs + Gradium + Twilio filled).
3. `pc cloud deploy` (uses fixed Dockerfile + `tetris-coach` pcc-deploy.toml).
4. Twilio: add credits (twil.io/yc-hack) → buy a voice number → TwiML Bin with `_pipecatCloudServiceHost = tetris-coach.YOUR_ORG_NAME` → attach to the number's Voice config.
5. Add a teammate's phone to `KNOWN_MEMBERS` to demo the returning-member-skips-intake path **over a live phone call**.

---

## Critical files
- **New:** [coach_core.py](server/coach_core.py), [coach_prompt.py](server/coach_prompt.py), [nutrition_backend.py](server/nutrition_backend.py), [bot.py](server/bot.py)
- **Edit:** [Dockerfile](server/Dockerfile) (COPY fix — add nemotron_llm.py/nvidia_stt.py), [pcc-deploy.toml](server/pcc-deploy.toml), [.env.example](server/.env.example), [bot-gpt.py](server/bot-gpt.py) (→ thin shim)
- **Reuse unchanged:** [nemotron_llm.py](server/nemotron_llm.py), [nvidia_stt.py](server/nvidia_stt.py), [pyproject.toml](server/pyproject.toml)
- **New (harness):** `server/improvement_log.md` + Workflow/loop script

## Verification (end-to-end)
1. Local Nemotron over WebRTC (localhost:7860): walk 5 voice scripts — intake→targets read-back; "what's for lunch?" (≤5 named options); "I had a burger out" (log+reoptimize); shellfish allergy → no salmon; "I'm diabetic" → disclaimer.
2. `/cekura-report` baseline → record pass-rates.
3. Run auto-improve harness ≥2 rounds → confirm pass-rate improves, `improvement_log.md` populated.
4. `pc cloud deploy` → dial the Twilio number → returning-member phone demo works.
5. GPT fallback (`uv run bot-gpt.py`) verified as a safety net if NVIDIA fleet drops.

## Team split (Darshan / Harshit / Albert)
- **Darshan** — coach_core + prompt + state/tools; owns the demo narrative.
- **Harshit** — Cekura suite + the auto-improve harness (Workflow/loop) — the winning component.
- **Albert** — nutrition_backend (MEALS/calc_targets/find_meals) + Pipecat Cloud/Twilio deploy + latency metrics.

## Risks & fallbacks
- **NVIDIA fleet unreachable while building** → develop on `bot-gpt.py` (same modules), flip to Nemotron once live. Connectivity pre-check in Part 0 catches this early.
- **Nemotron reasoning leaking into speech** → keep `NEMOTRON_ENABLE_THINKING=false` (TTFB shim handles the metric).
- **Twilio paid setup** → needs event credits/upgrade; do last, after local + Cekura + auto-improve are demo-ready.
