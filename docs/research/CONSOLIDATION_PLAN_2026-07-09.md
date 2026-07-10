# Consolidation Plan — One Owned Speculation Engine (2026-07-09)

Status: LIVE. This is the single consolidation plan the current agent wave executes.
It supersedes the scattered 07-08/07-09 goal prompts as the operative frame (they remain
valid source material). Author: consolidation audit + owner direction 2026-07-09.

## Why this exists

We have three piles of work that do not yet relate to each other:

1. **Control plane (real, shipped-quality).** Honest substrate routing (fleet vs GPU) grounded
   in the measured A100-is-just-batching finding, wired through quote -> submit -> receipt ->
   timeline, every GPU figure `[MODELED]`. A vLLM byte-stability soak passed on real A100s;
   `litGPULaneWorkers` lights `gpu_lane` the moment a verified vLLM worker registers. Advisory
   only (nothing in dispatch acts on it yet).
2. **Speculative render (real but framed generously).** Low-spp + OIDN + SSIM-gate gives
   14.3372x on an "unusually forgiving" scene and 3.87-7.87x on hard/representative scenes.
   The *parallelism* thesis (sample fan-out) DIED on real hardware (0.24-0.89x, never beat a
   single render). Stripped of framing, the owned mechanism has no measured win over a single
   Cycles render yet — the win is "render fewer samples", which stock Cycles also does.
3. **Token speculative decode (fantastical but synthetic).** 229-315x numbers are a local
   n-gram protocol on degenerate inputs (repeat/prose/json strings). Every *real model-backed*
   vLLM run was a LOSS (0.33-0.68x, non-lossless). No working LLM spec-decode win exists.

These do not compose and do not share a mechanism. This plan makes them ONE thing.

## The honest number model (how the fantastical numbers relate to the real ones)

This is the load-bearing discipline. Read it before quoting any combined number.

1. **Multipliers do NOT multiply.** `render_x * token_x` is meaningless unless both levers run
   INSIDE one delivered workload. The forgiving-scene 14.34x and the synthetic 315x describe
   different, non-composed workloads.
2. **A combined number legitimately exists only two ways:**
   - (a) **End-to-end measurement** of a single delivered job where both levers are active:
     `combined_x = baseline_total_time / spec_total_time`. Nothing else.
   - (b) A **staged table** that lists every measured multiplier, its exact workload, its
     dependency, and every modeled bridge — with NO naive product taken.
3. **Report representative inputs, not cherry-picks.** Token numbers on REAL prompts (currently
   <1x — that is the honest floor to beat). Render numbers on HARD/representative scenes
   (3.9-7.9x), with the forgiving-scene number labeled as a convergence-trivial outlier.
4. **The custom basis is what makes a real combined workload exist.** Example target: an
   animated product/catalog render whose (i) per-frame tile/denoise accept-or-refine decisions
   AND (ii) LLM-driven scene/material/caption generation both flow through the SAME owned
   accept/verify/repair engine -> ONE honest end-to-end multiplier, produced by one engine,
   not stitched from two ledgers.

## The custom basis: the owned SpecEngine

```
SpecUnit -> DraftProducer -> Verifier -> AcceptancePolicy -> RepairPolicy -> SpecReceipt
```

- **Render adapter:** draft = low-spp / tile subset; verify = reference/high-spp SSIM (global +
  worst-tile + p5); accept = tile clears the quality tier; repair = re-render failed tiles.
- **Token adapter:** draft = CX-owned proposer (forked from vLLM/llama.cpp spec-decode kernels
  + acceptance sampling); verify = target-model logits/greedy; accept = matching verified
  prefix; repair = fall back to a target-model step.
- **One SpecReceipt schema** for both: draft_cost, verify_cost, accepted_fraction, repair_cost,
  total_product_time, quality_tier, and `speedup_vs_baseline` where baseline is a REAL
  single-lane run of the same delivered unit. Same fields -> receipts compose by the staged
  table, and only an end-to-end run yields a product.

Operating rule (unchanged from the 07-09 deep plan): own the hot path that matters. Fork narrow
useful pieces of vLLM/Cycles/llama.cpp aggressively; do not rebuild commodity layers (CUDA)
without a measured reason. If a framework becomes the constraint, fork the useful part and drop
the rest.

## Branches (worked in parallel this wave — disjoint write paths)

### A — The SpecEngine substrate (the custom basis)
- **Build:** the modality-general core as real, compiling, unit-tested code — the traits above +
  the unified SpecReceipt schema. Language: Rust (agent-side, Metal+CUDA reach), with a thin
  schema mirror the Go control plane can read (routing already speaks receipts).
- **Writes:** `spec-engine/` (new crate) + `docs/research/SPEC_ENGINE_SUBSTRATE_DESIGN.md`.
- **Gate:** compiles + unit tests pass; a render adapter AND a token adapter can both implement
  the traits; SpecReceipt round-trips to JSON the control plane can ingest.
- **Kill:** if the abstraction forces either lane into a WORSE number than its standalone code,
  narrow the core until it doesn't. The engine must never tax the lanes.

### B — Render lane as a SpecEngine instance + the first honest combined receipt
- **Build:** a render adapter that expresses the Cycles low-spp/tile/SSIM lane through the
  SpecEngine contract; FIX the integrated-benchmark driver's device-selection bug (Blender 4.2
  picked CPU on the B300, burning $0.58 with no receipt); a local dry-run of the end-to-end
  path so the next cloud run produces a real `baseline/spec` number.
- **Writes:** `scripts/spec-lab/` (new adapter + the driver fix) +
  `docs/research/RENDER_SPEC_BRIDGE_DESIGN.md`.
- **Gate:** the integrated benchmark is device-correct (renders on GPU, not CPU fallback) and
  dry-runs locally; the design states the exact hard/representative scene set it will report,
  every row labeled measured/modeled.
- **Kill:** if no honest combined workload beats the best single-lane number, say so plainly and
  keep the lanes separate — a real negative, not a massaged positive.

### C — Fork spec-decode into a CX-owned token lane that WINS on a real model
- **Recon then build:** decide the fork base with evidence — vLLM (heavy, CUDA-driver-fragile,
  we already hit "driver too old") vs llama.cpp / candle (lighter, embeddable, Metal+CUDA,
  already in the agent). Build a CX-owned draft producer + verifier + acceptance sampling that
  is lossless/near-lossless and measured >1x on a REAL model on REAL prompts.
- **Writes:** `token-spec-poc/` (new top-level dir) +
  `docs/research/TOKEN_LANE_FORK_DESIGN.md`. Matches the SpecReceipt schema by contract (not by
  code import) this wave; wiring into `spec-engine/` is a sequenced follow-up.
- **Gate:** a real model + real prompt, lossless output, measured >1x wall-clock on
  fleet-class hardware (or a clear, honest local baseline + the exact fork plan if no local
  model fits), emitting a SpecReceipt-shaped result.
- **Kill:** if forked spec-decode cannot beat 1x losslessly on our hardware, pivot the token
  value to the routing/brokering lane (already real) and park token-decode ownership — with the
  measured reason recorded.

## Sequenced cloud follow-ups (STRICTLY one driver at a time)

The shared `.tracked_pods.json` / `terminate_all_tracked()` mechanism means concurrent pod
drivers kill each other's pods. This wave does DESIGN + LOCAL BUILD ONLY — zero cloud spend.
Cloud experiments run sequentially, after the wave, each money-safe (arm_remote_watchdog +
register_cleanup + verify tracked pods empty before/after):

1. B's integrated benchmark on a real GPU (device bug fixed) -> first honest combined render
   receipt.
2. C's forked token lane on a real fleet-class model -> first honest >1x lossless token receipt.
3. Only after both land: a genuinely nested combined workload (render job whose control/LLM
   steps ALSO run through the token lane) -> the one legitimate end-to-end combined number.

### Step-1 execution log (2026-07-09 evening — REAL runs, all money-safe, zero orphans)

Eight launch attempts produced TWO real end-to-end receipts (both honestly pruned at the
quality gate — see the staged table) and FOUR permanent hardenings, each bought by a distinct
real failure:
- **Silent CPU fallback** (B300, $0.58 prior + prevented since): fail-loud device guard now in
  BOTH runners (`exp_render_stack.py` + `exp_cycles_render_prod.py`), Blackwell rejected.
- **SSH peer reset killed a 49-min 4K render** (render was healthy at 99% GPU util): fixed by
  `runpod.ssh_detached()` — render runs nohup'd ON the pod, driver polls over short
  reconnecting SSH calls; validated live (progress tails streaming, `done rc=0 in 746s`).
- **Phantom-pod false halt** (`POD_NOT_FOUND` on terminate): `terminate()` now treats a
  definitively-absent pod as already-gone (untracks; no halt, no stale ledger entry).
- **H100 sm_90 first-render JIT** (TWO independent H100 SECURE pods failed the GPU probe at
  exactly 300s while every A100 passed in seconds): probe timeout now configurable
  (`gpu_probe_timeout_s`, driver sets 1500s); the next H100 passed and rendered — strong
  (not yet decomposed) support for the "Blender 4.2 tarball lacks sm_90 cubins" hypothesis;
  the driver's old claim that Hopper kernels exist is RETRACTED in its header.
Also: RunPod capacity crunch ~17:30-19:20 (A100/H100/H200 all unreachable across 5 spaced
sweeps) — pure supply, waited out. GPU policy ladder (A100->H100->H200, community+secure per
rung, upgrade-never-downgrade) applied across all 22 drivers (task #20).

**Current status:** the 4K/4096-spp keyframe_every=1 run (the config whose 4K variant cleared
delivery at worst-tile 0.911 / 5.84x on 2026-07-07) is IN FLIGHT on the hardened stack — it is
the live candidate for the first delivery-PASSING integrated receipt. At 1080p/192spp the same
stack lands worst-tile 0.8652 (preview, not delivery) — if 4K also falls short, the honest next
lever is the REPAIR loop (re-render failing tiles at higher spp), i.e. exactly the SpecEngine
accept/repair path, not a silent gate loosening.

## Done, for this wave

- `spec-engine/` compiles + tests (A).
- render and token adapters implement the SAME traits and emit the SAME receipt shape (A+B+C).
- the integrated-benchmark device bug is fixed and the path dry-runs locally (B).
- a forked-token POC is measured locally for an honest baseline, with the fork base chosen on
  evidence (C).
- THIS doc updated with the honest staged-multiplier table + the exact next cloud experiment.

## Live staged-multiplier table (updated by the 2026-07-09 synthesis; no products until earned)

Honest state after Branch A/B/C ran design -> build -> verify. Every number is labeled; NO row is
a product of two lanes' multipliers — invariant #1 held **structurally** in all three codebases
(independently verified: A's `aggregate()` has no lane-multiply path; B's headline is one ratio
`baseline_cost/total_product_time`; C's is one ratio `baseline_s/speculative_s` on the same unit).
The two NEW lanes produced **no >1x wall-clock win** this wave; the combined number is still
genuinely NONE-YET.

| lane | workload | number | label | composes? |
|------|----------|--------|-------|-----------|
| render (representative band) | live L40S, delivery tier (g>=0.98, worst-tile>=0.95), draft 8-32 spp vs 4096 ref: many_glass 3.87x / sphere_bump 4.59x / monkey 6.88x / cube_volume 7.87x | **3.87x - 7.87x** | MEASURED — prior-wave L40S; this wave RE-EXPRESSED through the Branch B render adapter (device-correct), not re-measured | standalone |
| render (forgiving) | scene_world_volume 2spp vs 4096 | 14.3372x | MEASURED — convergence-trivial OUTLIER, labeled, NOT the headline | standalone |
| render (integrated combined) | classroom 3840x2160, 4 frames, `T_ref(4096) / T_stack(draft 512 + adaptive+OIDN+guides+light-tree)` | — | NONE-YET — default job's `rerender` crop is MODELED so the receipt PARKS; this is the next cloud target | target |
| render (integrated, 1080p kf=4, REAL RUN 1) | classroom 1920x1080, 4 frames, ref 1536 / draft 192, keyframe_every=4 (1 anchor + 3 reprojected), A100 SECURE, $0.40 | 8.279x @ global 0.745 / worst-tile 0.164 | MEASURED end-to-end — **self-PRUNED: failed delivery gate** (the known reprojection wall; frames 1-3 collapse 0.27/0.21/0.16) — honest speed at unacceptable quality, correctly rejected | standalone (quality-fail) |
| render (integrated, 1080p kf=1, REAL RUN 2) | classroom 1920x1080, 4 frames, ref 1536 / draft 192, keyframe_every=1 (all-anchor, zero reprojection), H100 SECURE, $0.70, fully hardened stack (detached SSH + 1500s probe headroom) | 2.722x @ global 0.9747 / p5 0.9449 / worst-tile 0.8652 | MEASURED end-to-end, `modeled=false` — **PRUNED at the delivery bar** (needs worst-tile >=0.90; one hard tile in frame 1 at 0.865, trend 0.865->0.910 across frames). Preview-grade honest receipt | standalone (preview tier) |
| **render (integrated, 4K kf=1, REAL RUN 3 — the headline)** | classroom **3840x2160**, 4 frames, ref **4096** / draft **512**, keyframe_every=1 (all-anchor, zero reprojection), A100 SECURE, $2.57, 99-min DETACHED render (`done rc=0 in 5925s` — the exact run length the old SSH path died at 49 min of) | **5.561x @ global 0.9854 / p5 0.9664 / worst-tile 0.9095** (per-frame worst 0.9095/0.9169/0.9275/0.9379) | MEASURED end-to-end, `modeled=false`, token lane lossless riding along. **Harness verdict: prune under the STRICT delivery gate (g>=0.98 ✓ 0.9854, wt>=0.95 ✗ 0.9095)** — but it CLEARS the historically-published 0.95-tier (g>=0.95, wt>=0.90) and **independently REPRODUCES the banked 5.84x@0.958/0.911 product number within 5%** through the full integrated harness on different silicon. The remaining gap to strict delivery is worst-tile 0.91→0.95 — the REPAIR-loop lever | standalone; the strict-delivery pass is the repair-loop target |
| render (integrated, 4K kf=1 + REPAIR, REAL RUN 4 — decisive negative) | same 4K config + `repair_enabled` (two-draft selector, top-12 tiles, 2048spp bordered repairs, feathered composite), H100 SECURE, $3.90, 78-min detached run | 2.703x @ global 0.9855 / worst-tile **0.9095 — UNCHANGED to 4 decimals by repair**; `selector_recall = 0.0` | MEASURED end-to-end, `modeled=false`. **The repair machinery worked perfectly** (12 tiles selected/re-rendered/composited, every second charged: selection 172.6s + repair 253.5s) **but repaired the WRONG tiles**: two-draft divergence finds VARIANCE-limited tiles (pre-repair 0.969-0.980, already fine); the true worst tiles are BIAS-limited (OIDN systematic deviation, invisible to a raw draft; repaired tiles saturate ~0.98 even at 2048spp+OIDN — same denoiser, same bias). SECONDARY finding: the ratio is hardware-relative (H100 renders the REFERENCE proportionally faster: repair-free equivalent ≈4.6x on H100 vs 5.56x on A100). **Two-draft-divergence selector branch: measured, kill-rule applied.** Next candidate selector: CROSS-DENOISER DISAGREEMENT (OIDN vs OptiX on the SAME noisy render — different biases don't cancel; ~zero render cost; validatable for ~$0.75 with one frame + reference) | standalone (honest negative; selector iteration required for strict delivery) |
| token (real model) | candle Llama-3.2-1B GGUF, real prompts (prose/structured), CPU | **1.000x - 1.076x** target-call-reduction, LOSSLESS (`exact=true`) | MEASURED — the honest ~1x FLOOR; wall-clock `speedup_x` is actually <1x (0.02-0.46x) and labeled MODELED | standalone |
| token (call-reduction ceiling) | independent content-dependent oracle sweep, 4320 combos | up to **32.0x** `target_call_reduction_x` | MEASURED call-count CEILING (>=1 by construction) — a target-call count, NOT a wall-clock speedup | does NOT compose as a wall-clock multiplier |
| token (synthetic degenerate) | local n-gram on repeat/prose/json strings | 229-315x (mock repeat 7.03x) | SYNTHETIC — dead as a claim; superseded by the real-model floor above | does NOT compose |
| **combined** | one delivered job, both levers active | — | **NONE-YET** — produced only by the sequenced end-to-end GPU run pinned below | — |

## What the wave built (2026-07-09)

Verify-stage evidence per branch. "Compiles/tests" claims below were independently re-run from
clean by the verifiers; only SOUND rows carry a real build/test win.

### A — SpecEngine substrate (`spec-engine/`): SOUND, gate MET
- **Compiles + tests:** clean build (`cargo clean -p cx-spec-engine` then `cargo build
  --all-targets`) with zero warnings/errors; `cargo test` = **11 passed / 0 failed** (5 lib + 4
  receipt_json + 1 render + 1 token). Reproduced independently.
- **Two real modalities, one engine:** a synth_render adapter (truth=None, whole-unit accept,
  `exact=false`, SSIM score) and a synth_token adapter (truth=Some, partial m-of-k accept with
  `accepted_fraction=16/24` strictly in (0,1), `exact=true`) implement the SAME four traits and
  fold to the SAME `SpecReceipt` via one engine. No lane-multiply code path exists (invariant #1
  structural).
- **Schema:** `spec-engine/src/receipt.rs` is the canonical wire shape — `draft_cost_s /
  verify_cost_s / repair_cost_s / total_product_time_s / baseline_total_time_s` (serde aliases
  only to legacy `*_s`), a REQUIRED `exact` bool, an ENUM `quality_tier` (Fail/Preview/Delivery),
  nullable `speedup_vs_baseline`. JSON round-trips value-exact.
- **Remains:** demo speedups (my run 3356.65x render / 97.78x token; doc 3401.35x / 95.62x) are
  `evidence=synthetic`, non-reproducible run-to-run (constant baseline / `fnv_spin` wall-clock) —
  illustrative ONLY, never quote as performance. `control/spec_receipt.go` does NOT exist (Go
  ingest is proven structurally, not by a real unmarshal). `SpecUnit::modality()` is a required
  trait method with zero call sites (receipt modality is stamped from the pipeline tag).

### B — Render bridge (`scripts/spec-lab/`): CONCERNS, gate PARTIAL, money-safe
- **Compiles + tests:** `py_compile` of all 4 files + both embedded Blender scripts passes;
  **14/14** tests pass (7 adapter + 3 core + 4 integrated); adapter + 4-case driver dry-run
  matrix reproduce exactly; `.tracked_pods.json` = `[]` before and after (no cloud, no spend).
- **Device bug FIXED end-to-end:** the integrated driver actually shells `pod/exp_render_stack.py`
  (not `exp_cycles_render_prod.py`). Fix wired driver `require_gpu:True` -> `CX_REQUIRE_GPU` env
  -> in-Blender `SystemExit(3)` + a functional 64x64@1spp GPU probe (CPU disabled) before the 4K
  reference frame. `DEFAULT_GPU_PLAN` now `A100 80GB PCIe -> H100 80GB HBM3 -> H200` (all <=
  sm_90); `parse_gpu_plan` rejects every Blackwell SKU with a bug-citing `ValueError`;
  `--allow-unsupported-gpu` hatch exists; substring-safe across known-good SKUs.
- **Remains (why CONCERNS, not SOUND):**
  1. **Composition NOT achieved.** The adapter emits `draft_cost/verify_cost/repair_cost/
     total_product_time` (no `_s`), `baseline_cost`, `quality_gate` (not `exact`), and
     `quality_tier` as a free STRING. That JSON will **NOT** deserialize into A's
     `spec-engine::SpecReceipt` (required `*_s` fields absent even via alias; missing required
     `exact`; string `quality_tier` is not a valid enum variant). The wave "Done" bullet "A+B+C
     emit the SAME receipt shape" is met only against the plan TEXT, not on the wire.
  2. **The Blackwell guard rests on ONE unverified Cycles behavior** (that with CPU disabled a
     failed GPU kernel errors rather than silently CPU-falls-back); all four guards key off the
     ENUMERATION label, not the actual trace device, so for enumerate-then-kernel-fail they are
     not independent. The default plan stays <= sm_90 so this path is NOT exercised next run
     (mitigated); it matters only if Blackwell is ever forced.
- **New numbers this wave:** none measured (design + local build only). The 3.87-7.87x band is
  prior-wave L40S, now expressed through the adapter; the integrated combined receipt is NONE-YET.

### C — Token fork (`token-spec-poc/`): SOUND, gate MET (via the honest-baseline clause)
- **Compiles + tests:** `cargo test` = **9/9** (5 unit + 4 property); mock harness reproduces the
  design's acceptance table exactly; candle backend compiles offline; verifier RAN the real
  Llama-3.2-1B GGUF (CPU, offline) -> prose **1.000x**, structured **1.010x**, both `exact:true`.
- **Lossless proven the HARD way:** an INDEPENDENT content-dependent oracle (argmax genuinely
  depends on draft content) over 4320 (unit,drafter,k) combos gave byte-for-byte == plain greedy
  decode on every one, `exact=true` throughout, `max target_call_reduction_x=32.0` (proves deep
  multi-token speculation, not degenerate 1-token rounds). Fork base (candle) chosen on real
  cited evidence (`quantized_llama_batched.rs` forward/forward_padded lines, on-disk GGUFs, the
  repo's measured vLLM-loss ledger).
- **Remains:** NO demonstrated >1x wall-clock win — the deliverable is an honest ~1x lossless
  FLOOR + the exact F1/F2 fork plan; `speedup_x` is a wall-clock LOSS (0.02-0.46x) defused only by
  the MODELED label (consider making it null when `walltime_label==MODELED`). Its receipt matches
  B's `cx_speculative_core.py::to_dict()` (legacy `*_s` + `quality_gate`), NOT A's canonical
  `receipt.rs` (`*_cost_s` + `quality_tier` enum) — closer than B but still needs a field-rename
  map to serde-ingest into `spec-engine/`. A real >1x needs the fork + a draft model + fleet
  hardware (sequenced follow-up).

**The one cross-branch blocker:** three divergent wire schemas exist (A canonical `*_cost_s` +
enum `quality_tier` + required `exact`; B `*_cost` + string `quality_tier` + `quality_gate`; C
`*_s` + `quality_gate`). Independently flagged by BOTH the B and C verifiers. Reconcile the `_s`
suffix, `exact`-vs-`quality_gate`, and `quality_tier` string/enum BEFORE any real code merge, or
the staged-table composition claim cannot be made good. This is a naming/serde reconciliation, not
a redesign — all conceptual fields are present in all three.

## The EXACT next cloud experiment (ONE driver, money-safe — do NOT run this wave)

Plan step 1. This is the single ready-now driver whose money-safety was adversarially verified.

```
python3 scripts/spec-lab/run_integrated_production_benchmark.py      # default (Blender-4.2-safe) plan
```

- **Plan:** default `A100 80GB PCIe -> H100 80GB HBM3 -> H200` — all <= sm_90, so Cycles kernels
  exist and the unverified enumerate-then-kernel-fail Blackwell path is **not exercised**. Do NOT
  pass `--allow-unsupported-gpu`.
- **Money-safe:** one driver only (shared `.tracked_pods.json` -> concurrent drivers kill each
  other); `arm_remote_watchdog` + `register_cleanup`; verify `.tracked_pods.json == []` before AND
  after. `require_gpu_probe()` (64x64@1spp, CPU disabled, requires `rc==0 AND rendered AND
  device!=CPU AND output exists`) gates the 4K reference frame for pennies -> fails closed.
- **Produces:** the first honest MEASURED render `SpecReceipt` for the classroom job
  (3840x2160, 4 frames): `baseline_total_time_s = T_ref(4096 spp)`, `total_product_time_s =
  T_stack(draft 512 spp + adaptive+OIDN+guides+light-tree)`, `speedup = T_ref/T_stack`, every row
  labeled.
- **Honesty caveats to record with the receipt:** (i) the default job uses `hole_fill=rerender`
  -> one MODELED crop step -> the receipt PARKS (`delivery_eligible=false`); to EARN a
  delivery-eligible fully-MEASURED combined number, immediately follow with an `inpaint`/`nearest`
  variant. (ii) Ledger the probe rc/stderr even on this supported-SKU run, so the Blackwell guard
  has its first real datapoint before any future Blackwell contact.
- **Kill clause:** if the honest combined workload does not beat the best single-lane **7.87x**,
  report that plainly — a real negative, not a massaged positive.

Strictly AFTER this lands (still one driver at a time): step 2 = C's forked token lane on a
fleet-class model (needs the F1/F2 fork + a draft model first) -> first honest >1x lossless token
receipt; step 3 = a genuinely nested render job whose control/LLM steps ALSO run through the token
lane -> the ONE legitimate end-to-end combined number. No naive product of the rows above is ever
taken.
