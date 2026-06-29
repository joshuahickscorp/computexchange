# ComputeExchangeAgent · macOS menu-bar supplier app

A SwiftUI `MenuBarExtra` app that is the operator's face for the Rust `cx-agent`
supplier binary: it shows live agent status (running/idle, current job, today's
earnings, thermal state, model-cache size), exposes the operator toggles
(active / quiet-hours / power-only / min-payout), launches/stops the agent, and
opens the data dir.

It also ships:

- **Sparkle auto-update** (SwiftPM dependency, wired into the app lifecycle). The
  app checks an appcast over HTTPS and installs only EdDSA-signed updates.
- **First-run consent gate** · an onboarding sheet stating what runs on the
  machine, the resource limits, quiet hours, and the 90/10 split. The agent
  cannot launch (cannot earn) until consent is given; the gate is enforced in
  `AgentController.startAgent()`, not just in the UI.
- **Trust panel** · an earnings sparkline (the app's own observed `lifetime_usd`
  series), payout proof (last/next payout + payout-readiness), and a verification
  badge (honeypot pass/fail). Every element is sourced from `status.json` and
  shows an explicit "not available" state when the agent has not reported the
  data · it never shows a badge or figure it cannot back.

The local SwiftPM package builds with `swift build --package-path macapp`.
Producing a SIGNED, NOTARIZED `.app` requires an Apple Developer ID and is the
external release step driven by `macapp/sign-notarize.sh` · see below.

## What is built here vs what the owner must supply (honest line)

**Built and verified here**
- The Swift sources **compile and link** via SwiftPM, including the Sparkle 2
  dependency: `swift build --package-path macapp` → `Build complete!`. The
  `MenuBarExtra` shell, the Sparkle updater wiring, the consent gate, the trust
  panel, the `Process`-based agent launch/stop, and the `status.json` decode are
  all real, correct API usage · not pseudocode.
- The **status-file contract** is fully specified (see `StatusModel.swift`): the
  schema, fields, and staleness rule the app expects the agent to expose at
  `~/.compute-exchange/status.json`.
- The **Rust agent writes that file** (`agent/src/status.rs`): atomically
  (temp + rename), on registration, every heartbeat, and each task transition,
  with live telemetry + earnings. Proven by `make prove-local`'s `status-file`
  check.
- **Auto-update**: Sparkle is a real SwiftPM dependency (`Package.swift`), wired
  into the app lifecycle (`UpdaterController.swift`), driven by Info.plist keys
  (`SUFeedURL`, `SUPublicEDKey`, `SUEnableAutomaticChecks`), with a sample
  `appcast.xml` and the EdDSA-signing step folded into `sign-notarize.sh`.
- **Consent gate**: `Consent.swift` + `ConsentView.swift` show the terms before
  any work, persist consent, and HARD-gate `AgentController.startAgent()`.
- **Trust polish**: `TrustPanel.swift` draws the earnings sparkline, payout proof,
  and verification badge entirely from `status.json` data, with honest empty
  states everywhere the agent has not reported a value.
- Honest degradation: a missing/unreadable/stale status file renders an explicit
  message and `.offline` state · never a fabricated "running" (BLACKHOLE).

**Owner must supply (external · needs an Apple Developer account)**
- An **Apple Developer ID** to code-sign + notarize the `.app` (the script
  `macapp/sign-notarize.sh` does the rest; it fails loudly if credentials are
  unset and never fakes a signature). See "Signing and notarization" below for the
  exact environment variables.
- A **Sparkle EdDSA key pair** (generated once) · the PRIVATE key signs each
  release, the PUBLIC key goes in Info.plist. See "Auto-update (Sparkle)" below.
- A **release host** for the appcast + zips at your `SUFeedURL`.
- Assembling the `.app` bundle itself (Info.plist `LSUIElement = true`, the four+
  Swift sources, and the built `cx-agent` binary copied into
  `Contents/Resources/`) · an Xcode app target or a manual bundle. Prefs are
  written to an `agent.prefs.toml` sidecar (the app deliberately does not own the
  canonical `agent.toml` format).

## Auto-update (Sparkle)

Sparkle is wired in `UpdaterController.swift` via `SPUStandardUpdaterController`
(`startingUpdater: true`), and configured by Info.plist keys. The menu's
"Check for updates" item triggers a user-driven check; background checks run on
the `SUScheduledCheckInterval` (24h default).

**One-time owner step · generate the EdDSA key pair:**

```bash
# Sparkle ships generate_keys in its release. With the SPM checkout it lives under
# the resolved Sparkle artifact; or download the Sparkle release tarball.
./bin/generate_keys                 # creates a private key in your login keychain
./bin/generate_keys -p              # prints the PUBLIC key to paste into Info.plist
```

Paste the printed public key into `Info.plist` → `SUPublicEDKey` (it currently
holds `REPLACE_WITH_YOUR_SPARKLE_ED25519_PUBLIC_KEY`). Sparkle refuses to install
any update whose appcast `sparkle:edSignature` is not signed by the matching
private key · so a placeholder key means NO update can install (fail-closed,
never a silent unsigned install).

**Per release:** after signing + notarizing, sign the zip and update `appcast.xml`
with the new version, enclosure `url`, `length` (byte size of the zip), and the
`sparkle:edSignature`. `sign-notarize.sh` prints those for you when `SPARKLE_KEY`
points at Sparkle's `sign_update` tool. Host the appcast + zips at your
`SUFeedURL`.

## Signing and notarization (`macapp/sign-notarize.sh`)

The script codesigns (Developer ID + Hardened Runtime, inside-out: the bundled
`cx-agent` first, then the `.app` with `--deep` for Sparkle's nested helpers),
notarizes via `notarytool`, staples the ticket, and re-zips the stapled bundle as
the Sparkle artifact. It reads all credentials from the environment and **fails
loudly with the exact missing variable** if any is unset · it never pretends.

Exact environment variables the owner must provide:

| Variable | What it is |
| --- | --- |
| `DEVELOPER_ID` | Your codesigning identity, e.g. `"Developer ID Application: Your Name (TEAMID)"`. Must be in your login keychain (issued by the Apple Developer Program). |
| `TEAM_ID` | Your 10-char Apple Developer Team ID, e.g. `AB12CD34EF`. |
| `NOTARY_PROFILE` | *(option A)* Name of a stored notarytool keychain profile created once with `xcrun notarytool store-credentials`. |
| `APPLE_ID` + `APPLE_PASSWORD` | *(option B, instead of `NOTARY_PROFILE`)* Your Apple ID email and an **app-specific password** (appleid.apple.com → Security). |
| `APP_PATH` | *(optional)* Path to the built `.app` (default `build/ComputeExchangeAgent.app`). |
| `SPARKLE_KEY` | *(optional)* Path to Sparkle's `sign_update` tool · when set, the script EdDSA-signs the zip and prints the appcast `edSignature` + `length`. |

```bash
# Example (keychain-profile route):
export DEVELOPER_ID="Developer ID Application: Your Name (AB12CD34EF)"
export TEAM_ID="AB12CD34EF"
export NOTARY_PROFILE="cx-notary"
export SPARKLE_KEY="/path/to/sparkle/bin/sign_update"
APP_PATH=build/ComputeExchangeAgent.app macapp/sign-notarize.sh
```

## The status-file contract (`~/.compute-exchange/status.json`)

The app polls this file every 3s. The agent should write it atomically (write to
a temp file, then rename) so the reader never sees a half-written file.

```json
{
  "schema_version": 1,
  "state": "running",
  "agent_version": "0.1.0",
  "worker_id": "5d1c…",
  "current_job": { "job_id": "…", "job_type": "embed", "started_at": 1718900000 },
  "today_earnings_usd": 0.42,
  "balance_usd": 12.50,
  "lifetime_usd": 130.00,
  "thermal_state": "nominal",
  "gpu_temp_c": 61.0,
  "cpu_pct": 18.0,
  "model_cache_bytes": 4831838208,
  "active": true,
  "eligible_now": true,
  "last_heartbeat": 1718900123,
  "last_error": null
}
```

`state` ∈ `running | idle | paused | offline`; `thermal_state` ∈
`nominal | fair | serious | critical`. A `last_heartbeat` older than 90s is
treated as offline regardless of `state`.

### Optional trust fields (back the trust panel honestly)

The trust panel reads these OPTIONAL fields when the agent provides them; each is
absent until the agent has real data, and the app shows an explicit "not
available" state rather than inventing one:

```json
{
  "payouts_configured": true,        // control plane has a Stripe key (GET /v1/worker/connect/status)
  "payouts_connected": true,         // this supplier linked a payout account
  "payouts_enabled": true,           // the account can actually receive payouts
  "last_payout_usd": 42.50,          // amount of the most recent released payout
  "last_payout_at": 1718800000,      // unix secs of that payout
  "next_payout_at": 1719400000,      // unix secs of the next scheduled payout, if known
  "honeypots_passed": 12,            // honeypot checks this worker passed
  "honeypots_failed": 0,             // honeypot checks this worker failed
  "verification_label": "verified"   // "verified" | "honeypot-checked" | "unverified"
}
```

The payout fields mirror the control plane's `GET /v1/worker/connect/status`
(`configured` / `connected` / `payouts_enabled`); the verification fields mirror
the `Verification` aggregate the control plane derives from its append-only
`verification_events` log. The agent (which holds the worker token) is expected to
fetch these and fold them into `status.json`; the menu-bar app only reads the
file. The verification badge is GREEN only when checks ran, none failed, and the
label is a real `verified` / `honeypot-checked` · never a badge the counts cannot
back.

## Release flow (build → bundle → sign/notarize)

```bash
# 1. Build the SwiftPM executable (Release):
swift build --package-path macapp -c release

# 2. Assemble the .app bundle (one-time tooling, owner's choice of Xcode app
#    target or a manual bundle). It must contain:
#      Contents/MacOS/ComputeExchangeAgent      (the built executable)
#      Contents/Info.plist                       (LSUIElement = true; Sparkle keys)
#      Contents/Resources/cx-agent               (the built Rust agent binary, so
#                                                 AgentPaths.bundledBinary resolves)
#      Contents/Frameworks/Sparkle.framework     (from the SwiftPM Sparkle artifact)

# 3. Sign + notarize + staple (see "Signing and notarization" above for env vars):
APP_PATH=build/ComputeExchangeAgent.app macapp/sign-notarize.sh
```

## Entitlements (`ComputeExchangeAgent.entitlements`)

The notarized app runs with App Sandbox and denies mic/camera/location; it keeps
only `network.client` (the agent's outbound connections). `sign-notarize.sh`
applies this file at the codesign step. The `cx-agent` child it launches is the
trusted-publisher appliance that does the inference; the menu-bar app only reads
status, writes prefs, and launches the binary.
