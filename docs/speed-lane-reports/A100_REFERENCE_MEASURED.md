# Single-node reference, MEASURED on a real A100 SXM — and what it does to the fan-out thesis

*2026-07-06. The L3 reference half of the fan-out moat proof
(`FANOUT_PLANNER_WAVE1B.md` §6), run for real on a rented A100. This is the number
the fleet has to beat — and the measurement REFUTES the modeled break-even. Receipts
over hopes, even when the receipt is unwelcome.*

## What was measured (real hardware, real engine)

| field | value |
|---|---|
| GPU | **NVIDIA A100-SXM4-80GB** (RunPod community, user-provisioned pod, driven over SSH) |
| engine | **vLLM** (the strongest realistic single-node baseline — what a buyer actually rents an A100 to run) |
| model | TinyLlama-1.1B-Chat (ungated, fp16) — a 1.1B stand-in matched to the fleet's ~1B class |
| batch | **10,000 prompts × 256 tokens**, `ignore_eos` → exactly 2,560,000 fixed-work token-gens |
| **T_ref (end-to-end wall-clock)** | **57.83 s** |
| **aggregate throughput** | **44,269 tok/s** |

Raw artifact: `artifacts/a100-sxm-reference-2026-07-06.json`.

## The finding: the modeled curve understated the A100 by ~19×

The wave-1B modeled fleet-vs-A100 curve (`FANOUT_PLANNER_WAVE1B.md` §3) was calibrated
with **A100 = 2345 tok/s**. That figure was NOT a vLLM number — it was *our own Candle
CUDA bench at batch 64* (a much weaker A100 configuration). A real A100 SXM running
vLLM with the whole 10k-prompt batch to schedule does **44,269 tok/s — about 19× the
number the curve rested on.**

That moves the break-even wholesale:

| baseline | per-node tok/s | break-even N vs this A100 (44,269 tok/s) | was modeled |
|---|---|---|---|
| M3 Pro (measured real-traffic) | 139 | **≈ 318 nodes** | 18 |
| M4 Max (research, 1B) | ~460 | **≈ 96 nodes** | ~5 |

**The "~18 Macs beat an A100 on wall-clock" headline does not survive contact with a
real vLLM A100 for a small model.** For a 1B-class model, a single modern datacenter
GPU with a production serving engine is extremely hard to beat with a consumer fleet on
wall-clock throughput — you'd need hundreds of M3-Pro-class nodes, not dozens.

## Fair caveats (none of which close a 19× gap)

- **Engine:** A100 ran vLLM; the fleet runs Candle/Hawking. vLLM's continuous batching
  at 10k concurrency saturates the GPU far better than our batch-64 Candle bench did —
  which is precisely why the self-generated 2345 baseline was misleadingly low.
- **Precision:** A100 fp16 vs the fleet's Q4. Q4 is cheaper per token on bandwidth-bound
  decode, but the A100 at 44k tok/s is compute-saturated via batching, not
  bandwidth-bound; Q4 on the Mac is already baked into the 139 tok/s.
- **Model:** TinyLlama-1.1B vs Llama-3.2-1B — same size class, not the driver of a 19× gap.
- **Best-case-for-the-A100 by design:** the A100 got the entire 10k-prompt batch to
  schedule. That IS the competitive scenario for a big batch, so it's the honest number,
  not a strawman in either direction.

## What survives, and what has to change

**Survives:** the wave-1B fan-out SCHEDULING work (CREED entry 87) is still real and
valuable *for the marketplace* — node-rate-weighted sizing, adaptive-N, and endgame
racing cut measured control-plane wall-clock 2.7× and make the fleet, whatever its size,
finish sooner. That's a genuine marketplace improvement independent of the A100 comparison.

**Has to change:** the specific thesis headline — "a couple dozen Macs beat a rented
A100 on your batch's wall-clock" — is REFUTED for small models by this measurement and
must be retired or re-scoped. Candidate re-scopings, each needing its own evidence, none
of which is the current thesis:
- **Cost, not wall-clock** — the fleet is idle capacity; an A100 is $1-2/hr. But the
  owner's stated thesis was explicitly *time, not cost*, so this is a pivot, not a rescue.
- **Availability** — "beat the A100 you *can't get right now*", not the one you can rent.
- **Model sizes where the A100 is memory-bound** (large models) — but the Mac fleet is
  itself capped at ~7B, so this doesn't obviously favor the fleet either.

The honest next step is the owner's call: the fan-out engineering is sound and shipped,
but the "beat an A100 on wall-clock" framing needs to be dropped or re-pointed before it
goes in front of a buyer. This measurement is exactly what the L3 reference run was for —
to test the thesis against reality before building a moat on a false premise.
