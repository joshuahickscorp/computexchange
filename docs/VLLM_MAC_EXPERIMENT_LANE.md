<!-- CLAIM-SCOPE: internal-engineering-non-authoritative -->
# Mac vLLM/spec-decode experiment lane

`scripts/spec-lab/run_local_vllm_lab.py` gives the Mac Studio a reproducible
experiment loop without describing Apple measurements as CUDA measurements. It
uses the same batched greedy `/v1/completions` request contract as the agent's
current vLLM adapter and emits versioned JSON captures suitable for later
performance-proof ingestion.

The lane does not install vLLM, download a model, start paid compute, or modify
the agent's production gates.

## Execution classes

| Target | What it exercises | Permitted claim |
|---|---|---|
| `embedded-mock` | HTTP, JSON, batch choice mapping, token-count mapping, deterministic output hashes, and engine-neutral policy replay | Protocol/control-path evidence only |
| `local-vllm-metal` | An already-running vLLM-Metal OpenAI server on Apple Silicon | Local Metal endpoint E2E only; never CUDA |
| `local-openai-compatible` | Another loopback engine, including a future CX Candle/Metal OpenAI shim | The explicitly declared local accelerator only |
| `remote-vllm-cuda` | A remote or SSH-tunneled server bound to a valid `docker/vllm` runtime lock | Remote endpoint E2E including transport; never local-Mac or kernel-only CUDA |

The main vLLM control plane is Python with performance work in compiled GPU
kernels and extensions. The Apple plugin is primarily Python plus Metal/C++ and
uses MLX as its compute backend. vLLM-Metal has an OpenAI-compatible API and
currently documents paged attention plus MTP, draft-model, and n-gram
speculative decoding. These are useful seams to mine even when a media backend
will eventually use different kernels.

Primary references:

- [vLLM-Metal overview](https://docs.vllm.ai/projects/vllm-metal/en/latest/)
- [installation requirements](https://docs.vllm.ai/projects/vllm-metal/en/latest/installation/)
- [Metal speculative decoding](https://docs.vllm.ai/projects/vllm-metal/en/latest/speculative_decoding/)
- [repository](https://github.com/vllm-project/vllm-metal)

## Zero-install inspection and contract smoke

Check whether the official default vLLM-Metal environment or another configured
binary is already present. This only inspects files and package metadata:

```bash
python3 scripts/spec-lab/run_local_vllm_lab.py inspect-metal
```

The official plugin currently requires macOS on Apple Silicon, a native arm64
Python 3.12 environment, and Xcode Command Line Tools. If it is absent, the
probe emits `not_installed`, official setup links, and
`install_performed:false`. Installation remains an explicit operator action.

Run the dependency-free adapter-contract smoke:

```bash
python3 scripts/spec-lab/run_local_vllm_lab.py run \
  --target embedded-mock \
  --requests 5 \
  --concurrency 2 \
  --max-tokens 64 \
  --run-label mock-control-smoke \
  --comparison-group mac-vllm-control-v1 \
  --output .artifacts/vllm-lab/mock-control-smoke.json
```

The mock intentionally returns choices in reverse order. A passing capture proves
that the client restores prompt order, requires authoritative per-choice
`logprobs.tokens` for a batch, cross-checks their total against
`usage.completion_tokens`, and receives stable output hashes under concurrent
requests. For a single choice only, aggregate `usage.completion_tokens` is an
unambiguous fallback. Whitespace is never treated as token accounting. The mock's
throughput field is explicitly scoped to `protocol_control_path_only` and is not
model performance.

To leave the strict mock running for another local process:

```bash
python3 scripts/spec-lab/run_local_vllm_lab.py serve-mock --port 8765
```

The Rust adapter's own in-process HTTP mapping proof remains:

```bash
cargo test --manifest-path agent/Cargo.toml --no-default-features \
  vllm_runner_soak_mode_maps_real_http_response_to_batch_infer_result \
  -- --nocapture
```

## First physical matrix: vLLM-Metal baseline versus n-gram

Use a pinned, supported model and retain the launch logs. The same model revision,
weight-tree digest, tokenizer-tree digest, precision, prompt corpus, output cap,
request count, and concurrency must be used in both arms. The four identity flags
below are required before `compare` will call the captures lab-comparable.

For a baseline server, the current official CLI shape is:

```bash
source ~/.venv-vllm-metal/bin/activate
export MODEL='PINNED_MODEL_OR_LOCAL_PATH'
export REVISION='PINNED_REVISION'

VLLM_METAL_USE_PAGED_ATTENTION=1 \
  vllm serve "$MODEL" \
  --revision "$REVISION" \
  --port 8000 \
  --max-model-len 2048
```

In another terminal, capture at least 20 requests for a proof-shaped sample set:

```bash
python3 scripts/spec-lab/run_local_vllm_lab.py run \
  --target local-vllm-metal \
  --endpoint http://127.0.0.1:8000 \
  --model "$MODEL" \
  --model-revision "$REVISION" \
  --weights-sha256 "$WEIGHTS_SHA256" \
  --tokenizer-sha256 "$TOKENIZER_SHA256" \
  --precision-id "$PRECISION_ID" \
  --runtime-id "$BASELINE_RUNTIME_AND_FLAGS_ID" \
  --variant baseline \
  --requests 20 \
  --concurrency 4 \
  --max-tokens 128 \
  --run-label metal-baseline \
  --comparison-group metal-ngram-v1 \
  --output .artifacts/vllm-lab/metal-baseline.json
```

Stop the baseline server, then start the n-gram arm. vLLM-Metal documents greedy
sampling, paged attention, and synchronous scheduling as requirements:

```bash
VLLM_METAL_USE_PAGED_ATTENTION=1 \
  vllm serve "$MODEL" \
  --revision "$REVISION" \
  --port 8000 \
  --max-model-len 2048 \
  --no-async-scheduling \
  --speculative-config \
    '{"method":"ngram","num_speculative_tokens":3,"prompt_lookup_min":2,"prompt_lookup_max":3}'
```

Capture with the identical workload options, changing only the runtime/config ID,
variant, label, and output path:

```bash
python3 scripts/spec-lab/run_local_vllm_lab.py run \
  --target local-vllm-metal \
  --endpoint http://127.0.0.1:8000 \
  --model "$MODEL" \
  --model-revision "$REVISION" \
  --weights-sha256 "$WEIGHTS_SHA256" \
  --tokenizer-sha256 "$TOKENIZER_SHA256" \
  --precision-id "$PRECISION_ID" \
  --runtime-id "$NGRAM_RUNTIME_AND_FLAGS_ID" \
  --variant ngram \
  --requests 20 \
  --concurrency 4 \
  --max-tokens 128 \
  --run-label metal-ngram-k3 \
  --comparison-group metal-ngram-v1 \
  --output .artifacts/vllm-lab/metal-ngram-k3.json

python3 scripts/spec-lab/run_local_vllm_lab.py compare \
  --baseline .artifacts/vllm-lab/metal-baseline.json \
  --candidate .artifacts/vllm-lab/metal-ngram-k3.json \
  --output .artifacts/vllm-lab/metal-ngram-comparison.json
```

`compare` records matched endpoint measurements, but it is not an output-parity
proof. Before any speculative candidate can move beyond experiment-only status,
capture a stable repeat for each arm and run the fail-closed auditor:

```bash
python3 scripts/spec-lab/audit_vllm_metal_spec_parity.py \
  --baseline .artifacts/vllm-lab/metal-baseline-repeat-1.json \
  --candidate .artifacts/vllm-lab/metal-ngram-repeat-1.json \
  --baseline-repeat .artifacts/vllm-lab/metal-baseline-repeat-2.json \
  --candidate-repeat .artifacts/vllm-lab/metal-ngram-repeat-2.json \
  --tokenizer-path "$PINNED_TOKENIZER_PATH" \
  --output .artifacts/vllm-lab/metal-ngram-parity.json
```

It emits hashes and (when a pinned tokenizer is available) token IDs only; it
never copies completion text into the receipt. Exit status `2` means an identity,
repeatability, or exact-output failure and keeps both speed and production claims
false. The retained M3 Ultra C1 captures currently take that path: 15/16 match,
with one repeat-stable output mismatch.

### Fork-level parity gate before any new timing

The local `vllm-metal-cx` fork now carries a stricter process-isolated gate at
`tools/spec_decode_paired_parity_gate.py`. It starts fresh target-only and
n-gram engines for every arm/repeat, pins a full model revision plus corpus and
optional cache-history JSONL, requires C1 and C16 with K=1, verifies the
resolved paged-attention configuration, and compares generated token IDs
exactly. A bound history must produce an actual measured paged-cache hit. The
tool records no timing and makes no speed claim.

```bash
cd /absolute/path/to/vllm-metal-cx
PYTHONPATH=$PWD python tools/spec_decode_paired_parity_gate.py \
  --model "$MODEL" --model-revision "$FULL_40_CHARACTER_COMMIT" \
  --corpus /absolute/path/to/pinned-requests.jsonl \
  --prefix-cache-history /absolute/path/to/pinned-history.jsonl \
  --concurrency 1,16 --ngram-k 1,3 --repeats 3 \
  --output-json /absolute/path/to/paired-ngram-parity.json
```

The gate has model-free contract tests and a source-bound, real-model C1/C16 ×
K1/K3 physical parity receipt at
`/Users/scammermike/Downloads/vllm-metal-cx/.artifacts/cx-inference/ngram-exercise-v1/paired-parity-source-single-active-guard-sourcebuild-v1.json`.
That run confirms the C1 direct-target fallback and records actual non-C1 draft
activity, but it deliberately collects no timing and is not a performance
receipt. Any failed cell keeps speculative throughput claims quarantined.

The same capture flow supports `draft_model` and `mtp` variant labels. Their
model pairing, memory budget, and synchronous-scheduling requirements must come
from the vLLM-Metal documentation and be included in the operator's runtime ID.
The harness does not infer server launch flags from the endpoint.

## Comparing CX Candle Metal later

Expose the exact same workload through a loopback OpenAI-compatible shim and use:

```bash
python3 scripts/spec-lab/run_local_vllm_lab.py run \
  --target local-openai-compatible \
  --endpoint http://127.0.0.1:9000 \
  --engine-label cx-candle-metal \
  --accelerator-class metal \
  --model "$MODEL" \
  --model-revision "$REVISION" \
  --weights-sha256 "$WEIGHTS_SHA256" \
  --tokenizer-sha256 "$TOKENIZER_SHA256" \
  --precision-id "$PRECISION_ID" \
  --runtime-id "$CX_CANDLE_RUNTIME_AND_FLAGS_ID" \
  --variant baseline \
  --requests 20 \
  --concurrency 4 \
  --max-tokens 128 \
  --run-label cx-candle-metal \
  --comparison-group metal-engine-v1 \
  --output .artifacts/vllm-lab/cx-candle-metal.json
```

Only the same checkpoint, tokenizer, precision, workload, and physical Mac are
lab-comparable. A format or quantization change is a different workload unless a
separate approved proof contract explicitly permits it.

## Remote pinned vLLM

`remote-vllm-cuda` requires a runtime lock that passes
`docker/vllm/validate_runtime_lock.py`; the checked-in candidate template still
has placeholders and is intentionally rejected. The served model and speculative
method are derived from the lock rather than command-line claims:

```bash
export CX_VLLM_API_KEY='...'
python3 scripts/spec-lab/run_local_vllm_lab.py run \
  --target remote-vllm-cuda \
  --endpoint https://PINNED_ENDPOINT \
  --runtime-lock path/to/completed-runtime-lock.json \
  --executor-id 'PINNED_REMOTE_MACHINE_ID' \
  --requests 20 \
  --concurrency 4 \
  --run-label remote-vllm-ngram \
  --comparison-group remote-vllm-v1 \
  --output .artifacts/vllm-lab/remote-vllm-ngram.json
```

The API key value is never written to the artifact or ledger. A loopback URL is
valid for an SSH tunnel, but the capture still records the execution site as
remote and CUDA. Direct non-loopback plain HTTP is refused unless the operator
explicitly passes `--allow-insecure-http`.

## Artifact and pricing semantics

Every capture contains:

- client and executor identity, runtime identity, model identity, and workload digest;
- variant, comparison group, batch width, request concurrency, and sampling counts;
- measured wall time, request rate, authoritative output-token throughput from
  per-choice `logprobs.tokens` (or single-choice aggregate usage), nearest-rank
  p50/p95/p99 latency, output hashes, and token counts;
- exact engine-neutral policy receipts for widths 1/2/4/8 with acceptance and
  potential verifier-call reduction, but no invented speedup;
- explicit `cuda`, `non_cuda`, benchmark-domain, and claim-scope fields;
- a `performance_proof_bridge` aligned with
  `proof/performance/benchmark-manifest.schema.json`.

`--hourly-cost-usd` is optional. When supplied, the harness reports the measured
window cost and cost per million authoritative output tokens. It never supplies
or guesses a market price. Endpoint time does not include provisioning, teardown,
power, thermal, competing-load, or cleanup evidence, so the capture is not itself
a canonical physical performance proof. Those missing fields are enumerated in
the artifact and must be completed before using `scripts/performance_proof.py`.
A matched and byte-exact lab pair can set `descriptive_endpoint_ratio_valid:true`,
but `speed_claim_valid` remains false until that canonical physical proof and
review are complete.

By default each run is appended to
`docs/speed-lane-reports/spec-lab/local_vllm_spec_lab_ledger.jsonl`. Use
`--no-ledger` for disposable plumbing checks.
