<!-- CLAIM-SCOPE: internal-evidence-response-non-marketing -->
# Evidence response to the “brutal audit”

The useful goal is not to prove the audit wrong as one narrative. It mixed false,
stale, directionally correct, and inherently external claims. The defensible response
is a claim ledger: give each assertion an acceptance test, retain the criticisms that
survive, and refuse to promote code, mocks, or one-machine measurements into market
proof.

The executable scorecard is [`proof/5x5-gates.json`](../proof/5x5-gates.json), run by
[`scripts/five-by-five.py`](../scripts/five-by-five.py). The accepted local contract
ledger at `.artifacts/prove-local/proof-ledger.txt` is the sole authority for its
narrow local claims, and only while `scripts/verify_proof_ledger.py` accepts its
terminal status, required PASS rows, proof mode, and matching current/start/end
source identity. An earlier diagnostic exposed 12 integration fixtures that still
treated legacy worker arrays as dispatch authority; it stopped without a terminal
envelope, and none of its partial rows were admitted. The fixtures were corrected to
use exact current capabilities rather than restoring the legacy fallback. No test
count in this document is a product score, and no local ledger proves real payouts,
fleet scale, market demand, hardware soak, legal clearance, or production
availability.

## Claims that do not survive repository inspection

| Audit assertion | Repository fact | Honest verdict |
|---|---|---|
| Roughly 180K Go, 95K Rust, and 91K Python lines | At this inspection, repository-visible sources were about 53.4K Go, 41.4K first-party Rust (49.8K including the checked-in `agent/vendor` tree), and 77.4K Python lines. The worktree is active, so these rounded counts are a dated diagnostic, not a product metric. | The scouting figures were materially wrong and should not be used to establish audit depth. |
| The Stripe/Trolley payout rail is only a stub | `control/payment.go` contains a Stripe Connect transfer implementation bound to the supplier account with payout-row idempotency and returned-reference checks. | “Only a stub” is false as a code statement. A real charge, Connect payout, reversal/refund, provider fee, and reconciliation have not been proven, so the product-money conclusion remains open. |
| The default 3% take necessarily loses money below roughly $300 | Executable quotes no longer treat a flat percentage take as the margin guard. They require a versioned economic schedule, freeze supplier payout independently of buyer safety fees/SLA premium, price a standalone processor fixed fee plus variable fee and per-task control cost, reserve bounded extra accepted work, and block when every modeled partial/SLA scenario cannot meet the configured margin floor. A supplier provider call now additionally requires an integer-cent reservation from the causally linked historical succeeded-PaymentIntent fact or a capped operator-declared subsidy pool. | The old default arithmetic is stale as a statement about current admission code, and the narrow cash-boundary and minor-unit settlement prerequisites have current-source contract evidence. This does **not** prove realized profitability: retries outside accepted reserve, reversal/refund/chargeback recovery, full platform costs, external treasury reconciliation, and a paid corpus remain open gates. |
| GPU routing is hard-coded to `litGPUWorkers == 0` | Quotes and submissions call `EligibleVLLMWorkerCount` over live registered workers. | The literal claim is stale. Production vLLM readiness remains gated; a live count must not turn a soak-only runtime into supported supply. |
| A stranger cannot create an account or start supplier onboarding | Buyer signup/login and authenticated supplier ownership/onboarding routes exist. Supplier access is owner-bound and tax identity is delegated to Stripe-hosted onboarding. Enrollment wire v2 stores the trusted approval origin/request id with the short-lived account/audience/P-256-key-bound code, signs both in the device exchange transcript, and rejects relay substitution. The authenticated approval adapter and proofed one-time exchange have current-source contract evidence; the Mac app also remembers the exact pending request across restart and refuses a mismatched approval before network use. | The absolute claim is stale, but this is not yet a 5/5 onboarding outcome. The runtime credential remains bearer-only, the legacy unbound mint route remains, the entitled persistent-Keychain assertion is not runnable in the unsigned SwiftPM host, the account-facing browser/deep-link UI and response-loss recovery are absent, and public signed distribution is unfinished. |
| Project policy and terms readiness | This work is deliberately outside the current engineering release and is not an executable local proof gate. | It remains a separately authorized business-owner and professional-review task; no engineering result closes it. |

## Claims that were based on real evidence but overstate what it proves

| Audit assertion | Missing control | Required proof |
|---|---|---|
| vLLM is about 19x faster on the “identical A100” | The 44,269 tok/s vLLM run and 2,345 tok/s Candle run differ in engine, model, precision, workload exposure, and batching regime. It is a useful buyer-strength baseline, not an identical-stack comparison. | Pin model weights, tokenizer, precision, prompts, output length, batch policy, engine versions, GPU SKU, and provisioning/queue time. |
| The M3 Pro sustained run proves a 36.6% thermal decline | The recorded drop overlaps unrelated concurrent release builds and a system load above 66; throughput partially recovered when those builds ended. | Repeat on an otherwise-idle host while recording temperature, power, process load, fan/thermal state, and recovery. |
| A green local proof means the remaining work is external | Broader local economics, runtime lifecycle, crash recovery, the complete app outcome, API contracts, artifact lanes, and operational drills remain. | The proof script says explicitly that its matrix is not product 5/5 or launch readiness. |

## Criticisms that still stand

- No local test can prove marketplace liquidity, repeat paid use, production payouts,
  30-day margins, or a 99.9% production SLO.
- Spec-decode correctness is not a product speedup. It needs real-model, pinned,
  end-to-end evidence and ordinary job/receipt/billing integration.
- Secret-keyed post-commit audit sampling now has current-source contract evidence,
  including production refusal of missing or source-known fallback secrets. Durable
  verification work recovery, execution identity, a quantified collusion bound, and
  a physical adversarial verdict-to-reversal trace remain open.
- The canonical runtime matrix now rejects unsupported ingress and registration and
  materializes exact per-worker scheduler cells, so the old six-job by four-model
  Cartesian authority no longer counts or claims supply. That exact scheduler-
  authority prerequisite has current-source contract evidence. The full runtime gate
  remains open: agent advertisements, dispatch resolver kind, catalog lifecycle,
  receipts, attestation, promotion/rollback, and physical evidence are not yet
  matrix-bound.
- Quote-derived `jobs.actual_usd` is settlement revenue, not independent execution
  cost. Price auto-tuning has therefore been disabled until independent economic facts
  and a margin guard exist.
- The narrow cash-call boundary now rejects non-succeeded or amount/currency-
  mismatched PaymentIntents, records each historical receipt fact once across job
  and batch sources, and reserves integer cents before a supplier provider call.
  Unfunded and partially funded liabilities become explicit `awaiting_funding` debt
  outside the bounded due page; only exact collection cash or a finite, row-locked
  subsidy reservation re-arms them. Exact retries reuse one immutable reservation
  and payout key. Definitely-unsent failures become `ready`, while transport/5xx/
  malformed-success and stale-lease ambiguity remains sticky `outcome_unknown`,
  cannot be admin-rearmed, and stays `reversal_required` through a clawback until
  cash is resolved. Admin owed balances decrease only for a durable `cash_moved`
  operation; manual export is synced but non-cash. This narrow prerequisite has
  current-source contract evidence for those adversarial cases. Its theorem is
  deliberately call-side and historical: it does not establish that the
  PaymentIntent remains unrefunded or uncharged-back, automate provider lookup or
  reversal, or prove that Stripe cash is currently available.
- Minor-unit settlement is now its own current-source gate. The implementation
  freezes `floor_cent_carry_v1` in the economic plan, reserves the complete
  six-decimal supplier liability, passes exact cents/currency to the rail, and stores
  an append-only equation from liability microusd to cash cents plus non-negative
  remainder. Zero and sub-cent rows move to an explicit carried state instead of
  aborting the sweep; expired `sending` leases become conservatively unknown before
  any same-key retry; cash, possible cash, and carried value render separately. This
  narrow prerequisite has current-source contract evidence covering half-cent
  boundaries, repeated micro-liabilities, funding starvation/re-arm, concurrency,
  crash/response loss, cash-proof reporting, and dust-before-payable order.
  Floor-and-carry deliberately preserves rather than aggregates dust, so future
  supplier-level aggregation is an optimization, not permission to count remainder
  as margin or cash.
- The narrow subsidy money-authority prerequisite now has current-source contract
  evidence. A passkey request retains its originating credential and session UUID;
  a break-glass request retains the stable API-key UUID and is labeled
  `shared_credential_only`, never as a named human. Both are revalidated under a
  database lock inside the money transaction. Each new subsidy pool or liability
  reservation has one append-only typed action, normalized semantic SHA-256 binding,
  target/ref/cents/currency/reason, and a unique two-way deferred database link;
  exact retries preserve the first actor while conflicting retries fail. Injected
  action/resource failures roll both sides back, forged principal ids and mismatched
  links fail at commit, and the paginated `no-store` review surface omits raw detail,
  treasury references, credential material, and key/session hashes. Pre-provenance
  rows are shown as `legacy_unattributed` and cannot silently become authority for a
  new reservation. This proves credential-level attribution, not which human held a
  shared key, database-superuser resistance, external/WORM retention, or treasury
  cash. Other admin mutations, including payout-hold release, still need the same
  attributed idempotent-intent treatment before the whole operator trail is 5/5.
  Treasury references also remain operator assertions rather than bank
  reconciliation; reversal/refund/chargeback recovery, complete platform-cost
  attribution, and paid-corpus economics remain open.
- A private, unsigned app and locally built SDK/CLI archives are not public
  distribution. Signing, notarization, package publication, clean-machine installs,
  and stranger time-to-result remain separate gates.
- The canonical API/client support contract now derives all registered Go routes and
  auth kinds, every public Python method and CLI command, and the exact async/poll/
  completed-artifact behavior from shipped source. It explicitly rejects sync and
  token-stream claims. The former `gpt-*` -> Llama and `text-embedding-*` -> MiniLM
  rewrites have been removed; those names now return a typed error naming the native
  model to use, and accepted native targets must have an exact production runtime
  cell. The broad API outcome remains open because JavaScript is absent and most
  client operations lack operation-specific black-box network contracts.
- The Mac-side enrollment ceremony is now wired to the real one-time exchange, but
  that is a prerequisite rather than the terminal-free outcome: the authenticated
  approval adapter and proofed exchange core have current-source contract evidence,
  but an account-facing browser/deep-link UI still needs to call and present that API;
  response-loss/restart recovery is incomplete, local reset does not revoke the
  server credential, and ordinary worker requests still authenticate with a copyable
  bearer.
- The public site is intentionally later. Publishing it before the install, policies,
  receipts, build identity, and status surface are real would manufacture appearance,
  not product evidence.

Repository inspection twice caught flaws in the claim-control pass itself. The first
inventory omitted `web/index.html` and `web/demo.html`; a later adversarial review
showed that manually listing those two still left the public admin HTML and dozens of
top-level product/research documents unclassified. The policy now discovers root and
top-level documentation, client READMEs, public HTML, and project legal drafts; every
discovered file must be actively scanned or carry an enforced archival/internal
marker, and an adversarial new-file test rejects inventory escape. Active pages reject
static proof counters and require visible development-preview/simulation boundaries;
the old `docs/SITE-CLAIMS.md` ledger remains historical rather than current evidence.
This correction is part of the rebuttal: a claim gate that can omit an actual claim
surface is not a gate.

## Audit gaps now represented explicitly

The audit's strongest surviving objections are no longer buried in prose. The
canonical registry now has separate gates and next actions for:

- all-attempt margin safety, real money movement, current competitive price,
  supplier net value, and a paid-margin corpus;
- colluding model/precision substitution, burst and long-con detection bounds, and
  an actual adversarial financial reversal;
- buyer lifecycle/self-serve activation, central-topology claim honesty, liquidity,
  disconnect/rejoin, and credential-revocation recovery;
- physical apples-to-apples/thermal/workload benchmarks against paid and free local
  substitutes;
- end-to-end agent/dispatch/receipt runtime authority, a buyer-demanded CUDA/
  PyTorch-or-container cell, promotion/rollback rules, two-Mac and bounded-spend
  CUDA proof, and lane soak;
- held-out render/transcode generalization and ordinary verified billing;
- terminal-free enrollment recovery, request-time device binding, app-initiated
  revocation, signing/notarization, and clean installs;
- explicit sync/stream support boundaries, regression-proof rejection of branded
  cross-model aliases, JavaScript/npm distribution, operation-level black-box API
  contracts, and stranger time-to-result;
- name-collision screening, marketing-claim conformance, confidential-compute
  non-claims, incident drills, wedge validation, repeat paid use, and any future
  data-moat claim.

Scaffold validators are marked `prerequisite`; they do not count as those product
outcomes. Each unproven gate carries a concrete `next_action`, while external gates
state the hardware, provider, professional, or user evidence that local code cannot
manufacture.

## What now constitutes a rebuttal

A rebuttal is a reproducible artifact tied to one narrow claim. Examples:

1. Ownership: Buyer A is unable to read, mutate, onboard, or mint a token for
   Supplier B in a database integration test.
2. Settlement: a missing result object remains `verifying` and unpaid; after upload,
   verdict, counters, accepted telemetry, and ledger rows commit once in one database
   transaction.
3. Receipt honesty: one independently checked chunk in a ten-chunk job produces
   `sampled-verified`, never `fully-verified`.
4. Multi-lane orchestration: an inventory-driven run records distinct worker identity,
   lane, engine, build hash, artifact hash, settlement, and cleanup. Mock mode proves
   orchestration only; physical Apple/CUDA gates remain external.
5. Economics: every paid job eventually has independently sourced units, supplier
   liability, actual processor fee, refunds, net billed amount, and contribution margin;
   unknown facts stay unknown rather than zero.

The audit is therefore neither accepted wholesale nor dismissed wholesale. Its
falsifiable findings become gates; its stale statements are corrected with source and
tests; and its market/hardware conclusions stay open until the corresponding external
evidence exists.
