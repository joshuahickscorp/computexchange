# Operator runbooks

Concrete, copy-pasteable procedures for the four failure modes an operator hits.
All commands assume the control plane's `DATABASE_URL` and an admin key
(`Authorization: Bearer <admin_api_key>` from `make seed`). The admin console at
`/admin` (Fleet · workers table, Fraud flags, Scheduler explain, Audit log) and
the underlying views back most diagnosis: `GET /admin/jobs`, `/admin/workers`,
`/admin/fraud`, `/admin/payouts`, `/admin/scheduler/explain`, `/admin/actions`.

## 1. Stuck job (queued/running but not finishing)

**Symptom.** `GET /v1/jobs/{id}` sits at `queued`/`running`; `tasks_done` not advancing.

**Diagnose.**
- Why won't it claim: `GET /admin/scheduler/explain?worker_id=<id>` runs the exact
  hard-filter predicates `ClaimTask` uses against every currently-claimable task for
  that worker and returns per-reason counts (`memory_mismatch`, `model_mismatch`,
  `job_type_mismatch`, `hw_class_mismatch`, `residency_mismatch`, `throttled`,
  `payout_floor`, `supplier_inactive`) plus `eligible` — the fastest real diagnosis,
  since it names the ONE predicate actually failing instead of requiring the operator
  to reconstruct the claim query by hand. `no_queued_tasks=1` means the queue is
  genuinely empty for that worker (not a bug). Read-only; never claims, never mutates.
- Supply: `GET /admin/workers` — is any worker the right `hw_class`, with a recent
  `last_seen_at`? **Note:** the worker list does NOT include `supported_jobs`/
  `supported_models` (those live on the worker row internally but are not in this
  admin view) — use `scheduler/explain` above for the definitive job-type/model
  eligibility answer instead of trying to eyeball it from `/admin/workers`. No
  eligible worker ⇒ the hard filter correctly keeps it queued (this is not a bug; it
  is missing supply).
- Throttled / under-provisioned supply: `GET /admin/workers` also shows `throttled` +
  `effective_memory_gb` per worker. A worker that is `throttled=true` (the agent
  paused for memory pressure) or whose `effective_memory_gb` is below the job's
  `min_memory_gb` is **deliberately** excluded by the safe-dispatch filter — that is
  the supplier-protection contract working, not a bug (`scheduler/explain` reports
  these as `throttled` / `memory_mismatch`). It self-clears once the provider's
  memory frees up (next heartbeat). The provider sees the reason locally in
  `~/.compute-exchange/status.json` (`throttle_reason`). See
  [ALPHA_READINESS.md](ALPHA_READINESS.md#supplier-throttling).
- Stuck tasks: `psql "$DATABASE_URL" -c "SELECT id,status,claimed_by,visible_at FROM tasks WHERE job_id='<id>'"`. A task `claimed`/`running` past its deadline is auto-requeued by the **stale-task sweep** (its `visible_at` is pushed forward; see workers.go) — wait one sweep interval.

**Fix.** If supply exists but the job is wedged, force-requeue the task:
`POST /admin/tasks/{task_id}/requeue` (optional JSON body `{"reason": "..."}`,
recorded to the audit log at `GET /admin/actions`). Only acts on a task that is
`running`/`retrying` (409 otherwise — nothing to force). The equivalent raw SQL,
if the endpoint is ever unavailable: `psql "$DATABASE_URL" -c "UPDATE tasks SET
status='queued', claimed_by=NULL, visible_at=now() WHERE job_id='<id>' AND status
IN ('running','retrying')"`.
If the buyer wants out: `cx cancel <id>` (refunds unstarted work).

## 2. Bad / fraudulent worker

**Symptom.** A supplier fails honeypots or diverges on redundancy. Surfaced in
`GET /admin/fraud` (reputation, mismatch/clawback counts, `quarantined_at`) and,
identically, the Fraud flags panel on the `/admin` console. The console's Fleet ·
workers table (`GET /admin/workers`) is what maps a flagged `supplier_id` to the
actual `worker_id` a Suspend/Reinstate action needs — the fraud views are
supplier-scoped, suspend/reinstate are worker-scoped.

**Behavior (automatic).** A failed honeypot docks reputation, claws back the task
credit, and **auto-quarantines** the supplier (`status='suspended'`); the scheduler's
`status='active'` gate then excludes it from all future claims. **A repeated
redundancy MISMATCH alone does NOT auto-quarantine** — it only docks reputation
(-0.10 per confirmed tiebreak loss) and can push a supplier out of the higher
trusted tiers (`reputationTier` in reputation.go) or (below 0.5) into the
`/admin/fraud-flags` review list; only a honeypot fail calls `QuarantineSupplier`
and flips `status='suspended'` in this codebase today. A supplier that keeps
losing tiebreaks but never fails a honeypot will keep working, at a lower
reputation/tier, until an operator manually suspends it — this is a real gap
between the automatic and manual halves of this procedure, not a documentation
choice.

**Manual.** Identify the bad worker (Fleet table or `GET /admin/workers`, or the
Fraud flags panel for the `supplier_id`, cross-referenced against Fleet for its
`worker_id`). Suspend now: `POST /admin/workers/{worker_id}/suspend` — on screen in
the console's Fleet table as a "suspend" button per active row. Reinstate after
review: `POST /admin/workers/{worker_id}/reinstate` (a "reinstate" button appears
in the same row once suspended; 409 if the worker's supplier is not currently
suspended, 404 if the worker id doesn't exist). Both suspend and reinstate are
visible immediately in the Fleet table on the next refresh (15s auto-refresh, or
the panel's own "refresh" button) — that refresh IS the on-screen confirmation the
action took effect, no `psql` required.

## 3. Payout failure

**Symptom.** Owed credits not settling. Inspect: `GET /admin/payouts` (per-supplier
rollup across `pending`, `held`, `awaiting_funding`, `ready`, `sending`,
`outcome_unknown`, `carried`, `released`, `exported`, `clawed_back`, and
`reversal_required`). Cash sent, durable carry, unknown outcomes, and suspicious
released-without-cash rows are separate fields; status alone is not cash proof.

**Expected by rail.**
- **No rail configured** (default): credits reach `ready`/owed and stay there — the
  honest stub never marks `released` without a real transfer. This is correct, not a
  failure. **`ready` is a dead end, not a retry queue:** the release-worker sweep
  (`DuePayouts`) only ever selects `payout_status='held'` rows whose `release_at` has
  passed — a `ready` row is never automatically revisited by anything in this
  codebase, even after a payout rail is later configured. It stays `ready`
  permanently until an operator moves it back to `held` (see Fix below).
- **No exact cash reservation**: an elapsed liability becomes
  `awaiting_funding`, leaves the bounded send queue, and remains owed. A canonical
  succeeded PaymentIntent fact or explicit capped subsidy authorization re-arms it;
  an admin release cannot manufacture funding.
- **Manual export** (`CX_PAYOUT_EXPORT=/path/payouts.csv`): each payout key is synced
  to the CSV at most once for out-of-band settlement, the ledger becomes `exported`,
  and the operation records `cash_moved=false`. Reconcile that key against the
  external settlement; export is not evidence that cash moved.
- **Stripe** (`STRIPE_SECRET_KEY` set): a definitive pre-cash failure becomes
  `ready`. A transport error, 5xx, unreadable/malformed success, stale sending lease,
  or other ambiguous result becomes `outcome_unknown`, never `ready`. It retries
  only with the same ledger-entry payout key inside the conservative idempotency
  window; older unknowns require provider/read-only evidence.

**Fix.** To make the release worker genuinely retry a credit — whether it is
currently `held` with a not-yet-due `release_at`, or sitting `ready` with nothing
retrying it: `POST /admin/payouts/{entry_id}/release` (optional JSON body
`{"reason": "..."}`, recorded to the audit log at `GET /admin/actions`). This
accepts an entry that is CURRENTLY `held` OR `ready` (409 for every other status)
and always leaves it `held` with `release_at`
pulled forward to now(), so the existing release-worker sweep (`DuePayouts`)
picks it up on its very next cycle — it never marks the entry `released` itself
(that still requires a real transfer reference, enforced structurally by the
`ledger_released_requires_ref` constraint), so this can never fake a payout.
**Do not manually set `payout_status='ready'` on a stuck credit as a "re-hold" or
"retry" action** — an earlier version of this runbook did exactly that, and it
silently left the credit permanently stuck: `DuePayouts` (the real release-worker
sweep query) only ever selects `payout_status='held'` rows, so a row moved to
`ready` is never automatically revisited by anything in this codebase. The
endpoint above is the correct fix for both directions (a definitely-unsent `ready`
attempt that needs a retry, and a `held`-but-not-yet-due credit that needs releasing
early). Do not bypass it with raw SQL: that omits the authenticated action and can
contradict the immutable funding and payout-operation records.

Never rewrite a supposedly false `released`, `outcome_unknown`, or
`reversal_required` ledger row. Durable `cash_moved` and `outcome_unknown` operation
facts remain reconciliation authority through later status changes. Preserve the
provider/key/reference evidence, stop manual release attempts, and treat the case
as an incident. The automated provider lookup/reversal workflow remains an open 5/5
gate; a status edit is not recovery.

## 4. Storage failure (object store unreachable / object missing)

**Symptom.** `/healthz` is 500 (the control plane fatals at startup if the object
store is unreachable — a 200 means deps are wired), or a results fetch errors.

**Diagnose.** `curl -fsS "$S3_ENDPOINT/minio/health/live"` (or your S3 health). Check
`S3_ENDPOINT` (control-side) vs `S3_PUBLIC_ENDPOINT` (the URL agents reach) — a
presigned GET/PUT signed against the wrong endpoint is the usual culprit.

**Fix.** Restart the object store, then the control plane (it re-verifies deps at
boot). A missing per-task result object is surfaced by the merge as a hard error
(never a short artifact); requeue that task (`POST /admin/tasks/{task_id}/requeue`,
runbook 1) to re-produce it. Restore from backup if the store was lost — see
**Backups & disaster recovery** below.

## Deploy (first run + redeploy)

**Owner steps (minimal).**
1. `cp .env.example .env` and fill the **PRODUCTION OPS** block (and the Stripe /
   CX secrets). Generate passwords with `openssl rand -base64 32`.
2. Point `SITE_HOST` / `STORAGE_HOST` DNS A/AAAA records at the droplet, and
   open 80 + 443 (Caddy needs 80 for the ACME challenge).
3. `scripts/deploy.sh` (add `--monitoring` to bring up the monitoring profile).

`deploy.sh` validates the compose config, builds, rolls the stack with a health
gate (waits for both control instances + Caddy + Postgres + MinIO to report
healthy), then smoke-checks `https://$SITE_HOST/healthz`. It fails loudly and
nonzero on any unhealthy service or a non-200 edge · it never reports a green
deploy over a broken stack. Re-run it to ship a new build (`--pull` to
`git pull --ff-only` first).

## High availability (two control instances)

`docker-compose.prod.yml` runs **`control` + `control-2`**, both behind the
**Caddy** reverse-proxy as a round-robin, health-checked load balancer
(`reverse_proxy control:8080 control-2:8080`, active probe of `/readyz`). One
control process can die; Caddy stops routing to it and serves from the other.

**Why this is safe (and the honest caveats).**
- **Dispatch/poll is multi-instance-safe.** The task queue claims rows with
  `SELECT ... FOR UPDATE SKIP LOCKED` (control/scheduler.go), so both instances
  hand out distinct tasks with no double-dispatch. This is the load-bearing
  guarantee and it holds.
- **The Stripe webhook is safe to load-balance.** `POST /v1/stripe/webhook` is
  stateless, signature-verified, and idempotent on the event id, so it does not
  matter which instance Caddy routes a given webhook to.
- **Background sweeps have one automatically elected owner.** Both replicas run
  the same `CX_RUN_WORKERS=true` path. `control/worker_leader.go` holds a
  PostgreSQL **session advisory lock** for the lifetime of `workers.Run`, so only
  one replica can run payout release, charge collection, stale-task rescue,
  webhook delivery, reconcile, hedge, dispute, and the other sweeps. The other
  replica polls as a standby. Process exit, connection loss, or an owned sweep
  missing its liveness budget releases/discards the lock-bearing session; the
  standby then acquires it without an operator flipping environment variables.
- **Readiness covers the elected-worker path.** `/readyz` checks database
  reachability, recent progress of an enabled election loop, and every ticker
  owned by the current leader. A follower has no local sweep tickers, but it must
  continue observing the election. Caddy and the container healthchecks therefore
  drain a replica whose DB, election loop, or owned sweeps stop progressing.
- **The wake-on-work LISTEN/NOTIFY listener (`notify.go`) runs on EVERY
  instance regardless of `CX_RUN_WORKERS`** — it only relays a Postgres
  notification to that instance's own local long-poll waiters, so there is
  nothing to double-run or leader-gate there; each instance simply wakes its own
  callers. The **WedgedTicker** alert covers an owned sweep that stalls.
- **This is process HA, not failure-domain HA.** In this Compose topology both
  controls, Caddy, PostgreSQL, and MinIO still run on one host. Losing that host,
  PostgreSQL, its volume, or MinIO interrupts the service regardless of the
  second control process. Multi-host/database/object-store replication and a
  tested cross-host failover are separate deployment work; this file does not
  claim them.

## Backups & disaster recovery

**Real, offsite backups.** `scripts/backup.sh` (cron-friendly) takes a
custom-format `pg_dump` (`-Fc`) of the ledger/jobs DB **and** a mirror of the
MinIO object store, checksums both, and **ships them offsite** to an
S3-compatible bucket (`CX_BACKUP_OFFSITE` · AWS S3 / DO Spaces / R2 / B2). It
verifies the upload landed and prunes local staging to the last
`CX_BACKUP_KEEP_LOCAL` (default 7). The double-entry ledger is the source of
truth · back it up at least as often as you settle payouts.

BLACKHOLE: if the offsite destination or creds are missing, or the upload
fails, `backup.sh` **exits nonzero and shouts** · it never silently degrades to
a local-only copy and reports success.

**Owner steps.** Fill the offsite vars in `.env` (`CX_BACKUP_OFFSITE`,
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, optional `CX_BACKUP_S3_ENDPOINT`
for non-AWS), then add the cron below.

**Cron (daily 03:17, logs to syslog).** On the droplet:
```
17 3 * * *  cd /opt/computexchange && /usr/bin/flock -n /tmp/cx-backup.lock \
            ./scripts/backup.sh >> /var/log/cx-backup.log 2>&1
```
(`flock` prevents an overlapping run if one night's backup runs long.)

**WAL archiving / PITR (optional, for point-in-time recovery).** The base backup
above is the point WAL replay starts from. To recover to any second between base
backups, set on the Postgres server (`postgresql.conf` or compose `command`):
```
wal_level = replica
archive_mode = on
archive_command = 'aws s3 cp %p s3://cx-backups/wal/%f'
```
Then restore the base backup and replay WAL with `recovery.signal` +
`restore_command`. Without this you recover to the last nightly base backup
(RPO = up to 24h); with it, RPO ≈ seconds.

### Restore / DR drill (TESTED)

`scripts/restore.sh` pulls a backup from offsite, **verifies its checksums**,
and restores the DB (`pg_restore`, wrapped in one transaction so any error rolls
back the whole thing · never a half-restored DB) and the objects (`mc mirror`
back into MinIO).

```
scripts/restore.sh --latest                 # full restore, newest backup
scripts/restore.sh 20260629T031700Z         # a specific backup
scripts/restore.sh --latest --to cx_restore # DR DRILL: restore DB into a SCRATCH
                                            # database; live `cx` is untouched
```

**Drill procedure (run monthly; this is the exact tested path).**
1. `scripts/backup.sh` · take a fresh backup; confirm it ends with
   `offsite verified: …/db.dump present`.
2. `scripts/restore.sh --latest --to cx_restore` · restores the offsite dump
   into a scratch database, leaving live `cx` alone. It fails loudly on a
   checksum mismatch or a `pg_restore` error.
3. Verify the data survived:
   `docker compose -f docker-compose.prod.yml exec postgres psql -U cx -d cx_restore -c "SELECT count(*) FROM jobs;"`
   · compare against the same count on live `cx`.
4. Drop the scratch DB:
   `docker compose -f docker-compose.prod.yml exec postgres psql -U cx -d cx -c "DROP DATABASE cx_restore;"`

**Two independent restore proofs.** Besides this offsite drill, `make
prove-local` runs a `disaster-recovery` check on **every** run: it `pg_dump`s
the live DB, restores into a fresh database, and asserts the jobs survive. So
"we have backups" is proven restorable two ways · local round-trip on every CI
run, and offsite end-to-end on the monthly drill · never assumed.

**Real DR (the host is gone).** On a fresh droplet: install Docker, clone the
repo to `/opt/computexchange`, `cp .env.example .env` and restore your saved
secrets, `scripts/deploy.sh` to bring up an empty stack, then
`scripts/restore.sh --latest` to restore DB + objects from offsite, then
`docker compose -f docker-compose.prod.yml restart control control-2`.

## Monitoring & alert response

Bring up the stack with the monitoring profile:
`docker compose -f docker-compose.prod.yml --profile monitoring up -d`. Wiring,
scrape targets, and the metric→alert mapping are in `monitoring/README.md`.
Grafana is localhost-only · reach it via SSH tunnel
(`ssh -L 3000:localhost:3000 droplet`, then http://localhost:3000, login
`admin` / `GRAFANA_ADMIN_PASSWORD`); the `Computexchange · Control Plane`
dashboard is auto-provisioned.

**Owner steps (minimal).** Set `GRAFANA_ADMIN_PASSWORD` and at least one alert
channel (`SLACK_WEBHOOK_URL` and/or `PAGERDUTY_ROUTING_KEY`) and the watchdog
`DEADMANSSWITCH_URL` in `.env`. That is all · Prometheus, Alertmanager, the
exporters, the rules, and the dashboard are all provisioned from the repo.

**Per-alert response.**
- **WedgedTicker** (`cx_ticker_seconds_since_success > 300`) · the headline
  "looks alive but isn't" alert: the background sweep loop has not completed a
  cycle, so payouts are not settling and stale tasks are not requeued.
  Triage: `docker compose -f docker-compose.prod.yml logs --tail=200 control control-2`
  for a stuck query or panic; check **PostgresConnectionsNearLimit** (a wedged
  ticker is often pool exhaustion). Recover: restart the affected instance
  (`… restart control`); the other instance keeps serving. Then root-cause from
  the logs.
- **PayoutsNotReleasing** · work completing but `cx_payouts_released_total` flat
  for 2h. Either no rail is configured (expected in alpha; credits sit `owed`)
  or Stripe is rejecting every transfer. See §3 (Payout failure) above.
- **PostgresConnectionsNearLimit** · >85% of `max_connections` in use. Two
  control instances each hold `DB_MAX_CONNS`; lower it per instance, or raise
  Postgres `max_connections`. If `postgres_exporter` is not deployed,
  **PostgresExporterAbsent** fires instead (the blind spot is never silent) and
  pool exhaustion shows up indirectly as **WedgedTicker**.
- **HighHTTP5xxRate** · >5% 5xx at the Caddy edge. If one instance is bad, Caddy
  should already route around it; a sustained rate means both are affected or a
  dependency (Postgres/MinIO) is down · check `/healthz` on each and §4.
- **TLSCertExpiringSoon / TLSProbeFailing** · see "Cert renewal" below.
- **InstanceDown** · a scrape target is unreachable. For `control` the other
  instance still serves; restore the down one. For `prometheus`/`alertmanager`
  the **DeadMansSwitch** watchdog is your backstop.

## Rollback

The control image is tagged `computexchange/control:latest`. To roll back a bad
deploy:
1. `git -C /opt/computexchange checkout <last-good-sha>`
2. `scripts/deploy.sh` (rebuilds `:latest` from the good code and rolls the
   stack with the same health gate + smoke check).

The stack is recreated in place; pgdata/miniodata volumes persist, so a code
rollback does not touch data. If a **migration** shipped with the bad build and
must be undone, that is a data operation · restore from the pre-deploy backup
(§Restore) rather than a code rollback alone. Caddy health-checks mean that
during the roll, requests drain to whichever control instance is healthy.

## Secret rotation

All secrets live in `.env` on the droplet (mode `600`, never committed).

- **Stripe / GitHub / CX_* app secrets, Grafana, alert channels:** edit `.env`,
  then `docker compose -f docker-compose.prod.yml up -d` (recreates only the
  services whose env changed). Do **not** replace `CX_TOKEN_KEY` in place: there
  is no old-key overlap or lazy re-encryption path. Losing that key deliberately
  makes existing sealed GitHub tokens and buyer webhook secrets unreadable. A
  planned key rotation needs an explicit decrypt-and-reseal migration while the
  old key remains available; until that migration exists, reconnect each GitHub
  source and have each owning buyer exactly re-register its webhook, then verify
  delivery before retiring the old deployment/key backup.
- **POSTGRES_PASSWORD:** change it in Postgres first, then `.env`, then recreate:
  `docker compose -f docker-compose.prod.yml exec postgres psql -U cx -d cx -c "ALTER USER cx WITH PASSWORD '<new>';"`,
  update `POSTGRES_PASSWORD` in `.env`, then `… up -d postgres control control-2`
  (the `DATABASE_URL` is composed from it). Verify `/healthz` 200 after.
- **MINIO_ROOT_PASSWORD:** rotate via MinIO admin (`mc admin user`), update
  `.env`, recreate `minio control control-2`.
- **Offsite backup keys (AWS_*):** rotate at the provider, update `.env`, run
  `scripts/backup.sh` once to confirm the new keys ship offsite.

After any rotation, re-run a `scripts/restore.sh --latest --to cx_restore` drill
within the week so a stale-cred backup is caught early.

## Cert renewal

Caddy obtains and **auto-renews** TLS certs (Let's Encrypt) for `SITE_HOST` and
`STORAGE_HOST`, starting ~30 days before expiry; certs/keys persist in the
`caddy_data` volume across restarts. There is normally nothing to do.

If **TLSCertExpiringSoon** (inside 14d) or **TLSProbeFailing** fires, renewal is
failing. Check, in order:
1. **Ports:** 80 and 443 reachable from the internet? ACME HTTP-01 needs 80.
2. **DNS:** `SITE_HOST`/`STORAGE_HOST` still resolve to this droplet?
3. **Caddy logs:** `docker compose -f docker-compose.prod.yml logs caddy` for the
   ACME error (rate limit, DNS, challenge failure).
4. Force a renewal by recreating Caddy: `… up -d --force-recreate caddy`.
5. If rate-limited by Let's Encrypt, wait out the window or switch to the ACME
   staging CA temporarily to debug. The `caddy_data` volume must not be wiped
   (that discards the ACME account + valid certs).
