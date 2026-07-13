<!-- CLAIM-SCOPE: internal-engineering-non-authoritative -->
# Computexchange — Product Shape (Closed Alpha)

This doc fixes the **app topology** for closed alpha. It is a decision record, not
a design spec: it says which surfaces exist, who each one serves, and what is
deliberately *not* one app yet. Visual/design polish is deferred (see the
`Workflows` note at the end and [ALPHA_READINESS.md](ALPHA_READINESS.md)).

## Three surfaces, three jobs

Computexchange is **Apple-Silicon-first**. Buyers, suppliers, and the operator
have genuinely different jobs, so they get genuinely different surfaces. We do
**not** force buyer and supplier into one heavy app.

| Surface | Who | Wants | What it is today |
|---|---|---|---|
| **Supplier local app** | the provider, on their Mac | safety, earnings, local control | macOS menu-bar app (`macapp/`) driving the Rust agent (`agent/`) |
| **Buyer / API surface** | the customer | submit jobs, status, results, billing | `cx` CLI (`cli/`), Python SDK (`sdk/python/`), REST API, OpenAI-compatible Batch API, + the `Buyer` tab of the web skeleton |
| **Admin / operator** | the founder | queue, workers, payouts, fraud, incidents | `/admin/*` REST endpoints + the operator dashboard (`/`) + the `Admin` tab of the web skeleton |

### Supplier local app (runs on the provider Mac)
The provider's control surface is **local**, because the things a supplier cares
about are local: "is my Mac safe, am I earning, can I pause it." The Rust agent
writes `~/.compute-exchange/status.json` on every heartbeat and task transition;
the menu-bar app reads it and writes operator prefs back to the agent config.

What it shows: agent state (active/idle/paused/offline), current job + task id,
**resource limits and memory headroom**, **effective allocatable memory**,
**throttled state + reason**, earnings/balance, model-cache size, and a
logs/diagnostics pointer. The provider safety story — dynamic memory throttling —
is described in [ALPHA_READINESS.md](ALPHA_READINESS.md#supplier-throttling).

> A web page can't read a provider's local file, so the web skeleton's `Supplier`
> tab only *documents* this surface. The live supplier surface is the local app.

### Buyer / API surface (no heavy app required)
Buyers integrate, they don't sit in a dashboard. The load-bearing surfaces are the
`cx` CLI, the dep-free Python SDK, the REST API, and the OpenAI-compatible Batch
API — all already proven by `make prove-local`. The web skeleton's `Buyer` tab is
a thin convenience wrapper over the same `POST /v1/jobs` → `GET /v1/jobs/{id}` →
`/results` endpoints, for eyeballing a job without the CLI.

### Admin / operator surface
The operator needs the queue, the worker fleet (now including live **throttle +
effective-memory** state), payouts, fraud flags, and incident response. These are
the `/admin/*` endpoints (admin-key gated), the operator dashboard at `/`, and the
`Admin` tab of the web skeleton. Incident runbooks live in
[RUNBOOKS.md](RUNBOOKS.md).

## Why not one app

- A supplier wants **safety + earnings + local control** — and must run natively on
  Apple Silicon (Metal). That is a local, OS-integrated app.
- A buyer wants **submission + status + results + billing** — and wants it from
  code (CLI/SDK/REST), not a GUI.
- An operator wants **queue + workers + payouts + fraud + incident response** — an
  internal control panel, not a customer-facing product.

Collapsing these into one heavy app would serve none of them well in alpha. The
seams above are intentional and cheap to evolve.

## Deferred: Workflows / Pipelines (the "IDE" idea)

The richer "workflows / pipelines / codebase" UX is **deferred** and represented
only as a named placeholder (`Workflows` tab in the web skeleton, and this note).
It needs **product + design input** before it is built. There is intentionally
**no code editor** and **no arbitrary code execution** in alpha. Today, multi-step
work is expressed by submitting jobs (CLI / SDK / REST / Batch API).

## Where the surfaces live in the repo

```
macapp/                 supplier local app (SwiftUI menu bar) — swift build
agent/                  Rust supplier agent (Metal inference + throttling)
cli/ · sdk/python/      buyer CLI + SDK
control/ (api.go)       REST + /v1/* (buyer) + /admin/* (operator) + / (dashboard) + /app (skeleton)
web/dashboard.html      operator dashboard (served at /)
web/skeleton.html       role-based app skeleton (served at /app)
```
