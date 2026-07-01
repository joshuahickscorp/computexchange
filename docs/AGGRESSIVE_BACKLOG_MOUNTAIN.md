# Aggressive Backlog Mountain

Audit date: 2026-06-30

This is a work queue, not a vibe document. Every item has a proof gate. The goal is to keep
Claude or any agent moving through real repo-grounded progress until the remaining work is either
shipped, measured and rejected, or blocked by a named external dependency.

## Operating Rule

No task is done because a doc says it is. A task is done only when code, tests, docs, and product
surface agree.

## Progress Log

- 2026-06-30 — **Supplier-distinct verification (items 6, 7, 8) SHIPPED** on `feat/perf-wave`
  (uncommitted). Verification was only ever STRENGTHENED; no same-supplier peer is ever treated
  as independent.
  - Item 6 (supplier-distinct peer selection): `prunePeers` (`control/scheduler.go`) excludes
    candidates from the anchor's supplier; `MatchWorker.SupplierID` now carried via
    `CandidateWorkers`. Proof: `TestPrunePeersExcludesSameSupplier`.
  - Item 7 (supplier-distinct match crediting): `independentRedundancyMatch`
    (`control/verification.go`) — a 2-result agreement from the SAME supplier records
    `redundancy_same_supplier` and is NOT credited an independent `redundancy_match`. The
    no-store fallback no longer defaults an unknown peer to the committing supplier. Proof:
    `TestIndependentRedundancyMatch`.
  - Item 8 (supplier-distinct dispute reverification): `dispatchTiebreak` passes BOTH disputants'
    suppliers via the new `alsoSuppliers` arg, so the third opinion is independent of both sides;
    `ErrNoSupply` → provisional trust (never a fabricated pass). Proof:
    `TestPrunePeersExcludesDisputantSuppliers`.
  - Suite: `go build/vet ./...` clean, `go test ./control` green, `gofmt` clean.
  - Still open in this lane: item 9 (receipt label differentiating verified / sampled /
    cross-class / no-peer) — folded into the ClearingReceipt projection (items 13-15).

- 2026-06-30 — **Intake caps + detection honesty (items 16, 17, 19) SHIPPED** on `feat/perf-wave`.
  - Item 16 (per-file cap): `RawFile` now reads through the pure `readCapped` helper
    (`control/intake.go`, 25 MiB/file), returning the typed `errIntakeTooLarge` on overflow
    instead of an unbounded `io.ReadAll`. Proof: `TestReadCappedTypedError`.
  - Item 17 (aggregate cap): the document-set extract goes through the pure `fetchCappedDocs`
    helper (per-file + 200 MiB aggregate cap, typed error). Proof: `TestFetchCappedDocsCaps`.
  - Item 19 (.pdf supported-then-zero-records): a single `documentSetExts` source of truth
    (`.md/.txt/.html`) now drives BOTH detection and extraction, so `.pdf` no longer falsely
    matches. A PDF-only repo is honestly unsupported. Proof: `TestDetectPipelinePdfHonesty` +
    the corrected `TestDetectPipeline` (which had encoded the bug).
  - Suite: `go build/vet ./...` clean, `go test ./control` green, `gofmt` clean.

- 2026-06-30 — **Intake honesty round 2 (items 18, 20) SHIPPED** on `feat/perf-wave`.
  - Item 18 (truncated tree): `Tree` now returns GitHub's `truncated` flag; `handleCreateIntake`
    marks the detection via the pure `withTruncationWarning` (`DetectedPipeline.Truncated` + an
    honest reason note) so a partial listing never yields a confidently-wrong plan. Proof:
    `TestWithTruncationWarning`. Also capped the Tree error-body read.
  - Item 20 (audio not launchable from a repo): `patternLaunchable` (keyed on the persisted
    pattern name) drives both `DetectedPipeline.Launchable` and an EARLY refusal in
    `handleLaunchIntake`, so audio is recognized but cannot launch into the unwired binary-audio
    path. Proof: `TestPatternLaunchable`, `TestDetectPipelineLaunchableFlag`.
  - Suite: `go build/vet ./...` clean, `go test ./control` green, `gofmt` clean.
- 2026-06-30 — **Code-repo embed detector + extractor (item 21) SHIPPED** on `feat/perf-wave`.
  - A shared `codeRepoExts` (Go/Rust/TS/JS/Py/Java/C/C++/...) drives BOTH a new `code-repo`
    `intakePattern` (>=2 source files → an embed-index stage) and the `extractInput` case, so a
    matched file is always fetched (no detect/extract drift). Extraction goes through the capped
    `fetchCappedDocs` then the new pure, deterministic `extractCode` (`control/extract.go`,
    fixed-line-window chunking, stable bytes for stable embeddings). Launchable via
    `patternLaunchable`. Proof: `TestExtractCodeChunks`, the new `code repo` case in
    `TestDetectPipeline`, and the `code-repo` entry in `TestPatternLaunchable` (the single-source-file
    `unknown` case still holds — the threshold is >=2).
  - Suite: `go build/vet ./...` clean, `go test ./control` green, `gofmt` clean.
  - Item 24 (detection regression table) SHIPPED: `TestDetectPipeline` now has fixtures for PDF,
    audio, code-repo, CSV-stray, docs, mixed-repo, and unknown, documenting pattern priority honestly.
  - Still open in the intake lane: items 22-23 (unify client/server detection so the UI shows the
    server's detection, not a divergent local guess; extraction stats — records/files/bytes used and
    skipped — in the quote).

- 2026-06-30 — **Quote records/bytes used+skipped (item 23, records/bytes dimension) SHIPPED** on
  `feat/perf-wave`.
  - `QuoteInputScan` (`control/quote.go`) gained `blank_records` + `skipped_records` (blank +
    malformed), surfaced directly in the quote's `input` block, so the buyer sees how much input is
    actually usable before paying. `scanJSONL` now counts blank lines. Proof: `TestScanJSONLUsedAndSkipped`
    (existing scan tests still pass). Suite green, gofmt clean.
  - File-level dimension SHIPPED: `extractInput` now returns `ExtractionStats` (files matched/used/
    skipped, records, bytes) via the pure `docStats`, surfaced in the intake launch response under
    `extraction`. Proof: `TestDocStats`. **Item 23 complete** (records/bytes in the quote; files in
    the intake extract response — the quote scans merged JSONL, which has no file dimension). Also
    aligned `tabularExts` as a single source of truth for the tabular pattern. Suite green, gofmt clean.

- 2026-06-30 — **Generation-honeypot activation (items 10, 11) SHIPPED** on `feat/perf-wave`.
  Verification STRENGTHENED: byte-exact honeypots are now class-gated, and the seed path can no
  longer write dead/dangerous coverage.
  - Item 10 (class-aware comparison): extracted the inline gate into the pure
    `byteHoneypotComparable` (`control/verification.go`) — a byte-exact (`batch_infer`) honeypot
    is comparable ONLY with a non-blank `answer_class` matching the worker's `(engine|build_hash)`;
    tolerant types always comparable; blank/cross-class skipped. Proof: `TestByteHoneypotComparable`.
  - Item 11 (refuse blank-class byte-exact seeds): `validateHoneypotSeed` + `errHoneypotBlankClass`
    guard the new `Store.InsertHoneypot`; the fake blank-class `batch_infer` demo honeypot was
    REMOVED from `control/seed.go` (the embed honeypot, tolerant, stays). Proof:
    `TestValidateHoneypotSeed`. Real `batch_infer` honeypots are seeded operationally with a real
    class output (the procedure in docs/BUILD_STATUS.md).
  - Suite: `go build/vet ./...` clean, `go test ./control` green, `gofmt` clean. Integration tests
    use only the embed honeypot, so removing the placeholder is safe.
  - Still open in this lane: item 12 (honeypot counts in the receipt) — folded into ClearingReceipt
    (items 9, 13-15).

- 2026-06-30 — **Receipt verification label + coverage gaps (items 9, 12) SHIPPED** on `feat/perf-wave`.
  - Item 9 (no-independent-peer + cross-class labels): `deriveVerificationLabel` (`control/types.go`)
    now returns verified / honeypot-checked / **no-independent-peer** / **cross-class-skip** /
    unverified; the `Verification` receipt gained `same_supplier_matches` + `cross_class_skipped`
    counts, fed by `JobVerification` from the `redundancy_same_supplier` / `*_cross_class` events
    (neither counted as "checked", so coverage is never inflated). Proof: `TestDeriveVerificationLabel`.
  - Item 12 (honeypot counts): the receipt already carried `honeypots_passed` / `honeypots_failed`;
    confirmed surfaced and covered by the label test.
  - Suite: `go build/vet ./...` clean, `go test ./control` green, `gofmt` clean.
  - Still open: items 13-15 (a single ClearingReceipt API endpoint joining quote+actuals+verification+
    class+dispute+settlement; pipeline receipt; per-task drilldown) — larger API-surface builds.

- 2026-06-30 — **LaunchContract (items 1, 3 core) SHIPPED** on `feat/perf-wave`.
  - New `control/launch_contract.go`: `LaunchContract` (quote_id / max_usd / min_reputation /
    private_pool / verification) + the pure `applyTo` (stamps the contract onto a per-stage
    `jobSubmit`) + `launchContractFrom` (round-trip). Proof: `TestLaunchContractApplyTo`.
  - Wired through the indirect paths that used to DROP these fields:
    - Direct `/v1/jobs`: already carried the full contract via `createJob` (unchanged).
    - Intake launch (`handleLaunchIntake`): `launchRequest` gained the contract fields; every
      launched stage is now `contract.applyTo(...)` — fixes the dropped `max_usd` the broker audit flagged.
    - Pipeline (`handleCreatePipeline`): `pipelineCreateRequest` gained the fields; every fan-out
      stage carries the contract, so stage 1 inherits stage 0's budget+verification (item 3).
    - OpenAI Batch: **blocked by a named external dependency** — the OpenAI Batch wire format carries
      no CX contract fields, so a batch job cannot express a per-job contract. Documented at the call
      site; it still runs under `createJob`'s free-credit cap.
  - Suite: `go build/vet ./...` clean, `go test ./control` green, `gofmt` clean.
  - Still open: items 2 + 5 (intake/UI launch must REQUIRE or GENERATE a quote, not just propagate
    one), item 4 (composite multi-stage pipeline quote), and chained-stage inheritance for the
    `advanceIntake`/`advancePipeline` async paths (needs the contract persisted with the
    intake/pipeline row — only relevant once output→input chaining patterns land; current patterns
    are fan-out, so all stages are launched directly with the contract above).

- 2026-06-30 — **ClearingReceipt endpoint (item 13) SHIPPED** on `feat/perf-wave`.
  - New `GET /v1/jobs/{id}/receipt` (`handleJobReceipt`, buyer-scoped via `JobInvoice`) returns a
    single `ClearingReceipt` (`control/receipt.go`) joining: the invoice (QUOTE `quoted_usd` +
    ACTUALS + SETTLEMENT amounts), the verification receipt (counts + honest label + DISPUTE status),
    and the distinct verification CLASSES that produced the results (new `Store.JobVerificationClasses`).
    All six facets the proof names are present. Assembly is the pure `assembleClearingReceipt`. Proof:
    `TestAssembleClearingReceipt`.
  - Suite: `go build/vet ./...` clean, `go test ./control` green, `gofmt` clean.
  - **Item 15 (per-task drilldown) SHIPPED:** the receipt's `tasks[]` carries each task's chunk,
    status, honeypot flag, worker verification class, and latest comparison event, via the new pure
    `taskReceiptRow` + `Store.JobTaskReceipts` (which NEVER selects `known_answer`). The `TaskReceipt`
    struct has no answer/result field by construction. Proof: `TestTaskReceiptNeverLeaksHoneypotAnswer`.
  - **Item 14 (pipeline receipt) SHIPPED:** `GET /v1/pipelines/{id}/receipt` (`handlePipelineReceipt`,
    buyer-scoped) aggregates each stage's receipt via the pure `assemblePipelineReceipt` — real total
    charge, and `all_verified` true only when EVERY stage is verified (a single unverified stage flips
    it false). Proof: `TestAssemblePipelineReceipt`.
  - **ClearingReceipt lane (items 9, 12, 13, 14, 15) COMPLETE.** Suite green, gofmt clean throughout.

- 2026-06-30 — **Composite quote (item 4) + intake budget generation (item 2) SHIPPED** on
  `feat/perf-wave`.
  - Item 4: new `POST /v1/quote/pipeline` (`handlePipelineQuote`) prices each stage on the same
    input via the existing `buildQuote`, then aggregates with the pure `composeQuotes`
    (`control/quote.go`): total cost = SUM (the cap), an ETA BAND (best-case parallel p50 to
    sequential worst case), worst-stage confidence, and worst risk across stages. Proof: `TestComposeQuotes`.
  - Item 2: `handleLaunchIntake` now GENERATES a composite quote for the detected stages when the
    buyer set no `max_usd`, and uses its worst-case total as the spend cap — so an auto-launched
    intake carries a budget and can never run uncapped (createJob persists `max_usd` per job).
  - Suite: `go build/vet ./...` clean, `go test ./control` green, `gofmt` clean.
- 2026-06-30 — **Web UI honesty (items 22 + 5) SHIPPED** in `web/demo.html`.
  - Item 22 (detection divergence): `detectFile` now asks the SERVER (`POST /v1/intake`) when a live
    key is present and renders exactly what `intake.go` recognized (`serverDetect` maps the server
    `pipeline.stages`), falling back to the local heuristic ONLY offline — and then labelled `offline
    guess` so a local guess is never presented as authoritative. The old divergent `pipelineFor`
    (code→`custom` vs the server's code-repo→`embed`; docs→`batch_classification` vs the server's
    `json_extraction`) is now the explicit offline-only fallback.
  - Item 5 (no paid launch without a quote): `runPipelineLive` now prices the pipeline first
    (`POST /v1/quote/pipeline`), shows the buyer the expected/cap cost band, requires explicit
    `confirm`, and sends the confirmed worst-case total as `max_usd` on the `/v1/pipelines` launch —
    so the live Launch button can no longer bypass the quote/budget governor. Cancelling charges nothing.
  - **Browser-verified** via the `web-demo` static preview: page parses with zero console errors and
    the full JS-driven UI renders (nav, auth panel, seeded run rows); a simulated `rows.csv` drop
    offline yields `detected · tabular data · offline guess · 2-step pipeline` (Embed→Classify). The
    LIVE server-detect + quote-confirm paths exercise the running control plane (Go + Postgres + MinIO)
    with a buyer key — that full round-trip is the named external dependency for end-to-end proof.
- 2026-06-30 — **Mac app prefs/status truth (items 25 + 26 / Atlas F7) SHIPPED** across both runtimes
  (Swift 6.3.2 toolchain IS present — this was NOT toolchain-blocked).
  - Item 25 (prefs actually control the agent — the F7 "decorative toggle" fix): the Rust agent now
    reads an operator-prefs overlay in `AgentConfig::load` (`agent/src/config.rs`) — `CX_AGENT_PREFS`
    if set, else the conventional `agent.prefs.toml` sidecar next to the config (the exact file the
    menu-bar app already writes). `OperatorPrefs` + `apply_prefs` merge present knobs over the base
    (absent ones untouched; `max_concurrent_tasks = 0` → derive). The Swift `AgentController` launches
    the agent with `CX_AGENT_PREFS` set, and the stale "decorative / operators merge / scaffold"
    comments are corrected. Proof: 4 new Rust tests incl. `operator_prefs_actually_control_eligibility`
    (a toggled quiet-hours/power-only pref changes `is_eligible_to_run`) + `..._zero_concurrency_means_derive`.
  - Item 26 (the app shows APPLIED prefs, not just local UI state): the agent echoes an `applied_prefs`
    block in `status.json` (`agent/src/status.rs` `AppliedPrefs::from_config`, set once after config
    load in `main.rs` — `max_concurrent_tasks` is the RESOLVED permit count). The Swift `StatusModel`
    decodes a matching optional `AppliedPrefs` (nil until reported → "not yet reported", never faked).
    Proof: `status_doc_matches_swift_contract` now asserts the `applied_prefs` keys/values.
  - Suites: `cargo test -p cx-agent` → 94 passed / 0 failed; `swift build` → Build complete. The full
    end-to-end (toggle in the running .app → relaunch → menu reflects applied) is a manual run of the
    packaged app against a live control plane — the named external dependency for that last mile.
  - Remaining in this lane (further increments, not the core prefs/status truth): F6 trust panel fed by
    live payout/verification state (items 28-29), item 27 heartbeat advertises the reservation price,
    item 30 explicit unavailable states.

## P0. Preserve the Moat Through Existing Paths

1. Build `LaunchContract` shared by direct jobs, intake launch, pipelines, and OpenAI Batch.
   Proof: integration tests show `quote_id`, `max_usd`, `verification`, `min_reputation`, and
   `private_pool` propagate through each path.

2. Make `/v1/intake/launch` require or generate a quote contract.
   Proof: auto-launched jobs have persisted quote and budget fields.

3. Make `control/pipeline.go` propagate launch contract to every stage.
   Proof: stage 1 inherits verification and budget constraints from stage 0.

4. Add composite pipeline quote.
   Proof: detected multi-stage pipeline returns per-stage cost, total cap, ETA band, and risk.

5. Stop live UI launch from bypassing quote.
   Proof: browser/manual test shows no paid launch without quote confirmation.

6. Add supplier-distinct redundancy peer selection.
   Proof: same-supplier second worker is rejected as independent peer.

7. Add supplier-distinct peer result lookup.
   Proof: same-supplier completed sibling cannot create a verified match.

8. Add supplier-distinct dispute reverification.
   Proof: dispute resolver uses an independent supplier or records `no_peer`.

9. Receipt label for no independent peer.
   Proof: buyer status differentiates verified, sampled, cross-class skip, and no-peer.

10. Seed class-aware generation honeypots.
    Proof: `batch_infer` honeypot pass/fail tests cover non-blank `answer_class`.

11. Refuse blank-class byte-exact honeypot seed writes.
    Proof: seed/admin path fails safely.

12. Add honeypot coverage to receipt.
    Proof: completed job shows honeypot checked/pass/fail counts.

13. Add `ClearingReceipt` API projection.
    Proof: one endpoint returns quote, actuals, verification, class, dispute, and settlement.

14. Add pipeline receipt.
    Proof: pipeline view aggregates stage receipts honestly.

15. Add per-task receipt drilldown.
    Proof: verified task shows worker class and comparison event without exposing hidden
    honeypot answers.

## P0. Make Intake Honest and Safe

16. Add `io.LimitReader` and per-file cap to GitHub raw reads.
    Proof: oversized raw file returns typed intake error.

17. Add aggregate intake cap.
    Proof: many medium files cannot exhaust memory.

18. Honor truncated repository tree.
    Proof: quote/detect marks confidence low or refuses when tree is incomplete.

19. Fix `.pdf` supported-then-zero-records behavior.
    Proof: PDF-only repo is either unsupported or actually extracted with caps.

20. Fix audio detected-before-upload-path behavior.
    Proof: audio-only repo cannot launch into an unimplemented path.

21. Add code-repo embed/index detector.
    Proof: typical Go/Rust/TS/Python repo maps to deterministic chunked embed job.

22. Unify server and client detection.
    Proof: UI displays server detection output, not a divergent local guess.

23. Add extraction stats to quote.
    Proof: quote shows records/files/bytes used and skipped.

24. Add detection regression table.
    Proof: test fixtures for PDF, audio, code repo, CSV stray, docs, mixed repo.

## P0. Supplier App Truth

25. Add agent prefs overlay path.
    Proof: sidecar prefs are read by the agent at launch.

26. Echo applied prefs in `status.json`.
    Proof: Mac app can show applied values from agent, not only local UI state.

27. Wire active/power/quiet/min-payout prefs to heartbeat and scheduler eligibility.
    Proof: changed pref changes worker eligibility in local test.

28. Poll supplier payout readiness from control plane.
    Proof: `status.json` includes payout configured/connected/enabled fields.

29. Poll verification counts and recent trust events.
    Proof: trust panel shows non-empty real counts under fixture/local control plane.

30. Add app unavailable states for unwritable prefs or failed control-plane trust poll.
    Proof: UI does not imply a toggle worked when it did not.

## P1. Buyer Acquisition and Enterprise

31. OpenAI Batch compatibility script.
    Proof: sample submits, polls, downloads output, fetches receipt.

32. OpenAI-shaped quote endpoint.
    Proof: JSONL file request returns quote without creating job.

33. Batch output/error file parity tests.
    Proof: create/list/retrieve/cancel/output/error routes have fixtures.

34. API key scopes.
    Proof: quote-only key cannot submit or read results.

35. Webhook event filters.
    Proof: buyer can subscribe to job completed, failed, disputed separately.

36. Webhook secret rotation.
    Proof: old/new overlap works, old can be revoked.

37. Webhook replay endpoint.
    Proof: failed delivery can be replayed without bypassing SSRF guard.

38. Private pool membership API.
    Proof: buyer can add/remove supplier from pool.

39. Private pool UI.
    Proof: buyer can launch against pool and see private capacity.

40. Private quote split.
    Proof: quote distinguishes public eligible and private eligible workers.

41. Team invitations for suppliers.
    Proof: supplier joins buyer pool through explicit invite.

## P1. Marketplace Economy

42. Spot index simulation harness.
    Proof: synthetic queue/supply inputs produce bounded price movement.

43. Per job_type spot index table.
    Proof: embed backlog does not surge unrelated job types.

44. Bound quote price as ceiling.
    Proof: submit cannot exceed bound quote rules.

45. Decouple buyer discount from supplier offered-rate floor.
    Proof: discount does not make workers ineligible.

46. Compute-credit ledger kinds.
    Proof: mint/spend/expire/clawback entries balance.

47. Mint credits only on verified pass.
    Proof: failed or unverified jobs do not mint credits.

48. Treasury liability cap.
    Proof: credit issuance halts or discounts when cap is reached.

49. Credit spend on jobs.
    Proof: buyer can pay with credits without double-paying cash.

50. Credit clawback parity.
    Proof: fraud clawback reverses credit effects consistently.

51. Supplier-to-buyer conversion UX.
    Proof: supplier account can spend earned credits on a buyer job.

## P1. Scheduler and Quote Intelligence

52. Per worker/model/job latency history.
    Proof: scheduler fixtures rank faster historical worker under equal gates.

53. Failure-rate penalty.
    Proof: flaky worker loses tiebreak even with high raw throughput.

54. Warm-cache bonus.
    Proof: warm eligible worker improves quote ETA and scheduler rank.

55. Thermal decay model.
    Proof: overheated worker gets temporary rank penalty.

56. Candidate explanation API.
    Proof: admin can see why workers were rejected or selected.

57. Quote drift alert.
    Proof: observed p90 drift produces metric/alert.

58. No-independent-peer alert.
    Proof: paid volume with no independent peers triggers metric/alert.

## P2. Engine Lanes

59. Engine capability registry in agent.
    Proof: heartbeat reports engine capabilities from one registry.

60. Build hash includes all engine-class inputs.
    Proof: vLLM version/dtype/tp/attention backend changes class.

61. Per-model serve loop spike.
    Proof: concurrent same-model tasks improve throughput or are rejected with data.

62. Shared-prefix batched remainder/decode.
    Proof: GPU parity test shows batched equals serial.

63. Hawking batch-composition decision.
    Proof: either byte-invariant generation or tolerant comparator before paid use.

64. vLLM determinism soak.
    Proof: restart and cross-SKU tests define allowed class boundaries.

65. Qwen/large-model quality lane.
    Proof: model loads, coherent output, class-specific honeypots seeded.

66. Constrained JSON decode.
    Proof: schema-valid output rate improves with no comparator regression.

67. Metal SDPA prefill benchmark.
    Proof: prefill-isolated benchmark decides ship/reject under new build hash.

## P2. New Workloads

68. Artifact codec registry.
    Proof: each codec declares splitter, merger, size caps, and comparator.

69. Streaming merge.
    Proof: large results merge without buffering entire output.

70. Perceptual image comparator spike.
    Proof: honest jitter passes, quality-shaved outputs fail on fixture set.

71. Render lane only after comparator.
    Proof: no render job_type accepted before comparator registry entry.

72. PDF extraction with caps.
    Proof: benign PDF extracts, decompression bomb fails safely.

73. Audio upload path.
    Proof: audio-transcribe launches only from real bounded upload/object path.

74. Custom container comparator policy.
    Proof: no-comparator custom job is labeled metered-unverified.

75. Custom artifact size cap.
    Proof: oversized custom output fails safely and cannot exhaust control plane.

## P2. Ops and Security

76. Postgres advisory-lock leadership for background sweeps.
    Proof: two control processes do not double-deliver side effects.

77. Idempotency keys for payouts and webhook delivery.
    Proof: repeated sweep cannot double-send.

78. WAL/PITR backup plan.
    Proof: restore drill includes point-in-time target, not only latest dump.

79. CSP and security headers.
    Proof: static and API responses include expected headers.

80. CSRF protection for cookie-auth POSTs.
    Proof: cross-site form POST cannot mutate buyer state.

81. Admin route hardening.
    Proof: admin endpoints require explicit admin auth and are not exposed by accident.

82. Object retention policy.
    Proof: old inputs/results expire according to tier.

83. Alert suite for moat decay.
    Proof: fixtures trip alerts for zero verification, quote drift, payout backlog, webhook
    backlog, cross-class skip spike, and no peer.

## P3. Research Lanes

84. Attestation receipt schema.
    Proof: draft fields map to at least one real provider's evidence format.

85. Attested worker spike.
    Proof: one worker reports a real measurement and receipt displays it.

86. Private attested pool.
    Proof: private pool can require attested workers.

87. Model-parallel cluster sim.
    Proof: only a simulation until real co-located hardware exists.

88. RWKV/long-context lane.
    Proof: measured quality/throughput and comparator policy before product promise.

89. Weight-derived quant identity.
    Proof: requantized model changes build class automatically.

90. Public proof export.
    Proof: buyer can export receipt JSON without hidden honeypot answers.

## Do Not Fake

- Do not call a job verified because it completed.
- Do not call same-supplier redundancy independent.
- Do not call a sidecar pref applied until the agent reads it.
- Do not call render verified before a perceptual comparator exists.
- Do not call custom containers verified without a comparator.
- Do not mint credits on unverified or failed work.
- Do not sell a quote as binding if launch paths can bypass it.
- Do not ship a faster engine inside an old verification class.
- Do not hide missing peer coverage.
- Do not let docs be the deliverable when a test can exist.

## Compact Claude Goal Handoff

Use this only after reading the linked docs. It is intentionally below the goal-length limit.

```text
/goal Continue Compute Exchange frontier execution from docs/FRONTIER_EXPANSION_ATLAS.md, docs/AGGRESSIVE_BACKLOG_MOUNTAIN.md, docs/SELF_COMPETITION_WARGAME.md, docs/CUSTOMIZATION_AND_OWNERSHIP_MAP.md, and docs/COMPETITOR_AND_FRONTIER_RESEARCH_2026.md. Work repo-grounded only. Start with P0: LaunchContract through jobs/intake/pipelines/OpenAI Batch, supplier-distinct verification, generation honeypot activation, ClearingReceipt, intake caps/detection honesty, and Mac app prefs/status truth. For every lane, inspect code first, implement narrowly, add tests, run the relevant Go/Rust suites, and update docs. Do not claim progress from prompts. Do not weaken verification. Do not mark same-supplier peers independent. Do not ship render/custom/credits/engine changes without the proof gates listed in AGGRESSIVE_BACKLOG_MOUNTAIN.md. Keep going until each open item is shipped, measured-and-rejected, or blocked by a named external dependency.
```

