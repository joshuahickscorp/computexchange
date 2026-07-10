# A100 SXM capability sweep — throughput vs batch vs model size (MEASURED)

*2026-07-06. Follow-on to `A100_REFERENCE_MEASURED.md`. Where that measured one point
(a small model at max batch), this maps the whole surface: what one real A100-SXM4-80GB
delivers across the job shapes our buyers actually submit. Purpose: give the planner and
the quoting path the real competition curve, so we route honestly — fleet where it wins,
GPU where it doesn't.*

Measured on the user-provisioned RunPod A100 SXM via vLLM (fp16), `ignore_eos`,
`max_tokens=128`, distinct-prefix prompts (no prefix-cache inflation). Raw:
`artifacts/a100-sxm-capability-sweep-2026-07-06.jsonl`.

## The measured surface — aggregate tok/s

| model | batch=1 | batch=8 | batch=64 | batch=512 | batch=2048 | batch1→ceiling |
|---|--:|--:|--:|--:|--:|--:|
| **1.1B** (TinyLlama, fp16) | 387 | 2,954 | 19,864 | 43,570 | **44,852** | 116× |
| **7B** (Qwen2.5, fp16) | 100 | 784 | 5,355 | 11,116 | **11,310** | 113× |
| **14B** (Qwen2.5, fp16) | 52 | 412 | 2,822 | 5,667 | **5,708** | 109× |
| **32B** (Qwen2.5, AWQ int4) | 70 | 520 | 1,849 | 2,309 | **2,342** | 33× |

Two extra reads from the 32B row: (a) **quantization helps batch-1 latency** — 32B-AWQ
does 70 tok/s at batch=1, *faster* than 14B-fp16's 52, because int4 moves 1/4 the bytes
per token on bandwidth-bound decode; (b) **the batching advantage SHRINKS with model
size** (116× at 1B → 33× at 32B): a bigger model saturates the GPU's compute sooner, so
there is less headroom for batching to compound. Implication: the larger the model, the
*less* dominant a single A100 is at high batch — the fleet's relative position improves
with model size (though our fleet is itself capped ~7B, so we can't yet exploit that).

## The one insight that changes how we route

**The A100's crushing throughput is ENTIRELY a batching phenomenon.** From batch=1 to
saturation it gains ~**110×** (1B: 387→44,852; 7B: 100→11,310; 14B: 52→5,708), and it
saturates by ~batch 512 (512 and 2048 are within a few percent). Two consequences:

1. **At batch=1 — the latency-bound, one-prompt-at-a-time regime — the A100 is ordinary.**
   100 tok/s for a 7B, 52 tok/s for a 14B. That is *consumer-hardware territory*: our
   measured M3 Pro does ~139 tok/s on a 1B; an M4 Max does ~93 tok/s on an 8B (research).
   So on single-stream, latency-bound work **one A100 ≈ 1–3 Macs**, not hundreds. A small
   fleet genuinely competes here.

2. **At batch ≥ 64 the A100 pulls away fast**, and by batch 512 it is 5,700–44,900 tok/s —
   tens to hundreds of Macs. On big throughput batches the fleet cannot win on wall-clock;
   it would need ~50–300+ nodes (see `A100_REFERENCE_MEASURED.md`).

## The routing rule this buys the marketplace

This is the actionable output — the planner should read a job's effective concurrency and
choose accordingly:

- **Latency-sensitive / low-concurrency work** (interactive, a few prompts, fast turnaround
  per prompt): the fleet is competitive — a handful of nodes match an A100, and we can win
  on availability/cost while tying on latency. **Take these; route to the fleet.**
- **Large throughput batches** (hundreds–thousands of independent prompts, wall-clock is
  the sum of a lot of work): one A100 dominates. **Don't promise to beat a GPU here** — either
  route to a GPU-class supplier if the fleet has one, or quote honestly and compete on price,
  not wall-clock.
- **The crossover is around batch ~8–64** for these model sizes: below it the per-node gap
  is small; above it the A100's batching advantage compounds.

## Honest scope

- vLLM fp16 on the A100 vs the fleet's Candle/Hawking Q4 — different engine and precision.
  Q4 would lift the Mac's batch-1 number somewhat; it does not change the shape (A100
  batching advantage is compute-side, orthogonal to the Mac's bandwidth ceiling).
- `max_tokens=128`, synthetic corpus — a throughput/latency characterization, not a
  per-buyer-job promise. The single-stream (batch=1) rows are the decode-latency floor.
- These are single-A100 numbers. A supplier renting 8×A100 multiplies the throughput side
  again — reinforcing "don't race a datacenter on big batches."

## What this does for the thesis

`A100_REFERENCE_MEASURED.md` refuted "dozens of Macs beat an A100 on a big batch." This
sweep shows the **salvageable, honest version**: the fleet's real competitive lane is
**latency-sensitive and low-concurrency** work, where the A100's batching advantage is
dormant and a few nodes match it — plus availability and cost, which a rental can't touch.
That is a real, defensible marketplace position; it is just not "beat a GPU on throughput."
