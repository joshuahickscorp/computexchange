# Renderer Frontier Iterative Goal Prompt - 2026-07-09

This supersedes the older "5x", "10x", and one-more-loop goal framing for the renderer/spec-lab
work. Those prompts were useful, but the measured state moved. The next run should start from the
current evidence, compete against the best CX receipt, and continue until there is a real
diminishing-returns boundary, not until the first impressive number appears.

## Read First

Read these before taking action:

- `docs/speed-lane-reports/spec-lab/RENDERER_FRONTIER_10X_AND_DIMINISHING_RETURNS_2026-07-09.md`
- `docs/speed-lane-reports/spec-lab/EMPIRICAL_RENDERER_10X_PATH_2026-07-09.md`
- `docs/speed-lane-reports/spec-lab/RENDER_POLICY_TRAINING_2026-07-09.md`
- `docs/speed-lane-reports/spec-lab/SPECULATIVE_RENDER_LADDER_2026-07-09.md`
- `docs/research/CYCLES_KILLER_SPECULATIVE_OVERNIGHT_GOAL_2026-07-09.md`
- `docs/research/SPECULATIVE_ORCHESTRATOR_PLAN.md`
- `scripts/spec-lab/run_cycles_quality_ladder.py`
- `scripts/spec-lab/run_speculative_render_ladder.py`
- `scripts/spec-lab/train_render_policy.py`
- `scripts/spec-lab/cx_render_autoprobe.py`
- `scripts/spec-lab/runpod.py`

Use the older docs as background, not as the target. `5x` is no longer a target, and touching
`10x` once is no longer a target. The live job is to make the renderer frontier repeatable on
harder scenes, then stack speculation on the strongest renderer base.

## Current Proven State

Best measured renderer-side receipt:

- `14.3372x` on live RunPod `NVIDIA L40S`;
- scene `scene_world_volume.xml`;
- raw `2 spp` vs `4096 spp`;
- global SSIM `1.0`, worst-tile SSIM `1.0`, p5 tile SSIM `1.0`.

Harder-scene measured ceilings from the latest live run:

- `scene_cube_volume.xml`: `7.8721x`, raw `32 spp`, delivery tier.
- `scene_monkey.xml`: `6.88x`, raw `32 spp`, delivery tier.
- `scene_sphere_bump.xml`: `4.5932x`, raw `16 spp`, delivery tier.
- `cx_many_glass.xml`: `3.8656x`, raw `8 spp`, delivery tier.

This means:

- `5x` is below the active frontier.
- `10x` is proven for a friendly scene class.
- `14.3372x` is the current friendly-scene ceiling to beat.
- general `10x+` is not proven until representative/hard scenes clear delivery gates.
- brute low-spp/OIDN sweeps alone have hit diminishing returns on hard scenes.

The next multiplier has to come from actual selective refinement, trained/tuned routing, warm
runtime logistics, representative scene coverage, or spec orchestration stacked on top.

## Methodology

The loop is:

```text
stock Cycles reference -> best CX receipt -> beat our own receipt -> repeat renderer-only until
honest saturation -> stack speculative render/decode orchestration -> train/tune/iterate that stack
until it also saturates
```

Do not finish a goal merely because a new row beats the old row. A new best row is a branch signal:
keep going while there is a plausible next branch that can improve measured delivery speed, quality,
cost, repeatability, or generality.

Do not report back at every marginal improvement. Keep writing ledgers and reports locally, keep
the cloud watchdog active, and continue until the stop rules below are actually satisfied. If a run
hits `20x`, keep going if the active branch still has headroom. If it hits `50x`, keep going if the
next experiment is still evidence-safe and budget-safe. There is no fixed target ceiling.

## Required Loop Order

### 0. Safety and state

Before any paid cloud work:

1. Check git status and do not revert unrelated user changes.
2. Check live RunPod pods, tracked pod state, balance, and current spend.
3. Confirm `.secrets/runpod.env` is used only as ignored local secret storage.
4. Run focused compile/test gates for touched scripts.
5. Record the starting state in the relevant report or ledger.

After any paid cloud work:

1. Terminate pods created by the run.
2. Retry termination on failures.
3. List live pods again.
4. Confirm tracked pod state is empty.
5. Secret-scan outside `.secrets/`.
6. Record final balance, spend, and teardown state.

### 1. Import and rank the existing frontier

Start by regenerating the speculative ladder and policy from the current quality ledger:

```bash
python3 scripts/spec-lab/run_speculative_render_ladder.py --variants raw,oidn --min-rows 1
python3 scripts/spec-lab/train_render_policy.py
```

Use those outputs to choose the next branch. The decision rule is:

- friendly/world-volume-like scenes route to the current `10x+` policy, then seek new ceiling;
- cube/monkey/glass/sphere-like scenes route to selective refinement or threshold tuning;
- if a branch cannot beat the current CX best for that scene class, cut it and leave the receipt.

### 2. Renderer-only push comes first

Keep the speculative-decode architecture open, but do not use speculation to hide a weak renderer
base. Push the renderer itself before claiming compounded speedups.

Priority branches:

1. Actual tile/crop refinement:
   - identify failed or low-confidence tiles from the scoring pass;
   - rerender only those tiles/crops at higher spp if Cycles supports the required border/crop path;
   - merge the refined regions into the draft output;
   - re-score the final image;
   - report accepted tile fraction, refined tile fraction, final quality, final wall time, and net
     speedup.

2. Hard-scene `10x+` hunt:
   - target `scene_cube_volume.xml`, `scene_monkey.xml`, `scene_sphere_bump.xml`, and
     `cx_many_glass.xml`;
   - add or locate more representative CX product scenes if possible;
   - do not overfit `scene_world_volume.xml`.

3. Denoise and adaptive sampling:
   - compare raw vs OIDN/OptiX if available;
   - include denoise time and verification time;
   - test whether denoise lowers the hard-scene sample knee enough to beat current bests;
   - cut denoise if overhead erases the gain.

4. Warm runtime and logistics:
   - avoid repeating full runtime-tar upload where possible;
   - prefer image/persistent-volume/warm-worker paths if available;
   - raise the transfer preflight floor for expensive pods unless the branch is uniquely valuable;
   - keep render-speed claims separate from cold-start product-time claims.

5. Trained/tuned routing:
   - use measured receipts as the training set;
   - tune thresholds, scene-class routing, and GPU-tier selection;
   - measure whether routing improves the representative-scene frontier.

### 3. Speculative layer on top

After the renderer-only branch has been pushed until the next gain is no longer honest, attach the
speculative layer to the strongest renderer base.

For rendering, speculation means quality-gated approximation:

```text
draft render -> verify global/worst-tile/p5 -> accept | tile-refine | rerender -> receipt
```

The existing `run_speculative_render_ladder.py` imports measured rows and models refinement. The
next implementation should replace the modeled refinement with actual crop/tile rerender and merge
receipts where technically possible.

For token/spec-decode anchors, keep them separate and honest:

- use vLLM/Hawking only if the runner and CUDA path are real;
- measure acceptance rate and net speedup;
- do not combine token speculative-decode numbers with renderer numbers unless the same delivered
workload is measured end-to-end or the bridge is explicitly labeled staged/modeled.

The compounding story is allowed, but it must be auditable:

```text
best renderer quality knee + denoise + selective refinement + warm worker + trained routing
+ speculative scheduling/decode
```

`10x * 10x = 100x` is a thesis until measured. It becomes a claim only with either an end-to-end
receipt or a staged table where every multiplier is measured and dependencies are explicit.

## Suggested Commands

Local gates:

```bash
python3 -m py_compile \
  scripts/spec-lab/runpod.py \
  scripts/spec-lab/run_cycles_quality_ladder.py \
  scripts/spec-lab/cx_render_autoprobe.py \
  scripts/spec-lab/run_speculative_render_ladder.py \
  scripts/spec-lab/train_render_policy.py

python3 -m unittest \
  scripts/spec-lab/test_runpod_safety.py \
  scripts/spec-lab/test_cycles_quality_ladder.py \
  scripts/spec-lab/test_cx_render_autoprobe.py \
  scripts/spec-lab/test_speculative_render_ladder.py
```

Autoprobe:

```bash
python3 scripts/spec-lab/cx_render_autoprobe.py --check-cloud-api
```

Renderer hard-scene sweep, bounded:

```bash
python3 scripts/spec-lab/run_cycles_quality_ladder.py \
  --gpu-tier ada \
  --scene scene_cube_volume.xml,scene_monkey.xml,scene_sphere_bump.xml \
  --include-synthetic-scene \
  --synthetic-name cx_many_glass.xml \
  --ref-samples 4096 \
  --draft-samples 1,2,4,8,16,32,64,128 \
  --with-oidn \
  --oidn-device cpu \
  --min-balance 4 \
  --max-minutes 90 \
  --stage-timeout-s 2400 \
  --upload-timeout-s 900 \
  --transfer-preflight-mb 4 \
  --min-transfer-mbps 0.5
```

Premium GPU only when justified by the branch:

```bash
python3 scripts/spec-lab/run_cycles_quality_ladder.py \
  --gpu-tier hopper \
  --scene scene_cube_volume.xml,scene_monkey.xml \
  --include-synthetic-scene \
  --synthetic-name cx_many_glass.xml \
  --ref-samples 4096 \
  --draft-samples 1,2,4,8,16,32,64 \
  --with-oidn \
  --oidn-device cpu \
  --min-balance 4 \
  --max-minutes 75 \
  --stage-timeout-s 1800 \
  --upload-timeout-s 900 \
  --transfer-preflight-mb 4 \
  --min-transfer-mbps 0.5
```

Policy/spec refresh after every meaningful receipt batch:

```bash
python3 scripts/spec-lab/run_speculative_render_ladder.py --variants raw,oidn --min-rows 1
python3 scripts/spec-lab/train_render_policy.py
```

## Stop Rules

Stop and final-report only when all applicable conditions are true:

- no live pods and no tracked pods remain;
- secret scan is clean outside `.secrets/`;
- the latest scripts compile and focused tests pass, or failures are explained;
- at least two distinct next branches have been tried or cut with receipts after the latest best;
- representative/hard-scene attempts have either improved their best delivery receipt or shown why
  they cannot improve without a new implementation path;
- there is no remaining budget-safe, evidence-safe branch that is likely to improve the frontier in
  the current run.

Examples that are not stop conditions:

- hitting `5x`;
- hitting `10x` on a friendly scene;
- beating `14.3372x` once while a plausible next branch remains;
- generating a modeled speculative number without actual rerender/merge proof.

## Deliverables

Create or update:

- `docs/speed-lane-reports/spec-lab/RENDERER_FRONTIER_ITERATION_2026-07-09.md`
- `docs/speed-lane-reports/spec-lab/cycles_quality_ladder_ledger.jsonl`
- `docs/speed-lane-reports/spec-lab/speculative_render_ladder_ledger.jsonl`
- `docs/speed-lane-reports/spec-lab/render_policy_2026-07-09.json`
- any new tile-refinement/spec-decode runner tests.

The final report must separate:

- measured renderer-only speedup;
- measured denoise/refinement overhead;
- measured/speculative-layer speedup;
- staged or modeled multipliers;
- failed branches;
- spend and teardown state.

## Pasteable Goal

```text
/goal Read and execute `/Users/scammermike/Downloads/computexchange/docs/research/RENDERER_FRONTIER_ITERATIVE_GOAL_PROMPT_2026-07-09.md`.

The previous loop proved a renderer-side friendly-scene ceiling of 14.3372x on live L40S
(`scene_world_volume.xml`, raw 2 spp vs 4096 spp, global/worst/p5 SSIM all 1.0). Treat 14.3372x as
the current receipt to beat for friendly scenes, not as a finish line. Treat the hard-scene receipts
as the real product frontier: cube volume 7.8721x, monkey 6.88x, sphere bump 4.5932x, CX glass
3.8656x.

Run the iterative loop now. Do not stop or final-report because one new best row appears. Renderer
first: import the frontier, compete against the best CX receipt per scene class, implement or prove
actual selective tile/crop refinement if possible, rerender/merge/re-score, push denoise/adaptive
sampling/routing/warm-runtime branches, and keep iterating until the next renderer-only gain is no
longer honest or budget-safe. If the renderer hits 20x or 50x and there is still evidence-safe
headroom, keep going.

Then stack speculative render/decode orchestration on top of the strongest renderer base. For
rendering, replace modeled refinement with measured draft->verify->gate->tile-refine/rerender
receipts where possible. For token/vLLM/Hawking speculative decode, scaffold or run it only as a
separate measured layer, then combine multipliers only when the same delivered workload is measured
end-to-end or the staged dependency table is explicit.

Maintain RunPod safety exactly: check pods/tracked state/balance before and after, keep watchdogs
armed, fail closed, terminate all created pods, confirm live pods [] and tracked pods [], and secret
scan outside .secrets. Update ledgers and write
`docs/speed-lane-reports/spec-lab/RENDERER_FRONTIER_ITERATION_2026-07-09.md` with measured results,
failed branches, spend, teardown, and the next frontier.
```
