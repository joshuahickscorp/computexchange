<!-- CLAIM-SCOPE: internal-engineering-non-authoritative -->
# token-spec-poc — CX-owned lossless token speculative-decode lane

Branch C of `docs/research/CONSOLIDATION_PLAN_2026-07-09.md`. Recon verdict + full
design: **`docs/research/TOKEN_LANE_FORK_DESIGN.md`**.

This crate is a CX-OWNED, framework-independent implementation of the lossless
greedy speculative-decode loop:

```
SpecUnit -> DraftProducer -> Verifier -> AcceptancePolicy -> RepairPolicy -> SpecReceipt
```

## The one claim this crate defends

For **any** draft producer and **any** target greedy stream, the loop's output is
**token-for-token identical to the target model's own greedy decode**. That is the
property real vLLM spec-decode FAILED in this repo (0.68x / 0.406x AND non-lossless:
29/32 and 41/64 tokens mismatched — see the branch ledger). Here it is a
machine-checked invariant (`tests/lossless.rs`, swept over stream shapes, `k`,
n-gram order, and adversarial drafters).

We beat the 1x floor by being **correct first**: the token-emission ceiling of this
loop is provably `>= 1x` (each round emits at least one target-correct token), so
the honest floor is 1x — never the `<1x` a broken non-lossless path produces.

## What is MEASURED vs MODELED (honesty contract)

| quantity | status | where |
|----------|--------|-------|
| losslessness | **MEASURED** (unit + property tests) | `cargo test` |
| draft acceptance rate | **MEASURED** | default harness / `--features candle` |
| target-call reduction (speedup ceiling) | **MEASURED** | receipt `meta.target_call_reduction_x` |
| wall-clock `speedup_x` > 1x | **MODELED**, fork-gated | needs the two candle additions below + a fleet run |

`speedup_x` in the receipt is **labeled `MODELED`** and must never be quoted as a
speed win from this crate. Wall-clock `>1x` requires the K-token verify to cost
~one decode step, which needs the fork in `TOKEN_LANE_FORK_DESIGN.md`
(`forward_all_logits` + `KvCacheSlot::truncate`) and a fleet-class run.

## Run it

Default (pure Rust, no model, milliseconds — the guaranteed-green path):

```
cargo test                       # losslessness + accounting invariants (9 tests)
cargo run -- --k 16 --order 3    # acceptance/ceiling table over representative streams (JSONL receipts)
```

Real Llama-3.2-1B greedy stream (measures acceptance on ACTUAL model output; the
GGUF the agent already ships is at
`~/.cache/huggingface/hub/models--unsloth--Llama-3.2-1B-Instruct-GGUF/.../Llama-3.2-1B-Instruct-Q4_K_M.gguf`):

```
cargo run --features candle -- \
  --gguf ~/.cache/huggingface/hub/models--unsloth--Llama-3.2-1B-Instruct-GGUF/snapshots/*/Llama-3.2-1B-Instruct-Q4_K_M.gguf \
  --prompt "Explain speculative decoding in one paragraph." --max-new 128 --k 16 --order 3
```

(Add `--features metal` on Apple Silicon for the GPU greedy decode. The candle
backend measures acceptance on the model's own greedy stream; it does NOT claim
wall-clock — same honesty contract.)

## Local acceptance table (MEASURED, order-3 byte n-gram drafter, k=16)

Weak drafter (byte n-gram, no draft model) through the lossless loop:

| stream | accept_frac | ceiling_x | exact |
|--------|-------------|-----------|-------|
| code   | 0.19 | 1.40 | true |
| json   | 0.10 | 1.12 | true |
| prose  | 0.19 | 1.35 | true |
| repeat | 1.00 | 7.03 | true (degenerate outlier) |
| random | 0.00 | 1.00 | true (negative control — never < 1x) |

These are the **weak-drafter floor**. A real draft MODEL (e.g. SmolLM2-135M -> Llama
target, both already cached locally) is where acceptance climbs to the 0.6-0.8 band
published spec-decode reaches — the regime where the fork's one-pass verify turns
acceptance into wall-clock `>1x`. That measurement is the sequenced fleet follow-up.

## Real-model acceptance (MEASURED, Llama-3.2-1B greedy, CPU, offline)

Built + run in-sandbox (candle 0.10.2, CPU, `HF_HUB_OFFLINE=1`): the model greedy-
decodes 128 REAL tokens; the same weak order-3 **token** n-gram drafter runs through
the lossless loop against the model's own stream. Every run `exact=true`. Headline is
`ceiling_x` (end-to-end tokens per target call); `accept_frac` is the hit-rate among
*proposed* tokens only (conditional, not the end-to-end multiplier):

| prompt class | accept_frac (of proposed) | ceiling_x (end-to-end) | exact |
|--------------|---------------------------|------------------------|-------|
| prose      | 0.00 | 1.000 | true |
| code       | 0.06 | 1.024 | true |
| structured | 0.56 | 1.076 | true |

Lower than the byte-stream table on purpose: Llama's ~128k-token vocabulary makes
exact 3-gram repeats rare inside 128 tokens, so a zero-model drafter proposes seldom
and the end-to-end ceiling sits at 1.0-1.08x — the honest real-model floor, lossless,
never below 1x. The draft-MODEL rung + the fork are where >1x lives.

## Files

- `src/lib.rs` — the lossless core (`run_spec_decode`, `accept_round`), the
  `DraftProducer`/`TargetModel` traits, `NgramDraft`, `MockTarget`, `SpecReceipt`.
- `src/candle_target.rs` (`--features candle`) — real quantized-Llama backend.
- `src/main.rs` — the acceptance harness.
- `tests/lossless.rs` — the losslessness property + accounting invariants.

`SpecReceipt` mirrors `scripts/spec-lab/cx_speculative_core.py::SpecReceipt` and the
Branch A substrate contract **by shape**, not by import (per the wave's rule), so a
token receipt and a render receipt compose only through the plan's staged table.
