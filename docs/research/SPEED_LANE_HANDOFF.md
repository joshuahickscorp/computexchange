# Handoff → next chat: read this FIRST, then plan, then execute

> **SUPERSEDED 2026-07-06.** The "beat an A100 on wall-clock" thesis this handoff carries was
> REFUTED by real measurement: a real A100-SXM4-80GB under vLLM serves 44,269 tok/s (~19× the
> Candle-bench figure used here); honest break-even is ~318 M3-Pro-class nodes, not ~18. The
> salvaged, honest routing rule and current state of play: `docs/speed-lane-reports/A100_REFERENCE_MEASURED.md`,
> `A100_CAPABILITY_SWEEP.md`, `docs/research/SPEED_LANE_AUDIT_2_AND_HANDOFF.md`. Kept unedited
> below for the receipt trail.

*Written 2026-07-06 at the end of a very long build session. This is the entry point for the
next session. Its job: hand you the full state, tell you to keep PLANNING and sharpen the goal
prompt before you run it, then keep working. Order of operations for you, next session:*

1. **Read this handoff** (5 min) — the state of play + what's decided vs open.
2. **Read the three speed-lane docs** it points to.
3. **Keep planning:** pressure-test the plan, then AMELIORATE `SPEED_LANE_GOAL_PROMPT.md` if you
   find gaps (it was written fast at session-end — improve it, don't just trust it).
4. **Then execute** the goal prompt's iterative loop. Keep working through the speed lane.

---

## 30-second state of the whole project

computexchange is a compute marketplace (LLM inference jobs → a heterogeneous fleet of
strangers' Macs + GPUs). Over this session it went from "a well-built demo, self-graded ~5/10,
zero real exposure" to **the entire keyboard-reachable engineering frontier proven and SHIPPED
TO LIVE PRODUCTION**:
- **85 verified climbs** logged in `docs/internal/CREED_AND_PATH_TO_TEN.md` (Implementation Log
  entries 1–85), each with a real proof artifact, proven against real Postgres/MinIO/Metal.
- **Deployed to live prod** (`computexchange.net`, DO droplet, Stripe `sk_live`) — health-gated,
  backup + rollback image taken, telemetry tables partitioned live, new endpoints serving. The
  site held HTTP 200 throughout.
- The prior "audit → hardening" arc is **done**; what remained was human-gated (real users/money,
  RunPod GPU, Apple notarization credential, monitoring resize).
- **New frontier chosen by the owner: the SPEED LANE** — make this the fastest way to run a batch
  inference job. Win on WALL-CLOCK time, not cost. That's what the next loop is about.

## The speed-lane thesis (the owner's words, distilled)

> Quantization/compression only saves buyers MONEY. We need to save them TIME. A buyer should get
> their result back sooner than if they'd rented a single A100/H100 — because we fan their batch
> across many machines in parallel. "You never know what you'll get" (one node, or split across
> 200) becomes "you always get it faster, and we tell you exactly when."

## Where we graded (read the full docs, but here's the crux)

- `docs/research/SPEED_LANE_RESEARCH.md` — cited frontier report. **Key finding:** model-parallel
  distributed inference over WAN is killed by per-token network latency (Petals), BUT
  **data-parallel batch fan-out** (split independent prompts across nodes, each runs the whole
  model) has zero per-token network cost — and that's exactly our job shape. Math: **~17 Macs
  break even with an A100, ~50 Macs ≈ 3×**. Speculative decoding = the big per-node latency win
  (lossless, ~1.6–2× at batch-1, fades at high batch). The scheduler is as much of the latency as
  the kernel (vLLM: 62% host overhead).
- `docs/research/SPEED_LANE_GRADING.md` — two tiers. **Tier 1 (per-node vs frontier): ~3.75/10**
  — strong measurement/host-path, near-zero on decode acceleration (speculative decoding 0,
  flash/paged 2–3, continuous-batch DISPATCH 3 though 90% built). **Tier 2 (the marketplace moat:
  distributed fan-out + wall-clock SLA): ~1.8/10 — barely built, and it's the only thing that
  beats an A100 AND can't be copied.** The moat is wide open.
- `docs/research/SPEED_LANE_CURRENT_STATE.md` — our real measured numbers (M3 Pro 1B: 1.34× real
  / 1.67× best @ batch32; A100 spike 9.56× @ batch64) and what's built vs not.
- `docs/research/SPEED_LANE_GOAL_PROMPT.md` — the execution prompt (below, you'll refine it).

## Keep PLANNING before you execute — pressure-test these

The goal prompt was written fast at session-end. Before running it, sharpen it. Specifically
interrogate:

1. **Is the build ORDER right?** The prompt sequences: (1) wire continuous-batch dispatch →
   (2) speed-optimal fan-out scheduler [the moat] → (3) per-node speculative decoding →
   (4) wall-clock SLA. Challenge it: should the FAN-OUT SCHEDULER (Tier 2 A — the actual
   thesis-winning moat) come *first*, since it's the demonstrable "beat an A100" story and mostly
   Go/scheduling work you can prove locally by simulating N nodes? Or does continuous-batch
   dispatch come first because it's 90% done and de-risks the per-node rate the fan-out math
   depends on? Decide deliberately.
2. **The moat's proof needs multiple real nodes** to fully show "beats an A100." You have ONE M3
   Pro. Plan the split: prove the SCHEDULING LOGIC locally (simulate N nodes with real measured
   per-node rates; prove node-speed-weighted shard sizing + straggler hedging + optimal-N math
   minimize modeled wall-clock; unit-test it) and scope the real multi-node wall-clock run as an
   owner step — same as the RunPod soak pattern. Make sure the goal prompt says this crisply.
3. **Speculative decoding is determinism-sensitive.** It's "lossless" but changes the token path;
   it must pass a real byte/semantic-equivalence gate on real Metal, and it must not weaken the
   existing gates. Confirm the goal prompt makes that non-negotiable (it does — verify it's
   strong enough).
4. **Scope for a fresh classify pass.** The grading is the backlog, but re-scan the code for
   speed items it missed (e.g. transfer/serialization overhead in the split path, cold-load on
   the first shard, prefetch/warm-routing gaps). A quick 32-facet-style scope over the speed lane
   specifically will surface bundles cleanly.
5. **Hot-file contention.** `agent/src/runners.rs`, `quantized_llama_batched.rs`,
   `control/scheduler.go`, `control/api.go` are touched by multiple speed items — sequence them
   into single agents, never parallel (last session hit real merge churn when this slipped).

If any of the above changes the plan, EDIT `SPEED_LANE_GOAL_PROMPT.md` before you run it.

## The methodology to reproduce (this is why it's trustworthy)

Proven over 85 climbs + a live deploy this session:
- **Scope-classify** the backlog into disjoint-file bundles (done / code-doable / blocked).
- **Fan out parallel implementation waves** via the Workflow tool (opt-in multi-agent) — one
  agent per disjoint-file bundle, hot files sequenced.
- **Independently RE-VERIFY the risky claims yourself** after each wave — re-run determinism gates
  on real Metal, full integration suite on a fresh stack, fix regressions/warnings yourself,
  check CREED numbering, clean up orphaned processes. Don't trust sub-agent reports blind.
- **Prove against REAL infra** (real Postgres/MinIO per task, real Metal on the M3 Pro, real HTTP)
  — never a diff. **Measure the WIN** (real before/after tok/s or wall-clock, committed under
  `docs/`), not just correctness.
- **A fully-proven subset beats a rushed whole** — land the largest proven increment, name the
  precise remaining blocker honestly, never claim a speedup you didn't measure.
- **Log each climb** in the CREED Implementation Log (continue from entry 85). **Do NOT self-bump
  the scorecard grades** (Commitments 1 & 2 — grades move only via the adversarial re-audit).

## Critical context easy to lose

- **DO prod IS reachable**: `ssh -i ~/.ssh/tailor_droplet root@192.241.134.31` over IPv4 (NOT the
  default key, NOT IPv6). The prior session deployed via rsync of `control/` + `db/schema.sql` +
  `monitoring/alerts.yml` (never `.env`), then `bash scripts/deploy.sh` on the droplet
  (health-gated). A `rollback-latest` image + a `/root/cx-predeploy-*.dump` backup exist. Treat
  any prod action as hard-to-reverse: survey read-only, back up, confirm the SPECIFIC action.
- **The determinism gates** (must always pass on real Metal, `cargo test --release --features
  metal -- --ignored <name>`): `hawking_real_gguf_decode_matches_serial_and_is_coherent`,
  `batch_padded_bucket_equals_serial_mixed_lengths`,
  `batch_active_shrink_equals_serial_mixed_lengths`, `batch_shared_prefix_equals_serial`,
  `batch_width_split_matches_unsplit_batch`. `runners.rs` tests share `METAL_HARDWARE_TEST_LOCK`.
- **Baselines:** Rust build/clippy clean both `--features metal` and `--no-default-features` at
  exactly 4 hardware.rs doc warnings; Go build/vet clean, `gofmt -l control/ | grep -v
  webauthn.go` empty. More than baseline = yours to fix.
- **Local test infra recipe:** `initdb -U cx --auth=trust -E UTF8` with `LC_ALL=C` (dodges the
  multithreaded-postmaster bug), MinIO + `mc mb`, distinct ports per bundle.
- **Hawking dispatch wiring** (goal-prompt item 1): `HawkingRunner::run` is proven correct + churn-
  safe but returns an honest boundary; wiring it needs `ModelPool`/`pool.rs`/`main.rs`. See
  `docs/HAWKING_PORT_PLAN.md` (Week 6) + CREED entries 82/84.

## Open decisions the owner left (surface these; don't assume)

1. **Commit?** ~107 working-tree files are proven and now LIVE IN PROD, but nothing is committed
   to git (owner's standing convention: commit only when asked, and NEVER attribute AI in git —
   no `Co-Authored-By`, no "Generated with" footers, ever). The next session should ask whether to
   commit the proven-in-prod work before piling more on, so the tree doesn't drift further from
   git.
2. **Re-verify the research?** The deep-research verify pass was cut short by a transient API
   outage, so some frontier numbers (FA3, EAGLE, DistServe, Splitwise, Petals) are `[SOURCED]`
   (cited, real quotes) not `[VERIFIED]` (adversarially reconfirmed). Optional: re-run just the
   verification pass to promote them. Not blocking — the strategic conclusions hold regardless.
3. **Human-gated items still open** (only the owner can unblock): the RunPod CUDA soak (their $ +
   key; script built), Apple notarization (their app-specific password → one `xcrun notarytool
   store-credentials cx-notary` command), the monitoring stack (droplet resize + Slack/PagerDuty
   URLs), and the first real supplier+buyer. These gate the *business*, not the speed engineering.

## Standing constraints (always)

- **Never attribute AI in git** — no `Co-Authored-By: Claude`, no "Generated with …" footer, ever.
- **Commit / deploy only when the owner explicitly asks.**
- Keep the owner in the loop each turn: what landed with its real measured speedup, what you
  re-verified yourself, what deferred and why.

---

**Your first move, next session:** read the three speed-lane docs, do the planning pressure-test
above, sharpen `SPEED_LANE_GOAL_PROMPT.md` if warranted, then run the loop — starting by deciding
(deliberately) whether the fan-out scheduler moat or the continuous-batch dispatch wiring goes
first. Keep working. The win condition is a real, measured demonstration that a fan-out across the
fleet beats a single A100 on a real batch's wall-clock — and a quote that guarantees the buyer
exactly when their job will finish.
