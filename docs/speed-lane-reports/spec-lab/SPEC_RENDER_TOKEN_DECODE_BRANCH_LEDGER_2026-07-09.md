# Spec Render / Token Branch Ledger - 2026-07-09

| Branch | Status | Receipt | Next action |
| --- | --- | --- | --- |
| renderer_base | floor | friendly `14.3372x`, hard `7.8721x` | grow only branches that beat hard-scene delivery |
| render_tile_refine | park | `3.4897x` preview | revisit only warm/resident, not cold crop fanout |
| token_local_code | park | best `6.5565x`, acceptance `0.2018` | prune or keep as negative control |
| token_local_json | prune | best `2.5539x`, acceptance `0.0603` | prune or keep as negative control |
| token_local_prose | grow | best `28.2515x`, acceptance `0.972` | increase k / move to model-backed verifier |
| token_local_random | prune | best `0.4999x`, acceptance `0.0` | prune or keep as negative control |
| token_local_repeat | grow | best `28.8659x`, acceptance `1.0` | increase k / move to model-backed verifier |
| vllm_0_11_ngram | prune | H100 PCIe best `0.68x`, acceptance `0.3801`, non-lossless `29/32` mismatched | do not rerun until exactness fix exists |
| vllm_0_11_suffix | park | pinned wheel rejected `method="suffix"` | only retry on version-upgrade branch |
| vllm_0_11_draft_model | park | H100 HBM3 raised `NotImplementedError`; pinned `vllm==0.11.0` does not support draft-model speculation | retry only on version-upgrade branch or supported artifact |
| vllm_0_11_draft_fallback_ngram | prune | H100 HBM3 `0.406x`, acceptance `0.1984`, non-lossless `41/64` mismatched | do not rerun until exactness fix exists |
| vllm_0_24_modern_stack | park | A100 PCIe installed `torch 2.11.0+cu130`, `vllm 0.24.0`, `transformers 5.13.0`; baseline engine init failed before spec measurement | retry only on newer-driver host or CUDA-12.8-compatible torch/vLLM stack |
| vllm_0_20_2_modern_stack | park | L40S stderr captured `NVIDIA driver ... too old (found version 12080)` before baseline | retry only on newer-driver host or CUDA-12.8-compatible torch/vLLM stack |
| cx_native_token_unit_code | grow | best `28.220004x`, accepted units `0.4375`, exact `True` | grow predictor/unit sizes if accepted fraction stays high |
| cx_native_token_unit_json | grow | best `3.970432x`, accepted units `0.454427`, exact `True` | grow predictor/unit sizes if accepted fraction stays high |
| cx_native_token_unit_prose | grow | best `89.365695x`, accepted units `0.5`, exact `True` | grow predictor/unit sizes if accepted fraction stays high |
| cx_native_token_unit_random | prune | best `3.575189x`, accepted units `0.0`, exact `True` | keep as negative control unless a stronger predictor appears |
| cx_native_token_unit_repeat | grow | best `294.355517x`, accepted units `1.0`, exact `True` | grow predictor/unit sizes if accepted fraction stays high |
| cx_native_render_adapter | grow | `oidn` `12.9695x`, tier `delivery` | grow resident/warm measured render path before more cold tile spend |
| cx_native_token_gate_code | grow | best `2.663911x`, threshold `0.9`, attempted `0.71224`, fallback `0.28776`, accepted attempted `0.996344` | grow the gate with larger/smarter units or a production hot path |
| cx_native_token_gate_json | grow | best `1.542557x`, threshold `0.55`, attempted `0.484375`, fallback `0.515625`, accepted attempted `0.90457` | grow the gate with larger/smarter units or a production hot path |
| cx_native_token_gate_prose | grow | best `7.564348x`, threshold `0.9`, attempted `0.927083`, fallback `0.072917`, accepted attempted `1.0` | grow the gate with larger/smarter units or a production hot path |
| cx_native_token_gate_random | prune | best `1.023405x`, threshold `0.75`, attempted `0.0`, fallback `1.0`, accepted attempted `0.0` | keep as confidence-gate negative control |
| cx_native_token_gate_repeat | grow | best `315.460911x`, threshold `0.55`, attempted `1.0`, fallback `0.0`, accepted attempted `1.0` | grow the gate with larger/smarter units or a production hot path |
| cx_native_token_adaptive_code | grow | best `3.597807x`, widths `[128, 64, 32, 16, 8, 4, 2]`, threshold `0.75`, attempted `0.531128`, fallback `0.468872`, accepted attempted `1.0` | grow adaptive subspan policy and move hot path closer to production |
| cx_native_token_adaptive_json | park | best `2.419334x`, widths `[128, 64, 32, 16, 8, 4]`, threshold `0.55`, attempted `0.177541`, fallback `0.822459`, accepted attempted `0.732739` | tune width ladder/threshold before pruning |
| cx_native_token_adaptive_prose | grow | best `28.919275x`, widths `[128, 64, 32, 16, 8, 4]`, threshold `0.9`, attempted `0.58`, fallback `0.42`, accepted attempted `1.0` | grow adaptive subspan policy and move hot path closer to production |
| cx_native_token_adaptive_random | prune | best `1.009439x`, widths `[128, 64, 32, 16, 8, 4]`, threshold `0.75`, attempted `0.0`, fallback `1.0`, accepted attempted `0.0` | keep as adaptive negative control unless predictor improves |
| cx_native_token_adaptive_repeat | grow | best `107.786029x`, widths `[128, 64, 32, 16, 8, 4]`, threshold `0.55`, attempted `1.0`, fallback `0.0`, accepted attempted `1.0` | grow adaptive subspan policy and move hot path closer to production |
| cx_native_token_prefix_code | grow | best `5.643504x`, source `ngram`, widths `[256, 128, 64, 32, 16, 8, 4, 2]`, threshold `0.25`, accepted tokens `0.860189`, draft acceptance `0.799546`, target calls `5.72067x` | grow prefix acceptance with smarter proposals and a hotter selector |
| cx_native_token_prefix_json | grow | best `1.987658x`, source `ngram`, widths `[256, 128, 64, 32, 16, 8, 4, 2]`, threshold `0.25`, accepted tokens `0.583822`, draft acceptance `0.628416`, target calls `2.030403x` | grow prefix acceptance with smarter proposals and a hotter selector |
| cx_native_token_prefix_prose | grow | best `119.247118x`, source `ngram`, widths `[256, 128, 64, 32, 16, 8, 4, 2]`, threshold `0.1`, accepted tokens `0.996094`, draft acceptance `0.839737`, target calls `192.0x` | grow prefix acceptance with smarter proposals and a hotter selector |
| cx_native_token_prefix_random | prune | best `0.779056x`, source `ngram`, widths `[256, 128, 64, 32, 16, 8, 4, 2]`, threshold `0.25`, accepted tokens `0.0`, draft acceptance `0.0`, target calls `1.0x` | keep as prefix-accept negative control unless predictor improves |
| cx_native_token_prefix_repeat | grow | best `152.873506x`, source `ngram`, widths `[256, 128, 64, 32, 16, 8, 4, 2]`, threshold `0.25`, accepted tokens `1.0`, draft acceptance `1.0`, target calls `256.0x` | grow prefix acceptance with smarter proposals and a hotter selector |
| cx_native_token_prefix_copy_code | grow | best `11.117705x`, source `copy_match`, min match `3`, candidate depth `16`, widths `[64, 32, 16, 8, 4, 2]`, threshold `0.55`, accepted tokens `0.93278`, draft acceptance `0.296513`, target calls `12.668041x` | grow copy-match predictor or hot selector if it beats n-gram prefix for this scenario |
| cx_native_token_prefix_copy_json | grow | best `6.609782x`, source `copy_match`, min match `3`, candidate depth `2`, widths `[32, 16, 8, 4, 2]`, threshold `0.55`, accepted tokens `0.852702`, draft acceptance `0.305909`, target calls `6.77398x` | grow copy-match predictor or hot selector if it beats n-gram prefix for this scenario |
| cx_native_token_prefix_copy_random | prune | best `0.813218x`, source `copy_match`, min match `3`, candidate depth `16`, widths `[256, 128, 64, 32, 16, 8, 4, 2]`, threshold `0.75`, accepted tokens `0.0`, draft acceptance `0.0`, target calls `1.0x` | keep as copy-match negative control unless structured reuse appears |
| cx_native_token_prefix_copy_repeat | grow | best `155.149457x`, source `copy_match`, min match `4`, candidate depth `16`, widths `[256, 128, 64, 32, 16, 8, 4, 2]`, threshold `0.9`, accepted tokens `1.0`, draft acceptance `1.0`, target calls `256.0x` | grow copy-match predictor or hot selector if it beats n-gram prefix for this scenario |
| cx_native_token_prefix_copy_prose | grow | best `119.243916x`, source `copy_match`, min match `3`, candidate depth `16`, widths `[256, 128, 64, 32, 16, 8, 4, 2]`, threshold `0.75`, accepted tokens `0.995443`, draft acceptance `0.839188`, target calls `192.0x` | grow copy-match predictor or hot selector if it beats n-gram prefix for this scenario |
| cx_native_token_prefix_copy_runway_prose | grow | best `194.514908x`, source `copy_runway`, min match `3`, candidate depth `512`, widths `[256, 128, 64, 32, 16, 8, 4, 2]`, threshold `0.55`, accepted tokens `0.997559`, draft acceptance `0.845379`, target calls `192.0x` | grow copy-runway selector depth/widths where whole-span reuse beats token-by-token copy |
| cx_native_token_prefix_copy_runway_random | prune | best `0.770385x`, source `copy_runway`, min match `3`, candidate depth `16`, widths `[32, 16, 8, 4, 2]`, threshold `0.55`, accepted tokens `0.0`, draft acceptance `0.0`, target calls `1.0x` | keep as copy-runway negative control unless verified runway appears |
| cx_native_token_prefix_copy_runway_repeat | grow | best `258.594191x`, source `copy_runway`, min match `3`, candidate depth `512`, widths `[256, 128, 64, 32, 16, 8, 4, 2]`, threshold `0.55`, accepted tokens `1.0`, draft acceptance `1.0`, target calls `256.0x` | grow copy-runway selector depth/widths where whole-span reuse beats token-by-token copy |
| cx_native_token_prefix_copy_runway_code | grow | best `14.44762x`, source `copy_runway`, min match `3`, candidate depth `128`, widths `[96, 64, 32, 16, 8, 4, 2]`, threshold `0.55`, accepted tokens `0.933268`, draft acceptance `0.260069`, target calls `14.912621x` | grow copy-runway selector depth/widths where whole-span reuse beats token-by-token copy |
| cx_native_token_prefix_copy_runway_json | grow | best `5.649682x`, source `copy_runway`, min match `3`, candidate depth `256`, widths `[32, 16, 8, 4, 2]`, threshold `0.55`, accepted tokens `0.832357`, draft acceptance `0.233431`, target calls `5.818182x` | grow copy-runway selector depth/widths where whole-span reuse beats token-by-token copy |
| cx_native_token_prefix_json_template_json | grow | best `229.780843x`, source `json_template`, widths `[4096, 2048, 1536, 1024, 768, 512, 384, 256, 128, 64, 32, 16, 8, 4, 2]`, threshold `0.75`, accepted tokens `0.998698`, draft acceptance `0.573137`, target calls `361.411765x` | grow JSON-template parser/cache and structural predictors around the measured width knee |
| cx_native_token_prefix_json_template_random | prune | best `0.765712x`, source `json_template`, widths `[4096, 2048, 1536, 1024, 768, 512, 384, 256, 128, 64, 32, 16, 8, 4, 2]`, threshold `0.55`, accepted tokens `0.0`, draft acceptance `0.0`, target calls `1.0x` | keep as structured-output negative control unless JSON-like rows appear |
| modality_general_protocol | pass | `4/4` plugins ran: `ar`, `bytes`, `video`, `render` | keep as interface guard; no speed claim |
| byte_ngram_predictability | split | text `0.9929`, image `1.0`, audio `1.0`, binary `0.0042` acceptance | grow structured-byte predictors; prune random/high entropy |
| byte_entropy_sweep | threshold | acceptance crossed `0.5` near predictability `0.7094` | gate speculation by entropy/predictability |

Receipts:

- Token JSONL: `/Users/scammermike/Downloads/computexchange/docs/speed-lane-reports/spec-lab/token_spec_decode_ledger.jsonl`
- Branch JSONL: `/Users/scammermike/Downloads/computexchange/docs/speed-lane-reports/spec-lab/spec_render_token_branch_ledger.jsonl`
- CX-native JSONL: `/Users/scammermike/Downloads/computexchange/docs/speed-lane-reports/spec-lab/cx_native_speculation_ledger.jsonl`
- Iteration report: `/Users/scammermike/Downloads/computexchange/docs/speed-lane-reports/spec-lab/SPEC_RENDER_AND_TOKEN_DECODE_ITERATION_2026-07-09.md`
- Local protocol smoke: `scripts/spec-lab/pod/exp_protocol.py '{"modalities":["ar","bytes","video","render"]}'`
- Local byte receipt: `scripts/spec-lab/pod/exp_bytes_specdec.py '{"mode":"default","n_bytes":8192,"draft_ctx":8}'`
- Local entropy sweep: `scripts/spec-lab/pod/exp_bytes_specdec.py '{"mode":"entropy_sweep","n_bytes":8192,"draft_ctx":8}'`
- vLLM A1 receipt: `docs/speed-lane-reports/spec-lab/ledger.jsonl` lines `114-122`
- vLLM A2 receipt: `docs/speed-lane-reports/spec-lab/ledger.jsonl` lines `123-129`
- vLLM A5/A6 latest receipt: `docs/speed-lane-reports/spec-lab/ledger.jsonl` lines `130-141`
- vLLM A5/A6 `0.20.2` receipt: `docs/speed-lane-reports/spec-lab/ledger.jsonl` lines `142-153`

Guardrail: local protocol token rows are not vLLM/Hawking LLM throughput claims. vLLM rows above are
real model-backed receipts and are kept separate from renderer and local-protocol multipliers.
