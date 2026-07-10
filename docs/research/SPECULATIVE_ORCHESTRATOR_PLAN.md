# The Speculative Orchestrator — a general "draft → verify → gate" protocol for compute, and the experiment ladder to find what's real

*2026-07-06. Owner's thesis: speculative decoding shouldn't be LLM-only. "The same way you
can compress video (and any file) with AI, why can't you RENDER them with a small speculative
head? Render bits instead." This doc takes that seriously, reframes it into something buildable
and general, and lays out a prioritized experiment campaign to measure what's actually viable —
because ComputeExchange serves people rendering a long video they don't have the machine for,
not just AI people.*

---

## 1. The reframe: the general thing is a PROTOCOL, not a universal model

There is no single "speculative model" that works on every file format. But there IS a single
**protocol** that does, and it's what we build and fork:

```
draft(x)                 -> candidate            # cheap, fast, approximate
verify(candidate, x)     -> accept | residual | reject
accept_gate(evidence)    -> ship | correct | escalate-to-full
```

Every modality supplies its own `draft`, `verify`, and `gate`. The **orchestrator** — which
schedules drafts on cheap/fleet nodes, verification/correction on GPUs, batches them, and falls
back on rejection — is the general, format-agnostic thing. That is the product. It is not a
model; it is a *scheduler with a speculation contract*.

## 2. Why the owner's compression analogy is not loose — it's the theoretical backbone

Prediction and compression are the same math. An autoregressive model IS a compressor:
arithmetic-code the data under the model's next-symbol distribution and you get near-optimal
compression ("language modeling is compression," DeepMind 2023; a good predictor = a good
codec). So:

- **A model that can COMPRESS a file well can PREDICT (draft) its next chunk well.** The owner's
  intuition is exactly right at the level of principle.
- Speculative decoding is just: let a cheap predictor draft the next chunks, and let the
  expensive model verify them in one pass it was already paying for. **If a modality has a cheap
  predictor and an expensive "ground truth" generator, the draft→verify pattern applies.**

The catch — the honesty that keeps this real:
- **Lossless** verification (exact = what the target would have produced) exists ONLY for
  autoregressive-token generation (LLM text, AR audio/image/video tokens), via rejection
  sampling. That's the clean case.
- For **everything else** (diffusion images/video, path-traced 3D render, raw pixel frames),
  there is no free lossless verifier. Speculation becomes **quality-gated**: accept the cheap
  draft when a perceptual metric clears a tolerance, else spend more compute. This is *fine* for
  a marketplace that sells tiered quality — but it is lossy-with-a-gate, not a guarantee. We
  will never claim otherwise.

## 3. Per-modality instantiation of the protocol (where the owner's "render bits" lands)

| modality | draft (cheap) | verify (expensive / ground truth) | gate | lossless? |
|---|---|---|---|---|
| LLM text | small draft model / n-gram | big model single-pass verify | rejection sampling | **yes** |
| AR audio (Whisper decode, token TTS) | draft decoder | target decoder verify | rejection sampling | **yes** |
| Arbitrary FILE bytes | tiny byte/n-gram model | larger byte AR model | rejection sampling | **yes** (but weak/slow models) |
| **Video frames** | frame interp / motion-warp (DLSS-3-style) | full decode/render of the frame | SSIM/LPIPS tolerance | no (gated) |
| **3D / path-traced render** | low-spp / low-res pass | high-spp reference | predicted-residual variance | no (gated) |
| Diffusion image/video | few-step consistency/distilled draft | full-step diffusion | perceptual tolerance | no (gated) |

"Render bits instead" = the **residual/delta** idea, and it already ships in two forms we can
borrow: **P-frames** (video codecs render the *difference* from a predicted frame, not the whole
frame) and **1-spp + neural denoise** (render a noisy cheap draft, correct it with a small net —
OptiX/OIDN). The novel part isn't the per-frame trick; it's doing draft on the fleet and
verify/correct on a GPU, orchestrated and billed per quality tier.

## 4. The experiment ladder — measure viability cheapest-and-highest-signal first

Each experiment: a hypothesis, a method, ONE metric that decides it, a rough GPU cost, and the
bar that would make it worth productizing. Every run produces a committed artifact under
`docs/speed-lane-reports/` with real numbers (the standing discipline). Modeled ≠ measured.

**E0 — Reachable GPU + measurement harness (prerequisite).** A GPU we can drive directly
(SSH/SCP/tunnels — the current blocker is that this Mac can't route to many RunPod datacenters;
fix by pinning a reachable datacenter or provisioning from a cloud box). Harness: throughput +
per-modality quality (perplexity/acceptance for AR; SSIM/LPIPS/PSNR for pixels), money-safe
teardown. ~$1.

**E1 — LLM spec-dec anchor (known-good).** *Hypothesis:* vLLM native spec-dec (n-gram / EAGLE /
draft-model) gives ≥1.5× on decode-heavy mixes, lossless. *Metric:* tok/s speedup + acceptance
rate + byte-identical-to-greedy check. *Cost:* ~$2. *Bar:* >1.5×. *Purpose:* proves the harness
and the mechanism on the easy case; the ceiling reference.

**E2 — Arbitrary-file byte speculation (tests generality DIRECTLY).** *Hypothesis:* a tiny byte
draft accelerates a larger byte-level AR model over ANY file — text, image bytes, audio bytes,
binary — because prediction=compression holds for all of them. *Metric:* draft acceptance rate
by file type (does it stay >0 on non-text bytes?). *Cost:* ~$3. *Bar:* meaningful acceptance on
non-text bytes, even if absolute speed is modest — that's the "any file" proof of principle.
*Honesty:* byte models are slow in absolute terms; this tests the PRINCIPLE, not a shippable
codec.

**E3 — Video frame speculation (the "render a long video" use case).** *Hypothesis:* a cheap
interp/motion-warp draft can speculate K frames per full-rendered/decoded frame, gated by
SSIM/LPIPS, for a net speedup on temporally-coherent footage. *Metric:* net speedup at a fixed
quality tier + % frames accepted + failure modes (fast motion, cuts). *Cost:* ~$5. *Bar:* >1.3×
at a "good enough" tier on ordinary footage. *Grounding:* this is DLSS-3 frame-gen's premise;
we're testing it as a *distributable, gated* job.

**E4 — Low-spp + neural-correct render ("render bits").** *Hypothesis:* a cheap low-spp/low-res
draft + learned residual/denoise verify approaches full-quality at a fraction of the compute,
with a variance gate spending more only where needed. *Metric:* quality (PSNR/LPIPS) vs compute
vs a real adaptive-sampling baseline. *Cost:* ~$6. *Bar:* matches adaptive sampling AND the
draft can run on a cheaper/fleet node than the verify. *This is where 3D/film rendering enters.*

**E5 — The distributed orchestrator (the actual product).** *Hypothesis:* one protocol
(`draft/verify/gate`) with per-modality plugins gives a consistent speculate-cheap/verify-dear
speedup, and the version where **drafts run on the cheap fleet and verification runs on a GPU**
is ComputeExchange's structural edge (nobody else has both halves). *Metric:* distributed
wall-clock vs single-node, per modality. *Cost:* ~$8. *Bar:* the distributed split beats
single-node on ≥2 modalities. *This is the fork target.*

## 5. What we fork, and how aggressively

- **Fork vLLM's scheduler + continuous-batching + spec-dec machinery** as the reference
  orchestrator for E1/E2 (it already does draft/verify/batching well). Generalize its notion of
  "a sequence of tokens" into "a drafted output stream of any kind."
- vLLM is the WRONG engine for E3/E4 (it's an LLM server) — those plug a video codec / renderer /
  denoiser into the SAME orchestration protocol. So the fork's value is the *scheduler and the
  speculation contract*, not vLLM's model runtime.
- CUDA-first, per the owner: rented GPUs let us iterate far faster than one Mac, and vLLM's
  spec-dec is CUDA-mature. Apple-lane ports come after a mechanism proves out.

## 6. The honest risk register (where this likely breaks)

- **High-entropy / low-redundancy data** (already-compressed files, noise): the draft can't
  predict → acceptance ~0 → no speedup. Speculation only pays where the data is predictable.
- **Hard cuts / scene changes / fast motion** in video: draft fails, gate rejects, you pay full
  cost anyway (plus the wasted draft). Net can go negative — exactly what happened to per-node
  LLM spec-dec on the Mac (measured net-NEGATIVE, `PERF_AND_CAPABILITY_AUDIT.md`). The gate must
  make rejection cheap.
- **No lossless verify outside AR tokens:** everything pixel/render is a quality-tier promise,
  not a guarantee. The product must sell it that way.
- **The draft has to be genuinely cheaper AND run somewhere cheaper** or the distributed edge
  evaporates. E5 is the make-or-break.

## 7. Prerequisite / current blocker

Running E1–E5 needs a GPU this environment can reach directly. Today's session proved the Mac
can't route to many RunPod datacenters (only the RunPod HTTP proxy is reliably reachable, which
is fine for serving but not for SSH/SCP/persistent processes). Fix before E-runs: pin a reachable
datacenter, provision from a cloud box, or the owner provisions in a region known-reachable (the
owner's earlier pods at 154.x / 216.x WERE reachable). Then E1 → E2 → E3 in order; stop at the
first rung that fails its bar and report, per the loop discipline.
