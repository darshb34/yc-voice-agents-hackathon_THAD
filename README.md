# Tetris Nutrition Coach — a voice dietitian that adapts when life happens

**A YC Voice Agents Hackathon submission** — hosted by [Cekura](https://cekura.com) and [Daily](https://daily.co), with [NVIDIA](https://nvidia.com), [AWS](https://aws.amazon.com), and [Twilio](https://twilio.com).

GITHUB: https://github.com/darshb34/yc-voice-agents-hackathon_THAD
Branch : cekura-eval-pipeline

> *"Hey, what should I eat for lunch — I'm trying to lose weight but I'm in the Mission and only have twenty minutes."*

The Tetris Nutrition Coach is a **voice-first dietitian** you can phone. It takes you through a brief intake (goal, diet, allergies, activity), computes your daily macro targets, and then — for the rest of the day — recommends meals you can actually get (real nearby restaurants via Google Places, recipes, or grocery-store ready-to-eat options) that fit the macros you have left. When you eat off-plan, it **re-optimizes the remaining meals to close the gap** instead of scolding you.

It's the agent we wished existed: a real coach on the other end of a phone call, not another macro-counting app you forget to open.

---

## Table of contents

- [Why this exists](#why-this-exists)
- [What it does in 30 seconds](#what-it-does-in-30-seconds)
- [The auto-improvement loop — the heart of this project](#the-auto-improvement-loop--the-heart-of-this-project)
- [Tech stack](#tech-stack)
- [Repo map](#repo-map)
- [Run it locally](#run-it-locally)
- [Deploy to Pipecat Cloud + Twilio (phone)](#deploy-to-pipecat-cloud--twilio-phone)
- [Re-running the Cekura suite yourself](#re-running-the-cekura-suite-yourself)
- [Team & credits](#team--credits)

---

## Why this exists

Most nutrition apps fail at the same place: the moment you *deviate*. You skipped breakfast, you ate the office cookies, you went out for a burger — and the app's plan is suddenly useless because it was built on the assumption you'd follow it. So you stop opening the app.

A good human coach doesn't do that. A good coach takes the deviation as the new starting point and quietly re-plans the rest of the day so you can still hit your goal. That requires three things, and **all three are easier over voice than in a UI**:

1. **Fast intake** — goal, diet style, allergies, activity. One question at a time. Two minutes.
2. **Local, concrete recommendations** — not "eat 40g of protein," but *"the Cluck Sandwich at Souvla on Divisadero — forty-two grams of protein, five hundred forty calories."* Real places. Real macros. Real distances.
3. **Adaptive replanning** — *"No problem — that burger put you about three hundred over on fat, so let's keep dinner lean."* Logged, re-optimized, moving on.

We built the Tetris Nutrition Coach as the voice-first version of that human coach. You phone it the way you'd phone a friend who happens to be a dietitian.

## What it does in 30 seconds

```
☎  Member calls in.
🟢 Returning member? Profile loads from caller ID — skip the intake.
🆕 New member?    Three short questions → daily macro targets read back.

💬 "What should I eat for lunch?"
   →  recommend_meals(slot="lunch", remaining_macros=…)
   →  ranked options from MEALS catalog + (if asked) live Google Places nearby
   →  Spoken aloud, one meal at a time, with macros and where to find it.

💬 "I had a burger at lunch."
   →  log_meal(name="cheeseburger")  →  reoptimize_day()
   →  "Cool — that put you about three hundred over on fat. Let's keep
       dinner lean: try the grilled salmon bowl at Sweetgreen, twelve
       grams of fat, four hundred eighty calories."

📞 Goodbye → end_call().  Member's day-log persists for tomorrow.
```

The whole call runs over the phone (Twilio → Pipecat Cloud → NVIDIA Nemotron) with **sub-second STT** and a real conversational feel. Source: `@/Users/hgz/Projects/yc-voice-agents-hackathon_THAD/server/coach_core.py` (the pipeline + tool closures) and `@/Users/hgz/Projects/yc-voice-agents-hackathon_THAD/server/coach_prompt.py` (the persona).

---

## The auto-improvement loop — the heart of this project

> If you only read one section, read this one. This is the part we built specifically for the *"continuous feedback loop"* theme the hackathon judges weight most.

### The problem voice agents have

The reason voice agents demo well and ship badly is that **you cannot eyeball voice quality the way you can eyeball a webpage**. A prompt that looks fine to a human reading it on a laptop can produce an agent that stacks three questions in one breath, narrates every tool call until the caller asks *"are you still there?"*, lists ten meals when it should list four, or confidently suggests salmon to someone who told it they were allergic to fish thirty seconds earlier.

You can't catch those by hand. You need a test suite that **places real phone calls**, a way to score what came back, and a way to feed the failures *back into the agent*. That is exactly what Cekura provides.

### How we used Cekura

We used **two surfaces of Cekura together** during the hackathon, and they were each critical for different reasons:

1. **The Cekura Claude Code plugin** (`/plugin install cekura@cekura-skills`) — the MCP server + slash-command skills that let us drive Cekura from inside Claude Code. We used it for the **setup phase**: creating the project, the agent, defining scenarios (`A1 New member intake → first meal rec`, `C1 Allergy safety — shellfish/fish, asks for seafood anyway`, `D1 Signature move — log off-plan meal and adapt the day`, …), attaching the eleven project-level evaluators, and kicking off the first runs interactively while we were still iterating on what to even test.

2. **Cekura Cloud + their REST API** — for the **operational phase**, once we knew what we were running. We wrote a small standalone REST client (`@/Users/hgz/Projects/yc-voice-agents-hackathon_THAD/evals/cekura_client.py`) so the eval pipeline can run from a terminal or CI without needing Claude Code open — no OAuth tokens to refresh, no plugin to keep alive. This is what the auto-improvement harness in `@/Users/hgz/Projects/yc-voice-agents-hackathon_THAD/evals/` calls into.

The MCP was the right tool for *bootstrapping*; the API was the right tool for *iterating*. Both came from Cekura, both were essential.

### The loop, in plain English

```
  ┌─────────────────────────────────────────────────────────────────────┐
  │                                                                     │
  │   1. Run the suite.                                                 │
  │      Cekura phones the bot with simulated callers across 12+        │
  │      scenarios (onboarding, recommendations, allergy-safety,        │
  │      off-plan adaptation, eating-out, returning-member, end-of-day  │
  │      summaries). Real audio, real LLM, real tool calls.             │
  │                                                                     │
  │   2. Score every call.                                              │
  │      Cekura's evaluators rate each call across 11 metrics —         │
  │      Expected Outcome, Tool Call Success, Talk Ratio, Latency,      │
  │      Transcription Accuracy, Unnecessary Repetition, Infrastructure │
  │      Issues, plus three custom metrics we added for                 │
  │      recall/adaptation. We also added a deterministic 12th metric,  │
  │      "Goal Proximity" — see below.                                  │
  │                                                                     │
  │   3. Read the failures.                                             │
  │      Our report.py groups failures by ROOT CAUSE, not by symptom:   │
  │      "Dead air >10s", "Stuck loop / unnecessary repetition",        │
  │      "Tool call missed", "Task outcome missed". That tells us       │
  │      what to actually fix, not just what failed.                    │
  │                                                                     │
  │   4. Ask Cekura to improve the prompt.                              │
  │      The improvement step (evals/optimize_prompt.py) takes:         │
  │           - the current system prompt                               │
  │           - up to 3 failing run IDs                                 │
  │      …and calls Cekura's improve-prompt engine. Cekura reads the    │
  │      transcripts + metric failures and returns a REVISED PROMPT.    │
  │      We save the candidate + a unified diff for review.             │
  │                                                                     │
  │   5. Promote with `--apply`.                                        │
  │      One command writes the revision to the canonical prompt file   │
  │      (and only that file — the persona is engineered to live in     │
  │      one place so diffs stay auditable).                            │
  │                                                                     │
  │   6. Redeploy and re-run.                                           │
  │      `pcc deploy --yes` → Pipecat Cloud picks up the new prompt →   │
  │      step 1 again. Stop when the suite is green.                    │
  │                                                                     │
  └─────────────────────────────────────────────────────────────────────┘
```

The harness is all in `@/Users/hgz/Projects/yc-voice-agents-hackathon_THAD/evals` — about 600 lines of Python, no magic. The full design document is at [`docs/SELF_IMPROVEMENT_LOOP.md`](docs/SELF_IMPROVEMENT_LOOP.md).

### Concrete fixes Cekura's `improve-prompt` produced for us

Reading our baseline Cekura report ([`evals/reports/baseline_591149.md`](evals/reports/baseline_591149.md)) and then looking at the current production prompt (`@/Users/hgz/Projects/yc-voice-agents-hackathon_THAD/server/coach_prompt.py`), the loop directly produced these rules — each one traces back to a specific failure family the evaluators flagged:

| What Cekura flagged | What `improve-prompt` added to the prompt |
|---|---|
| `Tool Call Success` low — agent *announced* setting up the account but never called `create_account`. | "CRITICAL — actually call the tools, don't just talk about them … Saying it without calling the tool means it did NOT happen." |
| `Talk Ratio` skewed, `Unnecessary Repetition` failures — agent monologued for 4+ sentences per turn and restated the caller. | "Keep it to 1–2 short sentences per turn… Don't restate what the member just said back to them." |
| `Expected Outcome` failures during intake — agent stacked goal+diet+allergies into one question. | "Ask ONE thing at a time. During intake, get the goal, wait, then diet, wait, then allergies — never stack questions." |
| `Transcription Accuracy` / TTS clarity — agent said `"42g"` and `"540"` out loud, which TTS pronounced as `"forty-two g"` and `"five-four-zero"`. | "responses are spoken aloud, so read every number in words ('forty-two grams of protein', 'five hundred forty calories' — never '42g' or '540')." |
| `C1`/`C2` safety scenarios — agent treated medical/eating-disorder questions as normal coaching questions. | The entire `SAFETY — non-negotiable` block: never violate stated allergies, never prescribe aggressive cuts, defer to RD/MD for medical questions. |

Every one of those rules is a Cekura insight that landed in the agent. We did not write them by hand.

### One thing the loop *can't* fix — and what we did about it

The headline finding from our baseline run was **6.3-second per-turn latency** with **9 out of 9 runs tripping dead-air**. That is not a prompt problem — that is the Nemotron-120B sequential tool-call round-trips. No amount of prompt-engineering would have fixed it.

So we kept that as a separate, code-level loop (batching tool calls, holding phrases, instrumenting TTFB inside `@/Users/hgz/Projects/yc-voice-agents-hackathon_THAD/server/nemotron_llm.py`), and we only ran the prompt-improvement loop *after* latency was acceptable. The split — *behavior is a prompt problem, latency is a code problem* — is documented in [`docs/SELF_IMPROVEMENT_LOOP.md`](docs/SELF_IMPROVEMENT_LOOP.md) and is a real lesson from this hackathon.

### What's deterministic, what's LLM-judged

One subtle thing we're proud of: for the "did the recommended meal actually fit your remaining macros?" check (M4, *Goal Proximity*), we did **not** use an LLM judge. LLM judges are unreliable at arithmetic. Instead `@/Users/hgz/Projects/yc-voice-agents-hackathon_THAD/evals/goal_proximity.py` parses the macros the agent actually *spoke aloud* (handling `"five hundred forty calories"` etc.) and computes the closeness to the seeded remaining-budget in pure Python. Same input, same output, every time. The rest of the metrics — judgment calls like "was this acknowledged?" or "did the agent re-onboard a returning member?" — stay with Cekura's LLM evaluators where they belong.

### Results

Baseline (uncommitted starter prompt, Nemotron, full 12-scenario suite):

- **0 / 9 scored runs passed.**
- **Mean per-turn latency: 6.3 s.** 9/9 runs flagged for `>10s dead air`.
- `task_success` avg **0.28**, `conversational_quality` avg **2.56**, `tool_calling` avg **1.00 / 5**.
- Full breakdown: [`evals/reports/baseline_591149.md`](evals/reports/baseline_591149.md).

After running the behavioral loop (Cekura `improve-prompt` × N iterations) **and** the latency code-fixes:

- Prompt now contains the five rule-families above — all Cekura-derived, all directly traceable to baseline failures.
- *(Final post-fix pass-rate from the demo run will be filled in here at submission time — see the most recent file under `evals/reports/`.)*

---

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Orchestration | [Pipecat](https://pipecat.ai) | The hackathon framework; clean pipeline + transport abstractions. |
| Speech-to-text | [Nemotron Speech Streaming 0.6B](https://huggingface.co/nvidia/nemotron-speech-streaming-en-0.6b) (NVIDIA, hosted on AWS) | Open-weights, low-latency streaming. |
| LLM | [Nemotron 3 Super 120B](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-BF16) (NVIDIA, hosted on AWS) | Open-weights flagship; we instrumented per-turn TTFB ourselves in `@/Users/hgz/Projects/yc-voice-agents-hackathon_THAD/server/nemotron_llm.py`. GPT-4.1 alternate is wired in `@/Users/hgz/Projects/yc-voice-agents-hackathon_THAD/server/bot-gpt.py` for dev fallback. |
| Text-to-speech | [Gradium](https://gradium.ai) | Natural-sounding voice; fast first-byte. |
| Telephony | [Twilio](https://twilio.com) + [Pipecat Cloud](https://pipecat.daily.co) | Phone-number → TwiML Bin → WebSocket → Pipecat agent. |
| Local dev transport | SmallWebRTC | Browser → `localhost:7860`. No tunnels needed. |
| Restaurant search | [Google Places API](https://developers.google.com/maps/documentation/places) (with OpenStreetMap Nominatim fallback) | Real nearby restaurants, not a fake catalog. |
| Evaluation | [Cekura](https://cekura.com) — both their **Claude Code plugin** (MCP + skills) and their **REST API** | The setup-phase + ops-phase split described above. |

---

## Repo map

```
.
├── server/                          # the voice agent
│   ├── bot.py                       # Nemotron entry — Pipecat Cloud Dockerfile target
│   ├── bot-gpt.py                   # GPT-4.1 entry — dev fallback when NVIDIA fleet is down
│   ├── coach_core.py                # model-agnostic pipeline, tools, per-call state
│   ├── coach_prompt.py              # ← the single file Cekura's improve-prompt edits
│   ├── nutrition_backend.py         # MEALS catalog, calc_targets, find_meals, Google Places
│   ├── member_store.py              # per-member persistence (so a returning caller is "known")
│   ├── nemotron_llm.py              # custom VLLMOpenAILLMService with TTFB instrumentation
│   ├── nvidia_stt.py                # Nemotron streaming STT integration
│   └── pcc-deploy.toml              # Pipecat Cloud deployment config
│
├── evals/                           # ← the auto-improvement loop lives here
│   ├── config.yaml                  # Cekura IDs, scenarios, metrics, run policy
│   ├── cekura_client.py             # standalone REST client (no MCP / no OAuth)
│   ├── run.py                       # orchestrator — submits in waves, polls, dumps JSON
│   ├── report.py                    # pure-transform JSON → markdown report
│   ├── goal_proximity.py            # deterministic macro-arithmetic scorer (M4)
│   ├── optimize_prompt.py           # the improvement step — improve-prompt + diff + --apply
│   ├── prompts/                     # canonical prompt + auto-saved candidates + diffs
│   └── reports/                     # rendered markdown + raw result JSON
│
└── docs/
    ├── SELF_IMPROVEMENT_LOOP.md     # full design doc for the loop
    └── PERSISTENCE_CONTRACT.md      # how returning-member state is seeded for evals
```

Two files do the lion's share of the work:

- `@/Users/hgz/Projects/yc-voice-agents-hackathon_THAD/server/coach_prompt.py` — the persona. The loop is built so this is the *only* file that ever changes per iteration.
- `@/Users/hgz/Projects/yc-voice-agents-hackathon_THAD/evals/optimize_prompt.py` — the improvement step. Two commands (`--run-ids` then `--apply`) and you've shipped a measured behavioral fix.

---

## Run it locally

The bot runs over WebRTC in your browser at `localhost:7860` — no tunnel, no phone number, no cloud account needed for the inner loop.

### Prerequisites

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) package manager
- A [Gradium](https://gradium.ai) API key (TTS)
- Either an [OpenAI](https://platform.openai.com) key (GPT-4.1 path — easiest for local dev) **or** access to NVIDIA's Nemotron fleet (the production path)

### Setup

```bash
git clone git@github.com:darshb34/yc-voice-agents-hackathon_THAD.git
cd yc-voice-agents-hackathon_THAD/server
cp .env.example .env
# Fill in GRADIUM_API_KEY and one of:
#   - OPENAI_API_KEY                            (for bot-gpt.py)
#   - NVIDIA_ASR_URL + NEMOTRON_LLM_URL + NEMOTRON_LLM_MODEL  (for bot.py)
# Optional: GOOGLE_PLACES_API_KEY for richer nearby-restaurant search.
uv sync
```

### Run

```bash
# Production path (NVIDIA Nemotron LLM + Nemotron Speech Streaming STT):
uv run bot.py

# Dev fallback (GPT-4.1 + Gradium STT) — same coach, same prompt, easier to run:
uv run bot-gpt.py
```

Open `http://localhost:7860`, click **Connect**, and talk to the coach. First launch takes ~20 s while Pipecat downloads the VAD and turn-detection models. The persona, tools, and state machine are identical between the two entry points — they differ only in which AI services they construct (see `@/Users/hgz/Projects/yc-voice-agents-hackathon_THAD/server/coach_core.py`).

---

## Deploy to Pipecat Cloud + Twilio (phone)

The judge demo is a real phone call. Here's the full path.

### 1. Install the Pipecat CLI and log in

```bash
uv tool install pipecat-ai-cli
pc cloud auth login
```

### 2. Upload secrets

From `server/`:

```bash
pc cloud secrets set flower-bot-secrets --file .env
```

(The secret-set name is `flower-bot-secrets` for legacy reasons — the deployed agent is the coach. See `@/Users/hgz/Projects/yc-voice-agents-hackathon_THAD/server/pcc-deploy.toml`.)

### 3. Deploy the agent

```bash
pc cloud deploy
```

### 4. Wire up a Twilio phone number

1. [Add credits / upgrade your Twilio account](https://twil.io/yc-hack) and [buy a voice-capable phone number](https://help.twilio.com/articles/223135247).
2. Get your Pipecat Cloud org name: `pc cloud organizations list`.
3. [Create a TwiML Bin](https://www.twilio.com/docs/serverless/twiml-bins/getting-started#create-a-new-twiml-bin) pointed at the deployed agent:

   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <Response>
     <Connect>
       <Stream url="wss://api.pipecat.daily.co/ws/twilio">
         <Parameter name="_pipecatCloudServiceHost"
           value="flower-bot.YOUR_ORG_NAME"/>
       </Stream>
     </Connect>
   </Response>
   ```

   (Replace `YOUR_ORG_NAME` with the value from step 2.)

4. [Attach the TwiML Bin](https://www.twilio.com/docs/serverless/twiml-bins/getting-started#wire-your-twiml-bin-up-to-an-incoming-phone-call) to your number's **Voice Configuration**.

### 5. Call the coach

Dial your Twilio number. If you've added your phone to `KNOWN_MEMBERS` in `@/Users/hgz/Projects/yc-voice-agents-hackathon_THAD/server/nutrition_backend.py`, you'll get the returning-member experience (no re-intake). Otherwise you'll go through the live intake flow.

---

## Re-running the Cekura suite yourself

The full eval pipeline runs from one command — no Claude Code required, just an org-scoped Cekura API key.

```bash
pip install -r evals/requirements.txt
export CEKURA_API_KEY=...    # dashboard.cekura.ai → Settings → API Keys
```

```bash
# Full 12-scenario suite (waves of ≤10 sessions to respect Pipecat's concurrency cap):
python -m evals.run

# Just one family of scenarios:
python -m evals.run --bucket safety
python -m evals.run --bucket onboarding

# Re-render a report from a saved result without burning more credits:
python -m evals.report --result-json evals/reports/raw/result_591149.json
```

To then push a Cekura-suggested improvement through the loop:

```bash
# 1) Identify up to 3 failing run IDs from the report.
# 2) Ask Cekura to suggest a revised prompt:
python -m evals.optimize_prompt --run-ids 3199500 3199503 3199504
#    → writes evals/prompts/candidate_<ts>.txt and diff_<ts>.patch

# 3) Eyeball the diff. If it looks good, promote it:
python -m evals.optimize_prompt --candidate-file evals/prompts/candidate_<ts>.txt --apply

# 4) Sync the change into server/coach_prompt.py, then:
cd server && pc cloud deploy

# 5) Re-run the suite to verify the failures are gone:
python -m evals.run
```

The full design and rationale for each piece is in [`docs/SELF_IMPROVEMENT_LOOP.md`](docs/SELF_IMPROVEMENT_LOOP.md).

If you'd rather drive Cekura interactively (the way we did for initial setup), install their Claude Code plugin:

```bash
/plugin marketplace add cekura-ai/cekura-skills
/plugin install cekura@cekura-skills
```

Then `/cekura-report` runs a one-shot end-to-end test from inside Claude Code. See [docs.cekura.ai → Claude Code guide](https://docs.cekura.ai/mcp/claude-code-guide).

---

## Team & credits

**Team:** Darshan, Harshit ([@deepmind11](https://github.com/deepmind11)), Albert ([@albertnew2012](https://github.com/albertnew2012)).

**Built on top of:** [Pipecat](https://pipecat.ai) (orchestration), [NVIDIA Nemotron](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-BF16) (LLM) and [Nemotron Speech Streaming](https://huggingface.co/nvidia/nemotron-speech-streaming-en-0.6b) (STT) hosted by NVIDIA on AWS, [Gradium](https://gradium.ai) (TTS), [Pipecat Cloud](https://pipecat.daily.co) + [Twilio](https://twilio.com) (telephony), and [Cekura](https://cekura.com) (eval + self-improvement engine — both their [Claude Code plugin](https://docs.cekura.ai/mcp/claude-code-guide) and their [REST API](https://docs.cekura.ai)).

**Further reading inside this repo:**

- [`docs/SELF_IMPROVEMENT_LOOP.md`](docs/SELF_IMPROVEMENT_LOOP.md) — full design of the auto-improvement loop.
- [`docs/PERSISTENCE_CONTRACT.md`](docs/PERSISTENCE_CONTRACT.md) — how returning-member state is injected for seeded evals.
- [`evals/README.md`](evals/README.md) — operator-level notes on the eval pipeline.
- [`evals/reports/baseline_591149.md`](evals/reports/baseline_591149.md) — the "before" Cekura report.
