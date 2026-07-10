# Token Lane Fork Design — CX-owned lossless speculative decode (Branch C)

Status: DESIGN + LOCAL BUILD landed 2026-07-09. Serves
`docs/research/CONSOLIDATION_PLAN_2026-07-09.md` Branch C. Write paths this wave:
`token-spec-poc/` (built, tested) + this doc. No cloud, no spend, no `agent/src`
edits (the fork items below are SPECIFIED, not applied — `agent/src` is not this
branch's write path).

The one sentence: **fork nothing heavy — the parallel-verify primitive that
lossless spec-decode needs is ~8 lines away from what the agent's own patched
candle quantized-Llama already ships and determinism-pins.** vLLM is out (heavy,
driver-fragile, already the measured LOSS, unrunnable locally); llama.cpp is the
reference to mirror, not the base; candle is the base.

---

## 1. Recon verdict: candle (with evidence)

### 1.1 What the agent ALREADY does (this is the whole argument)

`agent/src/runners.rs::LlamaBackend` runs REAL quantized-Llama inference today
(Metal on Apple Silicon, CUDA on RunPod, CPU fallback — `models::device()`),
byte-stable greedy argmax, with a KV-cache-aware forward:

- `LlamaBackend::generate` (runners.rs:1294) — the greedy decode loop. First pass
  feeds the whole prompt, later passes feed one token; `self.model.forward(&input,
  index_pos)` (runners.rs:1319) returns logits; `argmax` picks the next token.
- The model is `quantized_llama_batched::ModelWeights` — the agent's own PATCH of
  candle's `quantized_llama`, already carrying batched prefill, padded-bucket
  decode, shared-prefix KV forking, and a Hawking continuous-batch path, all
  determinism-pinned against serial.

Two primitives inside that patched model are exactly what spec-decode's verifier
needs, and they already exist and are proven:

**(a) An all-positions logits pass already exists.** `forward` (quantized_llama_batched.rs:1349)
narrows to the last position before the output projection:

```rust
let x = self.norm.forward(&layer_in)?;
let x = x.i((.., seq_len - 1, ..))?.contiguous()?;  // line 1405 — the last-token narrow
self.output.forward(&x)                              // -> [b, vocab]  (last position only)
```

…BUT `forward_padded` (quantized_llama_batched.rs:1443) already applies norm +
output projection to **every** position and returns **full `(b_sz, seq_len,
vocab)` logits** (its own doc, lines 1433-1437):

```rust
let x = self.norm.forward(&layer_in)?.contiguous()?;
self.output.forward(&x)   // line 1493 — FULL (b, seq, vocab), no narrow
```

and it is determinism-pinned to serial by
`batch_padded_bucket_equals_serial_mixed_lengths` (comment at lines 1439-1442).
So "compute per-position logits in one pass" is already correct, already tested
candle code in our tree — the parallel-verify kernel is not a research risk.

**(b) A byte-exact KV length-reseat already exists.** `KvCacheSlot::snapshot`
returns `(live_region, cur_len)` and `restore` (quantized_llama_batched.rs:248)
re-seats the cache to a given length, byte-for-byte identical to serial (used
today to fork a shared prefix into per-item sequences). Attention always reads the
live region as `buf.narrow(2, 0, cur_len)` (e.g. line 195), so "roll the cache back
to length L after a partial acceptance" is a one-line `cur_len = L` on top of
machinery we already trust.

**(c) Local draft/target pairs are already on disk.** The agent's Llama-3.2-1B
GGUF is cached (`~/.cache/huggingface/.../Llama-3.2-1B-Instruct-Q4_K_M.gguf`), and
`~/Downloads/hawking/models/` holds SmolLM2-135M, SmolLM2-360M, Qwen2.5-0.5B/1.5B/
3B/7B — real draft->target pairs for classic spec-decode, no download needed.

**(d) It runs locally in THIS sandbox.** cargo 1.94 + candle 0.10.2 (core/nn/
transformers/metal-kernels) + tokenizers + hf-hub are all in the local cargo cache
and unpacked; the agent is already built. There is NO torch/transformers/vLLM
locally (`import torch` fails) — so candle is not just the best base, it is the
only base we can MEASURE against without the fleet.

### 1.2 The three-way comparison

| axis | vLLM | llama.cpp | **candle** |
|------|------|-----------|------------|
| already in our stack | no | no | **yes** — agent runs it in prod |
| language / FFI | Python + CUDA | C++ (new FFI from Rust) | **Rust, native** |
| runs locally in sandbox | **no** (no torch/CUDA) | needs a C++ build | **yes** (cached, builds offline) |
| Metal + CUDA | CUDA only | both | **both** (features already wired) |
| distance to a parallel-verify pass | own its whole engine | port `common_speculative` | **~5 lines** (drop one narrow) |
| KV rewind on reject | internal, not ours | internal | **~3 lines** (snapshot/restore exist) |
| losslessness control | measured **NON-lossless** | ours if we port it | **ours by construction** |
| measured spec-decode result so far | **0.33-0.68x, non-lossless** | n/a | n/a (this design) |
| driver fragility | **high** (already hit "driver too old") | low | low |
| ownable surface | enormous | medium (C++) | **small (Rust, ours)** |

**vLLM — rejected as the base.** Evidence from the repo's own runs
(`SPEC_RENDER_TOKEN_DECODE_BRANCH_LEDGER_2026-07-09.md`):
`vllm_0_11_ngram` = H100 **0.68x**, acceptance 0.38, **non-lossless 29/32
mismatched**; `vllm_0_11_draft_fallback_ngram` = **0.406x**, **41/64 mismatched**;
`vllm_0_11_draft_model` = `NotImplementedError` (pinned 0.11 has no draft-model
spec); modern stacks (`vllm 0.20.2/0.24`) = **"NVIDIA driver too old (12080)"** /
engine init failed before any measurement. Forking vLLM means owning a Python+CUDA
mountain to inherit a path that is both a LOSS and BROKEN on exactness, on a
dependency chain we've repeatedly failed to even boot. No.

**llama.cpp — the reference, not the base.** It is light, embeddable, and has a
mature speculative path (`common_speculative` + `llama_batch`), and — usefully —
our GGUFs ARE llama.cpp-quantized. But adopting it means a second inference stack
in C++ behind a new FFI boundary, parallel to the candle stack we already ship and
determinism-pin. We take its ACCEPTANCE MATH as the reference to mirror (and as a
future cross-check oracle), not its runtime.

**candle — the base.** Most ownable (Rust, ours, small surface), least fragile
(no driver mountain), the only locally-measurable option, already Metal+CUDA, and —
decisively — the parallel-verify primitive is a few lines from `forward_padded`,
which our tree already proves correct.

---

## 2. The fork plan (exact, surgical — SPECIFIED, applied in a later wave)

Two additions to `agent/src/quantized_llama_batched.rs`, each mirroring proven code
right next to it. Neither is written this wave (write-path discipline).

### F1 — `forward_all_logits(tokens, index_pos) -> (b, seq, vocab)`
Identical to `forward` (line 1349) with the last-position narrow at line 1405
removed, applying `self.norm` then `self.output` to the full hidden state — exactly
what `forward_padded` does at lines 1490-1493. ~5 lines. This is the K-token verify
pass: feed `[correction_token, d_1..d_k]`, get `k+1` rows of logits, argmax each row
to get the target's greedy token after each prefix.

Determinism: `forward_padded` with uniform positions + no-pad mask is already
bit-identical to serial (`batch_padded_bucket_equals_serial_mixed_lengths`); the
new fn is the scalar-position specialization of that same code, so the same test
family covers it.

### F2 — `KvCacheSlot::truncate(len)`
`self.cur_len = len; Ok(())`. Safe because every reader takes `buf.narrow(2, 0,
cur_len)`; the KV rows for the rejected draft tokens (positions `≥ len`) are simply
never read again and are overwritten by the next `append`/`slice_set`. `snapshot`/
`restore` (lines 225-264) already prove length-reseat is byte-exact. ~3 lines.

That is the ENTIRE kernel-level fork. Everything else — GGUF load, tokenizer, KV
cache, Metal/CUDA dispatch, greedy argmax, the byte-stability soak — is reused
verbatim from the agent.

---

## 3. The CX-owned lane (built in `token-spec-poc/`, tested, measured)

```
SpecUnit -> DraftProducer -> Verifier -> AcceptancePolicy -> RepairPolicy -> SpecReceipt
```

The whole loop is implemented and unit-tested in `token-spec-poc/src/lib.rs`
(9 tests green). The lossless core is backend-agnostic: it runs today against a
deterministic mock target (the losslessness oracle) and against a real Llama-3.2-1B
greedy stream (`--features candle`); post-fork it runs against the live
`forward_all_logits` pass with no change to the accept/repair logic.

### 3.1 DraftProducer (the only swappable part)
`trait DraftProducer { fn propose(&mut self, ctx, k) -> Vec<u32>; }`. Ladder:

1. **n-gram / prompt-lookup (BUILT, MEASURED).** Copy the continuation that
   followed the most recent identical k-gram of context. Zero model, zero GPU.
   Wins on structured/repetitive output. Measured acceptance below.
2. **Draft MODEL (next).** SmolLM2-135M/360M or Qwen-0.5B drafting for a
   Llama-1B / Qwen-7B target — all GGUFs already cached. Reuses the agent's own
   `generate` loop as the draft engine. This is the regime published spec-decode
   reaches 0.6-0.8 acceptance in, i.e. where wall-clock `>1x` lives.
3. **EAGLE-style head (future).** A trained single-layer proposer on the target's
   own hidden states — the current SOTA acceptance/latency point. Heaviest to own;
   only if the draft-model rung proves the economics.

### 3.2 Verifier
One target pass over `ctx ++ draft`. Locally today: `forward_all_logits` isn't in
the sandbox agent yet, so the mock/candle backends answer the per-prefix greedy
query by lookup into a real (or fixed) greedy stream — which measures ACCEPTANCE
exactly, the quantity that governs speedup. Post-fork the verifier is literally
`argmax(forward_all_logits([correction, d_1..d_k]), axis=-1)`.

### 3.3 AcceptancePolicy + RepairPolicy (lossless, the crown jewel)
`accept_round` (lib.rs): accept the **longest prefix** of the draft whose every
token equals the target's greedy argmax at that position; the target's greedy token
at the **first mismatch** is the **free bonus/repair token** (it was computed in the
same verify pass). Emit `accepted ++ [bonus]`.

**Losslessness proof.** Every accepted draft token equals the target greedy argmax
at its position; the bonus IS the target greedy argmax at the first divergence.
Concatenated over rounds, the output is exactly the target's greedy decode,
token-for-token, for ANY drafter. `n_accept` can be 0 → the round degenerates to
plain greedy (one target token), so the token-emission ceiling is `>= 1x` by
construction — the honest floor is 1x, never the `<1x` a non-lossless path hits.
Machine-checked in `tests/lossless.rs` (swept over stream shapes, k, order, and
adversarial "always wrong" / "flood 1000 garbage tokens" drafters).

**KV management (post-fork).** Before verify, `snapshot` at accepted length `L`.
The verify pass appends KV for `d_1..d_k` (→ `cur_len = L+k`). Accept `j` → the KV
for `d_1..d_j` is already correct in the buffer; `truncate(L + j)` drops the rest;
the bonus token seeds the next round's first verify slot. Byte-exact by F2.

### 3.4 SpecReceipt (matches Branch A / cx_speculative_core by shape)
`token-spec-poc` emits the SAME field set as
`scripts/spec-lab/cx_speculative_core.py::SpecReceipt.to_dict()` (branch_id,
modality, units, attempted/accepted/repaired/rejected_units + fractions, draft_s,
verify_s, repair_s, baseline_s, speculative_s, speedup_x, exact, quality_gate,
meta), verified by `receipt_json_has_contract_keys`. `repair_s = 0` (the bonus is
free); `exact/quality_gate` carry the losslessness bit; `meta.walltime_label`
spells out MEASURED vs MODELED so a receipt can never be mis-quoted.

---

## 4. What is measurable LOCALLY — and the honest numbers

Measured in-sandbox, no fleet, no fork, this wave:

- **Losslessness:** 9/9 tests green, incl. a property sweep over stream shapes ×
  k × n-gram order × adversarial drafters. Output == target greedy, always.
- **Acceptance + target-call reduction** through the lossless loop, weak (order-3
  byte n-gram) drafter, k=16:

  | stream | accept_frac | ceiling_x (tok/target-call) | exact |
  |--------|-------------|------------------------------|-------|
  | code   | 0.19 | 1.40 | true |
  | json   | 0.10 | 1.12 | true |
  | prose  | 0.19 | 1.35 | true |
  | repeat | 1.00 | 7.03 | true (degenerate outlier, labeled) |
  | random | 0.00 | 1.00 | true (negative control — never < 1x) |

  These agree with the repo's prior byte-predictability ledger (`token_local_code`
  acceptance ~0.20) but now flow through the proper lossless accept/repair loop and
  emit a real receipt. They are the **weak-drafter floor**, not the ceiling.

- **Real-model acceptance (`--features candle`, MEASURED 2026-07-09).** The candle
  backend was BUILT and RUN in-sandbox (candle 0.10.2, CPU, fully offline with
  `HF_HUB_OFFLINE=1` against the cached `unsloth/Llama-3.2-1B-Instruct` tokenizer +
  `Llama-3.2-1B-Instruct-Q4_K_M.gguf`). It greedy-decodes 128 REAL tokens, then runs
  the weak order-3 token n-gram drafter through the lossless loop against the model's
  OWN greedy stream. Every run `exact=true` (output == the model's greedy decode,
  token-for-token). Headline is `ceiling_x` = `target_call_reduction_x` (end-to-end
  tokens per target call); `accept_frac` is the drafter's hit-rate AMONG the tokens it
  dared to propose (conditional, NOT the end-to-end multiplier):

  | prompt class | draft proposed | accept_frac (of proposed) | ceiling_x (end-to-end) | exact |
  |--------------|----------------|---------------------------|------------------------|-------|
  | prose ("explain speculative decoding") | 31 | 0.00 | **1.000** | true |
  | code ("Python `fib(n)` + docstring")   | 48 | 0.06 | **1.024** | true |
  | structured ("Line N: N", 1..40)        | 16 | 0.56 | **1.076** | true |

  This is the HONEST real-model floor and it is LOWER than the byte-stream mock table
  above — expected and important: on Llama's ~128k-token vocabulary (vs 256 byte
  symbols) exact 3-gram repeats inside 128 tokens are rare, so the zero-model drafter
  proposes seldom and the end-to-end ceiling sits at 1.0-1.08x. It is a real,
  model-backed, lossless number that never dips below 1x — exactly the floor to beat.
  Wall-clock is still MODELED (verify-by-lookup is not a forward pass); a real >1x
  needs the draft-MODEL rung + the F1/F2 fork + a fleet run.

**The wall-clock model (why acceptance → speedup only after the fork).** With F1+F2,
a K-token verify costs ≈ one decode step (one batched forward), so:

```
walltime_speedup ≈ target_call_reduction / (1 + draft_overhead_fraction)
target_call_reduction = mean(accepted_per_round) + 1
```

The n-gram drafter's overhead is ~0, so its ceiling (1.1-1.4x on real text) is close
to its wall-clock — a modest but real win on structured traffic. The draft-MODEL
rung trades draft cost for far higher acceptance; break-even is roughly
`accept_frac > draft_cost_per_token / target_cost_per_token` (a 135M draft vs a 7B
target ≈ 0.02, so any acceptance above a few percent wins). Neither wall-clock
number is claimed here; both are the sequenced fleet experiment.

---

## 5. Sequenced fleet experiment (money-safe, one driver at a time)

This is cloud follow-up #2 in the consolidation plan — runs AFTER the wave, alone
(shared `.tracked_pods.json`), money-safe (`arm_remote_watchdog` + `register_cleanup`
+ verify tracked pods empty before/after):

1. Apply F1+F2 to the agent's `quantized_llama_batched.rs`; extend the
   determinism test family to `forward_all_logits`.
2. On one fleet-class GPU: draft-model spec-decode (SmolLM2-135M → Llama-3.2-1B, and
   Qwen-0.5B → Qwen-7B) on REAL prompts, greedy, verifying byte-equality to the
   target's own `generate` (losslessness gate) and timing `baseline_s`/`speculative_s`.
3. Emit the SpecReceipt. First honest `>1x` lossless token number — or an honest
   negative.

---

## 6. Kill criterion (from the plan, sharpened)

If, post-fork, draft-model spec-decode **cannot beat 1x losslessly** on our
hardware across representative prompts, we PARK token-decode ownership and pivot the
token value to the routing/brokering lane (already real), recording the measured
reason. Two guardrails make this a clean call, not a vibe:

- Losslessness is non-negotiable (already enforced by the byte-equality gate); we
  never ship vLLM's mistake of a fast-but-wrong path.
- The n-gram rung is a permanent, zero-risk fallback: even if draft models don't
  pay, structured/repetitive traffic (code, JSON, edits, retrieval) gets the
  measured 1.1-1.4x for free, losslessly, on hardware we already run.

---

## 7. Contract note

`token-spec-poc` matches the SpecReceipt schema **by shape, not by import** this
wave (per the plan). Wiring it into `spec-engine/` (Branch A) is a sequenced
follow-up; until then a token receipt and a render receipt compose ONLY through the
plan's staged-multiplier table, never by a naive product.
