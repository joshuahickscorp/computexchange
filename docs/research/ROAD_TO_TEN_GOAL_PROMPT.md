# Goal prompt — the iterative road to 10/10 on the speed / substrate-routing frontier

> **Paste everything below the line into a fresh session in this repo.** It is
> self-contained: it carries the mission, a measurable rubric, the iterative loop, a way
> to TEST that the loop is actually working (not vibes), the backlog sequenced toward 10,
> and the discipline proven over 91 CREED climbs. It reproduces the
> scope → parallel-waves → real-infra-proof → adversarial-re-audit cycle.
>
> **Read `docs/research/SPEED_LANE_AUDIT_2_AND_HANDOFF.md` FIRST** — it is the state of
> play and it commissions the double audit that Iteration 0 below executes.

---

You are working in the real git tree at `/Users/scammermike/Downloads/computexchange` — a
compute marketplace whose promise is **the easiest AND fastest way to run a batch
inference job, at the literal click of a button.** Your mission is to drive ONE frontier
as close to 10/10 as the keyboard can reach: **speed + substrate-routing** — read a job's
shape, pick the substrate that runs it fastest (the Apple fleet for latency/low-batch, a
lit GPU lane for throughput), quote a guaranteed wall-clock, deliver it in one click, and
prove it — every lane verification-gated.

Audit #1 graded this frontier **3.5/10** (deliberately harsh). Real A100 measurements this
session refuted the old "dozens of Macs beat an A100" headline and handed us the true
routing rule instead. Your job is to run a rigorous iterative loop that moves the grade
honestly, one measured climb at a time, until the only gaps left are owner-gated.

## Read first (context that exists — do not redo it)
- `docs/research/SPEED_LANE_AUDIT_2_AND_HANDOFF.md` — audit #1, the CUDA interpretation,
  the multi-node plan (Track A = 2 RunPods; Track B = 2nd Apple Silicon / the Studio,
  owner-gated), what 10 means, frontiers beyond 10.
- `docs/speed-lane-reports/A100_REFERENCE_MEASURED.md` + `A100_CAPABILITY_SWEEP.md` — the
  real A100 numbers and the routing rule (crossover ~batch 8–64; at batch=1 one A100 ≈ 1–3
  Macs; it dominates only at high batch).
- `docs/internal/CREED_AND_PATH_TO_TEN.md` Implementation Log entries 1–91 — the receipt
  trail and the format every climb follows. Continue numbering from the tail.
- `docs/HAWKING_PORT_PLAN.md`, `docs/VLLM_LANE.md` — the Apple and CUDA lane status.

## The rubric — 8 dimensions, each 0–10, so "progress to 10" is MEASURED not declared
A 10 on this frontier is all of these at once. Grade each from evidence (code + real
artifacts), never from hope:

1. **CUDA throughput lane** — 10 = `VllmRunner` carries real byte-verified traffic against
   a pinned vLLM/TRT-LLM supplier GPU, soak-proven, a CUDA verification class seeded.
   (audit #1: **2** — seam carries zero traffic; our Candle CUDA is ~19× below vLLM.)
2. **Apple per-node speed** — 10 = at/above MLX-class real-traffic throughput, byte-gated.
   (audit #1: **5** — bandwidth-capped; Hawking wired but 0.67× vs our own Candle batched.)
3. **Fan-out scheduler** — 10 = proven on real distinct nodes, wall-clock win measured.
   (audit #1: **4** — planner + endgame racing real, but only on fake-GPU workers on 1 box.)
4. **Substrate-routing intelligence** — 10 = the planner READS job shape and picks
   fleet / GPU-lane / recommend, provably optimal per the measured curve.
   (audit #1: **2** — the routing rule is KNOWN; nothing in code acts on it.)
5. **Wall-clock SLA + one-click UX** — 10 = a guaranteed completion time backed by REAL
   measured competition, an honest "we ran it on X because Y", one-click submit→deliver.
   (audit #1: **3** — SLA mechanism built but backed by the REFUTED model; no buyer path.)
6. **Multi-node proof on real hardware** — 10 = both Track A (CUDA, 2 pods) and Track B
   (Apple, ≥2 Macs) measured. (audit #1: **1** — nothing on real distinct nodes.)
7. **Measurement / methodology honesty** — 10 = every claim reproducible by a hostile third
   party. (audit #1: **8** — the one thing near frontier.)
8. **Verification across substrates** — 10 = every lane (candle/hawking/vllm) honeypot +
   golden-seeded and redundancy-classed. (audit #1: **6** — CUDA class modeled, soak unrun.)

The frontier grade is the honest weighted read of these, weighted toward the PRODUCT rows
(1, 4, 5) — the marketplace is judged by whether a buyer can click once and get the
optimal result, not by how clever the internals are.

## The iterative LOOP — scaffold this, run it, and TEST that it's working

**Iteration 0 (once): the SECOND audit.** Execute the mandate in
`SPEED_LANE_AUDIT_2_AND_HANDOFF.md` Part 6 — re-grade all 8 dimensions yourself from the
evidence (do NOT trust the 3.5), find what audit #1 missed, and adversarially verify the
one thing that would embarrass us: **that nowhere in the quote/site/product path could a
real buyer be promised "beat an A100" or a wall-clock backed by the refuted throughput
model.** If they can, that is a P0 finding to fix first, not a grade. Record audit #2 as a
dated section in the audit doc. This is the double audit the owner wants.

**Then repeat this cycle until the only remaining gaps are owner-gated:**
1. **GRADE** — current 8-dimension scores, each with a one-line receipt (a file:line or an
   artifact). No score without evidence.
2. **PICK** — the single highest-leverage move: `max(grade_gap × product_impact)` among the
   *unblocked* gaps. Prefer a product row (1/4/5) over an internals row when close.
3. **SCOPE** — classify the picked work into DISJOINT-FILE bundles; flag hot files
   (`agent/src/runners.rs`, `control/planner.go`, `control/api.go`, `control/quote.go`,
   `control/scheduler.go`); sequence bundles that share a hot file into one agent.
4. **BUILD** — fan out parallel implementation waves (the Workflow tool, opt-in
   multi-agent), one agent per disjoint bundle, each carrying the full discipline below.
5. **RE-VERIFY (yourself, don't trust the agents)** — re-run the real gates on REAL infra
   (Postgres+MinIO via `prove-local.sh`; real Metal for Apple; the 2 RunPods for CUDA when
   available). **Measure the WIN** — a real before/after wall-clock or tok/s, committed
   under `docs/`. Fix any regression / new warning yourself. A climb with no real measured
   artifact does NOT count — surface the blocker instead.
6. **LOG** — one CREED Implementation Log entry per proven climb, format of entries 86–91,
   with the proof artifact named. Do NOT self-bump the rubric grades.
7. **RE-AUDIT** — grades move ONLY through an adversarial re-audit pass (a fresh critical
   agent re-grading from evidence), never by self-report from the agent that did the work.
8. **REPEAT.**

### How to TEST the loop is real, not theater (run this check every 2–3 iterations)
The process is only trustworthy if it is falsifiable and self-correcting. Assert all of:
- **Every climb this cycle has a real measured artifact** (a committed number on real
  hardware), not a diff or a claim. If not → the climb is not a climb; revert the grade.
- **Grades moved only via re-audit**, never self-report. If a grade rose without a re-audit
  pass, it's invalid.
- **No stale refuted claim survives** — grep the tree each cycle for `2345`, `18 Macs`,
  "beat an A100", and any wall-clock guarantee not backed by a real measurement. A hit is a
  regression to fix, not to explain.
- **The PRODUCT advanced, not just internals** — at least every other iteration must move a
  product row (CUDA lane lit / routing wired / one-click UX), or the loop is polishing the
  moat while the button still doesn't exist (audit #1's central critique).
- **The blocker ledger is honest** — anything you couldn't prove is named with the precise
  reason (owner-gated: real buyers, real money, the Studio, a GPU supplier; or a real
  technical wall), never quietly dropped.
If any assertion fails, STOP building and fix the process before the next climb.

### Exit condition
Stop when every remaining gap is owner-gated and the honest ledger says so: real paying
buyers, real settled money, the Studio (Track B Apple multi-node), and a real GPU supplier
online (Track A at production scale). Hand off with the ledger and the current honest grade
— which will be "as close to 10 as the keyboard reaches," with the last mile being the
launch, not more code.

## The backlog, sequenced toward 10 (the weak rows first, product-weighted)
1. **Purge the refuted claims + de-risk the SLA (rubric #5, #7 — do this FIRST, it's a
   safety issue).** Grep out every live use of 2345 / "18 Macs" / "beat an A100"; make the
   SLA quote provably incapable of guaranteeing a wall-clock it can't back with a real
   measurement; add the honest "we're routing this to <substrate> because <reason>" to the
   quote. `control/quote.go`, `control/api.go`, `web/`.
2. **Wire substrate-routing into the planner (rubric #4 — the highest product leverage).**
   `control/planner.go` reads job shape (prompt count, model, deadline, latency-sensitivity)
   and returns a substrate decision using the measured routing rule (fleet for low-batch,
   GPU-lane for high-batch, crossover ~batch 8–64), with the reason surfaced to the quote.
   Prove it on real infra with synthetic jobs across the crossover.
3. **Light the CUDA lane by BROKERING vLLM (rubric #1, #8).** Wire `VllmRunner` to carry
   real traffic against a pinned vLLM server; run `scripts/runpod-vllm-soak.sh`; seed the
   CUDA `(nvidia_*, vllm, build_hash)` verification class (the Apple-hawking seeding in
   entry 89 is the template). On CUDA we broker, we do not rebuild the kernel.
4. **Prove the fan-out scheduler on real distinct nodes — Track A, 2 RunPods (rubric #3,
   #6).** Ready to fire when the RunPod key is available: open 2 MODEST GPUs (not 2×A100 —
   too fast to show the scheduling effect), register both as real vLLM workers, submit one
   batch, prove the planner splits across two real machines and endgame-races across them,
   measured. Reuse `scripts/runpod-all-cuda.sh`'s money-safety (watchdog + teardown).
5. **The one-click buyer path (rubric #5).** submit → auto-route → guaranteed quote →
   deliver → receipt proving the SLA was met and it beat the DIY alternative on the axis
   that mattered.
6. **Track B — Apple multi-node (rubric #6), OWNER-GATED on the Studio.** When the 2nd
   Apple Silicon device arrives: both Macs as workers, same batch, planner ON vs OFF,
   measured. Backs the re-scoped thesis (fleet wins on latency/low-batch).

## The discipline (non-negotiable — this is why the work is trustworthy)
- Prove everything against REAL infrastructure (real Postgres+MinIO per task, real Metal on
  the M3 Pro, real vLLM on the RunPod GPUs, real HTTP). NEVER assert from a diff.
- Measure the WIN, not just correctness — a real before/after number on real hardware,
  committed under `docs/`. Label modeled/simulated numbers as modeled, always.
- A fully-proven subset beats a rushed whole. Name the precise remaining blocker honestly.
- Determinism is sacred on the Apple lane (the byte-exact gates in `runners.rs` /
  `quantized_llama_batched.rs` must stay green); CUDA is a tolerant class by construction.
- Baselines: Rust `cargo build`/`clippy` clean on `--features metal` AND
  `--no-default-features` at exactly the 4 hardware.rs doc warnings; Go `build`/`vet` clean,
  `gofmt -l control/ | grep -v webauthn.go` empty. More than baseline = yours to fix.
- Hot files sequenced into single agents, never parallel. Distinct PG/MinIO ports per Go
  bundle. Clean up every process/temp dir. Leave the tree building.

## Standing constraints
- **Never attribute AI in git** — no `Co-Authored-By`, no "Generated with" footer, ever.
- **Commit/deploy only when the owner explicitly asks.**
- **Do NOT self-bump rubric grades** — they move only via the adversarial re-audit.
- Keep the owner in the loop each turn: what climbed with its real measured number, what you
  re-verified yourself, what deferred and why, and the current honest grade.

## Deliverable each turn
A tight update: the climb(s) that landed with their real measured wins, the process-health
check result, the updated 8-dimension grade (moved only via re-audit), and the honest
blocker ledger. When the code-doable frontier is swept, state the honest distance to 10 and
name exactly which owner actions (buyers, money, the Studio, a GPU supplier) close the rest.
