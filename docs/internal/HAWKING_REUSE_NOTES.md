# Hawking → Computexchange: reuse notes (working, 2026-06-29)

> SUPERSEDED by the authoritative, adversarially-verified result:
> **[docs/PERF_AND_CAPABILITY_AUDIT.md](../PERF_AND_CAPABILITY_AUDIT.md)** (Track H + full roadmap).
> Correction to the interim notes below: Hawking's hand-written Metal kernels are NOT the asset
> (measured ~0.62x llama.cpp at batch-1; spec-decode net-negative on the tested Mac). The assets
> are its **determinism discipline** (golden-hash harness) and its **continuous-batching scheduler** —
> adopt those, not the kernels.

Durable capture of the Hawking review so it survives a workflow crash. The rigorous,
adversarially-verified version is produced by the audit workflow's **Track H**.

## What Hawking is
A from-scratch **Rust + Metal Apple-Silicon LLM inference engine** at
`/Users/scammermike/Downloads/hawking`. No Python, no llama.cpp, no BLAS, no MPSGraph,
**no candle**. Loads GGUF via zero-copy mmap → Metal buffers; hand-written Metal kernels;
OpenAI-compatible server. Workspace crates: `hawking-core` (kernels/model/runtime),
`hawking-serve` (HTTP + continuous batching), `hawking-bench`, plus a separate **HIDE**
product (`hide-*`, ignore for computexchange).

## Transplantable assets (the gold)
1. **Golden-hash determinism harness** — invariants: "correctness gate before perf gate"
   (numerical equivalence atol=1e-3 fp16 vs reference), "same Engine runs bench/generate/serve,
   numbers match at equal batch size", "new levers default-off, must not change the default
   golden decode hash". CPU↔Metal parity verified 12/12 greedy token IDs on Qwen2.5-0.5B.
   → This **IS** computexchange's cross-worker verification moat, already engineered.
   Files: `crates/hawking-core/src/engine.rs` (Engine trait, `force_cpu`), `tests/`, `receipts/`,
   CI golden-hash gates.
2. **Continuous batching** (prefill/decode interleaving, slot manager) — the #1 throughput
   gap the main audit flagged in computexchange (today it serializes behind a per-model mutex).
   Files: `crates/hawking-serve/` (ARCHITECTURE.md §Server, §Request flow).
3. **Speculative decode** (n-gram + EAGLE draft + verify). Greedy spec-decode is byte-identical
   to greedy serial → determinism-SAFE. Files: `crates/hawking-core/src/speculate/`.
4. **Hand-written Metal kernels** (Q4_K/Q6_K GEMV, attention, RoPE, RMSNorm, GPU sampling,
   fused paths) — likely faster than candle-metal. Files: `crates/hawking-core/src/kernels/mod.rs`
   (~13k lines), `shaders/*.metal`, `src/{quant,sample,attn,moe}/`.
5. **Condense / quantization** (out-of-core low-bit press, AWQ/TQ bake, STRAND, int4 KV, RWKV-7
   SSM) — run bigger models in less supplier RAM; makes the README's 30–70B-on-unified-memory
   claim real. Files: `src/quant/`, `tools/{awq_bake,tq_bake,q4k_fast,condense}`, `vendor/strand-quant`.
   **Least-proven** part (see caveats).
6. **doctor / fit / autotune** — predicts the strongest usable config for the current Mac;
   maps onto computexchange `agent/src/hardware.rs` benchmarking + warm-model scheduling.
7. **MoE + dense** families (DeepSeek-V2-Lite, Mixtral, Qwen3-MoE; Qwen2.5, Llama 3.x, Mistral,
   Gemma2, Phi-3) — capability expansion.

## Honest caveats (do not oversell)
- **Apple-Silicon ONLY** (Metal/MPS, explicitly NO CUDA). Does **not** help the computexchange
  RunPod/CUDA lane. Keep candle/vLLM there.
- **Pre-proof.** "Zero win-eligible R3+ receipts exist yet"; frontier runs stopped pending a Mac
  Studio (M2 Max 96GB). Condense / sub-1-bit / spec-decode-revival wins are UNMEASURED.
  Measured today: ~31 decode tok/s on Qwen2.5-3B-Q4_K_M (M3 Pro 18GB). Treat throughput as
  potential, not proven.

## Integration thesis (to be verified by Track H)
The build-vs-wire question partly dissolves: **the custom engine exists.** Likely sequencing —
(a) adopt Hawking's **determinism harness** into computexchange verification first (cheap, hardens
the moat regardless of engine); (b) bring in **continuous batching + the Hawking backend** behind
the existing JobRunner contract for the Apple-Silicon lane, candle kept as fallback + CUDA lane;
(c) spec decode (determinism-safe greedy); (d) condense/bigger-models later, once Studio-proven.

## Workflow state
- Audit run `w5maj0yhm` (wf_983c94aa-108) stalled/limited; stopped to bank completed agents.
- Relaunching via resume with **Track H (Hawking integration)** appended + synth reframed.
