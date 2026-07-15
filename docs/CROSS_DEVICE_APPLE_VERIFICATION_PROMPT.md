# Global two-Mac spec-decode and adaptive-resource verification prompt

Use the prompt below in the coordinating session after both Macs can reach each
other through their existing SSH configuration. Replace only the bracketed host
and path values. Never paste or commit credentials.

```text
Validate the integrated CX spec-decode and adaptive-resource payload on two
Apple Silicon Macs.

Coordinator Mac: [HOST_A]
Peer Mac: [HOST_B]
Isolated verification worktree on A: [WORKTREE_A]
Isolated verification worktree on B: [WORKTREE_B]
vLLM-Metal fork path on A: [VLLM_METAL_A]
vLLM-Metal fork path on B: [VLLM_METAL_B]

Delivery branch: codex/spec-decode-greedy-integrated
Integrated payload commit under test:
6bfccb53ab08013152a7e0b2442ed398169b101a
Frozen spec-decode parent:
cc4f27075302f20494b07d1b6ab8fd19931cc12e

Non-negotiable boundaries:
- Test the exact integrated payload commit above in clean detached worktrees.
- Do not modify speculative-decoding math, kernels, sampling, token output,
  runtime identity, receipt schemas, or existing verification artifacts.
- Use the existing SSH configuration without printing keys, tokens, complete
  environment dumps, or unredacted process arguments.
- Use a local/no-payment control-plane fixture and synthetic non-sensitive jobs.
  Do not change production services, submit paid work, merge, or push.
- max_cpu_pct sizes host-local admission capacity only. It is not an OS-level
  CPU throttle and does not constrain Candle's global worker threads. Do not add
  thread-pool tuning during this verification. Record it as a measured follow-up
  only if the evidence shows admission control is insufficient.
- Never intentionally OOM or thermally abuse a machine. Exercise pressure gates
  with conservative ceilings or safe synthetic reservations.
- Preserve every raw result, output digest, runtime identity, command, and log in
  a timestamped per-device evidence directory. Redact secrets before sharing.

Phase 1 — establish identical source and machine identity on both Macs:
1. Fetch origin, create a clean detached worktree at
   6bfccb53ab08013152a7e0b2442ed398169b101a, and prove:
     git rev-parse HEAD
     git status --porcelain
   HEAD must equal the pinned SHA and status must be empty.
2. Record Mac model, Apple chip, logical CPU count, unified memory, macOS and
   Xcode/clang versions, Rust/Python versions, and the agent build hash. Do not
   assume the two machines share a verification class.
3. Prove the integration did not alter the frozen spec-decode implementation:
     git diff --exit-code \
       cc4f27075302f20494b07d1b6ab8fd19931cc12e \
       6bfccb53ab08013152a7e0b2442ed398169b101a -- \
       agent/src/hardware.rs agent/src/runners.rs docker/vllm/PROVENANCE.md \
       docs/VLLM_LANE.md docs/VLLM_MAC_EXPERIMENT_LANE.md \
       docs/research/INFERENCE_50X_EXECUTION_PLAN.md \
       docs/research/INFERENCE_50X_RECOVERY_PROMPT.md \
       docs/speed-lane-reports/VLLM_METAL_M3_ULTRA_SPEC_DECODE_2026-07-14.md \
       scripts/spec-lab
   This command must produce no diff.
4. Confirm the Mac app still writes automatic sizing:
     rg -n 'max_concurrent_tasks = 0' \
       macapp/ComputeExchangeAgent/AgentController.swift

Phase 2 — static and device-safe gates on both Macs:
1. Run and retain complete logs for:
     cargo fmt --manifest-path agent/Cargo.toml -- --check
     git diff --check
     cargo test --manifest-path agent/Cargo.toml --no-default-features
     cargo check --manifest-path agent/Cargo.toml
2. Run the four resource_governor tests explicitly and prove all four pass:
     cargo test --manifest-path agent/Cargo.toml --no-default-features \
       resource_governor
3. Run the checked-in vLLM runtime-lock gate:
     python3 -B -m unittest docker/vllm/test_runtime_lock.py -v
4. From scripts/spec-lab, run the checked-in inference/spec suite:
     python3 -B -m unittest -v \
       test_audit_vllm_metal_spec_parity \
       test_cx_vllm_endpoint_attestation_v1 \
       test_cx_inference_exact_cache_runner_v1 \
       test_cx_inference_policy_v1 \
       test_cx_inference_prefix_trace_v1 \
       test_cx_inference_receipt_v1 \
       test_prepare_vllm_exact_cache_workload_v1 \
       test_prepare_vllm_shared_prefix_workload_v1 \
       test_reproduce_historical_c1_vllm_bench_profile_a \
       test_run_local_vllm_lab \
       test_score_1000x_lanes \
       test_screen_inference_lane_abba
5. In each pinned vLLM-Metal fork, run:
     python -m pytest -q \
       tests/test_spec_decode_paired_parity_gate.py \
       tests/test_reproduce_historical_c1_ngram_divergence.py
   Record the fork commit, Python environment identity, and any warnings.

Phase 3 — adaptive-resource behavior on each Mac:
1. Launch the local agent with max_concurrent_tasks=0, max_cpu_pct=50, and
   conservative memory_headroom_gb/max_memory_pct values. Capture the startup
   queue ceiling, CPU units, allocatable memory, and build/runtime identity.
2. Submit a fixed synthetic workload containing independent embed/rerank/audio
   work, several same-model batch_infer jobs, and one safe exclusive-class job.
   For every task record task type, triage mode, CPU units, memory reservation,
   runtime key, queue/admission/start/finish timestamps, and output digest.
3. Prove weighted CPU and RAM reservations never exceed their admission ledgers,
   admission occurs before start_task, independent work overlaps when capacity
   permits, same-model generative work is tagged stacked, and exclusive work does
   not overlap another CPU-admitted task.
4. Repeat the identical workload with max_cpu_pct=100. The derived CPU ledger
   should scale with logical CPUs, and independent overlap/wall time may improve.
   Do not fail solely because observed OS CPU utilization exceeds max_cpu_pct;
   this setting is an admission weight, not a hard utilization throttle.
5. Safely lower the memory ceiling or use a bounded synthetic reservation to
   prove new starts pause and resume while active work exits and checkpoints
   cleanly. Do not induce real memory exhaustion.
6. Confirm the status surface reports the exact resolved adaptive queue ceiling,
   while the Mac preference sidecar continues to contain
   max_concurrent_tasks=0 for automatic sizing.

Phase 4 — spec-decode parity, identity, and receipts on each Mac:
1. Run the pinned local vLLM baseline/candidate procedure for the machine's
   declared verification class. Keep prompts, seeds, model/tokenizer identities,
   sampling parameters, maximum tokens, endpoint attestation, and runtime lock
   identical between paired runs.
2. Require exact token-ID parity and stable repeated output digests. A mismatch,
   missing authoritative token list, identity drift, fallback ambiguity, or
   non-production/placeholder runtime lock fails closed.
3. Verify resource admission changes only when work starts. It must not change
   generated tokens, authoritative token accounting, runtime identity, receipt
   schema, verification class, or the bytes of existing receipts.
4. Revalidate the relevant ABBA/parity/identity receipts with their strict
   validators. Keep performance claims scoped to the observed workload and
   verification class; do not promote experimental cache or prefix results to a
   general production speedup.
5. Compare repeated hashes within each machine/class. Compare output hashes
   across the two Macs only if the runtime-matrix contract says their hardware,
   build, model, tokenizer, and execution identities are equivalent. Otherwise,
   report the classes separately rather than forcing cross-class byte equality.

Phase 5 — coordinated report and decision:
1. Produce one matrix with a column per Mac and rows for source SHA, machine and
   verification class, builds, Rust tests, runtime-lock tests, inference/spec
   tests, fork parity tests, 50%/100% CPU admission budgets, memory budget, queue
   ceiling, parallel overlap, stacked behavior, exclusive behavior, pressure
   pause/resume, token/output parity, runtime identity, and receipt integrity.
2. Include measured wall times and throughput, but distinguish admission-ledger
   behavior from OS CPU utilization and runtime thread-pool behavior.
3. State PASS only if every required source, build, safety, admission, parity,
   identity, and receipt gate passes on both machines. Otherwise state FAIL,
   preserve logs, minimize a reproduction, and identify the first failing gate.
   Do not silently tune around a failure.
4. End with exactly one recommendation:
   - promote the integrated payload for the tested Apple verification classes;
   - hold for a named correctness/admission defect; or
   - open a measured runtime-thread-pool follow-up, without modifying this lane.
```
