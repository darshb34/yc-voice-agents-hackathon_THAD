<!-- CEKURA-REPORT-START -->
# Cekura Quality Report — Field & Flower (Nemotron)

- **Result:** Phase 1 baseline — full 12-scenario suite (`591149`) · generated 2026-05-30 19:18 UTC
- **Status:** in_progress · scored 9/12 runs · **0/9 passed** (0%)
- **Connection:** Pipecat Cloud (`flower-bot`) via pipecat_v2
- ⚠️ **2 run(s) never started** (`pipecat-agent-concurrency-limit-reached`) — exceeded the 10-agent cap; re-run these in a later wave.

## Headline

- ⏱️ **Latency is the dominant problem:** mean per-turn agent latency **6.3s** across scored runs (worst run avg 8.2s). 9/9 runs tripped Infrastructure Issues (silent >10s).
- 🎙️ **STT is healthy:** mean transcription WER **0.0%** — not the bottleneck.

## Results by scenario

| Scenario | Bucket | Outcome | Latency | WER | Dead air | Link |
|---|---|---|---|---|---|---|
| Mid-Order Bouquet Switch | edge_case | ❌ failure | 5.2s | 0% | ⚠️ | [view](https://dashboard.cekura.ai/5636/results/591149?call_id=3199505) |
| Orchid Unavailable, Rose Romance Order | edge_case | 🚫 failure | — | — | — | [view](https://dashboard.cekura.ai/5636/results/591149?call_id=3199498) |
| Sympathy Bouquet Out-of-Stock Alternative | edge_case | ❌ failure | 5.4s | 0% | ⚠️ | [view](https://dashboard.cekura.ai/5636/results/591149?call_id=3199504) |
| Anniversary Relative Date Delivery | happy_path | ❌ failure | 8.2s | 0% | ⚠️ | [view](https://dashboard.cekura.ai/5636/results/591149?call_id=3199503) |
| Birthday Gift Order Workflow | happy_path | ❌ failure | 7.4s | 0% | ⚠️ | [view](https://dashboard.cekura.ai/5636/results/591149?call_id=3199508) |
| Graduation Gold Delivery Order | happy_path | ⏳ in_progress | — | — |  | [view](https://dashboard.cekura.ai/5636/results/591149?call_id=3199501) |
| Multi-Item Floral Order with Delivery | happy_path | ❌ failure | 3.0s | 0% | ⚠️ | [view](https://dashboard.cekura.ai/5636/results/591149?call_id=3199506) |
| Sympathy Whites Funeral Order | happy_path | ❌ failure | 8.2s | 0% | ⚠️ | [view](https://dashboard.cekura.ai/5636/results/591149?call_id=3199499) |
| Wildflower Medley Order with Special | happy_path | ❌ failure | 7.8s | 0% | ⚠️ | [view](https://dashboard.cekura.ai/5636/results/591149?call_id=3199500) |
| Complex Address STT Accuracy Test | stt_stress | ❌ failure | 8.1s | 0% | ⚠️ | [view](https://dashboard.cekura.ai/5636/results/591149?call_id=3199497) |
| Quantity/Date Homophone Test | stt_stress | ❌ failure | 3.4s | 0% | ⚠️ | [view](https://dashboard.cekura.ai/5636/results/591149?call_id=3199507) |
| Recipient Name Complex Spelling | stt_stress | 🚫 failure | — | — | — | [view](https://dashboard.cekura.ai/5636/results/591149?call_id=3199502) |

## Failures by root cause

### ❌ Dead air / latency (agent silent >10s) (9)
[Mid-Order Bouquet Switch](https://dashboard.cekura.ai/5636/results/591149?call_id=3199505) · [Sympathy Bouquet Out-of-Stock Alternative](https://dashboard.cekura.ai/5636/results/591149?call_id=3199504) · [Anniversary Relative Date Delivery](https://dashboard.cekura.ai/5636/results/591149?call_id=3199503) · [Birthday Gift Order Workflow](https://dashboard.cekura.ai/5636/results/591149?call_id=3199508) · [Multi-Item Floral Order with Delivery](https://dashboard.cekura.ai/5636/results/591149?call_id=3199506) · [Sympathy Whites Funeral Order](https://dashboard.cekura.ai/5636/results/591149?call_id=3199499) · [Wildflower Medley Order with Special](https://dashboard.cekura.ai/5636/results/591149?call_id=3199500) · [Complex Address STT Accuracy Test](https://dashboard.cekura.ai/5636/results/591149?call_id=3199497) · [Quantity/Date Homophone Test](https://dashboard.cekura.ai/5636/results/591149?call_id=3199507)
> ❌ The main agent acknowledged the testing agent's bouquet selection (03:01) but did not confirm 'Tulip Tower' or the 'Garden Party' bouquet.

## Metric family rollup (avg normalized score)

| Family | Avg score |
|---|---|
| conversational_quality | 2.56 (n=36) |
| performance | 3229.61 (n=18) |
| stt_accuracy | 5.00 (n=9) |
| task_success | 0.28 (n=18) |
| tool_calling | 1.00 (n=9) |

## Next steps

1. **Cut per-turn latency** — the agent narrates each tool step (`add_to_order` → `get_order_summary` → `set_delivery_details`) with a round-trip each. Batch tool calls / trim narration; consider a holding phrase so silence isn't dead air. This is the Phase 4 prompt-optimization target.
2. **Concurrency** — keep waves ≤10 or raise the Pipecat cap (`pcc deploy --max-agents N`); re-run the skipped scenarios.
<!-- CEKURA-REPORT-END -->