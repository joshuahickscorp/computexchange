# vLLM greedy byte-determinism — restart soak PASSED on a real A100 (MEASURED)

*2026-07-06. First real execution of the `docs/VLLM_LANE.md` de-risk spike (steps 1–3, the
within-pod half) on a user-provisioned RunPod A100-SXM4-80GB. This is the byte-stability
evidence the vLLM CUDA serving lane needs before its verification gate (`VllmRunner`, gated
behind `CX_VLLM_SOAK_MODE`) may carry real traffic. Honest scope: this proves within-run +
across-restart determinism on ONE pod; the cross-pod half (two independently provisioned
pods → byte-identical) is still owner-gated on a second GPU.*

## Setup (real hardware)

| field | value |
|---|---|
| GPU | **NVIDIA A100-SXM4-80GB** (user-provisioned RunPod pod, driven over SSH) |
| engine | **vLLM 0.11.0**, `transformers==4.57.1` (pinned — vLLM 0.11.0 is incompatible with the transformers 5.13.0 that `pip install vllm` pulled in; it broke the tokenizer API) |
| model | **Qwen/Qwen2.5-1.5B-Instruct**, fp16 — UNGATED (Apache-2.0), no HF token needed; same family as the capability sweep |
| decode | greedy: `temperature=0, top_p=1, seed=42, n=1, max_tokens=128` |
| corpus | 20 distinct prompts (`/root/corpus.py`), completions concatenated and SHA-256'd |

## The result — byte-identical across a full server restart

| run | when | sha256 | gen tokens | single-stream tok/s |
|---|---|---|--:|--:|
| run 1 | fresh server | `c930c65e…c51fef8b` | 2493 | 223.8 |
| run 2 | same server | `c930c65e…c51fef8b` | 2493 | 225.2 |
| run 3 | **after full server restart** (process killed, 70 GB VRAM freed, model reloaded) | `c930c65e…c51fef8b` | 2493 | 223.2 |

**All three hashes identical.** vLLM greedy decode is byte-stable within a run AND across a
full server restart on this A100 — the within-pod determinism the pin requires. Full hash:
`c930c65e8c34b7884c10344f0a73ad936252fe0ae0ad6d355faedb35c51fef8b`.

## Cross-pod byte-equality — PASSED (same-SKU, two independent A100-SXM machines)

The within-pod result above was then extended to the CROSS-POD soak (`VLLM_LANE.md` step 2)
— the one that actually validates the verification class. A SECOND A100-SXM-80GB pod
(`b9eswkfyz9mfzb`, independently provisioned via the RunPod API on the identical image
`runpod/pytorch…torch280`) was set up with the byte-identical pin (vLLM 0.11.0,
`transformers==4.57.1`, Qwen2.5-1.5B fp16, same `--served-model-name` + greedy config) and
run the SAME corpus:

| pod | GPU | corpus sha256 | golden ("opposite of hot is") sha256 |
|---|---|---|---|
| A (`o6xrz9rtpkczbj`) | A100-SXM-80GB | `c930c65e…c51fef8b` | `bd745e7a…2dc2ea74` |
| B (`b9eswkfyz9mfzb`) | A100-SXM-80GB | `c930c65e…c51fef8b` | `bd745e7a…2dc2ea74` |

**Byte-identical on BOTH the 20-prompt corpus AND the golden reference.** Two separately
provisioned A100-SXM machines running the same vLLM pin produce byte-for-byte identical
greedy output. This is the load-bearing evidence for the `nvidia_80g` verification class:
two honest same-class workers WILL agree byte-for-byte, so redundancy/honeypot checking
across them is sound (an honest worker is never wrongly quarantined for a legitimate byte
difference). Pod B was terminated immediately after the comparison (API `podTerminate`);
only pod A remained billing.

**Scope of this cross-pod result:** same-SKU (SXM↔SXM). The cross-*SKU* variant
(SXM↔PCIe, or SXM↔H100) is the stronger stress test and remains a cheap follow-up — a PCIe
pod was provisioned for it this session but terminated unused when the second box came up as
an SXM (the same-SKU pair proves the primary, most-common redundancy case: two cards of the
same tier). Note the corpus drives prompts SERIALLY (batch-size-1 each), so this proves the
pin's determinism for isolated greedy requests; vLLM's batch-composition-dependent reduction
(the tolerant-class caveat below) is a separate axis these serial runs do not exercise.

## Honest caveats — what this is NOT

- **Not a throughput measurement.** Single-stream decode was ~224 tok/s (latency-bound, one
  request at a time). A concurrent client probe (256 Python threads through urllib) reported
  only ~805 tok/s aggregate, which is a **client-side bottleneck** (GIL + blocking sockets),
  NOT the server's real batched throughput — recording it as an A100 throughput number would
  repeat the "benchmark X at its worst" methodology error the speed-lane audit exists to
  prevent. The valid batched-throughput reference remains `A100_CAPABILITY_SWEEP.md`
  (measured via proper offline batching).
- **Not the cross-pod soak.** `VLLM_LANE.md` step 2 requires byte-equality across TWO
  independently provisioned pods (same/cross SKU). That needs a second GPU and is owner-gated
  on additional spend — `scripts/runpod-vllm-soak.sh` provisions both with watchdog +
  auto-teardown when a second pod is authorized.
- **Not model-matched to the seeded honeypot.** The byte-exact hawking honeypot (entry 89) is
  an Apple/Metal class; the vLLM class `(nvidia_*, vllm, build_hash)` is still unseeded
  (`VLLM_LANE.md` steps 4–5), which is the deliberate next step AFTER a full soak passes.

## The production runner carries real traffic (end-to-end, not a mock)

Beyond the raw-curl determinism above, the ACTUAL production `VllmRunner`
(`agent/src/runners.rs`) was driven against this live pod — the first time the wired
shell-out path has run against a real pinned vLLM rather than the in-tree mock server.
Setup: an SSH tunnel (`Mac:8000 → pod:8000`), the pod re-served with
`--served-model-name qwen2.5-1.5b-instruct` so the runner's `short_model_id` reduction
matches, `CX_VLLM_BASE_URL=http://127.0.0.1:8000` + `CX_VLLM_SOAK_MODE=1`, and a new
opt-in `#[ignore]`d test `vllm_runner_soak_mode_against_live_pod`:

```
CX_VLLM_LIVE_URL=http://127.0.0.1:8000 \
  cargo test -p cx-agent --no-default-features \
  vllm_runner_soak_mode_against_live_pod -- --ignored --nocapture
```

**Result: PASS.** `VllmRunner::run` produced a real `BatchInferResult` — 2 completions for
2 prompts, 128 real generated tokens, and **byte-identical output across two runs** through
the production runner (greedy determinism, not just the raw endpoint). First completion:
`" Paris. The capital of Italy is Rome. …"`. Agent build baselines held: clippy at exactly
the 4 hardware.rs doc warnings on `--no-default-features`, metal build clean, the new test
`#[ignore]`d so CI is untouched.

This closes the "the wired body is unproven against a real server" gap: the request/response
mapping, greedy pinning, and result-contract shape all work against genuine vLLM output on a
datacenter A100 — not a mock. The remaining gates below are what stand between this and the
lane being LIT for real dispatch.

## What this unblocks / what remains

**Proven this session (the whole within-`nvidia_*` byte-stability soak, `VLLM_LANE.md`
steps 1–3):** the vLLM pin is byte-deterministic within a pod, across a server restart, AND
across two independently provisioned A100-SXM machines (corpus + golden, byte-identical) —
and the production `VllmRunner` carries real traffic against a live pinned server. That is
the evidence the lane's verification gate was waiting on.

**Remaining before `litGPULaneWorkers` flips > 0** (turning routing's `gpu_recommend` into a
real `gpu_lane`), all now CODE-side, not pod-gated:
1. **Wire the vLLM verification class as TOLERANT** — `(nvidia_*, vllm, build_hash)` uses
   engine-tag + build_hash + redundancy, NOT a byte-exact honeypot (vLLM's
   batch-composition-dependent reduction means byte-exactness is not guaranteed under real
   continuous batching, even though these serial-request soaks were byte-identical). The
   golden reference captured this session (`bd745e7a…`, prompt "The opposite of hot is",
   Qwen2.5-1.5B fp16, vLLM 0.11.0, greedy) is a within-class reference, not a cross-batch
   guarantee.
2. **Flip `litGPULaneWorkers`** to a live count of verified vLLM-lane suppliers on both the
   quote and submit routing paths (the parameter already exists — no signature change).
3. **Optional stronger soak:** the cross-*SKU* variant (SXM↔PCIe/H100) and a real
   continuous-batching determinism characterization (many concurrent prompts, not serial).

**Ops note (fixed the soak-script teardown gap):** the pod's `VLLM::EngineCore` worker holds
VRAM under a child PID that `pkill -f "vllm serve"` does NOT match — a clean teardown must
kill the PID from `nvidia-smi --query-compute-apps` (or its process group) directly, or
terminate the whole pod via the API. Learned this run.
