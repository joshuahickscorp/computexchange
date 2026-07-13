# Performance proof contract

The canonical benchmark manifest is
[`proof/performance/benchmark-manifest.schema.json`](../proof/performance/benchmark-manifest.schema.json).
It prevents a speed claim from silently changing the workload around the result.

Each arm pins the model revision, weights and tokenizer hashes, compute/weight/accumulator
precision, quantization, prompt-corpus hash, requested output tokens, batch policy, engine,
agent and toolchain builds, hardware identity and SKU, power and thermal state, competing-load
snapshot, sampling order, and the complete provisioning/queue/input-transfer/compute/output-transfer
timing boundary. Unknown fields and placeholder versions fail closed.

The Python validator/evaluator is normative for semantic constraints that JSON
Schema cannot express portably (for example fixed-batch cross-field equality).
The companion JSON Schema is the strict structural interchange contract. Version
1 measures output tokens only: every observation records its actual batch size and
the evaluator derives the only permitted completed-unit count as
`batch_size × requested_tokens_per_prompt`. Caller-supplied inflated unit counts
therefore fail before throughput is calculated.

## Comparability

Every difference must be declared by its exact allowed path. Runtime changes and granular
hardware/environment changes can be declared, so Apple-versus-CUDA comparisons are representable.
Workload, model hashes, precision, prompt corpus, output policy, batching, sampling, thresholds,
and timing scope cannot be declared away and must remain identical.

Validate a pair:

```bash
python3 scripts/performance_proof.py validate \
  --baseline-manifest path/to/baseline.manifest.json \
  --candidate-manifest path/to/candidate.manifest.json
```

## Observation runner and evaluator

The runner ingests one observation set per manifest and emits a per-lane or cross-substrate
artifact. End-to-end throughput includes provisioning, queue, input transfer, compute, and
output transfer. Latency uses deterministic nearest-rank p95/p99. It independently gates OOM,
restart, disconnect, output-hash corruption, and cleanup failures for both arms.

```bash
python3 scripts/performance_proof.py run \
  --baseline-manifest path/to/baseline.manifest.json \
  --candidate-manifest path/to/candidate.manifest.json \
  --baseline-observations path/to/baseline.observations.json \
  --candidate-observations path/to/candidate.observations.json \
  --artifact .artifacts/performance/comparison.json
```

The tool deliberately does not provision paid hardware or invent measurements. A physical
collector must provide bound manifest hashes, all measured samples, event evidence hashes,
power/thermal/load readings, and cleanup evidence. Each supported lane needs its own evaluation;
results are never averaged across lanes to hide a failing one.

## Local contract proof

The committed fixtures are synthetic. They prove that a valid synthetic comparison passes and
that degraded throughput, p95, p99, OOM, restart, disconnect, corrupt output, and failed cleanup
are all detected. They do not prove a benchmark, thermal result, nightly hardware gate, or buyer
workload win.

```bash
python3 -m unittest -v scripts/test_performance_proof.py
python3 scripts/performance_proof.py fixture-proof --check
```

The generated artifact is
[`proof/performance/synthetic-evaluator-proof.generated.json`](../proof/performance/synthetic-evaluator-proof.generated.json)
and labels all physical gates `NOT_PROVEN`.
