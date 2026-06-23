# ComputeExchangeAgent — macOS menu-bar supplier app (SCAFFOLD)

A SwiftUI `MenuBarExtra` app that is the operator's face for the Rust `cx-agent`
supplier binary: it shows live agent status (running/idle, current job, today's
earnings, thermal state, model-cache size), exposes the operator toggles
(active / quiet-hours / power-only / min-payout), launches/stops the agent, and
opens the data dir.

## What is done vs scaffold (honest line)

**Done / verified here**
- All four Swift sources **type-check cleanly** against the macOS 13 SDK
  (`swiftc -typecheck -sdk "$(xcrun --show-sdk-path)" -target arm64-apple-macosx13.0 *.swift`
  → exit 0). The `MenuBarExtra` shell, the `Process`-based agent launch/stop, the
  `status.json` decode, and all SwiftUI views are real, correct API usage — not
  pseudocode.
- The **status-file contract** is fully specified (see `StatusModel.swift`): the
  schema, fields, and staleness rule the app expects the agent to expose at
  `~/.compute-exchange/status.json`.
- The **Rust agent now writes that file** (`agent/src/status.rs`): atomically
  (temp + rename), on registration, every heartbeat, and each task transition,
  with live telemetry + earnings. Proven by `make prove-local`'s `status-file`
  check (a real run produces a valid, fresh document).
- Honest degradation: a missing/unreadable/stale status file renders an explicit
  message and `.offline` state — never a fabricated "running" (BLACKHOLE).

**Scaffold / not done here**
- There is **no Xcode project (`.xcodeproj`) or `Package.swift`** checked in; a
  menu-bar `.app` bundle needs an `Info.plist` with `LSUIElement = true` and an
  app target. Creating that bundle, then **code-signing + notarizing it, requires
  an Apple Developer account and Xcode — these are EXTERNAL and cannot be done on
  this machine.** (Type-checking the sources can; producing a signed, notarized,
  distributable `.app` cannot.)
- Prefs are written to a `agent.prefs.toml` **sidecar**, not merged into the
  canonical `agent.toml`, because the app deliberately does not own the agent's
  config format (it would need a TOML round-trip to preserve `control_url` /
  `worker_token` / `supplier_id`).

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

## Building (requires Xcode — local) and signing/notarizing (requires an Apple Developer account — EXTERNAL)

These are the real release steps. The first two need only Xcode (local); the
signing/notarization steps need a paid Apple Developer ID and are **external to
this environment**.

```bash
# 0. Type-check the sources as-is (works here, no project needed):
swiftc -typecheck -sdk "$(xcrun --show-sdk-path)" -target arm64-apple-macosx13.0 \
    ComputeExchangeAgent/*.swift

# 1. Create the app target (one-time): an Xcode macOS App project named
#    "ComputeExchangeAgent", add the four .swift files, set in Info.plist:
#       LSUIElement = YES        (agent/menu-bar app: no Dock icon)
#       LSMinimumSystemVersion = 13.0
#    and copy the built `cx-agent` Rust binary into the target as a bundled
#    resource (Copy Files build phase → Resources), so AgentPaths.bundledBinary
#    resolves at runtime.

# 2. Build a Release archive (Xcode required, local):
xcodebuild -project ComputeExchangeAgent.xcodeproj \
           -scheme ComputeExchangeAgent -configuration Release \
           -archivePath build/ComputeExchangeAgent.xcarchive archive

# 3. Code-sign with Hardened Runtime  (EXTERNAL: needs your Developer ID cert).
#    Sign the bundled cx-agent first (inside-out), then the app:
codesign --force --options runtime --timestamp \
    --sign "Developer ID Application: YOUR NAME (TEAMID)" \
    build/ComputeExchangeAgent.xcarchive/Products/Applications/ComputeExchangeAgent.app/Contents/Resources/cx-agent
codesign --force --options runtime --timestamp --deep \
    --sign "Developer ID Application: YOUR NAME (TEAMID)" \
    build/ComputeExchangeAgent.xcarchive/Products/Applications/ComputeExchangeAgent.app

# 4. Notarize  (EXTERNAL: needs an App Store Connect API key / Apple ID).
ditto -c -k --keepParent \
    build/ComputeExchangeAgent.xcarchive/Products/Applications/ComputeExchangeAgent.app \
    build/ComputeExchangeAgent.zip
xcrun notarytool submit build/ComputeExchangeAgent.zip \
    --keychain-profile "cx-notary" --wait

# 5. Staple the notarization ticket (EXTERNAL), then verify (local):
xcrun stapler staple \
    build/ComputeExchangeAgent.xcarchive/Products/Applications/ComputeExchangeAgent.app
spctl --assess --type execute --verbose \
    build/ComputeExchangeAgent.xcarchive/Products/Applications/ComputeExchangeAgent.app
```

## Entitlements review (also external — needs the signing step)

Per the action plan §K, the notarized app should run with App Sandbox and deny
mic/camera/location. The `cx-agent` child it launches is the trusted-publisher
appliance that does the inference; the menu-bar app only reads status, writes
prefs, and launches the binary. Adding the `.entitlements` file and reconciling
it with the App-Sandbox network access the agent needs is part of the signing
work above, which requires the Developer identity this machine does not have.
