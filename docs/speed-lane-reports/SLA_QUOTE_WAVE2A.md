# Speed Lane wave 2A — the wall-clock speed-SLA quote

*2026-07-06. Target item 3 of `docs/research/SPEED_LANE_GOAL_PROMPT.md`: extend
the firm-quote tier into a guaranteed completion time backed by the fan-out
planner's predicted wall-clock — "your N-prompt batch in X minutes,
guaranteed" — with an automatic, deterministic remedy on a miss. Closes both
wave-1B §7 quote follow-ups (the conservative band as the guarantee basis; the
quote-path split-size reconciled with live sizing).*

Proof-layer discipline (same split as wave 1B): everything below is proven at
the **real-control-plane layer** (real Postgres + MinIO + HTTP, fake-GPU
workers) or the **pure-unit layer**. No tok/s claim is made anywhere; the
guarantee's *calibration* against real fleet variance is the owner-gated
remaining step (§7).

## 1. The product

`POST /v1/quote` may now carry an `sla` block:

```json
"sla": {
  "guaranteed_secs": 138,
  "premium_usd": 0.000972,
  "conservative_model_secs": 62,
  "safety_margin_factor": 1.25,
  "merge_allowance_secs": 60,
  "remedy": "If your job's results are not merged within guaranteed_secs ... refunded automatically ..."
}
```

Submitting with `firm_quote=true` + that `quote_id` binds **both** the
existing price cap **and** the time guarantee. The guarantee clock is the
buyer-visible span: `jobs.created_at` (submit) → `jobs.results_merged_at`
(the merged artifact exists). On completion the outcome is decided exactly
once: met → `sla_met=true`, nothing else; missed → `sla_met=false`, **one**
`sla_refund` ledger credit for the premium, one `sla_missed` timeline event,
and the refund netted off the amount actually collected.

## 2. The guarantee formula — every term justified

```
guaranteed_secs = ceil( conservative_model_secs × 1.25 ) + 60
```

- **`conservative_model_secs`** — the planner's CONSERVATIVE makespan band
  (wave 1B, `control/planner.go`): the speed-optimal assignment over the live
  fleet's REAL measured per-worker rates (`worker_tps_cache`), warm bits, and
  measured cold-load times (`benchmark_results.load_ms`), re-costed with every
  rate degraded to **75 % of measured** (`plannerConservativeRateFactor` —
  grounded in the measured 91–111 tok/s serial spread + the thermal
  sustained-vs-peak facet). It includes the queue-ahead term (tasks already
  waiting are in the plan's item count). MODELED from measured inputs, labeled
  as such on the wire (`conservative_model_secs`).
- **`× 1.25` (`slaSafetyMarginFactor`)** — explicit margin for what the band
  structurally cannot see at quote time: claim-poll latency, integer task
  granularity, and queue arrivals between quote and submit (a quote stays
  bindable for 15 min and is not a reservation). Combined effect vs the
  measured-rate p50: 1/0.75 × 1.25 ≈ **1.67×**, which strictly covers the
  measured 1.58× sustained-vs-peak derating (`sustainedDeratingFactor`) — a
  long batch job that thermally throttles is inside the guarantee by
  construction.
- **`+ 60` (`slaMergeAllowanceSecs`)** — the tail the planner never models:
  the clock stops at `results_merged_at`, which lands after the last commit
  (merge time, plus one background completion-sweep cadence of 20 s when the
  synchronous commit-path finalize did not fire). 60 s = 3× that cadence — a
  deliberate allowance, not a measured number, and named as such.

**Honest-degradation preconditions** (`deriveQuoteSLA`, all unit- and
HTTP-tested): a guarantee is offered ONLY when (a) live eligible supply ≥ 5
(`slaMinEligibleWorkers` — the pre-existing gate), (b) the ETA was genuinely
planner-backed (≥ 3 live workers with real measured rates AND the planner
enabled — `CX_DISABLE_FANOUT_PLANNER=1` kills every offer), (c) the band is
positive, and (d) the premium prices above $0. Anything less keeps the quote
byte-identical to pre-wave: **no planner data ⇒ no guarantee, ever.**

## 3. The premium and the remedy semantics

- **Premium** = `roundUSD(expected × 0.15)` (`slaPremiumRate`) — an
  explainable round number (the `privatePoolPremiumRate=0.25` discipline), the
  price of the platform underwriting the guarantee, and **exactly what comes
  back on a miss** — surcharge and remedy can never drift apart.
- **Charging**: at binding, the premium is folded into the job's
  `estimated_usd` — the SAME single money path every cost takes
  (estimate → per-task `buyer_charge` rows → `actual_usd`); the firm price cap
  grows by exactly the premium (`firm_quote_max_usd = cost_max + premium`) so
  the surcharge is priced on top of the committed cap, never squeezed out of
  it. Deliberate consequence, named: the premium flows through the standard
  buyer→supplier/platform split; on a miss the platform absorbs the full
  refund (the firm-quote-overage absorption discipline).
- **Miss remedy**: one `sla_refund` ledger row, `amount = min(premium,
  firm-capped chargeable)` (a refund larger than the bill would mint money,
  not remedy), `payout_ref = 'sla-<job_id>'`. Both charge paths — single-job
  (`JobChargeInfo`) and batch (`firmChargeAmountSQL` in `FormChargeBatch`) —
  net the refund off the collection, floored at $0. No new payment rails.
- **Deliberately NOT wired into `deadline_secs`**: the deadline drives the
  stuck-run watchdog's rescue→KILL ladder (`workers.go reapStuckJobs`). A
  missed SLA must **complete and refund** — killing a late job would destroy
  the buyer's results to punish lateness. `sla_guarantee_secs` is a money
  trigger only; the watchdog's geometry is untouched (its file was not).

## 4. Enforcement — exactly-once by construction

Three sites can observe the same completion:

1. the commit-path finalize (`api.go finalizeJobIfDone` — after the merge
   stamps `results_merged_at` and `actual_usd` settles, BEFORE the charge
   decision, so the refund nets the very first collection),
2. the collect sweep (`collect.go settleSLAOutcomes`, first duty of the 60 s
   `collectCharges` tick, ahead of the Stripe gate and of terminal-coverage
   charging — the backstop for jobs finalized by the background completion
   sweep in workers.go),
3. any re-run of either.

`SettleJobSLA` locks the jobs row `FOR UPDATE`, judges only
`complete`+merged+undecided rows, stamps `sla_met` once (`WHERE sla_met IS
NULL`), and inserts the refund INSERT-if-absent by `payout_ref`, backed by the
new partial unique index `ledger_sla_refund_ref_uniq` — the same replay-proof
pattern as the stripe-fee recorder. The `sla_missed` event is emitted only by
the call that actually decided. Boundary: exactly ON the guarantee = met
("within" is inclusive; unit-pinned).

## 5. What landed (files)

| Piece | Where |
|---|---|
| Offer: `QuoteSLA`, formula constants, `slaGuaranteedSecs`, `deriveQuoteSLA`, buildQuote wiring + warning, persistence (`quotes.sla_guaranteed_secs/sla_premium_usd`) | `control/quote.go`, `db/schema.sql` |
| Wave-1B follow-up (a): quote-path split size now runs `adaptiveSplitSizeLive` (exact record count → width floor applies), explicit `split_size` still wins | `control/quote.go` |
| Wave-1B follow-up (b): `plannerETASecs` returns the conservative band; new `etaBandSecs` (estimateETASecs is now a thin wrapper, signature unchanged) | `control/api.go` |
| Binding: `boundQuote` carries the offer; firm submit stamps `jobs.sla_guarantee_secs/sla_premium_usd`, grows the firm cap by the premium, folds the premium into the estimate, `sla_bound` event | `control/quote.go`, `control/api.go`, `control/store.go` (jobRow/insert), `db/schema.sql` |
| Enforcement: `SettleJobSLA` + `settleSLAOutcome` + `SLAUndecidedCompleteJobs` + sweep pass; finalize hook | `control/collect.go`, `control/api.go` |
| Money netting: `firmChargeAmountSQL` + `JobChargeInfo` net the recorded refund, floored at 0 | `control/collect.go`, `control/store.go` |
| Surfacing: `GET /v1/jobs/{id}` (`sla_guarantee_secs`, `sla_premium_usd`, `sla_met`), invoice/receipt (`sla_premium_usd`, `sla_refund_usd`, `sla_met`) | `control/types.go`, `control/api.go`, `control/store.go` |
| Schema: additive columns on quotes + jobs (`sla_met` outcome), partial unique index `ledger_sla_refund_ref_uniq`; mirrored in `Store.Migrate` | `db/schema.sql`, `control/store.go` |

## 6. Proof (all REAL infrastructure, 2026-07-06, M3 Pro dev machine)

**Unit (pure, no DB)** — 8 new tests in `control/quote_test.go`, suite
130 → 138 PASS 0 FAIL: formula term-by-term (185 = ceil(100×1.25)+60),
guarantee strictly above its own band, conservative-not-p50 basis, every
degradation gate of `deriveQuoteSLA`, premium math at 6-dp rounding (a
$0.003072 quote still prices a non-zero premium), refund capping
(min(premium, chargeable), zero floors), the inclusive miss boundary
(span == guarantee is MET; +1 ms is a MISS), and the binding rules (offer ⇒
guarantee+premium+grown cap; no offer ⇒ price-only; guarantee without premium
⇒ refuses to bind).

**Real-infra integration** (`control/sla_integration_test.go`, fresh native
Postgres 17 on **:55493** + MinIO on **:19300**, schema.sql applied via psql,
shared TestMain harness, real HTTP throughout):

- `TestSLAQuoteHonestDegradation`: 4 eligible workers (real registration +
  benchmarks) → `sla_eligible=false`, **no** sla block; 6 eligible but planner
  disabled → still **no** sla block; planner on → offer present, terms obey
  the formula over its own returned band, premium = 15 % of expected, and the
  quotes row persists exactly the returned offer. Observed offer:
  **guaranteed 138 s = ceil(62 × 1.25) + 60, premium $0.000972**.
- `TestSLAQuoteFirmSubmitGuaranteeMet`: firm submit binds guarantee 138 s +
  premium, firm cap grown by exactly the premium, one `sla_bound` event;
  5-worker fleet completes in **0.31 s measured** (buyer-visible span) →
  commit-path finalize records `sla_met=true`, **zero** refund rows, receipt
  invoice shows met + no refund; two settle-sweep re-runs change nothing.
- `TestSLAForcedMissRefundsExactlyOnce`: slow fleet (declared 1.5 s/chunk) +
  tuned 1 s guarantee (the plan's sanctioned forcing lever) → job **still
  completes** in **1.54 s measured**; exactly **1** `sla_refund` row for
  exactly the premium ($0.000972); a direct re-settle **plus two full sweep
  re-runs** leave it at 1 row (idempotency, the money-truth core); exactly
  **1** `sla_missed` event; `JobChargeInfo` nets $0.007446 → $0.006474
  (= actual − refund); receipt shows premium, refund, and `sla_met=false`.

**Suite status** (identical stack, ports :55493/:19300):

- Unit: **130 → 138 PASS, 0 FAIL** (8 new: 7 SLA tests + the SLA-offer
  degradation matrix), before and after.
- Integration: before **241 PASS, 0 FAIL, 1 SKIP** (pre-existing
  `CX_CLAIM_LOAD` gate), exit 0; after **257 PASS, 0 FAIL, 1 SKIP**, exit 0 —
  the +16 decomposes as **+10 this wave** (7 unit-in-tag + 3 SLA integration)
  and **+6 landed concurrently by the parallel wave-2B bundle**
  (honeypot_hawking_regate_test.go — the tree is shared; a name-set diff of
  the two verbose logs confirms ZERO tests that passed before are missing
  after). One earlier BEFORE invocation showed a suite failure that did not
  reproduce on an immediate identical re-run (the parallel bundle was editing
  its own files in the shared tree at that moment) — recorded honestly as an
  unattributed flake observation; every subsequent run was clean.
- `go build ./...`, `go vet ./...` (default + `-tags integration`): clean.
  `gofmt -l control/` (minus webauthn.go): empty.
- `db/schema.sql` re-applied to the SAME live DB mid-session (idempotent
  upgrade path) — exit 0, new columns/index present, every pre-existing test
  still green afterward.

## 7. Proven vs modeled vs remaining (the honest ledger)

**Proven (this wave):** the offer/degradation logic over real HTTP; the
binding (price cap AND time guarantee) through the real submit path; the
enforcement's exactly-once refund under deliberate re-observation; the netting
on the shared charge math; the buyer-visible surfacing (status, timeline,
invoice, receipt). All at the scheduling/money plane with fake-GPU workers.

**Modeled (labeled, not proven):** the guarantee's *headroom* — the band, the
1.25 margin, and the 60 s allowance are constructed from measured inputs
(rate spread, thermal gap, sweep cadence) but their combined miss-rate on a
REAL heterogeneous fleet is a model until real nodes run under it. The wire
field is named `conservative_model_secs` for exactly this reason.

**Remaining, named precisely:**

- **Guarantee calibration is owner-gated on real nodes**: the L3 multi-node
  run (wave-1B report §6 runbook) should also record, per job, the realized
  span vs `sla_guarantee_secs` (the `eta_calibration` table already pairs
  predicted/realized) — only that data can justify tightening (or force
  loosening) the 1.25/60 s terms. No tightening without it.
- **Jobs that never complete** (failed / watchdog-cancelled) are not judged by
  this wave: their remedy stays the existing partial-settle path and `sla_met`
  honestly remains NULL. Whether a terminal failure should ALSO refund the
  premium is a product decision left open (it would be strictly
  buyer-favorable; one guarded UPDATE + the same refund call).
- **A refund recorded after a charge was already collected** (impossible via
  this wave's ordering — both settle sites run before their respective charge
  sites — but conceivable under a future out-of-band charge) stays a ledger
  credit surfaced on the invoice; there is no automated Stripe refund rail.
- **Dedicated metrics counter** (`cx_sla_misses`) — metrics.go is outside this
  bundle's file set; the structured log + ledger row + timeline event carry
  observability for now.
- The premium's revenue split (suppliers share it via the standard per-task
  split) is a simplicity-first choice, documented in §3 — revisit only if the
  remedy economics ever matter at real volume.
