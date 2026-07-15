# Cross-device Apple verification handoff prompt

Use this prompt in the coordinating Codex/Claude session after both Macs can SSH
to each other. Replace the bracketed values; do not paste credentials into chat
or commit them.

```text
We are validating the isolated CX adaptive-resource branch, not continuing the
spec-decode lane.

Coordinator Mac: [HOST_A]
Peer Mac: [HOST_B]
Repository path on A: [REPO_A]
Repository path on B: [REPO_B]
Branch/commit under test: codex/cx-greedy-agent at [COMMIT]

Constraints:
- Use the machines' existing SSH configuration; never print keys, tokens, config
  secrets, or full environment dumps.
- Keep the spec-decode checkout and all of its uncommitted files untouched.
- Do not push, merge, or change production services.
- Use a no-payment/local control-plane fixture and synthetic non-sensitive jobs.
- Treat max_cpu_pct as host-local admission capacity, not an OS-level CPU or
  Candle global-thread-pool throttle. Do not add runtime thread-pool tuning unless
  the measurements prove that a separate follow-up is necessary.
- Record commands, machine class, logical CPU count, unified memory, macOS
  version, agent build hash, and pass/fail evidence in one timestamped report.

On both Macs:
1. Confirm the tested commit and a clean isolated worktree.
2. Run cargo fmt --check and the no-default-features resource_governor tests.
3. Build the normal Metal agent and run its existing device-safe unit tests.
4. Launch the local agent with max_cpu_pct=50 and conservative memory headroom;
   capture the startup resource-governor log without exposing the worker token.
5. Submit a mixed synthetic workload: independent embed/rerank tasks, then
   multiple same-model batch_infer tasks. Capture triage mode, CPU units, memory
   reservation, runtime key, start/finish times, and output hashes.
6. Repeat max_cpu_pct=100. Compare overlap and wall time. The 100% run should
   admit more independent work when resources allow; neither run may exceed its
   CPU admission budget or cross the configured memory headroom.
7. Verify same-model generative tasks report stacked mode and produce identical
   output hashes across repeated runs on the same verification class. Do not
   demand byte equality across different Apple hardware/build classes unless the
   existing runtime-matrix contract says they are equivalent.
8. Exercise pressure behavior with a safe synthetic reservation or reduced
   configured ceiling—do not intentionally OOM or thermally abuse either Mac.
   Prove new task starts pause and resume while active work exits cleanly.
9. Diff the separate spec-decode worktree before/after and prove it did not move.

Finish with a compact matrix for A and B: build, tests, derived CPU/RAM budget,
parallel overlap, stacked behavior, pressure pause/resume, output integrity, and
any divergence. If a check fails, preserve logs and minimize a reproduction;
do not silently tune around it.
```
