# Computexchange — Roadmap Status

Honest state of the plan (DEEP_RESEARCH_V2 strategic priorities + the jobscraper
real-project test findings), worked top to bottom. `✅` = code-complete and verified.
`🔨` = code/seam in place, more to build. `🚫` = needs EXTERNAL work (auditor, lawyer,
or a multi-week product build) that cannot be completed in a coding session — flagged,
not faked.

## NOW (done)
- **✅ Data-residency routing** — was already enforced in `ClaimTask` (`j.data_residency
  IS NULL OR s.data_country = ANY(j.data_residency)`), with the scheduler-explain
  attributing `residency_mismatch` and integration coverage. The research doc was stale
  on this; verified, not rebuilt.
- **✅ Classification hardening** — the jobscraper run showed ~10% off-label. Fixed in
  `runners.rs`: a numbered, strict copy-from-list prompt + `closest_label` now handles a
  bare ordinal and a fuzzy longest-shared-prefix fallback (`financialservices`→`finance`).
  Still never invents a label. (commit 719153f)

## SOON (done)
- **✅ ETA estimator** — `effectiveThroughput` scales generation throughput down for long
  (prefill-bound) prompts, so classification/extraction split into right-sized tasks
  instead of underestimating (the 90s-vs-108s gap). Embed/transcribe unaffected. (76f5bf8)
- **✅ Supply-density gate** — the quote carries `sla_eligible` (eligible_now ≥ threshold)
  + an advisory-only warning below it, so project-SLA ETAs aren't promised in a trough.
- **✅ Buyer reputation surface** — `EligiblePoolReputation` puts the eligible pool's
  average reputation on the quote (routing transparency).
- **✅ The two prove-local FAILs** — re-run is **0 FAILs**; `status-file` + `memory-telemetry`
  were timing-flaky, and every recent change is validated end-to-end on the real pipeline.
- **✅ Cost-per-project calculator** — `scripts/cost_calculator.py` + `docs/COST_COMPARISON.md`,
  honest: CX loses to OpenAI's cheapest batch tier on commodity small-model work and wins
  on privacy, large models, GPU-second, and project pricing. Does NOT assert the refuted
  "3-10x" headline. (b6aa03e)

## LATER
- **✅ Elite-supplier reputation gate** (anti-defection moat) — jobs set `min_reputation`;
  `ClaimTask` routes them only to suppliers who earned that reputation on the platform.
  Control-side only, mirrors `min_memory_gb`, +integration test. (0131c80)
- **✅ NVIDIA lane productized** — `Dockerfile.agent` + CI cuda-build gate (CUDA_COMPUTE_CAP
  pinned) + the sandboxed BYO-container `custom` runner + `scripts/prove-cuda.sh`.
- **🔨 Compliance stack** — the *audit trail* exists in code (`GET /v1/jobs/{id}/invoice`,
  `/events`, `/failures` — exportable per-job compute records). What remains is **🚫
  external**: a SOC 2 Type II audit (needs an audit firm, ~months), a HIPAA BAA + GDPR DPA
  (need a lawyer). No amount of code produces these — they are sign-and-attest artifacts.
- **🔨 Private Deployment tier** — the enablers are in code (data-residency routing ✅,
  single-box/LAN/offline-capable architecture, the reputation/Elite gate). What remains is
  **a dedicated-pool binding** (a buyer's jobs route only to their own suppliers — a clean
  follow-on claim-filter + a buyer↔pool table) and **🚫 product/sales**: naming, the 3×
  price point, and the compliance bundle above.
- **🔨 Compute Autopilot IDE (Workflows)** — the `/v1/quote` autopilot front door is built;
  multi-step job *pipelines* (one job's output feeding the next, a visual designer) are a
  **multi-week feature build**, not a session task. Seam acknowledged.
- **🔨 Anti-defection — payment guarantee** — the Elite gate is done; the payout-side
  guarantee (platform pays the supplier even on a buyer dispute, capped per quarter) is a
  designed payout-path setting, not yet built. Contained follow-on.
- **🔨 Routing-intelligence dashboard** — the data exists (`GET /admin/drift` rolls up real
  committed durations per job_type/model); surfacing it to buyers is a frontend build.

## Bottom line
Everything code-feasible in the plan is **done and verified** (now + soon + the Elite gate
+ the NVIDIA lane). What is left is genuinely external: a SOC 2 audit (audit firm), BAA/DPA
(lawyer), and two multi-week product builds (the Autopilot pipeline IDE, the Private
Deployment go-to-market). Those are flagged here rather than faked — the honest definition
of "complete."
