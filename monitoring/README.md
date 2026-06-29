# Monitoring stack

Prometheus + Alertmanager + Grafana + exporters, wired into the prod compose as
a profile. Brings up nothing extra in plain `docker compose up`; you opt in with
the `monitoring` profile.

```
docker compose -f docker-compose.prod.yml --profile monitoring up -d
```

## What scrapes what

| Job | Target | Provides |
|---|---|---|
| `control` | `control:8080`, `control-2:8080` `/metrics` | the `cx_*` business metrics incl. `cx_ticker_seconds_since_success` (WEDGED-TICKER) |
| `caddy` | `caddy:2019/metrics` | `caddy_http_requests_total{code}` · the only place 5xx is observable |
| `node` | `node-exporter:9100` | host CPU / mem / disk |
| `cadvisor` | `cadvisor:8080` | per-container mem vs `mem_limit`, restart loops |
| `blackbox-tls` | public 443 via `blackbox-exporter:9115` | `probe_ssl_earliest_cert_expiry` (cert expiry) |

The Go control plane exposes hand-rolled `cx_*` exposition (see
`control/metrics.go`) · it deliberately does NOT export raw HTTP request
counters or DB-pool gauges. So:

- **5xx** is measured at the **Caddy edge** (`caddy_http_requests_total`), which
  is also where it matters (what users actually saw).
- **DB pool exhaustion** has no native gauge. The primary signal is indirect —
  pool exhaustion blocks every sweep query and the **WedgedTicker** alert fires.
  For a direct rule, deploy `postgres_exporter` (point it at the `cx` DB); the
  `PostgresConnectionsNearLimit` rule activates the moment its series appear,
  and `PostgresExporterAbsent` fires (ticket) until then so the blind spot is
  never silent.

## Alerts (`alerts.yml`)

`WedgedTicker` (payout/sweep loop stalled · the headline alert),
`PayoutsNotReleasing`, `PostgresConnectionsNearLimit`, `HighHTTP5xxRate`,
`TLSCertExpiringSoon`, `InstanceDown`, host disk/memory, container restart
loops. Every rule has a paired `*Absent` / `*NoData` rule so a missing metric
pages instead of reading as green. `DeadMansSwitch` always fires; wire its
receiver to an external watchdog so a dead Prometheus is caught.

## Alert delivery (`alertmanager.yml`)

Slack for everything, PagerDuty for `severity: page`, a watchdog webhook for the
heartbeat. Credentials come from `.env` (`SLACK_WEBHOOK_URL`,
`PAGERDUTY_ROUTING_KEY`, `DEADMANSSWITCH_URL`). The compose entrypoint runs
`envsubst` over this file at start so the `${...}` placeholders resolve from the
container env. Unset creds → Alertmanager logs a loud delivery error on the
first alert (never a silent drop).

## Grafana

Auto-provisioned datasource + the `Computexchange · Control Plane` dashboard
(ticker freshness, throughput, queue depth, 5xx, TLS expiry, container memory).
Login with `GRAFANA_ADMIN_PASSWORD` from `.env`. Grafana is bound to localhost
only; reach it via SSH tunnel (`ssh -L 3000:localhost:3000 droplet`) — it is not
fronted by Caddy.

## Log shipping

All app services use the `json-file` driver with rotation (`max-size: 10m`,
`max-file: 5`) via the `x-logging` anchor in the compose file, so container logs
are bounded and survive restarts (`docker compose logs -f <svc>`). For a
queryable, retained log store, add Loki + Promtail (Promtail tails
`/var/lib/docker/containers/*/*-json.log`, ships to Loki, Grafana queries it as a
second datasource). Not shipped by default to keep the droplet footprint small;
the rotation policy above is the baseline.
