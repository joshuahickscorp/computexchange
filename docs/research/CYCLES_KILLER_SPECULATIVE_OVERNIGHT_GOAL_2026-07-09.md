# CX Cycles-Killer + Speculative Renderer Overnight Goal

> Superseded for the next iterative renderer/spec run by
> `docs/research/RENDERER_FRONTIER_ITERATIVE_GOAL_PROMPT_2026-07-09.md`.
> Keep this file as background and source context. The active frontier now starts from the measured
> `14.3372x` friendly-scene receipt and the harder-scene ceilings documented in
> `docs/speed-lane-reports/spec-lab/RENDERER_FRONTIER_10X_AND_DIMINISHING_RETURNS_2026-07-09.md`.

Date: 2026-07-09  
Workspace: `/Users/scammermike/Downloads/computexchange`

## Mission

Do not merely reach render parity with Cycles. Build the first serious scaffold for a ComputeExchange
renderer platform that can eventually make stock Cycles feel like a component, not the product.

The desired outcome is not "we wrote a better path tracer overnight." That would be fake. The
desired outcome is a brutally evidence-driven platform pass that:

1. Treats Cycles as a proven reference engine and raw material.
2. Preserves every legal/license/attribution boundary.
3. Auto-detects the execution substrate: Apple Silicon, CUDA, or no GPU.
4. Runs the correct proof path for that substrate without manual babysitting.
5. Converts existing Cycles-fork work into a CX-native renderer platform scaffold.
6. Re-enters the speculative era with a concrete `draft -> verify -> gate -> refine/escalate`
   render protocol.
7. Produces either an unbelievable measured result by morning or an obviously valuable long-running
   process with logs, receipts, and safety state.

This is an overnight "swing hard" goal. Be aggressive, but never lie.

## Non-Negotiable Philosophy

### What "wipe out Cycles" means

It means outperforming stock standalone Cycles as a product substrate:

- better automation;
- better packaging;
- better cloud/runtime selection;
- better receipts;
- better quality gates;
- better warm-worker story;
- better speculative acceleration path;
- better reproducibility;
- better cost accounting;
- better operator experience;
- better integration with ComputeExchange job routing;
- better docs generated from real evidence.

It does **not** mean claiming authorship of Cycles, stripping licenses, removing attribution, or
pretending a tiny native renderer is already feature-complete against Blender/Cycles.

Cycles is mature. CX should beat it by wrapping, orchestrating, specializing, distributing, gating,
and speculating better.

### What "copy everything" means

Use everything legally available from Cycles and related open-source infrastructure:

- source-code structure knowledge;
- standalone build behavior;
- kernel/backend docs;
- XML/USD scene ingestion;
- GPU backend concepts;
- sample-subset mechanics;
- session/render-scheduler hooks;
- OpenImageIO/OpenEXR/OIDN ecosystem patterns;
- documentation structure and build contracts;
- benchmark methodology;
- device capability matrices.

But preserve license compliance and attribution. Do not erase upstream origin. Do not copy
incompatible code into a differently licensed area. If unsure, document the uncertainty and keep
the integration as a patch/fork boundary.

## Current Evidence Baseline

Read these before acting:

- `docs/research/CX_CYCLES_FORK_GOAL_PROMPT.md`
- `docs/research/CX_CYCLES_FORK_GRADE_AND_POTENTIAL_AUDIT_2026-07-08.md`
- `docs/research/CYCLES_SOURCE_MAP.md`
- `docs/research/CYCLES_RESIDENT_WORKER_ARCHITECTURE_2026-07-08.md`
- `docs/research/SPECULATIVE_ORCHESTRATOR_PLAN.md`
- `docs/speed-lane-reports/spec-lab/CYCLES_QUALITY_LADDER_AND_PROVISIONING_2026-07-08.md`
- `renderer/README.md`
- `renderer/DECOUPLED_SHADING_NOTES.md`
- `docs/speed-lane-reports/STUDIO_SCALE_10_RECEIPTS_2026-07-09.md`

Important local artifacts:

- `.artifacts/cycles/runtime/cx-cycles-hopper-sm90-runtime-20260708.tar.gz`
- `.artifacts/cycles/runtime/cx-cycles-hopper-sm90-batch-runtime-20260708.tar.gz`
- `.artifacts/cycles/runtime/cx-cycles-ada-sm89-batch-runtime-20260708.tar.gz`

Known current grades from the latest audit:

- strict overall: `7.9 / 10`
- research scaffold: `9.7 / 10`
- product substrate: `7.6 / 10`
- proven sample-parallel potential: `7.2 / 10`
- proven single-frame quality-tier potential: `7.8 / 10`

These are the numbers to beat.

## External Anchors To Verify If Needed

If internet access is available and current docs matter, prefer primary sources:

- Blender/Cycles GPU rendering manual:
  `https://docs.blender.org/manual/en/latest/render/cycles/gpu_rendering.html`
- vLLM speculative decoding docs:
  `https://docs.vllm.ai/en/latest/features/speculative_decoding/`
- Open Image Denoise docs:
  `https://www.openimagedenoise.org/documentation.html`
- Cycles upstream:
  `https://projects.blender.org/blender/cycles`

Do not rely on stale memory for current backend support, vLLM speculative APIs, or OIDN packaging.
If a fact could have changed, verify it against primary docs before making it load-bearing.

## Absolute Safety Rules

### Cloud and money safety

Before any cloud work:

1. Check active pods.
2. Check tracked pod state.
3. Check balance/spend if the tool exposes it.
4. Record the state in the ledger/report.

After any cloud work:

1. Terminate all pods created by the run.
2. Retry termination if the provider call fails.
3. Independently list pods again.
4. Confirm tracked state is empty.
5. Record final balance/spend.

Never run concurrent RunPod drivers if they share tracked-pod cleanup state.

Never persist API keys or credentials to repo files, logs, markdown, tarballs, or generated scripts.
Use environment variables or existing secret mechanisms only.

If a pod becomes unreachable, fail closed: keep it tracked, retry termination, and document the
failure. Do not silently untrack a maybe-live pod.

### Git and filesystem safety

The worktree may be dirty. Do not revert user changes. Do not run destructive git commands. Do not
delete artifacts unless explicitly necessary and documented. Do not use `git reset --hard`,
`git checkout --`, or broad cleanup commands.

Use `apply_patch` for manual code edits. Keep edits scoped and review diffs.

### Evidence safety

No fake wins. No modeled number can be presented as measured. No "probably faster" can become a
claim. Every result needs:

- command;
- hardware/substrate;
- exact artifact paths;
- wall time;
- quality metrics;
- status;
- failure mode if failed;
- spend if cloud was used.

If the overnight run is still processing, it must have:

- visible live logs;
- current stage name;
- last successful receipt;
- safety state;
- next expected milestone.

## Substrate Autoprobe: First Required Scaffold

Build a single entrypoint that detects what platform it is on and runs the correct proof path.

Preferred file:

- `scripts/spec-lab/cx_render_autoprobe.py`

It should emit one final JSON line and also append JSONL receipts.

Suggested receipt path:

- `docs/speed-lane-reports/spec-lab/cx_render_autoprobe_ledger.jsonl`

Suggested report path:

- `docs/speed-lane-reports/spec-lab/CX_RENDER_AUTOPROBE_2026-07-09.md`

### Autoprobe detection

Detect:

- OS and architecture.
- Apple Silicon / Metal availability.
- NVIDIA / CUDA availability.
- `nvidia-smi` availability.
- CUDA device names and compute capability if available.
- Whether existing Cycles runtime tars are present.
- Whether `renderer/` native GPU smoke can run.
- Whether Blender/Cycles standalone binary exists in an imported runtime root.
- Whether cloud credentials are available.
- Whether RunPod or another GPU backend is reachable.

### Autoprobe routing

If Apple Silicon is available:

1. Run the lightweight native renderer tests:
   - `cd renderer && cargo test --release -- --nocapture`
2. Try the wgpu smoke if dependencies are available:
   - `cd renderer && cargo run --example wgpu_smoke --features gpu`
3. If a local Cycles/Blender path is available, run a tiny Apple/Metal smoke.
4. Record Metal status. Do not block the whole run if local Blender is not installed; record a
   clear skip.

If CUDA is available locally:

1. Run CUDA device diagnostics.
2. Select runtime root:
   - Hopper/`sm_90`: use `cx-cycles-hopper-sm90-batch-runtime-20260708.tar.gz`.
   - Ada/L40S/`sm_89`: use `cx-cycles-ada-sm89-batch-runtime-20260708.tar.gz`.
3. Extract runtime root to a temp or artifact path.
4. Run binary smoke.
5. Run render smoke.
6. Run one quality-ladder mini probe.

If no local GPU is available but cloud credentials are available:

1. Check pod/balance safety.
2. Prefer no-build runtime-root cloud proof. Do not rebuild Cycles unless the tar is invalid.
3. Prefer L40S/Ada for cheap validation unless the target specifically requires Hopper.
4. Use H100/H200 only for bounded proof with clear stop conditions.

If no GPU and no cloud:

1. Run compile/unit scaffolds.
2. Build reports and plans.
3. Do not claim runtime proof.

## Cycles-Killer Platform Scaffold

The platform should make stock standalone Cycles feel primitive by comparison.

### Required components

Create or update a coherent set of scripts/docs that make these first-class:

1. `autoprobe`
   - Chooses Apple/CUDA/cloud/no-GPU path.

2. `runtime root resolver`
   - Maps hardware to prebuilt tar.
   - Verifies tar exists.
   - Verifies binary smoke.
   - Refuses rebuild by default when tar is valid.

3. `scene catalog`
   - Official scenes.
   - Synthetic scenes.
   - Real CX scenes if available.
   - Scene tags: volume, caustic, glass, product, animation, hard/noisy, easy.

4. `quality ladder`
   - Low-spp draft vs high-spp reference.
   - Global SSIM.
   - Worst-tile SSIM.
   - p5 tile SSIM.
   - MAE/max error.
   - Tier: fail / preview / delivery.

5. `denoise lane`
   - OIDN or OptiX if available.
   - Must include denoise time.
   - Must compare raw and denoised variants.
   - Must record if OIDN/OptiX is unavailable.

6. `speculative gate`
   - Draft render.
   - Verify against reference or higher-spp target.
   - Accept if quality clears policy.
   - Escalate failed tiles/frames/scenes.
   - Record acceptance rate and net speedup.

7. `receipt docs`
   - Generated from JSONL where possible.
   - Must distinguish measured/modelled/skipped/failed.

8. `cost/SLA model`
   - GPU type.
   - runtime root upload/extract time.
   - render time.
   - denoise time.
   - verification time.
   - failed/escalated work.
   - estimated customer SLA.

### Product superiority target

By morning, try to show one of:

- a measured quality-gated low-spp + denoise/speculative lane beating high-spp reference by a large
  factor while meeting delivery quality;
- a working platform autoprobe that runs on Apple/CUDA/cloud and produces clean receipts;
- a validated cloud no-build renderer proof on CUDA with a new harder CX scene;
- a meaningful OIDN/OptiX result with quality and time included;
- a concrete in-process scheduler scaffold with compile/tests and source-hook evidence.

The best outcome is a real measured result. The acceptable fallback is a serious scaffold plus
honest proof of where the overnight wall is.

## Compete Against Ourselves Before Spec Decode

The next run must not stop at render parity, `5x`, or even the current best measured row. The order
of operations is:

```text
stock Cycles baseline -> best current CX renderer receipt -> beat our own receipt -> repeat until
the next renderer-only gain is no longer honest -> then adopt speculative decode/orchestration ->
train/iterate/optimize that stack until it also saturates
```

This is a methodology requirement, not motivational language. Every branch should be framed against
two baselines:

1. stock standalone Cycles at the chosen reference quality;
2. the best CX result already in the ledger for the same or nearest comparable workload.

The renderer-side push comes first. For every scene or workload class, search the quality frontier as
aggressively as the evidence allows:

- lower raw spp until global and worst-tile gates fail;
- add OIDN/OptiX/other denoise and re-test the lower knee including denoise time;
- isolate failing tiles/frames and estimate or implement the smallest refinement unit;
- prefer warm image/persistent/runtime-root paths that remove cold-start tax from product claims;
- add representative CX scenes before over-optimizing official toy scenes;
- keep any branch that improves measured delivery speedup, quality, cost, or repeatability;
- cut any branch that cannot beat the current CX best, but leave the receipt.

Do not claim a general `10x` renderer until the ledger proves it on representative scenes. However,
do explicitly hunt for `10x+` renderer-side delivery rows. Current proof already puts `5x` behind
us and a `14.3372x` friendly-scene row in hand, while the hard-scene live ceiling is still
`7.8721x`, so the live target is:

```text
make 10x boring, then make 10x the baseline we compete against
```

Only after the renderer branch has been pushed as high as it can honestly go should the run re-enter
speculative decode adoption. At that point the job is to stack additional leverage on top of the
best renderer substrate:

- wire the speculative render ladder to the best renderer receipt, not to a weaker baseline;
- run or scaffold vLLM/speculative-decode anchors where CUDA is reachable;
- train or tune draft policies, denoise thresholds, tile-refinement thresholds, and scene/workload
  classifiers from measured receipts;
- iterate acceptance, rejection, and refinement policies until marginal gains flatten;
- optimize cost routing so the result is cheaper than owning a workstation or renting a render farm
  for the same delivered quality;
- keep Hawking/spec decode integration as a thin capability/receipt boundary unless a real API
  contract is proven.

The compounding story must remain honest. `10x * 10x = 100x` is only valid when the multipliers are
measured on the same delivered workload or are explicitly marked as staged/modelled. The desired
final platform shape is still maximalist:

```text
best renderer quality knee + denoise + selective refinement + warm workers + scene specialization
+ speculative scheduling + trained routing/acceptance policies
```

Keep pushing this stack until there is no further measurable, quality-preserving, cost-effective
gain left in the current budget.

## Re-Entering The Speculative Era

Speculative rendering is not LLM speculative decoding. Be precise.

For autoregressive tokens, speculative decoding can be lossless through rejection sampling. For
rendered pixels/frames, this is a quality-gated approximation protocol:

```text
draft(input, budget) -> candidate
verify(candidate, input, policy) -> accept | refine | reject
gate(evidence, policy) -> ship | escalate | rerender
receipt(all measurements) -> auditable result
```

### First speculative renderer protocol

Implement or scaffold:

- `scripts/spec-lab/run_speculative_render_ladder.py`

Suggested ledger:

- `docs/speed-lane-reports/spec-lab/speculative_render_ladder_ledger.jsonl`

Suggested report:

- `docs/speed-lane-reports/spec-lab/SPECULATIVE_RENDER_LADDER_2026-07-09.md`

Minimum viable behavior:

1. Pick a scene.
2. Render high-spp reference.
3. Render low-spp draft.
4. Optionally denoise draft.
5. Score draft vs reference.
6. Classify tier.
7. If fail/preview, compute what would be escalated:
   - whole frame;
   - tile subset;
   - higher spp;
   - denoise.
8. Record net speedup including all overheads.

Stretch behavior:

1. Actually rerender failed tiles or failed sample bands.
2. Merge refined output.
3. Re-score final output.
4. Report accepted tile percentage and final speedup.

### Quality policy

Default:

- Delivery:
  - global SSIM `>= 0.98`
  - worst-tile SSIM `>= 0.95`
- Preview:
  - global SSIM `>= 0.90`
  - worst-tile SSIM `>= 0.85`

Do not allow global SSIM alone to pass. The previous monkey-scene rows prove global SSIM can look
excellent while worst-tile quality collapses.

### Stop rules for speculation

Stop or pivot if:

- denoise adds more time than it saves;
- worst-tile failures dominate;
- low-spp draft cannot reach preview on a representative scene;
- acceptance rate is too low;
- the result only works on tiny official scenes;
- overhead erases all speedup.

If speculation fails, document why. A clean negative result is valuable.

## Dead Paths To Avoid

Do not spend the night repeating known-dead loops:

1. One-GPU standalone subprocess sample fanout as a speed claim.
   - Best no-build H100 heavy subprocess row was about `0.8908x`.

2. One-GPU manifest-worker fanout as a speed claim.
   - Best resident-batch row was about `0.8894x`.

3. Naive tree subprocess merge.
   - Python/OpenImageIO currently wins.

4. More premium cloud spend searching random cold capacity without a warm/reachable plan.

5. Any "Cycles killer" claim based only on official monkey/sphere toy scenes.

Use those paths only as correctness harnesses or regression checks.

## High-Value Paths To Push

### Path A: OIDN / denoise validation

Goal:

- validate standalone OIDN or another denoise lane on CUDA cloud;
- include denoise time;
- compare raw vs denoised;
- measure quality lift and speedup.

Why:

- Low-spp + denoise is the most plausible immediate multiplier.

### Path B: Representative CX scene

Goal:

- create or locate a harder CX scene;
- run no-build quality ladder;
- include volumes/glass/caustics/product geometry if possible;
- avoid overfitting to official examples.

Why:

- Real-scene proof is the biggest missing credibility piece.

### Path C: Warm worker / image scaffold

Goal:

- convert runtime tar usage into a service-like worker path;
- record no-build startup, smoke, render, teardown;
- if possible, build/push image or generate Dockerfile/runbook.

Why:

- No-build tar solved build tax, not provisioning tax.

### Path D: In-process scheduler source audit/scaffold

Goal:

- map exact Cycles source hooks for a true in-process sample scheduler;
- add patch skeleton or compile-time guard if feasible;
- do not attempt a risky huge fork unless source hooks are understood.

Why:

- This is the only sample-parallel path that can resurrect fanout on one GPU.

### Path E: Speculative tile refinement

Goal:

- draft low spp;
- identify failing tiles;
- rerender only failed tiles or model the exact refinement workload;
- merge and re-score.

Why:

- This is the most direct "render bits instead" experiment.

## Apple Silicon Lane

If the run is on Apple Silicon:

1. Run native `renderer/` tests.
2. Run wgpu Metal smoke if available.
3. Run or scaffold a local Cycles/Metal smoke if Blender/Cycles exists.
4. Do not block CUDA/cloud progress because Apple lacks CUDA.
5. Record Apple results separately.

Apple Silicon is a valid developer lane and a future worker lane, but the overnight product proof
should prefer CUDA cloud when available because the current Cycles fork evidence is CUDA-heavy.

## CUDA Lane

If CUDA is available locally or in cloud:

1. Do not rebuild by default.
2. Use runtime tar by GPU class.
3. Run binary smoke.
4. Run render smoke.
5. Run quality ladder.
6. Run denoise/speculative ladder if possible.
7. Record `nvidia-smi`, GPU name, driver, CUDA visibility, and runtime root.

CUDA proof must include actual wall-clock time and quality metrics.

## Documentation Output

By the end of the run, create or update:

1. A main overnight report:
   - `docs/speed-lane-reports/spec-lab/CX_CYCLES_KILLER_OVERNIGHT_2026-07-09.md`

2. JSONL ledgers for every runner:
   - autoprobe;
   - quality ladder;
   - speculative ladder;
   - cloud provisioning;
   - denoise;
   - failures/skips.

3. A concise next-step prompt:
   - what to run next;
   - what succeeded;
   - what failed;
   - what is still processing;
   - exact artifact paths.

The report must be honest enough that another agent can continue without guessing.

## Implementation Style

Prefer adding small, composable scripts over one giant script.

Use structured JSONL receipts.

Every runner should have:

- deterministic argument parsing;
- explicit timeouts;
- clear stage names;
- final JSON status line;
- cleanup/finally behavior;
- no secret persistence;
- no silent success on missing artifacts;
- unit tests where cheap.

Run `python3 -m py_compile` on new Python scripts.

Run targeted unit tests for new scripts.

Do not let perfect architecture block the first useful proof.

## Morning Success Criteria

The strongest possible morning result:

- Autoprobe chooses a lane correctly.
- CUDA cloud no-build runtime proof runs.
- A harder scene quality ladder runs.
- OIDN or denoise variant is validated.
- Speculative render ladder accepts a meaningful percentage of draft output or identifies failed
  tiles precisely.
- Delivery-tier output reaches global SSIM `>= 0.98` and worst-tile `>= 0.95`.
- Net speedup is measured and includes overhead.
- Cloud cleanup is verified.
- Report and ledgers exist.

Acceptable strong result:

- No cloud run due reachability/capacity, but local Apple/native scaffolds pass, all scripts compile,
  and the cloud runner is ready with safety checks.

Acceptable still-processing state:

- A bounded cloud proof is running.
- Logs show active stage.
- Last safety state is recorded.
- It is not stuck waiting forever without timeout.

Unacceptable:

- fake "Cycles killer" claims;
- unbounded cloud spend;
- leaked credentials;
- pods left running;
- speed claims without quality gates;
- quality claims without artifacts;
- another loop proving one-GPU subprocess fanout is below `1x`.

## Suggested Execution Order

1. Read all required docs.
2. Audit existing scripts and artifacts.
3. Build `cx_render_autoprobe.py`.
4. Add tests/py_compile for autoprobe.
5. Run autoprobe locally.
6. If CUDA/cloud available, run no-build runtime smoke.
7. Build or extend speculative render ladder.
8. Validate with local/no-GPU dry run first.
9. Run cheapest meaningful GPU proof.
10. If proof succeeds, spend up to the next meaningful tier on harder scene/OIDN/speculation.
11. Write report.
12. If a long-running proof remains active, leave it running only with logs, timeout, and cleanup
    path clear.

## Final Instruction To The Overnight Agent

Be maximalist in ambition and conservative in claims. Build the thing that makes the next proof
easy. If a real multiplier appears, chase it hard. If the multiplier does not appear, leave a clean
map of why and what wall must be broken next.

The goal is to wake up to either an unbelievable measured result or a serious machine still working
on a bounded, valuable proof.
