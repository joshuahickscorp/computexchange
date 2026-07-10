# Spec Render + Token Decode Iteration - 2026-07-09

## Scope

This report keeps the two speculation branches separate:

- pixel/render speculation: quality-gated draft/verify/refine over render outputs;
- token speculation: exact draft/verify/repair over token streams.

The token rows below are local protocol receipts, not vLLM/Hawking model throughput claims.
They exist to prove the branch/prune loop and receipt schema before paid GPU speculative decode runs.

## Current Renderer Receipts

- Render-only friendly floor: `14.3372x`.
- Render-only hard-scene delivery floor: `7.8721x`.
- Batch crop tile-refine scaffold: `3.4897x` preview, not a hard-scene winner.

No renderer speedup in this report is multiplied by token speculative decode. Multipliers are only comparable
after an end-to-end workload uses both branches in the same delivered path.

## Local Token Spec Decode Receipt

- Backend: `local_ngram_token_protocol`.
- Claim scope: local protocol receipt; not model-backed LLM throughput.
- Rows: `20`; lossless rows: `20`.
- Grow rows: `10`; prune rows: `5`.
- Best local protocol speedup: `28.8659x` on `repeat`
  with `num_spec_tokens=32`, acceptance `1.0`,
  target-call reduction `32.0x`.

## Scenario Bests

| Scenario | Best speedup | k | Acceptance | Action |
| --- | ---: | ---: | ---: | --- |
| `code` | `6.5565x` | `32` | `0.2018` | `park` |
| `json` | `2.5539x` | `32` | `0.0603` | `prune` |
| `prose` | `28.2515x` | `32` | `0.972` | `grow` |
| `random` | `0.4999x` | `4` | `0.0` | `prune` |
| `repeat` | `28.8659x` | `32` | `1.0` | `grow` |

## Branch Decisions

Grow:
- `repeat` k=`32`: `28.8659x`, acceptance `1.0`, lossless `True`.
- `prose` k=`32`: `28.2515x`, acceptance `0.972`, lossless `True`.
- `repeat` k=`16`: `15.5151x`, acceptance `1.0`, lossless `True`.
- `prose` k=`16`: `14.8208x`, acceptance `0.9745`, lossless `True`.
- `repeat` k=`8`: `7.6771x`, acceptance `1.0`, lossless `True`.

Prune:
- `random` k=`32`: `0.1104x`, acceptance `0.0`.
- `random` k=`16`: `0.2003x`, acceptance `0.0`.
- `random` k=`8`: `0.3409x`, acceptance `0.0`.
- `random` k=`4`: `0.4999x`, acceptance `0.0`.
- `json` k=`32`: `2.5539x`, acceptance `0.0603`.

## Real Model-Backed vLLM Receipt

Runner: `scripts/spec-lab/pod/exp_ar_vllm.py`, via `python3 scripts/spec-lab/orchestrator.py --max-minutes 90 --only A1-ngram`.

Implementation repair:

- Changed the runner so baseline and speculative vLLM engines run in isolated child processes.
- Result: the previous `Engine core initialization failed` boundary became measurable on H100 PCIe.

Paid run:

- GPU: RunPod secure `NVIDIA H100 PCIe`.
- Stack: `torch 2.8.0+cu128`, `vllm 0.11.0`, `transformers 4.57.1`.
- Pod: `ctp075wfgckvmi`; ledger lines `114-122` in `ledger.jsonl`.

Measured A1 no-draft speculative decode variants:

| Variant | Method | k | Prompts/max tokens | Speedup | Acceptance | Exactness | Decision |
| --- | --- | ---: | --- | ---: | ---: | --- | --- |
| `base` | `ngram` | `5` | `64 / 128` | `0.3341x` | `0.2061` | `46/64` mismatched | prune |
| `spec3` | `ngram` | `3` | `64 / 128` | `0.421x` | `0.3025` | `48/64` mismatched | prune |
| `spec8` | `ngram` | `8` | `64 / 128` | `0.4948x` | `0.1358` | `45/64` mismatched | prune |
| `suffix8` | `suffix` | `8` | `64 / 128` | n/a | n/a | unsupported by pinned `vllm==0.11.0` | park for version-upgrade test |
| `decode-heavy` | `ngram` | `5` | `32 / 512` | `0.68x` | `0.3801` | `29/32` mismatched | prune |

Decision: prune the pinned `vllm==0.11.0` no-draft branch for now. It is slower than baseline
and violates exact greedy equivalence on every measured n-gram variant. The next real token branch
is either `draft_model` on the isolated runner or a vLLM version-upgrade branch where suffix decoding
is actually accepted by the installed wheel. The latest vLLM documentation lists both n-gram and
suffix as lightweight speculation methods, but the pinned `0.11.0` wheel rejected `method="suffix"`.
Reference: `https://docs.vllm.ai/en/latest/features/speculative_decoding/`.

## Real Model-Backed vLLM Draft-Model Receipt

Runner: `scripts/spec-lab/pod/exp_ar_vllm.py`, via `python3 scripts/spec-lab/orchestrator.py --max-minutes 100 --only A2-draft`.

Paid run:

- GPU: RunPod secure `NVIDIA H100 80GB HBM3`.
- Stack: `torch 2.8.0+cu128`, `vllm 0.11.0`, `transformers 4.57.1`.
- Pod: `2n0z0dmmss05ce`; ledger lines `123-129` in `ledger.jsonl`.

Measured A2 variants:

| Variant | Method | k | Speedup | Acceptance | Exactness | Decision |
| --- | --- | ---: | ---: | ---: | --- | --- |
| `base` | `draft_model` | `5` | n/a | n/a | unsupported | park |
| `spec4` | `draft_model` | `4` | n/a | n/a | unsupported | park |
| `fallback-ngram` | `ngram` | `5` | `0.406x` | `0.1984` | `41/64` mismatched | prune |

Decision: park pinned `vllm==0.11.0` draft-model speculation because the wheel raises
`NotImplementedError: Speculative decoding with draft model is not supported yet`. The next vLLM
branch should be a version-upgrade branch targeting a wheel that supports suffix/draft-model
speculation, or a supported method from the installed enum (`eagle`, `medusa`, `mtp`) with a real
compatible artifact. Do not rerun A2 on `vllm==0.11.0`.

## vLLM Version-Upgrade Compatibility Receipts

Runner changes:

- `setup_base.sh` now accepts `SPEC_LAB_VLLM_VERSION` and `SPEC_LAB_TRANSFORMERS_VERSION`.
- `orchestrator.py` passes those setup overrides to the pod and records them in `ledger.jsonl`.
- A5/A6 rungs now isolate version-selected suffix and draft-model branches.
- `exp_ar_vllm.py` now preserves child `stderr_tail` whenever a phase emits an error.

Paid compatibility runs:

| Branch | GPU | Stack | Ledger lines | Result | Decision |
| --- | --- | --- | --- | --- | --- |
| A5/A6 `latest` | secure `NVIDIA A100 80GB PCIe` | `torch 2.11.0+cu130`, `vllm 0.24.0`, `transformers 5.13.0` | `130-141` | baseline engine init failed before suffix/draft measurement | park |
| A5/A6 `0.20.2` | secure `NVIDIA L40S` | modern vLLM branch; stderr shows torch CUDA init requires newer driver than host `12080` | `142-153` | baseline engine init failed before suffix/draft measurement | park |

The `0.20.2` run captured the root cause: `RuntimeError: The NVIDIA driver on your system is too old
(found version 12080)`. Therefore these are not speculative-decode performance failures. The branch is
blocked at stack/driver compatibility: either run the modern vLLM branch on a newer-driver host, or build
a CUDA-12.8-compatible torch/vLLM stack before spending more A5/A6 time.

<!-- CX_NATIVE_SPINE_START -->
## CX-Native Speculation Spine

This is the primary branch now: ComputeExchange-owned `SpecUnit -> DraftProducer -> Verifier -> AcceptancePolicy -> RepairPolicy -> SpecReceipt` machinery.
vLLM, Hawking, Cycles, ffmpeg, and future custom kernels are reference material or accelerators to mine, not the architecture boundary.

Local CX-native token unit receipt:

- Rows: `374`.
- Ungated rows: `59`; fixed confidence-gated rows: `98`; adaptive rows: `217`.
- Span-adaptive rows: `54`; prefix-accept adaptive rows: `163`.
- Prefix proposal rows: n-gram `40`; copy-match `65`; copy-runway `34`; JSON-template `24`.
- Grow rows: `240`; park rows: `31`; prune rows: `103`.
- Best preferred CX-native token speedup: `315.460911x` on `repeat` with unit size `8192`; action `grow`.
- Best-row gate: confidence threshold `0.55`; attempted fraction `1.0`; fallback fraction `0.0`.
- Accepted total fraction: `1.0`; accepted attempted fraction: `1.0`; exact: `True`.
- Claim scope: local CX protocol receipt, not model-backed LLM throughput.

Confidence-gated token receipt:

- Best gated row: `cx_native_token_gate_repeat_k8192_c0p55` at `315.460911x`.
- Attempted fraction: `1.0`; fallback fraction: `0.0`.
- Accepted attempted fraction: `1.0`; target-call reduction: `6144.0x`.

Adaptive token receipt:

- Best adaptive row: `cx_native_token_prefix_copy_runway_adaptive_repeat_m3_q512_w256x128x64x32x16x8x4x2_c0p55` at `258.594191x`.
- Widths: `[256, 128, 64, 32, 16, 8, 4, 2]`; threshold: `0.55`.
- Attempted fraction: `1.0`; fallback fraction: `0.0`; accepted attempted fraction: `1.0`.
- Target-call reduction: `256.0x`; chosen width mean: `256.0`.

Prefix-accept adaptive token receipt:

- Best prefix row: `cx_native_token_prefix_copy_runway_adaptive_repeat_m3_q512_w256x128x64x32x16x8x4x2_c0p55` at `258.594191x`.
- Widths: `[256, 128, 64, 32, 16, 8, 4, 2]`; threshold: `0.55`.
- Proposal source: `copy_runway`; strategy: `prefix_accept_copy_runway`.
- Accepted token fraction: `1.0`; draft-token acceptance: `1.0`.
- Target-call reduction: `256.0x`; verifier calls: `24`.

Copy-match prefix token receipt:

- Best copy-match prefix row: `cx_native_token_prefix_copy_adaptive_repeat_m4_w256x128x64x32x16x8x4x2_c0p9` at `155.149457x`.
- Min match: `4`; proposal sources: `{'copy_match': 6144}`.
- Accepted token fraction: `1.0`; target-call reduction: `256.0x`.
- Draft pressure: `low`; exact: `True`; action: `grow`.

Copy-runway prefix token receipt:

- Best copy-runway prefix row: `cx_native_token_prefix_copy_runway_adaptive_repeat_m3_q512_w256x128x64x32x16x8x4x2_c0p55` at `258.594191x`.
- Min match: `3`; candidate depth: `512`; proposal sources: `{'copy_runway': 6144}`.
- Accepted token fraction: `1.0`; target-call reduction: `256.0x`.
- Draft pressure: `low`; exact: `True`; action: `grow`.

JSON-template prefix token receipt:

- Best JSON-template prefix row: `cx_native_token_prefix_json_template_json_w4096x2048x1536x1024x768x512x384x256x128x64x32x16x8x4x2_c0p75` at `229.780843x`.
- Widths: `[4096, 2048, 1536, 1024, 768, 512, 384, 256, 128, 64, 32, 16, 8, 4, 2]`; proposal sources: `{'json_template': 10674, 'ngram': 37}`.
- Accepted token fraction: `0.998698`; target-call reduction: `361.411765x`.
- Draft pressure: `medium`; exact: `True`; action: `grow`.

Measured render receipt adapted into the same CX receipt shape:

- Render adapter rows: `78`; grow `7`, park `69`, prune `2`.
- Branch: `cx_native_render_adapter_oidn_scene_world_volume.xml`.
- Scene: `scene_world_volume.xml`; variant: `oidn`.
- Tier: `delivery`; speedup: `12.9695x`; action: `grow`.
- Claim scope: imported measured render receipt, not a new render.

Next branch: grow the copy-match/copy-runway/JSON-template prefix predictors where they beat n-gram, and split into hotter bounded-index/selector and copy-aware confidence branches before any dependency-centered probe.
<!-- CX_NATIVE_SPINE_END -->




























## Modality-General Local Receipts

- Protocol smoke: `scripts/spec-lab/pod/exp_protocol.py` ran one `GeneralSpeculator`
  across `ar`, `bytes`, `video`, and `render`; `modalities_ok=4/4`. The render plugin
  rejected and repaired its draft, which is correct protocol behavior. No speed claim.
- Byte speculative decode: `scripts/spec-lab/pod/exp_bytes_specdec.py` at `8192` bytes,
  `draft_ctx=8`, measured acceptance `text=0.9929`, `image=1.0`, `audio=1.0`,
  `binary=0.0042`. Structured bytes grow; random binary prunes.
- Entropy sweep: acceptance crossed `0.5` at predictability level `0.7094`; below that,
  draft overhead should be treated skeptically unless a stronger predictor is available.

## Next Loop

1. Grow the CX-native JSON-template structural predictor. It is now the JSON winner at
   `229.780843x`, exact, accepted tokens `0.998698`, draft-token acceptance `0.573137`, and
   target-call reduction `361.411765x` with width ladder
   `[4096,2048,1536,1024,768,512,384,256,128,64,32,16,8,4,2]`. Random still prunes for
   JSON-template at `0.765712x`. The next JSON fruit is predictive wrap/reset handling,
   dual-hypothesis structural proposals near cycle boundaries, and still-cheaper encoded-row
   construction.
2. Treat `4096` as the current measured JSON-template knee. Wider `8192/6144` probes over-drafted
   and lost wall-clock, so reopen them only after the proposal/acceptance boundary or hot-path
   implementation changes. Do not retreat to the old `1024` knee unless a receipt proves the new
   hot path regressed.
   A no-write row-boundary cycle-copy guard was also pruned: it stayed exact but added Python
   selector overhead and did not reduce the `3` current reject events on this fixture
   (`~180-187x` with the guard versus the written `229.780843x` floor). Reopen that only as a
   cheaper persistent index or compiled branch.
3. Grow the CX-native copy-runway branch where whole-span reuse beats token-by-token copy. Exact grow
   rows remain repeat `258.594191x`, prose `194.514908x`, and code `14.44762x` with width cap
   `[96,64,32,16,8,4,2]`, `q=128`, and target-call reduction `14.912621x`. This replaces the prior
   code copy-match floor `11.117705x`. Random still prunes for runway at `0.770385x`.
4. Move the shared `SpecUnit -> DraftProducer -> Verifier -> AcceptancePolicy -> RepairPolicy -> SpecReceipt`
   protocol toward production shape: stable schemas, modality adapters, branch scoring, and custom
   schedulers/Rust/CUDA hot-path candidates. Code runway still shows high over-drafting pressure
   (`draft_acceptance` `0.260069`) while JSON-template has moved into a lower-pressure structural
   branch; both need selector/index/parser heat, not dependency-centered probes.
5. For rendering, grow measured OIDN/resident/warm speculative adapters and measured tile-refine repair.
   Do not spend on cold tile fanout until warm runtime/image/volume logistics are solved.
6. Treat vLLM/Hawking/Cycles/ffmpeg as reference mines and parts shelves, not dependency centers. Fork, copy,
   or rebuild narrow pieces only when they provide a measured kernel/API/checkpoint/scheduler advantage we can
   own inside the CX-native path.
7. Paid RunPod work remains gated by tracked/live pods, balance/spend, watchdogs, teardown, secret scan, and
   monotonic GPU fallback: if the requested GPU is unavailable, upgrade only; never downgrade.

## Verification And Safety

- `python3 -m py_compile scripts/spec-lab/cx_speculative_core.py scripts/spec-lab/run_cx_native_speculation_ladder.py scripts/spec-lab/run_token_spec_decode_ladder.py scripts/spec-lab/run_speculative_render_ladder.py scripts/spec-lab/runpod.py scripts/spec-lab/orchestrator.py scripts/spec-lab/experiments.py`: pass.
- `python3 scripts/spec-lab/test_cx_speculative_core.py`: pass, `3` tests.
- `python3 scripts/spec-lab/test_cx_native_speculation_ladder.py`: pass, `14` tests.
- `python3 scripts/spec-lab/test_token_spec_decode_ladder.py`: pass, `3` tests.
- `python3 scripts/spec-lab/test_speculative_render_ladder.py`: pass, `3` tests.
- `python3 scripts/spec-lab/test_runpod_safety.py`: pass, `9` tests.
- JSONL parse: branch ledger `2280` rows ok; CX-native ledger `2282` rows ok; token ledger `21` rows ok; speculative render ladder ledger `21` rows ok.
- RunPod tracked state: `scripts/spec-lab/.tracked_pods.json` is `[]`.
- RunPod live-pod query: `python3 scripts/spec-lab/runpod.py pods` returned `[]`.
- RunPod balance query after A1: `clientBalance=40.7079824357`, `currentSpendPerHr=0.01`.
- RunPod balance query after A2: `clientBalance=40.4413158496`, `currentSpendPerHr=0.01`.
- RunPod balance query after A5/A6 `latest`: `clientBalance=40.2937578357`, `currentSpendPerHr=0.01`.
- RunPod balance query after A5/A6 `0.20.2`: `clientBalance=40.1978462858`, `currentSpendPerHr=0.01`.
- Current RunPod balance query: `clientBalance=40.1172557386`, `currentSpendPerHr=0.01`.
- `runpod.py` now exposes `pods` and no longer cycles the GPU plan back downward; fallback is
  monotonic upward through the ordered GPU list.
- Secret scan outside `.secrets`: no real key found; matches were placeholders, test fixtures,
  redaction regexes, and docs examples.

## Continuation Goal Prompt

```text
/goal Continue docs/research/SPEC_RENDER_AND_TOKEN_SPEC_DECODE_DEEP_PLAN_2026-07-09.md.
Use the current receipts in docs/speed-lane-reports/spec-lab/SPEC_RENDER_AND_TOKEN_DECODE_ITERATION_2026-07-09.md
and docs/speed-lane-reports/spec-lab/SPEC_RENDER_TOKEN_DECODE_BRANCH_LEDGER_2026-07-09.md. Keep speculative
rendering and token speculative decode separate until the same end-to-end workload truly uses both. The center is
ComputeExchange-owned speculation: `SpecUnit -> DraftProducer -> Verifier -> AcceptancePolicy -> RepairPolicy ->
SpecReceipt`. vLLM, Hawking, Cycles, ffmpeg, and similar systems are reference mines and parts shelves only.
Pillage kernels, APIs, scheduling ideas, memory layouts, and compatibility tricks when useful, then own the hot
path inside CX.

Start from these measured floors: renderer-only friendly `14.3372x`, hard-scene delivery `7.8721x`, render adapter
OIDN delivery `12.9695x`, fixed/gated local repeat `315.460911x` exact, n-gram prefix repeat `152.873506x`, prose
`119.247118x`, code `5.643504x`, JSON `1.987658x`; copy-match JSON `6.609782x` with bounded `q=2`, repeat
`155.149457x`, code `11.117705x`, prose `119.243916x`; copy-runway repeat `258.594191x`, prose `194.514908x`,
code `14.44762x`, JSON `5.649682x`; JSON-template structural proposal JSON `229.780843x`, exact, accepted tokens
`0.998698`, draft acceptance `0.573137`, target-call reduction `361.411765x`, width ladder
`[4096,2048,1536,1024,768,512,384,256,128,64,32,16,8,4,2]`. Random remains the negative control:
copy-match `0.813218x`, copy-runway `0.770385x`, JSON-template `0.765712x`.

Grow JSON-template first: keep the bulk encoded-row cache, then improve predictive wrap/reset handling, dual-hypothesis
cycle-boundary proposals, structural row generalization beyond the current fixture, and Rust/CUDA/custom-kernel
candidates if proposal construction remains the measured bottleneck. Treat `4096` as the current measured knee;
`8192/6144` are parked until the proposal/acceptance boundary changes. Do not re-add the Python row-boundary
cycle-copy guard; it was exact but slower in no-write measurement. Reopen it only as a cheaper persistent-index or
compiled branch. Also grow copy-runway for repeat/prose/code by testing hotter bounded indexes, selector caching,
width-policy splits, and Rust/CUDA/custom-kernel candidates if selector overhead shows up in the receipt. For rendering,
grow resident/warm measured speculative render receipts, OIDN-assisted verify/refine, and tile repair only when warm
runtime or transfer preflight removes cold-start drag. Do not claim a combined multiplier until a single delivered
workflow uses both render and token branches.

RunPod GPU fallback is monotonic upward only: if the requested GPU is unavailable or fails driver/CUDA gates, upgrade,
never downgrade. Run paid branches only after tracked/live pod, balance, currentSpend, watchdog, teardown, and
secret-scan preflight. Do not stop merely because one branch improves; keep iterating until remaining branches are
pruned by measured acceptance, exactness, wall-clock, reproducibility, or logistics evidence.
```
