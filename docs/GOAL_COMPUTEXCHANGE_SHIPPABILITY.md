# Goal: Computexchange Closed-Alpha Shippability Pass

## Mission

Turn the current locally proven engine into a closed-alpha-ready product surface for
Computexchange: Apple Silicon-first, safe for suppliers, usable by buyers, operable by
the founder, and intentionally minimal on frontend/design.

This is not a polish pass. This is a shippability pass.

## Current Assumption

The repo is already locally proven around the core pipeline. The most recent stated
gate is `make prove-local` at 82/82, with operations, install/uninstall, a buildable
menu-bar app, admin views, OpenAI-compatible batch API, CLI cancel, multi-agent proof,
manual payout export, load test, and disaster recovery.

Do not restart from scratch. Read the current repo, confirm the actual state, and build
only on what exists.

## Product Decision

Computexchange is Apple Silicon-first for launch.

CUDA, RunPod, DGX-class hardware, TOPLOC, polished frontend design, and a full IDE are
not launch blockers. Keep any existing thin CUDA rail intact, but do not widen it in
this pass.

The frontend/app should be only a skeleton until there is higher-level design input.
That means functional structure, placeholders, wiring, and proof that the surfaces fit
together. No decorative dashboard, no design system, no marketing site, and no IDE.

## Non-Negotiables

- Preserve the existing proof gate.
- Keep `cx` as the short command identity.
- Rename user-facing product language to Computexchange where safe.
- Do not break existing commands, scripts, config, proof flow, or wire contract.
- No arbitrary code execution.
- No fake telemetry, fake payouts, fake verification, or fake resource readings.
- No CUDA expansion in this pass.
- No polished frontend design.

## Objective 1: Dynamic Provider Throttling

Build supplier-side resource protection so a provider can earn while still using their
own Mac safely.

Implement configurable resource headroom:

- `memory_headroom_gb`, or equivalent.
- `max_memory_pct`, or equivalent.
- optional `max_cpu_pct` enforcement if already present in config.
- power-only and quiet-hours behavior must remain respected.
- min payout rate must remain respected if already implemented.

Use real runtime resource readings:

- total memory is not enough.
- use available/free memory or pressure where possible.
- calculate effective allocatable memory as available memory minus supplier headroom.
- preserve enough headroom that a consumer Mac does not get pushed into OOM or swap death.

Enforce at the correct points:

- before polling or claiming work where possible.
- immediately before execution.
- after completing a task before taking another.
- when pressure rises, pause new claims and surface the throttle reason.

Surface provider state:

- current available memory.
- reserved headroom.
- effective available memory for jobs.
- active task memory estimate if known.
- throttled/not throttled.
- throttle reason.
- current task id if running.

Prefer extending the existing status file/menu-bar surface if present.

Add focused tests for pure eligibility logic. Do not require intentionally causing an
OOM.

## Objective 2: Scheduler Safety Contract

Make the control plane understand enough about provider limits to avoid unsafe dispatch.

Add or verify support for:

- worker effective available memory.
- worker total memory.
- worker reserved headroom.
- worker current pressure/throttle state.
- worker supported models and job types.

Ensure workers do not receive tasks they cannot safely run.

If the current claim path cannot filter by all needed fields, add the smallest
load-bearing schema/query change needed. Do not introduce a new broker or large
scheduler subsystem.

The scheduler should prefer:

- compatible model.
- compatible job type.
- enough effective memory.
- same hardware class for verification peers.
- higher reputation.
- better benchmark throughput.
- not currently throttled.

## Objective 3: Minimal App And Frontend Skeleton

Strip frontend ambition down to a product skeleton.

If a macOS menu-bar app exists:

- keep it buildable.
- show status, start/stop placeholder, resource state, and logs/diagnostics placeholder.
- do not polish visuals.
- do not add complex flows.

If a web frontend exists:

- keep it minimal.
- create bare role-based sections only:
  - Supplier
  - Buyer
  - Admin/Operator
- use simple forms/placeholders wired to existing APIs where practical.
- no marketing landing page.
- no heavy UI framework work unless already present.

Supplier skeleton:

- current agent status.
- active/inactive.
- resource limits.
- memory headroom.
- current job.
- earnings/payout placeholder.
- logs/diagnostics placeholder.

Buyer skeleton:

- API key placeholder.
- submit job placeholder.
- job status placeholder.
- results placeholder.
- webhook placeholder.

Admin/operator skeleton:

- workers.
- jobs.
- payouts.
- fraud flags.
- stuck jobs.
- backup/restore links or documentation pointers.

The "IDE" idea is deferred. Represent it only as a placeholder route or panel named
`Workflows`, `Jobs`, or `Pipelines`. It should say, in code or docs, that final workflow
and codebase UX needs product/design input. Do not implement a code editor.

## Objective 4: Product Shape Decision

Clarify the app topology in docs and skeletons.

Recommended shape for closed alpha:

- Supplier local app: runs on the provider Mac, controls local earning/resource state.
- Buyer/API surface: CLI, SDK, REST, and minimal web skeleton.
- Admin/operator surface: internal control panel or minimal existing admin views.

Do not force buyer and supplier into one heavy app yet. They have different jobs:

- supplier wants safety, earnings, and local control.
- buyer wants job submission, status, results, and billing.
- operator wants queue, workers, payouts, fraud, and incident response.

## Objective 5: Computexchange Rename

Rename user-facing product language from Compute Exchange / computeexchange to
Computexchange where safe.

Keep:

- `cx` CLI identity.
- existing package/module names if changing them is churny or risky.
- old names where required for compatibility, with comments if helpful.

Update:

- README headline and user-facing language.
- app labels if safe.
- docs.
- config comments.
- CLI help text if safe.
- installer text if safe.

Avoid risky churn:

- database names.
- module paths.
- binary names.
- script names.
- Docker image names.
- public API paths.

Only rename those if clearly safe and verified.

## Objective 6: Closed Alpha Readiness Docs

Update docs to clearly separate:

- locally proven.
- skeleton only.
- external blockers.
- deferred design.
- deferred CUDA.

Document:

- how supplier throttling works.
- how to configure memory headroom.
- how to read throttle/status output.
- what the supplier app skeleton does and does not do.
- what the buyer skeleton does and does not do.
- what remains before inviting real users.

External blockers remain external:

- Apple Developer ID signing/notarization.
- real payment rail credentials or manual payout process for alpha.
- real Mac Studio / second Mac hardware.
- real buyer.
- real supplier.
- legal/compliance review.

## Objective 7: Alpha Safety And Ops Check

Before declaring done, confirm these alpha safety basics:

- install check still works.
- uninstall check still works if present.
- status surface still works.
- backup script still works or remains documented.
- disaster recovery proof still passes if part of `prove-local`.
- admin/operator views still compile or respond.
- stuck-job and payout runbooks still reflect current behavior.

Do not build a new observability platform. Keep the current metrics/runbook approach.

## Implementation Constraints

Use existing local patterns.

Prefer:

- small schema additions over new systems.
- pure eligibility functions with tests.
- current status file over new daemons.
- current web/macapp skeletons over new frontend architecture.
- docs that name deferred work honestly.

Avoid:

- broad refactors.
- new frontend design systems.
- full IDE implementation.
- CUDA work.
- TOPLOC work.
- arbitrary code execution.
- dependency sprawl.

## Verification

Run, at minimum:

```bash
cargo fmt
cargo test
go test ./...
make prove-local
```

Also run any existing relevant checks:

```bash
swift build
web build command, if one exists
script syntax checks, if already used
install --check, if present
```

Do not claim an external blocker is solved unless it actually happened.

## Acceptance Criteria

The pass is done when:

- provider memory headroom is configurable.
- provider memory headroom is enforced before new work is accepted.
- effective available memory is surfaced in status output.
- throttling state and reason are visible.
- scheduler/worker claim path cannot assign unsafe work.
- Computexchange branding appears in user-facing docs/app text where safe.
- `cx` remains intact.
- frontend/app work is reduced to a minimal skeleton, with final design deferred.
- docs explicitly explain what is skeleton-only.
- docs explicitly explain remaining external blockers.
- normal tests pass.
- proof gate does not regress.

## Final Response Requirements

When finished, report:

- files changed.
- proof/test results.
- current proof count if `make prove-local` reports one.
- what is now real.
- what remains skeleton-only.
- what remains external.

Keep the answer honest and concise.
