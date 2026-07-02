# Supplier surface + workflows · where they live

Relocated from `web/skeleton.html` when that role-tab skeleton was retired (the
operator surface is now the passkey-gated Control Room at `/admin`; the old
dashboard at `/` and skeleton at `/app` are gone). Nothing here changed — this is
the durable home for the two notes the skeleton carried.

## The supplier control surface is the local menu-bar app

A supplier's real surface is the **macOS menu-bar app** (`macapp/`), which reads
`~/.compute-exchange/status.json` written by the running `cx-agent`. A browser
cannot read a provider's local file, so there is no web supplier console — the app
is it. The fields the agent already emits (see `agent/src/status.rs` and
`macapp/README.md` for the full contract):

| Field | Meaning |
|---|---|
| `state` | active / idle / paused / offline |
| `current_job` / `current_task_id` | what is running now |
| resource limits | memory headroom + max memory % (from `agent.toml`) |
| `effective_memory_gb` | available − reserved headroom |
| `throttled` / `throttle_reason` | why new claims are paused |
| `today_earnings_usd` / `balance_usd` | earnings surface |
| logs / diagnostics | opened from the app (`~/.compute-exchange`) |

A worker's live throttle + effective-memory state is also visible to the operator
in the Control Room's fleet line (`GET /admin/summary` · `GET /admin/workers`).

## Workflows / pipelines are expressed through jobs

There is no workflow/pipeline/"IDE" console surface, and none is planned here.
Multi-step work is expressed by **submitting jobs** — via the `cx` CLI, the Python
SDK, or the OpenAI-compatible Batch API. The final workflow UX (if any) needs
product + design input before it is built; there is deliberately no code editor and
no arbitrary code execution.
