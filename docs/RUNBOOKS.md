# Operator runbooks

Concrete, copy-pasteable procedures for the four failure modes an operator hits.
All commands assume the control plane's `DATABASE_URL` and an admin key
(`Authorization: Bearer <admin_api_key>` from `make seed`). The admin views back
most diagnosis: `GET /admin/jobs`, `/admin/workers`, `/admin/fraud`, `/admin/payouts`.

## 1. Stuck job (queued/running but not finishing)

**Symptom.** `GET /v1/jobs/{id}` sits at `queued`/`running`; `tasks_done` not advancing.

**Diagnose.**
- Supply: `GET /admin/workers` — is any worker the right `hw_class` + `supported_jobs`/`supported_models`, with a recent `last_seen`? No eligible worker ⇒ the hard filter correctly keeps it queued (this is not a bug; it is missing supply).
- Throttled / under-provisioned supply: `GET /admin/workers` also shows `throttled` + `effective_memory_gb` per worker. A worker that is `throttled=true` (the agent paused for memory pressure) or whose `effective_memory_gb` is below the job's `min_memory_gb` is **deliberately** excluded by the safe-dispatch filter — that is the supplier-protection contract working, not a bug. It self-clears once the provider's memory frees up (next heartbeat). The provider sees the reason locally in `~/.compute-exchange/status.json` (`throttle_reason`). See [ALPHA_READINESS.md](ALPHA_READINESS.md#supplier-throttling).
- Stuck tasks: `psql "$DATABASE_URL" -c "SELECT id,status,claimed_by,visible_at FROM tasks WHERE job_id='<id>'"`. A task `claimed`/`running` past its deadline is auto-requeued by the **stale-task sweep** (its `visible_at` is pushed forward; see workers.go) — wait one sweep interval.

**Fix.** If supply exists but the job is wedged, force-requeue the task lease:
`psql "$DATABASE_URL" -c "UPDATE tasks SET status='queued', claimed_by=NULL, visible_at=now() WHERE job_id='<id>' AND status IN ('running','retrying')"`.
If the buyer wants out: `cx cancel <id>` (refunds unstarted work).

## 2. Bad / fraudulent worker

**Symptom.** A supplier fails honeypots or diverges on redundancy. Surfaced in
`GET /admin/fraud` (reputation, mismatch/clawback counts, `quarantined_at`).

**Behavior (automatic).** A failed honeypot docks reputation, claws back the task
credit, and **auto-quarantines** the supplier (`status='suspended'`); the scheduler's
`status='active'` gate then excludes it from all future claims. Repeated redundancy
mismatches drive reputation below the tier/quarantine threshold.

**Manual.** Suspend now: `POST /admin/workers/{worker_id}/suspend`. Reinstate after
review: `psql "$DATABASE_URL" -c "UPDATE suppliers SET status='active', quarantined_at=NULL WHERE id='<supplier_id>'"`.

## 3. Payout failure

**Symptom.** Owed credits not settling. Inspect: `GET /admin/payouts` (per-supplier
rollup by state: `pending`/`held`/`released`/`clawed_back`).

**Expected by rail.**
- **No rail configured** (default): credits reach `ready`/owed and stay there — the
  honest stub never marks `released` without a real transfer. This is correct, not a failure.
- **Manual export** (`CX_PAYOUT_EXPORT=/path/payouts.csv`): owed credits are appended
  to the CSV for out-of-band settlement; reconcile the CSV against `/admin/payouts`.
- **Stripe** (`STRIPE_SECRET_KEY` set): a non-2xx transfer surfaces loudly and the
  credit stays owed (never faked released). Re-running the release worker retries
  idempotently (idempotency key per supplier+amount).

**Fix.** Re-hold a prematurely-released-by-error credit:
`psql "$DATABASE_URL" -c "UPDATE ledger_entries SET payout_status='ready', release_at=now() WHERE id='<entry_id>'"`, then let the release worker retry.

## 4. Storage failure (object store unreachable / object missing)

**Symptom.** `/healthz` is 500 (the control plane fatals at startup if the object
store is unreachable — a 200 means deps are wired), or a results fetch errors.

**Diagnose.** `curl -fsS "$S3_ENDPOINT/minio/health/live"` (or your S3 health). Check
`S3_ENDPOINT` (control-side) vs `S3_PUBLIC_ENDPOINT` (the URL agents reach) — a
presigned GET/PUT signed against the wrong endpoint is the usual culprit.

**Fix.** Restart the object store, then the control plane (it re-verifies deps at
boot). A missing per-task result object is surfaced by the merge as a hard error
(never a short artifact); requeue that task (runbook 1) to re-produce it. Restore
from backup if the store was lost — see **Backups & disaster recovery** below.

## Backups & disaster recovery

**Backup** (cron-friendly): `make backup` (or `scripts/backup.sh`) writes a
timestamped `pg_dump` of the ledger/jobs DB plus a copy of the object-store data
under `.artifacts/backups/`. The double-entry ledger is the source of truth — back
it up at least as often as you settle payouts.

**Restore / DR drill.** `prove-local` runs a real disaster-recovery check every run
(`disaster-recovery`): it `pg_dump`s the live DB, restores into a fresh database,
and asserts the jobs survive — so "we have backups" is proven restorable, not
assumed. To restore for real: create a fresh DB and `psql <new_url> -f <dump.sql>`,
re-point `DATABASE_URL`, and restart the control plane.
