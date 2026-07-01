# Launch Re-Evaluation — 2026-06-30

Supersedes the verdict in `SHIP_AND_DOMINATE.md` §1–§3, which predates the Phase 2/3/3.5
work, the P0 backend frontier work, and the 2026-06-30 web-app simplification (sign-in
removed; device + payment is the only identity). Read this first; that doc's perf/market
sections (§4–§7) still stand.

## Do we need a launch audit? — Yes, but a SHORT one.

A lot shipped since the original audit, and most of its "ship blockers" are now
**code-resolved**. What changed materially enough to re-verify is small and specific:

1. The **frontend wall is largely down** (signup→key→card→results, billing UI, verification
   receipt, API-key lifecycle all exist).
2. The **sign-in flow was just removed** in favor of silent device identity + payment-only
   onboarding — a deliberate change to the **trust/abuse model** that needs its own check.

So the audit is no longer "build the product surface." It is: confirm the few external
deps, the ops gaps, and the **one new abuse vector** the de-auth opens.

## Original 9 blockers — reconciled to current reality

| # | Original blocker | Status now |
|---|---|---|
| 1 | Stripe Connect live creds + supplier tax onboarding | **Code-ready, externally blocked.** `POST /v1/supplier/onboard` + `GET /v1/worker/connect/status` exist; tax_id/country captured. Needs LIVE Stripe creds (external). |
| 2 | Legal/compliance green-light | **External, unchanged.** FINTRAC/MSB, CRA Part XX, GST/HST, PIPEDA/Law 25, ToS. None code. |
| 3 | Buyer surface: signup→key→card→results | **DONE, then simplified.** Built in Phase 2/3; on 2026-06-30 the sign-in/password UX was removed — identity is now the device (silent provisioning), the only onboarding is "connect a payment method." |
| 4 | Production data durability (DR) | **Substantially addressed.** `scripts/backup.sh`/`restore.sh` do offsite S3; `bootstrap-prod.sh` backs up before a live upgrade. Gap: scheduled/automated cadence + a restore drill. |
| 5 | Monitoring + alerting | **Still a gap (ops).** `/metrics` + `/healthz` exist; nothing scrapes/alerts. Unacceptable above a hand-watched alpha. |
| 6 | Buyer `charge_status` exposed + failure notify | **DONE.** `JobView.ChargeStatus` (COALESCE `not_attempted`) is exposed; submit-time 402 gate already mitigated the worst case. |
| 7 | Single-point-of-failure control plane | **Still single droplet (ops).** SKIP-LOCKED queue is multi-instance-ready; only one instance runs. |
| 8 | macOS app sign + notarize + auto-update | **Code-ready, externally blocked.** Swift app builds (verified `swift build`), Sparkle wired; needs Apple Developer ID + notarization. |
| 9 | TLS cert renewal + secrets rotation | **Ops, unchanged.** Caddy assumed; document + monitor renewal. |

**Net:** of 9, four are code-resolved (3, 4, 6, and effectively the frontend epics in §3),
two are code-ready/externally-blocked (1, 8), and three are pure ops/legal (2, 5, 7, 9).
**No remaining blocker is a core-correctness gap.**

## NEW — introduced by the 2026-06-30 de-auth (must verify before public launch)

- **Free-credit abuse vector (the one real new risk).** Silent device signup means any
  anonymous visitor gets a `cx_test_` key + sandbox credit per browser, with no email
  friction. Rate-limiting today is **per-IP rate only** (`ipLimiter` 30/s burst 60) — it
  throttles bursts but does **not cap total signups** per IP over time. Mitigations to pick
  before opening the marketing site to a live backend:
  - lower or zero the default `CX_SANDBOX_CREDIT_USD` grant, OR
  - require a card *before* any free credit (card-first), OR
  - add a per-IP/day signup cap (not just a rate limit), and/or a proof-of-work/turnstile on
    `/v1/signup`.
  Real spend is still hard-gated on a saved card (402), so the blast radius is *free* credit,
  not money — but it must be a deliberate decision, not a default.
- **Device session refresh (follow-up, not a blocker).** `sessionTTL = 30 days`. The web app
  re-provisions only when `cx_key` is empty; an *expired-but-present* token will 401 without
  auto-refresh. Add a 401 interceptor that clears `cx_key` and re-runs `ensureDeviceKey()`.
  Low urgency (30-day window), but it is a real edge.
- **Multi-device = multi-account by default.** Each browser/device is its own buyer; the same
  card on two devices makes two Stripe customers, not one plan. This matches the "device +
  payment = identity" model and is fine for launch, but if "same card unifies devices/plan"
  is desired later, that is a backend dedup feature (by payment fingerprint) — out of scope now.

## What's left to do (folded into the running plan)

**Before charging real customers (external/ops, mostly not code):**
- [ ] Stripe **live** Connect creds + verify payout release end-to-end (blocker 1).
- [ ] Legal/compliance green-light (blocker 2).
- [ ] Monitoring/alerting: scrape `/metrics`, alert on pool exhaustion, payout failures, cert
      expiry, wedged tickers (blocker 5).
- [ ] Decide + implement the **free-credit abuse** mitigation above (NEW — small code change).
- [ ] Apple Developer ID sign + notarize the menu-bar app (blocker 8).
- [ ] Automate backup cadence + run one restore drill (blocker 4 finish).
- [ ] TLS renewal monitoring + secret rotation runbook (blocker 9).

**Nice-to-have hardening (real, not blockers):**
- [ ] 401 device-session refresh interceptor (web app).
- [ ] Per-API-key rate limiting (currently per-IP, gameable).
- [ ] Background-ticker liveness guard; webhook dead-letter queue.
- [ ] Copy pass on the web app: remove residual "paste a live key" placeholder copy in
      Settings billing/payout (now that identity is automatic).

**Recommendation:** the engine + product surface are launch-grade. Gate a **capped,
hand-watched alpha** on: Stripe live creds, the free-credit mitigation, and basic alerting.
Everything else (HA, notarized app distribution, full legal) gates the *scale* step, not the
first real transaction.
