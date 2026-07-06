# Control plane hot path 4.5→5 — "the benchmark now measures the real query"

> Produced against a dedicated, throwaway local stack stood up for this rung
> specifically: native Postgres 17 (port `5460`, unix socket
> `/tmp/cx_bundleBQ_pgsock`) + native MinIO (S3 API `:59300`, console `:59301`,
> bucket `cx-jobs`). This answers
> docs/internal/CREED_AND_PATH_TO_TEN.md's "Control plane hot path & queue
> performance" facet, rung **4.5 → 5: Make the benchmark measure the real
> query**.

Generated 2026-07-05T21:36:56Z · git commit `f8dc90d` (dirty — uncommitted
session work present) · Apple M3 Pro · 12 cores · 19 GB RAM · Darwin 25.6.0
arm64

## What the rung asked for

> Replace the simplified stand-in query in the bench harness with the exact,
> verbatim SQL from `scheduler.go`'s `ClaimTask`, run under realistic planner
> settings (no forced `enable_seqscan`), so the very first measurement is
> honest about what's actually shipped.
>
> Proof artifact: the bench harness file diff shows the stand-in query
> replaced with the real one, and a committed run of the corrected bench.

## What already existed in the working tree

This exact fix was already present, uncommitted, in the working tree before
this pass started (`docs/internal/CREED_AND_PATH_TO_TEN.md`'s Implementation
Log, entry 49). This report exists to independently reproduce and verify that
claim against fresh, dedicated infra — not to redo the implementation.

**The shared-constant mechanism** (`control/scheduler.go`):

- A new package-level function, `ClaimTaskSQL(claimedByPredicate string) string`,
  renders the EXACT claim CTE text — every JOIN, every correlated subquery
  (`cheaper_class_online`, `worker_tps`, `warm_for_task`,
  `job_dispatched_count`, the budget-governor projected-spend subqueries),
  and the full computed `ORDER BY` — parameterized only by the one condition
  that differs between the pinned and general claim branches.
- `Store.ClaimTask` does `claimTaskQuery := ClaimTaskSQL` and calls that
  function value directly — not a copy of the string, the actual function
  reference — so there is no second hand-maintained copy of this SQL to
  drift out of sync.
- `control/main.go` adds a `control print-claim-sql` subcommand that calls
  `ClaimTaskSQL` directly and prints its return value verbatim to stdout —
  the seam `scripts/bench-local.sh` uses to EXPLAIN ANALYZE the literal
  production string instead of a hand-copied stand-in.
- `scripts/bench-local.sh` shells out to `control print-claim-sql`, asserts
  the output contains `cheaper_class_online` (i.e. is really the full query,
  not a stand-in) and does NOT itself contain `enable_seqscan` (i.e. the
  query text embeds no forced planner GUC — realistic planner settings are
  the harness's job, not the query's), then splices the literal bytes into a
  `BEGIN; PREPARE cx_bench_claim(...) AS <literal SQL>; EXPLAIN (ANALYZE,
  TIMING ON, FORMAT JSON) EXECUTE cx_bench_claim(...); ROLLBACK;` block via
  `printf` (never an unquoted heredoc, which would let bash itself expand the
  query's own `$1`/`$2`/`$3` Postgres bind placeholders).
- `control/control_test.go`'s `TestClaimTaskSQLIsTheSharedConstant` is a
  no-DB unit test that locks in every JOIN/subquery/ORDER-BY fragment and
  the absence of `enable_seqscan`, so a future edit that reintroduces a
  second hand-copied query text fails a fast test, not just a slow load run.

## Independent verification performed in this pass

1. **Byte-identity proof, not just an assertion.** Ran `control print-claim-sql`
   directly (`cd control && go run . print-claim-sql`) and diffed its output
   against `.artifacts/bench-local/claim_query.sql` (the file the harness
   actually spliced into its `EXPLAIN ANALYZE` block during the same run):
   **byte-for-byte identical**, 11,680 bytes / 184 lines both ways. Because
   `ClaimTask` calls the identical `ClaimTaskSQL` function value (confirmed by
   reading `control/scheduler.go` lines 740-742), this closes the loop: the
   harness's `EXPLAIN ANALYZE` target, the CLI's printed output, and
   `ClaimTask`'s own executed SQL are the same Go value by construction, not
   by discipline.
2. **`go build ./...`, `go vet ./...`, `gofmt -l .`** — clean, with the one
   pre-existing `gofmt -l` hit (`control/webauthn.go`) unrelated to this
   change and unchanged by it.
3. **Full unit suite** (`go test ./...`, no infra) — green.
4. **Dedicated throwaway infra stood up for this pass**: native Postgres 17
   on port `5460`, unix socket directory `/tmp/cx_bundleBQ_pgsock`, database
   `cx`; native MinIO on `:59300` (console `:59301`), bucket `cx-jobs`.
   `db/schema.sql` applied cleanly.
5. **Full integration suite** (`go test -tags integration -count=1 ./...`
   against that stack) — green, **14.123s, zero regressions**. Explicitly
   re-ran just the claim-path tests to confirm each one named in the original
   claim: `TestClaimTaskSQLIsTheSharedConstant`,
   `TestFailEndpointOnlyClaimingWorker`,
   `TestClaimDispatchInterleaveFairness`, `TestClaimHardFilter` (all 11
   sub-cases: unsupported job type, unsupported model, insufficient memory,
   wrong hw class, data residency mismatch, offered rate below worker floor,
   supplier quarantined, worker throttled, effective memory below job min,
   reputation gate, private pool), `TestRescueDeadClaimRequeues` — all PASS.
6. **Ran `scripts/bench-local.sh` twice** against this stack (once fresh
   provisioning, once `KEEP=1` reusing the same stack) to confirm the
   measurement is repeatable and not a one-off artifact.

## The real, measured numbers (both independent runs)

Both runs seeded the harness's default realistic-scale synthetic queue: 60
suppliers, 300 workers, ~200 synthetic jobs of 50 tasks each (a third
budget-capped with real in-flight `running` tasks), totaling 10,000 tasks /
9,500 claimable, spread across all nine `hw_class` cost ranks. Every run
executed the literal `control print-claim-sql` output inside `BEGIN; PREPARE;
EXPLAIN (ANALYZE, TIMING ON, FORMAT JSON) EXECUTE; ROLLBACK;`, under default
(realistic) planner settings — no forced `enable_seqscan` anywhere — and the
harness re-checked queue depth after every run to confirm the rollback left
the queue unchanged (repeatable measurement, not a draining one).

| run | queue | workers | runs | p50 | p90 | min | max | plan |
|---|---|---|---|---|---|---|---|---|
| fresh provision | 9,500 claimable | 301 | 11 | 1219.408 ms | 1247.178 ms | 1198.860 ms | 1283.103 ms | seq scan |
| `KEEP=1` reuse | 9,500 claimable | 301 | 11 | 1391.546 ms | 1507.097 ms | 1257.401 ms | 1552.653 ms | seq scan |

Both runs report **plan = seq scan**, not an index scan on
`tasks_ready_unclaimed_idx` — this is the honest, unflattering number this
rung exists to surface (per the facet writeup's predicate-mismatch finding
between the partial index and the query's `claimed_by` OR-branch), not a
gamed one. Fixing that is explicitly the next rung (5→6, "Fix the index so
the real query can use it") and is out of scope here.

Unrelated baseline measured in the same runs, included for completeness:
`POST /v1/quote` p50 ≈ 1.5 ms / p90 ≈ 1.9-2.1 ms (n=30) — unchanged by this
rung, not the subject of this report.

## Reproducing this

```
scripts/bench-local.sh
# or, to pin the exact ports/socket used for this report:
BPGPORT=5460 BMINIO_PORT=59300 BMINIO_CONSOLE=59301 BCONTROL_PORT=18190 scripts/bench-local.sh
```

`.artifacts/bench-local/report.md` and `.artifacts/bench-local/claim_query.sql`
are regenerated per run and gitignored by repo convention (see
`.gitignore`); this file is the committed, durable record of the numbers
actually observed.

## Infra teardown

Both the dedicated Postgres (port 5460, `/tmp/cx_bundleBQ_pgdata`,
`/tmp/cx_bundleBQ_pgsock`) and MinIO (`:59300`, `/tmp/cx_bundleBQ_miniodata`)
instances stood up for this pass were stopped and their data directories
removed after this report was written.
