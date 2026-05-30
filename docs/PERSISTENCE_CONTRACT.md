# Single-User Persistence & Session-Seeding Contract

**Audience:** Albert & Darshan (agent), HG (eval harness).
**Status:** north-star target — the eval scenarios G/H/I depend on this and will **fail until the agent implements it**. That's intended; it defines the direction.

This pins down the interface so the agent and the eval harness can be built independently and meet in the middle.

---

## 1. Why

For the hackathon we assume the deployment serves **exactly one user**. That changes the agent's lifecycle:

- **First call ever:** run onboarding (the existing intake flow), then **persist** the resulting profile + daily targets.
- **Every later call:** the user is the *same person*. The agent must **load the saved profile and skip onboarding**, and must also know **what the user already ate today** so it can size recommendations to the *remaining* budget — not start the day fresh every call.

A coach that re-onboards on every call, or forgets this morning's breakfast by dinner, fails the product.

There are **two persistence horizons**:

| Layer | Lifetime | Resets | Holds |
|---|---|---|---|
| **Profile** | durable, across days | on account delete | name, goal, diet style, allergies, restrictions, activity level, biometrics, daily macro targets |
| **Daily log** | one calendar day | at local midnight | the meals logged today + the running *remaining* macro budget |

---

## 2. How state reaches the agent (two paths — implement both)

### 2a. Production: a persisted file (your "markdown file" idea)
On first-call onboarding, write the profile to a single-user store the agent reads on boot. JSON is easier to parse than markdown; if you want a human-readable file, keep a `.md` mirror but treat the `.json` as source of truth.

```
data/member_profile.json     # durable profile + targets (Layer 1)
data/daily_log_<YYYY-MM-DD>.json   # today's logs + remaining (Layer 2)
```

On every `log_meal`, append to today's daily-log file and update `remaining`. On boot, the agent loads both (if the daily-log file for *today* is missing, the day is fresh).

### 2b. Eval / testing: injected per-session context (Cekura `test_profile`)
The eval harness can't rely on a file surviving across Cekura's isolated, concurrent Daily sessions. Instead, **Cekura injects the seeded state per session** via the scenario's attached **test profile**. The agent must read this injected context at session start and treat it exactly like a loaded profile + daily log.

> **Implementation note for the agent (`coach_core.bot_entry`):** when Cekura starts a `pipecat_v2` session it passes the test-profile `information` map into the session (call/run metadata / dynamic variables on the `DailyRunnerArguments`). Read it there, hydrate the per-call member state from it, and branch to the returning-member path. If no injected context and no file → genuinely new user → onboard.

**Precedence:** injected per-session context (2b) **overrides** the file (2a) for that call. This lets the harness seed any scenario deterministically without touching disk.

---

## 3. The injected-context schema (the actual contract)

Cekura `test_profile.information` must be a **flat map of string values** (the API rejects nested objects and blank strings). Structured data is therefore **JSON-encoded into string values**. These are the exact keys the agent must read:

| Key | Type | Example | Meaning |
|---|---|---|---|
| `single_user_assumption` | string (prose) | "…load this state at session start and SKIP onboarding." | Human/LLM-readable instruction; agent may ignore programmatically. |
| `member_name` | string | `"Sam Rivera"` | Profile: name |
| `goal` | string | `"cut"` | `cut` \| `maintain` \| `bulk` |
| `diet_style` | string | `"vegetarian"` | `omnivore` \| `vegetarian` \| `vegan` \| `pescatarian` \| `paleo` |
| `allergies` | string (comma-sep) | `"peanut"` | hard filter; `"none"` if empty |
| `restrictions` | string (comma-sep) | `"none"` | non-blank required; use `"none"` |
| `activity_level` | string | `"moderate"` | `sedentary`\|`light`\|`moderate`\|`active`\|`very_active` |
| `biometrics` | string | `"declined"` | `"declined"` or a JSON blob `{"sex","age","weight_kg","height_cm"}` |
| `daily_targets_json` | JSON string | `"{\"calories\":1870,\"protein_g\":140,\"carbs_g\":187,\"fat_g\":62}"` | computed daily macro target |
| `today_logs_json` | JSON string (array) | `"[{\"name\":\"greek yogurt parfait\",\"slot\":\"breakfast\",\"calories\":320,\"protein_g\":24,\"carbs_g\":38,\"fat_g\":8}, …]"` | meals already logged today |
| `today_logs_summary` | string (prose) | "Breakfast: greek yogurt parfait (320 cal)…" | human/LLM-readable mirror of the array |
| `remaining_json` | JSON string | `"{\"calories\":1140,\"protein_g\":96,\"carbs_g\":93,\"fat_g\":42}"` | daily target − today's logs |

> The same key set is the recommended shape for the production file (2a) — just as real JSON values instead of JSON-in-strings.

Seeded fixtures already created in Cekura (project 5636):

| Test profile | ID | State |
|---|---|---|
| Single user — Sam (returning, fresh day) | **16012** | profile only, `today_logs=[]`, `remaining == daily_targets` |
| Single user — Sam (mid-day, breakfast+lunch logged) | **16013** | + breakfast (greek yogurt parfait) + lunch (lentil quinoa salad); `remaining = 1140 cal / 96 P / 93 C / 42 F` |

---

## 4. Required agent behavior on boot

```
state = injected_session_context()  or  load_profile_file()  or  None
if state and state.member:
    hydrate per-call member from state.profile
    hydrate today's logs + remaining from state (or empty if fresh day)
    DO NOT onboard. Greet generically: "Welcome back — how's the day going?"
    Recommendations must respect the loaded profile (diet/allergies) and be
    sized to `remaining`, not the full daily target.
else:
    genuinely new user -> run onboarding -> persist profile (Layer 1)
```

On `log_meal` (any call): append to today's daily log + recompute `remaining`, and persist (Layer 2) so a later same-day call sees it.

---

## 5. What the eval measures against this (so you know the bar)

| Metric | ID | What it checks |
|---|---|---|
| **M1 No Re-Onboarding** | 148009 (Cekura, binary) | Agent never re-asks a known profile field / never re-creates the account. |
| **M2 Recall Accuracy** | 148010 (Cekura, continuous) | Every profile + prior-meal fact the agent states matches the injected ground truth; remaining budget computed as target − logs. |
| **M3 Adaptive Problem-Solving** | 148011 (Cekura, continuous) | Picks the closest-to-goal option among choices + proposes safe compositional tweaks (swaps, portions, "less oil", add-ons). |
| **M4 Goal Proximity** | deterministic, `evals/report.py` | `1 − normalized macro error` of the recommended (and tweaked) meal vs. the scenario's `remaining` target (config `target:`). Computed by us, not an LLM. |

Scenarios that exercise this: **G** (273178, tp 16012), **H** (273179, tp 16013), **I** (273180, tp 16013), in folder `Coach eval suite v1.Returning (seeded)`.

---

## 6. Open questions for the agent side

1. **Exactly where** does Cekura's `pipecat_v2` run surface the `test_profile.information` map on `DailyRunnerArguments`? (room metadata? a `body`/`config` field?) Confirm by logging the runner args on a seeded call, then read from there.
2. Local-day boundary for the daily log — use the deploy's timezone or the member's? (Hackathon: deploy TZ is fine.)
3. Where does the production profile file live in the Pipecat Cloud container — bundled read-only, or a writable volume? If the FS is ephemeral, Layer 2 writes won't survive a cold start; a tiny external KV may be needed post-hackathon. (For the demo, injected context (2b) sidesteps this entirely.)
