# Computexchange — Closed-Alpha Readiness

This is the honest map for a closed alpha. It separates **what is locally proven**,
**what is skeleton-only**, **what is an external blocker**, and **what is
deliberately deferred** (design and CUDA). It complements
[RELEASE_CANDIDATE.md](../RELEASE_CANDIDATE.md) (the proof ledger) and
[PRODUCT_SHAPE.md](PRODUCT_SHAPE.md) (the app topology).

The dividing rule is unchanged: anything buildable, testable, and provable on one
Apple-Silicon Mac is **done and proven** (`make prove-local`); everything else
genuinely needs the outside world.

---

## ✅ Locally proven (engine)

Reproduced by one command — `make prove-local` (last run on the maintainer's Mac:
**82/82**; this pass adds throttle-surface + scheduler-safety + app-skeleton
checks, which raise the count). See [RELEASE_CANDIDATE.md](../RELEASE_CANDIDATE.md)
for the full ledger. The core engine — execution on Metal, verification, the
Postgres queue, payouts hold→ready, disaster recovery, install, OpenAI Batch API,
multi-supplier — is real, not scaffolding.

New in this pass, and proven:

- **Dynamic provider throttling** (agent) — real available-memory readings → a pure
  eligibility decision → enforced before every claim. Unit-tested
  (`agent/src/config.rs`, `agent/src/hardware.rs`).
- **Scheduler safety contract** (control) — the SKIP-LOCKED claim filters on the
  worker's live **effective memory** and **throttled** state, so a memory-pressured
  worker is never dispatched work. Proven by `TestClaimHardFilter` (throttled +
  effective-memory cases) and `TestMatchExcludesThrottledWorker`.
- **Throttle status surface** — the agent's `status.json` carries the resource
  block; `prove-local` asserts it is present and coherent (`status-file` check).

---

## 🟡 Skeleton-only (structure + wiring, not designed)

These exist as **functional structure wired to real APIs**, with final design
**deferred** until there is product/design input. Do not mistake them for finished
product.

- **macOS menu-bar app** (`macapp/`) — buildable (`swift build`), shows status,
  resource/throttle state, earnings, start/stop, and operator prefs (incl. memory
  headroom). Not visually polished; no complex flows. Signing/notarization is
  external.
- **Web app skeleton** (`web/skeleton.html`, served at `/app`) — bare role tabs:
  `Supplier` (documents the local-app surface), `Buyer` (real submit/status/results),
  `Admin/Operator` (real `/admin/*` workers/jobs/payouts/fraud), and `Workflows`
  (deferred placeholder). Vanilla HTML/JS, no framework, no marketing page.
- **`Workflows` / pipelines / "IDE"** — a **named placeholder only**. No code
  editor, no arbitrary code execution. Needs product/design input.

The operator dashboard (`web/dashboard.html`, served at `/`) is the one slightly
fuller surface (buyer jobs + metrics); it predates this pass and is left intact.

---

## ⛔ External blockers (cannot be closed on one machine)

Unchanged from [RELEASE_CANDIDATE.md](../RELEASE_CANDIDATE.md); restated so the
alpha checklist is in one place:

- **Apple Developer ID** — signing + notarization of a distributable `.app` (the
  local build path is proven; the identity is external).
- **Payment rail credentials** — Stripe Connect / Trolley keys. The transfer code
  exists and the **manual-export rail** (`CX_PAYOUT_EXPORT`) is the alpha
  stand-in; a *licensed* rail moving real money is external.
- **Real hardware** — a real Mac Studio / a second physical Mac for cross-machine
  redundancy and churn measurement (proven locally as two agent processes).
- **Real buyer** and **real supplier** — first paid job, first real payout.
- **Legal / compliance review** — MSB/FINTRAC, CRA Part XX, GST/HST, ToS.

---

## 🚫 Deferred by decision

- **Design** — no design system in alpha; the skeletons above are intentionally
  plain. (Since superseded in part: the Control Room console ships at /admin and
  the public informational page at / · web/index.html, receipts in
  docs/SITE-CLAIMS.md.) See [PRODUCT_SHAPE.md](PRODUCT_SHAPE.md).
- **CUDA / RunPod / DGX / TOPLOC** — Computexchange is Apple-Silicon-first for
  launch. Any thin CPU/CUDA rail is left intact but **not widened** in this pass.
  There is no external-cloud supply class.

---

## Supplier throttling

How a provider earns while keeping their Mac usable.

### How it works
1. Every heartbeat (~30s) **and before every claim**, the agent takes a **real**
   memory reading via `sysinfo` — total and *available* (free + reclaimable), not
   just total.
2. It computes **effective allocatable memory** = `available − memory_headroom_gb`.
3. It **pauses claiming new work** ("throttles") when any of:
   - utilization has reached `max_memory_pct` (the box is near swap), or
   - the reserved headroom would be breached (`available ≤ headroom`), or
   - a known next-task estimate exceeds the effective pool.
4. The decision (and effective memory) is sent on the heartbeat. The control
   plane's **safe-dispatch filter** refuses to hand work to a throttled worker, or
   to one whose effective memory is below the job's `min_memory_gb` — using
   effective memory, falling back to total only before the first heartbeat.

Enforced at the right points: before claiming, and re-evaluated every cycle (so
finishing a task and looping back re-checks before the next claim). The throttle
logic is a **pure function** with unit tests; we never force a real OOM to prove it.

### How to configure memory headroom
In `agent/agent.toml` (see `agent/agent.example.toml`):

```toml
memory_headroom_gb = 8.0    # GB reserved for YOUR use; lower on a dedicated box
max_memory_pct = 85.0       # pause new work once physical memory use hits this %
```

Omit either to take the conservative built-in default (**8 GB / 85%**). Set **both
to 0** to turn the governor **off** (a dedicated box running flat out — the agent
still reads and reports real memory, it just never pauses). The menu-bar app
writes `memory_headroom_gb` to a config sidecar via its "Memory headroom" stepper.

> The available-memory reading (`sysinfo`) is **conservative on macOS** — it errs
> toward under-counting reclaimable memory, so the agent throttles a little early.
> That bias is deliberately on the side of supplier safety. `make prove-local`
> runs its two stress agents with the governor off (`0/0`) so the multi-agent /
> load-test pipeline checks aren't gated by a memory-saturated dev Mac; the
> governor's enforcement is proven separately by unit tests + the
> `TestClaimHardFilter` matrix cases.

### How to read throttle/status output
The agent writes `~/.compute-exchange/status.json` atomically. The resource block:

```json
{
  "total_memory_gb": 64.0,
  "available_memory_gb": 40.0,
  "reserved_headroom_gb": 8.0,
  "effective_memory_gb": 32.0,      // available − headroom (allocatable for jobs)
  "throttled": false,
  "throttle_reason": null,          // a human string when throttled
  "current_task_id": "uuid|null"
}
```

When `throttled` is true, `state` is `paused` and `throttle_reason` says why
(e.g. "reserved headroom: 6.0 GB available ≤ 8.0 GB headroom"). The menu-bar app
shows effective memory, headroom, and a throttle banner. Operators see the same
`throttled` + `effective_memory_gb` per worker via `GET /admin/workers`.

---

## What remains before inviting real users

| Item | Status |
|---|---|
| Engine (execution/verification/queue/payout-lifecycle) | ✅ proven locally |
| Supplier throttling + scheduler safety | ✅ proven locally (this pass) |
| Supplier local app | 🟡 skeleton, buildable; needs signing (external) + design |
| Buyer surface (CLI/SDK/REST/Batch) | ✅ proven; web `Buyer` tab is skeleton |
| Operator surface | 🟡 admin endpoints proven; panels are skeleton |
| Payment rail | ⛔ external creds; manual-export rail is the alpha stand-in |
| Distribution (signed `.app`) | ⛔ external (Apple Developer ID) |
| Two real Macs / real buyer / real supplier | ⛔ external (field) |
| Legal/compliance | ⛔ external |
| Design + Workflows/IDE | 🚫 deferred (needs product/design input) |
| CUDA / cloud supply | 🚫 deferred (Apple-Silicon-first) |
