# Verification & Result Trust 7→8 — adversarial quarantine bounds

> Produced against a real, throwaway local stack: native Postgres 17 (port
> 55917, unix socket `/tmp/cx_adv_pgsock`) + native MinIO (S3 API `:55918`,
> console `:55919`, bucket `cx-jobs`), `db/schema.sql` applied verbatim. This
> answers docs/internal/CREED_AND_PATH_TO_TEN.md's "Verification & result
> trust" facet, rung **7 → 8: Prove gameability bounds with a real adversarial
> harness**. The harness is `control/adversarial_test.go`
> (`TestAdversarialGameabilityBounds`), gated behind `-tags integration` like
> the rest of the deterministic proof matrix.

Generated 2026-07-06T02:44Z · git commit `f8dc90d` (dirty — uncommitted
session work present) · Apple M3 Pro · Darwin 26.6 (build 25G5028f)

## What was built

A real adversarial worker — not a mock. It authenticates with its own
freshly-minted `worker_tokens` row (`Store.CreateWorkerToken`, the same
onboarding path a real supplier uses), makes real HTTP calls against the real
running control-plane test server (`GET /v1/worker/poll`, `POST
/v1/worker/task/{id}/commit` — the exact handlers a genuine Rust agent hits),
and really PUTs bytes to real MinIO-backed storage at the presigned result
key. The only thing "adversarial" about it is *what bytes it chooses to
commit*; the wire protocol is indistinguishable from an honest worker.

Three cheat strategies, each a real, repeatable scenario function:

1. **Garbage** (`runGarbageScenario`) — commits random/malformed bytes
   (`garbageBytes()`: a non-JSON prefix plus real `crypto/rand` bytes) instead
   of a real computed result, on every task including the honeypot.
2. **Replay** (`runReplayScenario`) — harvests a real, honestly-computed
   result from an entirely separate earlier job (`replayJob`), then commits
   that stale, previously-seen result verbatim on every later, different
   task instead of ever computing a fresh one.
3. **Honeypot-skim** (`runHoneypotSkimScenario`) — recognizes the seeded
   honeypot exactly the way `TestPipelineChaining`'s `driveOneTask` helper
   already does (the presigned `input_url` contains the seeded honeypot's
   known input ref, `demoHoneypotEmbedRef`) and answers it correctly, while
   committing garbage on every real, non-honeypot (primary or redundancy)
   task — "passes only honeypots while cheating elsewhere," the rung's own
   third named cheat.

Each scenario submits real embed jobs one at a time
(`redundancy_frac=1.0`,`honeypot_frac=1.0` explicit — the "verification
genuinely on" configuration the 6→7 rung's server-side floor guarantees a
real buyer gets by default), drives the adversary plus **two** independent,
distinct-supplier honest peer workers through the real poll→commit lifecycle,
and counts the adversary's own real task commits until the engine
auto-quarantines it. Quarantine is confirmed two ways: (a) a real DB read,
`suppliers.status = 'suspended'`, and (b) a real subsequent poll refusal —
since a suspended supplier's worker is not met with an HTTP 403 (`ClaimTask`'s
claim query hard-filters `s.status = 'active'` in its `WHERE` clause, so it
just finds no eligible row and gets a plain 204, indistinguishable on its face
from "nothing queued"), `quarantinedNow` seeds one genuinely claimable task
for exactly this worker's (job_type, model) pair immediately beforehand, then
confirms the poll *still* returns 204 despite real claimable work existing —
that gap (real work present, nothing dispatched) is the actual, unambiguous
refusal signal.

Each of the 3 scenarios is run 5 times per test invocation against fresh
identities (no shared quarantine/reputation state across runs), and the whole
test invocation was run 4 times total (20 real runs per scenario) to confirm
the bound is consistent, not lucky.

## Why two honest peers, not one

The first version of this harness used a single honest peer and found the
honeypot-skim cheat **completely undetectable** — a real, if initially
unwelcome, finding. The reason: a bare 2-way primary/redundancy disagreement
only ever reaches `pass_with_penalty` in `control/verification.go`'s own
verifier — it takes a genuinely independent **third** worker actually
claiming and committing the pinned tiebreak task for `resolveTiebreak` to run
its real N-way vote and dock/claw back the confirmed loser. With only one
honest peer, that third opinion could never be dispatched to anyone able to
answer it (the shared integration harness's seeded demo worker turned out to
be a live, eligible candidate that `SelectRedundancyPeerExcluding` sometimes
picked instead of this harness's own peer, leaving the pinned tiebreak task
queued forever — see the "harness bugs found and fixed" section below). A
second honest peer, and excluding the shared demo worker from candidacy for
the duration of this test, made the tiebreak path actually resolve through
the real HTTP path.

## Headline result — measured N per scenario (own real task commits before real quarantine)

| scenario | runs | min N | max N | median N | published bound |
|---|---|---|---|---|---|
| garbage | 20 | 1 | 5 | 2 | **N ≤ 10** |
| replay | 20 | 1 | 12 | 3 | **N ≤ 20** |
| honeypot-skim | 20 | 15 | 26 | 20.5 | **N ≤ 40** |

All 60 real runs (20 per scenario, across 4 separate full test invocations)
landed inside the published bound. Full per-run values:

- **garbage:** 2,1,3,1,1, 2,2,2,3,1, 2,2,5,2,2, 2,1,3,1,1
- **replay:** 4,3,3,2,6, 4,4,1,7,1, 12,1,1,7,1, 11,3,2,3,1
- **honeypot-skim:** 17,18,17,20,18, 23,21,23,24,21, 18,26,18,15,22, 23,19,23,26,19

## Why garbage/replay are fast and honeypot-skim is structurally slower

**Garbage and replay are caught almost immediately** because every task they
touch — including the honeypot, which they do not even try to recognize — is
wrong. Each job's task ordering is randomized across 3 real pollers (adding
this randomization was itself a harness fix — see below), so which task type
the adversary draws varies job to job; as soon as it draws the honeypot
dispatch, it fails, and `QuarantineSupplier` fires **unconditionally**
(`control/verification.go`'s honeypot-fail branch: dock reputation, claw back
any credit, quarantine, requeue — independent of the resulting reputation
number). Replay's tail is a little longer than garbage's (max 12 vs. max 5)
because an honestly-computed-but-stale embed result can occasionally
cosine-agree with a redundancy peer by chance on a *different* input before
the honeypot draw catches it — garbage never does, since it fails every
comparator unconditionally.

**Honeypot-skim is fundamentally different and structurally slower to catch**
by design and by construction: it always passes the honeypot, so it is never
caught by the fast honeypot-fail path at all. Its only detection path is
reputation eroding via repeated **confirmed tiebreak losses**
(`EventMismatch`, −0.10 each) until it crosses `quarantineRepFloor` (0.2) from
a 0.90 start — 8 confirmed losses in the worst case, each of which requires
*both* a sampled tiebreak dispatch (`effectiveCheckProb`, reputation-weighted)
*and* a real third worker actually resolving it. This produces a genuine,
worth-publishing asymmetry: **passing honeypots keeps the cheat's own
reputation elevated, which (by the engine's own reputation-weighted audit
design) *lowers* the sampled-tiebreak rate**, partially offsetting its own
detection risk. It is still reliably caught — 20/20 real runs, max N=26 — just
several times slower than a cheat that also fails the honeypot.

## Harness bugs found and fixed along the way (real findings, not noise)

Building this harness surfaced two real, independent bugs — both fixed as
part of this pass, not left as "the adversary evaded detection":

1. **The shared integration test harness's verifier never ran the 3-way
   tiebreak path at all.** `control/integration_test.go`'s `TestMain`
   constructed `NewVerifier(itStore)` **without** `.WithStorage(itStorage)`,
   unlike `main.go`'s real production wiring
   (`NewVerifier(store).WithStorage(storage)`). `dispatchTiebreak`'s own
   early-out (`if v.storage == nil { return nil }`) silently no-ops in that
   configuration — every HTTP-driven integration test's 2-way redundancy
   mismatches were capped at `pass_with_penalty` forever, never escalating to
   a real docked/clawed-back tiebreak loser through the real poll→commit
   path. This was invisible until an adversarial scenario actually needed the
   tiebreak path to fire. Fixed with a one-line change
   (`control/integration_test.go`'s `TestMain`) to match `main.go` exactly;
   confirmed zero regressions across the full existing 210-test integration
   suite before publishing this report.
2. **A fixed poll order starves the adversary of ever drawing the
   honeypot.** The first harness iteration polled the same 3 identities in
   the same fixed order every round. `ClaimTask`'s `ORDER BY` is stable
   across ties (e.g. insertion order — primaries are created before their
   redundancy/honeypot clones), so the *same* identity drew the *same* task
   "slot" in every single job — an artifact of the harness's own poll
   scheduling, not a real property of a hostile worker or the engine, and one
   that silently made the garbage scenario look far slower to catch than it
   really is. Fixed by shuffling poll order every round
   (`driveAdversarialJob`, `math/rand.Shuffle`).
3. **The shared seeded demo worker competed for the tiebreak-peer slot.**
   `SelectRedundancyPeerExcluding`'s real peer search sometimes picked the
   shared demo worker (live, eligible, same hardware class) over this
   harness's own second honest peer — the resulting pinned tiebreak task then
   sat `queued` forever because the harness never polls as the demo worker.
   Fixed by staling out the demo worker's liveness
   (`excludeDemoWorkerFromCandidacy`) for the duration of this test.

A fourth, smaller and out-of-scope finding was flagged separately (not fixed
here): when a job's garbage primary happens to be the last task committed,
`finalizeJobIfDone`'s buyer-facing merge step throws an unhandled parse error
and the commit handler returns a raw HTTP 500 to whichever worker committed
last — even though verification (dock/clawback/quarantine) and payout
scheduling both already ran and fully completed before that point. The cheat
is still correctly detected and penalized; only the buyer-facing job
status/HTTP response in that specific case is ungraceful. Spun off as its own
follow-up task rather than folded into this rung.

## Verification performed

- Full existing integration suite (`go test -tags integration -count=1
  -v ./...`) confirmed green **before** any change: 210 passed, 0 failed.
- Full existing integration suite re-confirmed green **after** the
  `TestMain` fix and the new harness landed, run together: **211 passed** (210
  pre-existing + the new `TestAdversarialGameabilityBounds`), **0 failed**.
- `TestAdversarialGameabilityBounds` run 4 separate times end to end (20 real
  runs per scenario total) — every single run landed within the published
  bound.
- `gofmt -l control/ | grep -v webauthn.go` — empty.
- `go vet -tags integration ./...` — clean.
- Unit suite (`go test ./...`, no tag) — green throughout.

## Honest caveats

- **Bounds carry real headroom over the observed max**, not a tight fit —
  10 vs. observed max 5 (garbage), 20 vs. 12 (replay), 40 vs. 26
  (honeypot-skim) — so ordinary scheduling/sampling variance on a slower or
  busier CI box does not flake the test on a detection-strength difference
  that isn't really there. The tight, real numbers are published above, not
  hidden behind the generous CI-safe bound.
- **Single-box test**, same caveat as the claim-load report: Postgres, MinIO,
  and the control-plane test binary all shared one Apple M3 Pro's cores.
- **Honeypot-skim's N is a probabilistic bound, not a deterministic one** —
  it depends on both a reputation-weighted sampled tiebreak-dispatch decision
  and a real third-worker resolution succeeding repeatedly. 20/20 real runs
  landed well inside the published bound, but this is a measured empirical
  range (15–26), not a mathematical guarantee for every possible run.
- **This proves detection through the real HTTP-driven path this repository's
  own integration harness uses**, not yet against genuinely different
  physical hardware (that is rung 8→9's job, gated on real heterogeneous
  suppliers existing).

## Reproducing this

```
initdb + pg_ctl -D pgdata -o "-p 55917 -c unix_socket_directories=/tmp/cx_adv_pgsock" ...
minio server minio-data --address localhost:55918 --console-address localhost:55919
psql ... -f db/schema.sql
cd control && DATABASE_URL=postgres://cx@localhost:55917/cx?sslmode=disable \
  S3_ENDPOINT=http://localhost:55918 S3_PUBLIC_ENDPOINT=http://localhost:55918 \
  S3_BUCKET=cx-jobs S3_ACCESS_KEY=minioadmin S3_SECRET_KEY=minioadmin S3_REGION=us-east-1 \
  go test -tags integration -count=1 -run TestAdversarialGameabilityBounds -v ./...
```

## Teardown

All throwaway infra (native Postgres on :55917, native MinIO on
:55918/:55919, the unix socket dir, `pgdata`/`minio-data`) was stopped and
removed after this run. Nothing from this test was left running.
