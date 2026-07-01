# Archive Index

This index tracks markdown files removed from the working tree during documentation
consolidation passes. Nothing is actually lost: every removed file is fully recoverable
from the annotated git tag that was cut immediately before its removal.

Retrieval:
- Restore a file into the working tree: `git checkout <tag> -- <path>`
- View a file's contents without restoring it: `git show <tag>:<path>`

## Consolidation pass: 2026-07-01 (tag `pre-consolidation-2026-07-01`)

Rationale: these were point-in-time research reports, closed-out session/audit logs, and
strategy docs that explicitly self-declare superseded/closed status in their own text, with
zero live inbound links from the current canonical roll-ups (`docs/internal/ROADMAP_REVIEW.md`,
`RELEASE_CANDIDATE.md`, `README.md`). Their durable conclusions were independently confirmed
already folded into still-active docs (`docs/COST_COMPARISON.md`, `docs/internal/ROADMAP_STATUS.md`,
`docs/ALPHA_READINESS.md`, `docs/internal/LAUNCH_REEVALUATION_2026-06.md`).

- `docs/internal/DEEP_RESEARCH_V1.md` — Deep Research Report v1 (106 agents/24 sources) on the compute-marketplace landscape: verification moat, Apple Silicon capacity play, RunPod benchmark; June 2026. Retrieve: `git checkout pre-consolidation-2026-07-01 -- docs/internal/DEEP_RESEARCH_V1.md`
- `docs/internal/DEEP_RESEARCH_V2.md` — Deep Research Report v2 (215 agents/51 sources combined with v1) on project-based pricing as a paradigm shift; June 2026. Retrieve: `git checkout pre-consolidation-2026-07-01 -- docs/internal/DEEP_RESEARCH_V2.md`
- `docs/internal/PRODUCTION_AUDIT.md` — Production audit + multi-pass session log (Pass 1-9) closing out MLX-lane / dispute-resolution / security fixes; authored 2026-06-27. Retrieve: `git checkout pre-consolidation-2026-07-01 -- docs/internal/PRODUCTION_AUDIT.md`
- `docs/internal/SHIP_AND_DOMINATE.md` — Shippability + Dominance Roadmap synthesis from a 27-agent audit workflow; dated 2026-06-28. Superseded in-text by `docs/internal/LAUNCH_REEVALUATION_2026-06.md`. Retrieve: `git checkout pre-consolidation-2026-07-01 -- docs/internal/SHIP_AND_DOMINATE.md`
- `docs/internal/HAWKING_REUSE_NOTES.md` — Interim working notes on transplanting the Hawking engine (golden-hash harness, continuous batching); dated 2026-06-29. Superseded in-text by `docs/PERF_AND_CAPABILITY_AUDIT.md`. Retrieve: `git checkout pre-consolidation-2026-07-01 -- docs/internal/HAWKING_REUSE_NOTES.md`
- `docs/internal/RUNPOD_SPIKE.md` — $5 RunPod A100 CUDA spike validating the Candle inference stack runs on NVIDIA hardware; result PASS, 2026-06-23. Follow-on productization confirmed shipped in `docs/internal/ROADMAP_STATUS.md`. Retrieve: `git checkout pre-consolidation-2026-07-01 -- docs/internal/RUNPOD_SPIKE.md`
- `docs/internal/ACCRETION.md` — "The Grand Plan": go-to-market/liquidity strategy doc grounded in a June 2026 market-data audit, repriced against H100 price collapse. Retrieve: `git checkout pre-consolidation-2026-07-01 -- docs/internal/ACCRETION.md`
- `docs/internal/GOAL_COMPUTEXCHANGE_SHIPPABILITY.md` — Mission brief + acceptance criteria for the closed-alpha shippability pass (memory throttling, branding, frontend skeleton). Criteria confirmed complete in `docs/ALPHA_READINESS.md`. Retrieve: `git checkout pre-consolidation-2026-07-01 -- docs/internal/GOAL_COMPUTEXCHANGE_SHIPPABILITY.md`
- `docs/internal/COMPUTE_EXCHANGE_HARDENED_ACTION_PLAN.md` — 1,480-line "Hardened Action Plan": original full go-to-market thesis, phased build plan, and decision log; June 2026. Carries its own scope-update banner declaring its CUDA/NVIDIA-rail sections historical-only. Retrieve: `git checkout pre-consolidation-2026-07-01 -- docs/internal/COMPUTE_EXCHANGE_HARDENED_ACTION_PLAN.md`

Summary: 9 files pruned, 0 files merged, 0 non-markdown directories pruned in this pass.
