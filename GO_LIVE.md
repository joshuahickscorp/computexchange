# Draft external-service runbook — not a go-live attestation

<!-- CLAIM-SCOPE: draft-runbook-non-authoritative -->

This file records candidate commands for later Stripe, Apple, and RunPod work. It
does not mean the engine, product, policies, deployment, or market are go-live, and
running a script cannot close the corresponding 5/5 outcome gate by itself. Use
[`proof/5x5-gates.json`](proof/5x5-gates.json) for current status and required
evidence. Never provision paid infrastructure or use live credentials from this
runbook without the user's explicit authorization and a stated spend/scope cap.

**What you supply** (the only things a script can't do for you): a Stripe secret key,
an Apple Developer ID + notary credentials, and a RunPod API key. Everything else is
scripted.

---

## 1 · Stripe — payments + supplier payouts

Prereq: the code is deployed but the control service is expected to remain
unready until all live-money inputs exist. Production requires
`STRIPE_SECRET_KEY`, two distinct webhook secrets, the two hardening secrets,
and all five versioned `CX_ECON_*` values; startup rejects an incomplete set.

```bash
# a) put your Stripe keys in .env (hidden prompts; re-runnable, blank keeps current).
#    Paste sk_live_… for STRIPE_SECRET_KEY, review the five economic inputs, and
#    either paste both endpoint secrets or leave those two blank for step (b).
bash scripts/setup-keys.sh

# b) auto-register BOTH webhook endpoints and capture their signing secrets → .env.
#    Creates: /v1/stripe/webhook (setup_intent.succeeded, payment_method.attached)
#         and /v1/stripe/connect-webhook (account.updated).
HOST=computexchange.net bash scripts/stripe-webhooks.sh

# c) load the new secrets (on the droplet):
cx reload      # or: docker compose -f docker-compose.prod.yml up -d control
```

Verify: `curl -s https://computexchange.net/readyz` → 200, and in the Stripe
dashboard both endpoints show "enabled". A supplier then links payouts in-app
(Earn → connect), which drives `account.updated` → `payouts_enabled=true`.

> Set both Connect onboarding URLs explicitly in the production `.env`, for example
> `CX_CONNECT_RETURN_URL=https://computexchange.net/earn?connected=1` and
> `CX_CONNECT_REFRESH_URL=https://computexchange.net/earn/connect/refresh`.
> Production startup requires both to be HTTPS URLs on `SITE_HOST`; there is no
> cross-marketplace fallback.

> Note: `stripe-webhooks.sh` writes a NEW endpoint's secret automatically. If an
> endpoint already exists, Stripe won't re-reveal its secret via API — the script
> says so and points you at the dashboard "reveal" (it never invents one).

---

## 2 · Apple — the notarized macOS supplier app

Prereq (one-time, from an Apple Developer account):
- Install your **Developer ID Application** certificate in your login keychain.
- Generate the Sparkle EdDSA key pair once and paste the PUBLIC key into
  `macapp/ComputeExchangeAgent/Info.plist` → `SUPublicEDKey`
  (see [macapp/README.md](macapp/README.md) → Auto-update). Until you do, the app is
  fail-closed: no update can install (never a silent unsigned one).

```bash
# a) build + assemble the runnable (UNSIGNED) .app bundle. Builds the Swift app,
#    embeds cx-agent + the icon + Sparkle.framework. → build/ComputeExchangeAgent.app
macapp/assemble-app.sh

# b) sign + notarize + staple (needs your Developer ID). Fails loudly naming any
#    missing variable; never fakes a signature.
export DEVELOPER_ID="Developer ID Application: Your Name (TEAMID)"
export TEAM_ID="TEAMID"
export NOTARY_PROFILE="cx-notary"   # created once via: xcrun notarytool store-credentials
#   (or instead of NOTARY_PROFILE: export APPLE_ID=… APPLE_PASSWORD=<app-specific>)
export SPARKLE_KEY="/path/to/sparkle/bin/sign_update"   # optional: prints appcast sig
APP_PATH=build/ComputeExchangeAgent.app macapp/sign-notarize.sh
```

Verify: the script runs `spctl --assess --type execute` at the end — a pass means
Gatekeeper will accept the app. `sign-notarize.sh` also prints the Sparkle
`edSignature` + `length` to paste into `macapp/appcast.xml` for the release.

---

## 3 · RunPod — CUDA re-proof on real NVIDIA hardware

Why: the archived June spike proved the *old* Candle CUDA build. This re-runs the
same proof (`scripts/prove-cuda.sh`) against the current branch, so the perf-wave
CUDA-relevant code (KV preallocation, the batched-vs-serial parity test, engine
tags, the Qwen fix) is validated on a GPU — not just Metal. This is the bare CUDA
re-proof; it is **not** the vLLM determinism soak (that needs pinned reference
servers — see [docs/VLLM_LANE.md](docs/VLLM_LANE.md)).

Prereq (one-time): add your SSH **public** key in the RunPod console
(Settings → SSH Public Keys) so provisioned pods let you in.

```bash
# one command: provision A100 → rsync repo → prove-cuda.sh → print ledger → terminate.
# It TEARS THE POD DOWN on exit so a forgotten box can't bleed cost.
export RUNPOD_API_KEY=...            # RunPod console → Settings → API Keys
bash scripts/runpod-spike.sh
```

Knobs (env): `GPU_TYPE` (default `NVIDIA A100 80GB PCIe`), `CLOUD_TYPE`
(`COMMUNITY` cheaper / `SECURE`), `GPU_COUNT`, `KEEP=1` to leave the pod up for
inspection. Subcommands: `up` (provision only), `ssh` (shell in), `down`
(terminate). The proof log lands in `.artifacts/runpod-spike-proof.log`.

Expect the script to emit a narrow CUDA test ledger and teardown status. A green
run is hardware evidence for the exact pinned build/workload only; it does not
promote CUDA to production, prove multi-lane identity, establish a competitive
speedup, or close the 24-hour soak gate. A **non-zero exit tears the pod down too**;
only `KEEP=1` leaves it running (and warns that it is costing money).

---

## The honest remainder (no script closes these)

- **Legal/compliance** — FINTRAC/MSB opinion, CRA Part XX, GST/HST, PIPEDA, ToS.
  Needs a professional. (RELEASE_CANDIDATE.md → "Legal & compliance".)
- **Free-credit exposure** — the sandbox grant is OFF by default
  (`CX_SANDBOX_CREDIT_USD=0`); signups are now capped per-IP-per-day
  (`signupsPerIPPerDay`, control/api.go). Decide the grant policy before turning it
  on for the public site.
- **Multi-Mac field test** — cross-machine within-class byte/cosine agreement on
  real heterogeneous Apple hardware; only two physical boxes prove it.
- **First real buyer + first real payout** — the rails are built and (after §1)
  live; a paying customer is the last external step.
