# Studio/Scale 10/10 receipts - 2026-07-09

Source prompts:
- `/Users/scammermike/Downloads/project_audits/studio_unification_goal_prompts_2026_07_08.md`
- `/Users/scammermike/Downloads/project_audits/integrated_reaudit_after_loop_2026_07_08.md`

## Proof ledger

Local deterministic proof was preserved after the code changes:

- Command: `SKIP_LIVE=1 make prove-local`
- Result: `276 pass, 0 skip, 0 fail`

Full live proof was run on this host after the receipt/identity fixes:

- Command: `make prove-local`
- Current ledger: `.artifacts/prove-local/proof-ledger.txt`
- Ledger metadata: commit `2814477aa654d62dbe1a5d8db1d19197de6e9bd7`, `started_at=2026-07-09T02:38:40Z`
- Result: `313 pass, 0 skip, 0 fail`
- Key live rows: `job-embed`, `job-infer`, `job-whisper`, `job-classify`, `job-embed-multi`, `openai-batch`, `warm-routing`, `budget-cap`, `metrics-planed`, `admin-console`, and `multi-agent`.

Normal test target:

- Command: `make test`
- Result: Rust `176 passed, 45 ignored`; Go `go test ./...` green.

## BLACKHOLE density pass

Pre-pass target set from the audit sweep:

- `agent/src/runners.rs`: 9873
- `control/integration_test.go`: 7053
- `control/store.go`: 4626
- `control/api.go`: 3623
- `agent/src/main.rs`: 3147
- Target-set total: 28322

Post-pass target set:

- `agent/src/runners.rs`: 9873
- `control/integration_test.go`: 6874
- `control/store.go`: 4626
- `control/api.go`: 3623
- `agent/src/main.rs`: 3153
- Target-set total: 28149

Delta:

- Target set: -173 LOC
- New domain file: `control/budget_integration_test.go`, 189 LOC
- Repo counted LOC: 64902 across `agent/src` and `control`

What moved:

- Budget governor integration proofs moved out of `control/integration_test.go` into `control/budget_integration_test.go`.
- The extracted rows still pass inside the 276-proof matrix: `TestBudgetCapPausesDispatch` and `TestBudgetCapCountsInflightTasks`.

Repo/artifact mass after the live proof:

- Files: 15268
- Directories: 2725
- Repo size: 22G
- `.artifacts`: 710M
- Largest artifacts: three Cycles runtime tarballs at about 191-192M each; `.artifacts/prove-local/control` at 19M; `.artifacts/prove-local/cx` at 8.3M.

## Distinct routing receipts

Code boundary:

- The agent now treats the control-plane registration response as authoritative for `worker_id` and `supplier_id`.
- After registration, status, heartbeat, and receipts use the server-bound IDs instead of the pre-registration placeholder ID.

Live harness boundary:

- The proof script launches a second agent with a separate token, supplier id, config, data dir, and status path.
- The proof does not trust a seeded worker row or quiet logs. It waits for the second worker row to be rewritten by live registration with `version <> 'seed'` and a fresh `last_seen_at`.
- The live proof then submits one multi-task job and queries completed tasks for `COUNT(DISTINCT worker_id)`.

Live receipt:

- `multi-agent`: `2 distinct agents committed results for one job (local two-agent proof; physical two-Mac proof remains external)`.

Physical/vLLM boundary:

- This host is `Darwin arm64` on `Apple M3 Pro`.
- `nvidia-smi` is not available.
- No `VLLM`, `CUDA`, or remote second-host environment variable was present during the receipt sweep.
- Therefore this run proves real control-plane distinct-worker routing and task commits, but it is not a physical two-Mac proof and not a live CUDA/vLLM lane proof.

External receipt still required for the physical distinct-node claim:

- Two host identities or one live vLLM/GPU endpoint.
- Worker IDs, supplier IDs, engine tags, build hashes, hardware classes, model class, verification class, memory, thermal state, and tok/s.
- Quote/routing response, submitted job ID, task IDs, completed task rows, worker IDs per task, result receipt, and teardown.
- Wall-clock comparison must avoid the refuted naive "fan-out beats A100" claim; use the existing measured crossover tests and quote voice.

## Hawking boundary

Hawking remains a thin capability/receipt boundary:

- Worker capability carries `engine` and `build_hash`.
- Receipts expose verification classes as `engine|build_hash`.
- The control side allows engine tags including `candle`, `mlx`, `vllm`, and `hawking`, but byte-exact comparison is pinned to the same `(hw_class, engine, build_hash)` class.
- Hawking-specific honeypot tests prove same-class pass/fail and cross-class skip behavior.

No Hawking codebase merge was performed. The integration point is the receipt/capability class only: engine tag, build hash, model class, verification class, tok/s, memory, thermal state, and output agreement policy.
