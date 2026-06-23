# RunPod CUDA Spike — the $5 validation of the NVIDIA lane

The cheapest, most reversible test of one question:

> **Does our exact inference stack (Candle runners: MiniLM embed, quantized
> Llama, whisper) compile and run on a real NVIDIA GPU, and complete the full CX
> job lifecycle (register → poll → infer → commit → verify)?**

This is the expensive-to-be-wrong thing. Everything else about the NVIDIA lane
(supply classes, detection, per-class honeypots, a signed image) is cheap
productization that only matters *after* this passes. So we spend $5 to answer it
first.

## ✅ Spike result — PASS (A100-80GB, 2026-06-23)

Ran end-to-end on a RunPod A100 SXM (CUDA 12.8 toolkit already on the image,
driver 580), for ~$0.50 of GPU time.

- `cargo build --release --no-default-features --features cuda` — **succeeded in
  1m 25s, zero errors** (candle-core/nn/transformers + cx-agent all linked against
  CUDA). The `cuda` feature + `Device::new_cuda(0)` branch compile on real hardware.
- Agent logged **`compute device: CUDA (NVIDIA GPU)`** and ran the real runners on
  `device="cuda"` with weights pulled from HuggingFace:
  - `all-minilm-l6-v2` — **3,213 embeddings/sec**, p99 4 ms
  - `llama-3.2-1b-instruct-q4` — **170.8 tokens/sec**, p99 7 ms
- Expected caveat confirmed: the agent advertised `hw_class=cpu` (x86 box) while
  genuinely running on the GPU — the cosmetic label the follow-on horizon change fixes.

Conclusion: the inference stack is real on NVIDIA; enabling the lane is a feature
flag, not a rewrite. Next step is the `nvidia_*` horizon change (below) to productize it.

## Why CUDA is a feature flag, not a rewrite

The runners in `agent/src/runners.rs` never name a device — they call
`models::device()`. Device selection lives in one place (`agent/src/models.rs`).
Candle ships a `cuda` backend that mirrors its `metal` backend. So the entire
NVIDIA enablement is:

- `agent/Cargo.toml` — a `cuda` feature mirroring `metal` (**landed**).
- `agent/src/models.rs` — a `Device::new_cuda(0)` branch + honest `cuda`
  telemetry label (**landed**, validated against the default build).

The CUDA branch body can only be compiled on a CUDA box, so **the pod's first
build is its first real check.**

### This inverts "Why no agent image"

The README explains there is deliberately no Dockerfile for the agent because
**Metal is unavailable in Linux containers.** For CUDA that reasoning *reverses*:
CUDA runs in Linux containers — that is exactly how RunPod works (Linux + the
NVIDIA Container Toolkit). So a CUDA agent is the one supply target that *can* be
containerized. We don't build that image for the spike (we build on the pod), but
it's the natural deployment unit when the lane is productized.

## Topology (control on your Mac, CUDA agent on the pod)

We test the real product scenario — a remote supplier machine joining a running
exchange — not "everything on one box." The control stack stays on the
already-proven Mac path; only the agent is remote and on CUDA.

```
  Your Mac (make up)                          RunPod pod (1× cheap NVIDIA GPU)
  ┌───────────────────────────┐              ┌──────────────────────────────┐
  │ control plane  :8080  ─────┼── tunnel ───▶│  cx-agent (built --features   │
  │ MinIO (S3)     :9000  ─────┼── tunnel ───▶│  cuda) → real CUDA inference  │
  │ Postgres                   │   (HTTP)     │  register · poll · commit     │
  └───────────────────────────┘              └──────────────────────────────┘
        ▲ submit jobs (cx CLI / curl)
```

The agent reaches **two** endpoints, so expose both:

- **control plane :8080** → `CX_CONTROL_URL` on the pod.
- **MinIO :9000** → `S3_PUBLIC_ENDPOINT` on the *Mac's control plane* (presigned
  URLs are signed against the address the agent reaches, so this must be set
  **before** `make up`).

Use `cloudflared` quick tunnels (free, no account, two of them):
`cloudflared tunnel --url http://localhost:8080` and
`cloudflared tunnel --url http://localhost:9000`.

## The $5 budget

Models are tiny (MiniLM, Llama‑3.2‑1B Q4 ≈ 1 GB, whisper‑tiny), so any CUDA GPU
works — **do not** rent a powerful one.

| Pick | Rate (community cloud) | Notes |
|---|---|---|
| RTX A4000 16 GB | ~$0.17–0.20/hr | cheapest sane option |
| RTX 3090 24 GB | ~$0.22–0.30/hr | lots of headroom |
| RTX 4090 24 GB | ~$0.34–0.44/hr | overkill; still fine |

**Expected spend ≈ $0.10–0.40.** Build (~10–15 min compiling Candle's CUDA
kernels) + run (~15–30 min) ≈ ½–1 GPU‑hour. The $5 ceiling is ~12–25 hours — you
will not approach it in a focused session.

**Hard guardrails (so $5 is a ceiling you can't trip over):**
1. **Community Cloud**, **On‑Demand** (or Spot, cheaper), ≥12 GB VRAM, ≤$0.40/hr.
2. **No persistent network volume** (or <10 GB) — volumes bill even while the pod
   is *stopped*. Use ephemeral container disk.
3. Set an account **spending limit** in RunPod billing if available.
4. **Terminate** (not just stop) the pod the moment the run prints its result.
5. Pick a template that already ships the **CUDA toolkit / `nvcc`** (e.g. a RunPod
   CUDA or PyTorch devel image) — Candle's `cuda` feature compiles GPU kernels.

## Runbook

**On the Mac:**
```bash
# 1. Two free tunnels (separate shells); copy the printed https URLs.
cloudflared tunnel --url http://localhost:8080   # -> CONTROL_URL
cloudflared tunnel --url http://localhost:9000   # -> S3_PUBLIC_URL

# 2. Point presigned URLs at the tunnel, then bring up control+PG+MinIO.
export S3_PUBLIC_ENDPOINT=<S3_PUBLIC_URL>
make up
make seed            # prints api_key + worker_token
```

**On the pod (CUDA template with nvcc):**
```bash
curl https://sh.rustup.rs -sSf | sh -s -- -y && . "$HOME/.cargo/env"
git clone <repo> cx && cd cx/agent
cargo build --release --no-default-features --features cuda   # first build = the real CUDA check
CX_CONTROL_URL=<CONTROL_URL> CX_WORKER_TOKEN=<token> \
  ./target/release/cx-agent run
```
Watch the agent log: it must print `compute device: CUDA (NVIDIA GPU)` — if it
says CPU, the device failed to open and the log says why (no silent fallback).

**Back on the Mac — drive work to the remote CUDA worker:**
```bash
# confirm the worker registered (note its advertised hw_class — see caveat)
curl -s -H "Authorization: Bearer <api_key>" localhost:8080/admin/workers

# submit one of each live workload at a tier/class the worker satisfies
cx submit --type embed ...
cx submit --type batch_infer ...
cx submit --type audio_transcribe ...
```

## Pass criteria

- Pod build of `--features cuda` **succeeds** (the real compile check).
- Agent logs `compute device: CUDA`.
- All three jobs reach `complete`, results download, and the verifier accepts them
  (cosine ≥ 0.999 for embed; the commit isn't clawed back).
- `cx_*` metrics advance; the worker shows `device=cuda` telemetry.

If all four hold, the NVIDIA lane is real and we productize it. If the build or
inference fails, we learned it for ~$0.30 instead of after building the class
plumbing.

## Honest caveat (and the follow-on)

For this minimal spike the agent advertises **`hw_class = cpu`** — `hardware.rs`
`classify()` only recognizes Apple Silicon brand strings and falls back to `cpu`
on x86. It still runs inference on the GPU (telemetry honestly reports
`device = cuda`). The `cpu` label is cosmetic for the spike; submit jobs the
`cpu`-class worker is eligible for.

**Productizing the lane (do this only after the spike passes) — the Plane‑B-style
horizon change, in lockstep across the three contract files:**
1. `proto/manifest.schema.json` — add NVIDIA class(es) to the `hw_class` enum
   (VRAM‑tiered is cleaner than card names: e.g. `nvidia_24gb`, `nvidia_80gb`).
2. `agent/src/types.rs` — matching `HardwareClass` variant(s) + serde rename.
3. `control/types.go` — matching constant(s).
4. `agent/src/hardware.rs` — detect CUDA (probe `nvidia-smi` for real VRAM) and
   return the NVIDIA class instead of `cpu`.
5. Control side — seed models for the class and make **honeypots per‑class**:
   floating‑point kernels differ across architectures, so an Apple‑baked
   known‑answer may not match a CUDA result. Keep redundancy **within‑class**
   (it already is); never cross‑compare Apple vs NVIDIA results byte/cosine.
6. Add a repeatable `prove-cuda` path mirroring `scripts/prove-local.sh`.

**Strategic note:** NVIDIA is a TAM / single‑API‑across‑supply play, *not* where
we win — on commodity GPU we're a late entrant against Vast/RunPod. Apple Silicon
stays the wedge and the brand; NVIDIA is "we also run anywhere."
