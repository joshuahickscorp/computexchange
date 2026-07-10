# Spec Render And Token Spec Decode Deep Plan - 2026-07-09

## Purpose

This plan is for the next autonomous iteration loop after the Cycles renderer-only frontier work.
The job is not to chase one fixed number. The job is to grow the branches that keep producing
measured fruit and prune the branches that stop producing honest gains.

Current frontier:

- Cycles render-only friendly scene: `14.3372x`, `scene_world_volume.xml`, delivery.
- Cycles render-only hard-scene best: `7.8721x`, `scene_cube_volume.xml`, delivery.
- Tile/crop refinement scaffold: implemented, measured, but not frontier-winning yet.
- Speculative render ladder: imports actual `tile_refine` receipts and maps `78` measured render
  gates into CX receipts; best adapted OIDN delivery row is `12.9695x`.
- CX-native local token speculation: fixed/gated repeat peak `315.460911x` exact; n-gram
  prefix-accept bests are repeat `152.873506x`, prose `119.247118x`, code `5.643504x`,
  JSON `1.987658x`; copy-match prefix-accept grows JSON to `6.609782x` with a bounded
  `q=2` selector, and still carries repeat `155.149457x`, code `11.117705x`, and prose
  `119.243916x`; copy-runway prefix-accept grows repeat `258.594191x`, prose
  `194.514908x`, and code `14.44762x`. The JSON winner is now the CX-owned structural
  JSON-template proposal source at `229.780843x`, exact, accepted token fraction
  `0.998698`, draft-token acceptance `0.573137`, and target-call reduction
  `361.411765x` with width ladder
  `[4096,2048,1536,1024,768,512,384,256,128,64,32,16,8,4,2]`. Random remains a
  negative-control prune, including copy-match at `0.813218x`, copy-runway at
  `0.770385x`, and JSON-template at `0.765712x`.
- Paid vLLM branches are parked/pruned as reference probes: pinned `0.11.0` n-gram was
  slower/non-lossless, draft-model unsupported, and modern vLLM runs hit host driver compatibility.

Important framing: speculative decode is a general paradigm here, not just LLM token inference.
vLLM, Hawking, Cycles, ffmpeg, and similar systems are not the product boundary. They are reference
mines and parts shelves for the broader CX pattern:

```text
cheap draft of future work -> expensive verifier -> accept the valid prefix/region/work unit
-> repair or fall back only where the draft fails
```

For language this means drafted tokens and target-model verification. For rendering this means
drafted pixels/tiles/frames/material variants and renderer/quality verification. The point is to
connect the pixel-rendering pipeline to the speculative-decode idea so render work can be accepted,
partially accepted, refined, or rejected with receipts.

## ComputeExchange-Native Ownership Constraint

This is a ComputeExchange-native acceleration path. Do not center the architecture on vLLM,
Hawking, Cycles, ffmpeg, or any other external runtime. Use those projects aggressively as evidence
and source material: copy ideas, fork narrow pieces, mine kernels, scheduling tricks, memory layouts,
API contracts, compatibility fixes, and benchmark methodology when they help. Then put the useful
piece behind CX-owned `SpecUnit -> DraftProducer -> Verifier -> AcceptancePolicy -> RepairPolicy ->
SpecReceipt` machinery.

The operating rule is not "avoid custom work." The operating rule is "own the hot path that matters."
Do not rebuild CUDA or another commodity layer without measured reason, but custom kernels, custom
resident workers, custom verifiers, custom schedulers, custom acceptance policies, and custom
proposal sources are first-class branches when the receipt says the generic tool is too rigid or too
slow. If importing a framework helps expose the next fruiting branch, use it. If the framework becomes
the constraint, fork the useful part and leave the rest.

This does not need a new product name. It is part of the ComputeExchange process: branch, measure,
prune, and grow the owned path.

The next phase has three top-level branches:

1. Speculative render layer: make draft -> verify -> accept/refine/fallback a real measured runtime,
   not a modeled table.
2. Modality-general speculative decode substrate: define the shared accept/reject/repair machinery
   that can operate on pixels, tiles, frames, material variants, and tokens.
3. Token speculative decode: build or wire a real speculative decode backend, measure acceptance
   and wall-clock speed, and keep its receipts separate until an end-to-end combined workload
   exists.

The combined claim remains staged until proven:

```text
renderer multiplier * render speculation multiplier * token speculative decode multiplier
```

No combined multiplier may be claimed unless either:

- the same delivered workload is measured end to end; or
- a staged table lists every measured multiplier, every dependency, and every modeled bridge.

## Operating Principle: Branch, Measure, Prune

Every branch must carry:

- hypothesis;
- exact code path;
- expected win;
- quality/safety gate;
- cost budget;
- kill rule;
- continuation rule;
- next branch if successful.

Do not stop because a branch improves. Improvement is a reason to split and grow the branch. Stop
only when:

- the branch loses to the current best comparable receipt;
- quality gates fail and the plausible fixes are exhausted;
- cost/logistics dominate and no warm path exists;
- the next experiment cannot distinguish signal from noise;
- safety requires stopping.

No fixed target exists. `20x`, `50x`, or `100x` is not a finish line. A number is only a frontier
point. If the branch still has evidence-safe headroom, keep iterating.

## Shared Measurement Contract

All render experiments must report:

- scene;
- device and cloud;
- reference samples and reference time;
- draft samples and draft time;
- verification time;
- refine/rerender/fallback time;
- total product time;
- global SSIM;
- worst-tile SSIM;
- p5 tile SSIM;
- tile count;
- accepted/refined/rejected tile fraction;
- final tier: fail, preview, delivery;
- speedup versus reference;
- exact code path and runtime hash or patch list.

All token speculative decode experiments must report:

- target model;
- draft model or draft algorithm;
- backend;
- prompt set;
- max tokens;
- batch size/concurrency;
- baseline tok/s;
- speculative tok/s;
- acceptance rate;
- rejection/resample count;
- output equivalence rule;
- latency p50/p95;
- GPU utilization if available;
- wall-clock speedup.

All combined experiments must report both render and token sections, plus:

- whether the workload is genuinely end-to-end;
- whether the multipliers are simultaneous or sequential;
- which parts are modeled;
- total delivered output quality.

## Branch 0: Safety, State, And Frontier Import

Before any paid work:

1. Check `git status --short`.
2. Check RunPod tracked state, live pods, balance, and current spend.
3. Refuse cloud work if any unknown pod is running.
4. Confirm `.secrets/runpod.env` stays ignored.
5. Run focused local tests for touched scripts.
6. Import current ledgers:
   - `cycles_quality_ladder_ledger.jsonl`
   - `tile_refinement_ledger.jsonl`
   - `speculative_render_ladder_ledger.jsonl`
   - `render_policy_training_ledger.jsonl`
7. Recompute the current best per scene and branch.

After paid work:

1. Terminate created pod.
2. Retry termination if needed.
3. Confirm tracked pods `[]`.
4. Confirm live pods `[]`.
5. Recheck balance and current spend.
6. Secret scan outside `.secrets`.
7. Append results and failures to reports.

## Branch 1: Speculative Render Runtime

### 1A. Real Runtime State Machine

Build one local/runtime API around:

```text
draft -> score -> accept | refine tiles | fallback whole frame -> score final -> receipt
```

Required code shape:

- `RenderSpecJob`: scene, target tier, ref policy, draft policy, refine policy.
- `RenderSpecReceipt`: timings, quality, accepted/refined/fallback fractions.
- `RenderBranchDecision`: accept, refine, fallback, prune.
- `RenderVerifier`: common scorer for global/worst/p5 SSIM.
- `RenderLedgerWriter`: appends stable JSONL rows.

Kill rule:

- prune if product speedup loses to the best direct render policy for the same scene class after
  two parameter sweeps.

Continue rule:

- grow if it beats direct render or clears delivery at lower total time than direct delivery.

### 1B. Tile Refinement Parameter Ladder

The current batch-crop tile branch reached preview but not delivery:

- cube volume, 4 tiles, `3.4897x`, worst tile `0.946571643`.

Experiment ladder:

1. Retry only with warm runtime or strong transfer gate.
2. `max_refine_tiles`: 6, 8, 12, 16.
3. `refine_samples`: 32, 48, 64, 96.
4. grids: 8, 12, 16.
5. thresholds: 0.94, 0.95, 0.96, 0.98.
6. rank failed tiles by worst SSIM, error energy, and p5-local error.
7. compare against raw 32/64 direct policies.

Kill rule:

- prune any tile configuration that cannot beat raw delivery for the same scene after warm runtime
  removes upload noise.

Continue rule:

- if one config reaches delivery and beats direct render, split into grid/threshold/sample
  optimization.

### 1C. Resident Worker

The batch manifest still resets the Cycles session per job. The next real speed branch is a
resident worker with a command protocol:

```text
load scene once
render draft
score or export draft
render selected crops
render fallback if needed
return receipt
```

Implementation options:

- minimal stdin/stdout JSON protocol in standalone Cycles;
- Python controller with one long-lived Cycles subprocess if Cycles can keep scene/session hot;
- C++ resident server if process control is the bottleneck;
- image/volume path first if upload dominates.

Use existing Cycles internals if they are reasonable. Customize the fork when the interface needed
for CX cannot be expressed through existing CLI flags without paying fixed costs.

Kill rule:

- prune if resident protocol only saves upload/process cost but not enough to beat direct policy.

Continue rule:

- grow if it makes tile refinement delivery competitive or enables many-scene/variant amortization.

### 1D. Denoise-Assisted Spec Render

OIDN already helps some hard scenes. The speculative layer should test:

```text
low spp raw -> OIDN -> verify -> accept/refine/fallback
```

Parameter ladder:

- draft samples: 1, 2, 4, 8, 16.
- OIDN CPU vs available GPU path.
- verify before and after denoise.
- refine raw crops versus denoised draft.
- compare denoise time against sample-time saved.

Kill rule:

- prune if denoise overhead makes delivery slower than raw direct delivery.

Continue rule:

- grow if denoise lowers the delivery sample knee and leaves enough headroom for refinement.

### 1E. Temporal / Multi-Frame Spec Render

If scenes can produce frames or camera/material variants, single-image SSIM underestimates the
opportunity.

Experiments:

- frame N draft predicts frame N+1 failed regions;
- reuse accepted tiles across similar frames;
- material variant cache for product rendering;
- shared reference for many variants;
- amortized resident scene load.

Kill rule:

- prune if the workload is only single still images and no reuse exists.

Continue rule:

- grow if amortization beats single-frame direct render by a new factor.

## Branch 2: Modality-General Speculative Decode Substrate

This is the paradigm-shift branch. It treats render outputs as decodable work units, not just as
images rendered at one fixed quality setting.

The shared abstraction:

```text
Draft unit:
  token span | image tile | frame region | material variant | cached scene component

Verifier:
  target model logits | high-spp render | perceptual/SSIM gate | physical/semantic constraint

Accept:
  exact token prefix | tile region | frame block | cached/material result

Repair:
  resample tokens | crop-refine tile | rerender frame region | fallback full render
```

Required substrate objects:

- `SpecUnit`: token span, tile, frame region, or render variant.
- `DraftProducer`: cheap model/render/cache predictor.
- `Verifier`: expensive truth source or quality gate.
- `AcceptancePolicy`: exact match, SSIM/worst-tile, perceptual, semantic, or hybrid.
- `RepairPolicy`: targeted correction, crop refinement, rerender, or full fallback.
- `SpecReceipt`: accepted fraction, rejected fraction, repair cost, final quality, speedup.

Render-specific decode experiments:

- decode an image as tiles: accept high-confidence draft tiles, refine failed tiles;
- decode a frame sequence as regions: accept unchanged regions, refine changed regions;
- decode product variants: accept cached geometry/lighting and repair material-dependent regions;
- decode sample-space: accept low-spp estimate where variance/quality gate passes, add samples only
  where it fails.

Kill rule:

- prune a speculative unit type if verification plus repair costs more than direct production.

Continue rule:

- grow if accepted fraction increases, repair stays local, and final product time beats direct
  render or direct decode.

## Branch 3: Token Speculative Decode

### 2A. Inventory And Baseline

First inspect existing token/Hawking/vLLM code and identify:

- whether there is a working vLLM runner;
- whether Hawking code exists as a protocol, model, or placeholder;
- which local/RunPod image supports the backend;
- which model sizes are feasible on current GPU tiers;
- what prompt corpus already exists.

Baseline before speculation:

- target model greedy decode;
- fixed prompt set;
- fixed max tokens;
- batch sizes 1, 4, 8, 16 if memory allows;
- deterministic output settings;
- tok/s and latency p50/p95.

Kill rule:

- do not optimize speculation until baseline is stable and reproducible.

### 2B. Reference-Mining Backend Probes

Probe existing speculative decode support where it fits, but treat the result as evidence and
extractable machinery rather than the center of the plan:

- vLLM built-in speculative decode if available in the installed version;
- n-gram/prompt lookup style draft where no small model is needed;
- small draft model path;
- Medusa/EAGLE-style heads only if supported by model/checkpoint/backend;
- kernel, scheduler, memory-layout, batching, and verifier ideas that can be copied or rebuilt in
  CX-owned form.

Do not spend cycles integrating a framework just because it exists. Run it when it can answer a
specific question: expected acceptance, verifier overhead, scheduler shape, model compatibility, or
kernel/memory behavior. If it is slower, non-lossless, unsupported, or too rigid, prune the dependency
branch and keep any useful part as a CX-native implementation task.

Kill rule:

- prune a backend if setup instability exceeds measurement value, acceptance is too low, exactness
  fails, or the framework contract blocks CX-native render/token coupling.

Continue rule:

- grow if speedup is real at useful batch/concurrency and outputs pass equivalence, or if the probe
  reveals a kernel/API/scheduler that should be copied into the CX-owned path.

### 2C. CX-Custom Spec Decode Protocol

This is the primary branch. Framework support is allowed to inform it, but the CX protocol owns the
receipt, branch scoring, and render/token coupling:

```text
target model verifies tokens
draft source proposes k tokens
accept prefix
reject at first mismatch
resume target
receipt logs acceptance and speed
```

Draft sources:

- small model;
- same model lower precision or lower layers if feasible;
- n-gram cache;
- prompt/template predictor;
- CX-owned structural predictors, starting with the measured JSON-template row predictor;
- task-specific deterministic predictor;
- retrieval/corpus predictor for repeated workloads.

Key experiments:

- speculation length k: 2, 4, 8, 16.
- acceptance thresholds or greedy exact match.
- batch scheduling with mixed acceptance.
- prompt classes: repetitive, code, chat, structured output.
- target/draft model size ratios.
- prefix-accept verification that accepts the valid prefix and repairs only the first mismatch.
- custom proposal sources for code, JSON, prose, and repeated templates.
- JSON-template growth: generalize the current verified-row parser beyond one row shape,
  keep the bulk encoded-row cache hot, infer id/value/status transitions more cheaply,
  improve predictive modulus/wrap/reset handling, add dual-hypothesis proposals near
  observed cycle boundaries, and split width caps around the measured `4096` knee. The
  measured `8192/6144` probes over-draft and lose wall-clock today, so grow them only
  after proposal construction or the acceptance boundary changes. A Python row-boundary
  cycle-copy guard was measured as exact but slower and is pruned; reopen that idea only
  as a cheaper persistent-index or compiled branch.
- hot-path implementations: Python reference, vectorized/Rust candidate, CUDA/custom kernel candidate
  when verifier/proposal accounting shows the overhead is in the inner loop.
- render-linked token speculation: scene/material/text changes that let token drafts propose render
  units, cache keys, or repair regions.

Kill rule:

- prune if acceptance rate and overhead cannot beat baseline tok/s.

Continue rule:

- grow if any prompt class gives sustained speedup with exact output equivalence.

### 2D. Hawking-Specific Branch

Treat Hawking as a named branch only after inventory proves what exists locally.

If Hawking is present:

- run its baseline tests;
- connect receipts to the token spec ledger;
- measure acceptance and speed on the same prompt set as vLLM;
- compare against CX-native protocol rows and vLLM reference-probe rows.

If Hawking is absent or placeholder-only:

- write a scaffold that states the contract;
- do not claim Hawking speedup;
- use CX-native local protocol and targeted vLLM probes as the measured branches.

Kill rule:

- prune Hawking-specific work if it cannot produce a real measured receipt within the budgeted
  iteration.

Continue rule:

- grow if it beats the current CX-native/probe frontier or enables CX-specific customization.

## Branch 4: Cross-Modal Orchestrator

The shared idea is not "rendering and tokens are the same." The shared idea is:

```text
cheap draft -> expensive verifier -> accept prefix/region -> repair failures -> receipt
```

Build a common experiment registry:

- `branch_id`
- modality: render, token, combined
- hypothesis
- parent branch
- config
- receipt paths
- score
- prune/continue decision

Branch score:

```text
score = measured_speedup * quality_gate * reproducibility * generality / cost
```

Where:

- quality gate is 0 for fail, 0.5 for preview, 1 for delivery/exact;
- reproducibility improves with repeated rows;
- generality improves across scenes/prompt classes;
- cost penalizes long setup and upload drag.

This gives the loop a brain: it keeps growing fruiting branches and prunes the rest.

## Branch 5: Combined End-To-End Workloads

Only after both render speculation and token speculation have real receipts, define end-to-end
workloads:

1. Render job plus generated scene/material prompt.
2. Product-variant batch with text descriptions and rendered outputs.
3. Agent-driven render iteration: model proposes edit, renderer verifies image.
4. Report generation: token output plus render artifacts.

Measure:

- baseline end-to-end time;
- optimized end-to-end time;
- render-only contribution;
- token-only contribution;
- orchestration overhead;
- final delivered quality.

Kill rule:

- prune combined claims if the components do not overlap in the same delivered workflow.

Continue rule:

- grow if total product time beats the sum of isolated baselines and the quality is auditable.

## Immediate Execution Order

1. Re-import current frontier and write branch registry.
2. Inventory token/vLLM/Hawking code in the repo.
3. Establish token baseline on local or RunPod.
4. Add the modality-general speculative unit/receipt schema.
5. Add token speculative decode ledger and report.
6. Run targeted reference probes only when they answer a concrete CX-native design question.
7. Run the cheapest render speculative-decode unit path that does not require new cloud spend.
8. If speedup appears, sweep prompt/scene classes and speculation lengths/unit sizes.
9. If no speedup appears, prune backend/unit type and try next branch.
10. In parallel, make render speculation consume actual tile-refine rows, not modeled rows.
11. Mine external projects only for pieces that can be owned inside the CX path; do not let a framework
   define the architecture boundary.
12. Start a custom kernel/scheduler/resident-worker branch whenever a measured receipt identifies
   overhead in the generic path.
13. Do not spend on more tile refinement until warm runtime/image path is available or transfer gate
   is strict enough.
14. Write a unified branch/prune report after every loop.

## Reports To Maintain

- `docs/speed-lane-reports/spec-lab/SPEC_RENDER_TOKEN_DECODE_BRANCH_LEDGER_2026-07-09.md`
- `docs/speed-lane-reports/spec-lab/token_spec_decode_ledger.jsonl`
- `docs/speed-lane-reports/spec-lab/spec_render_token_branch_ledger.jsonl`
- `docs/speed-lane-reports/spec-lab/SPEC_RENDER_AND_TOKEN_DECODE_ITERATION_2026-07-09.md`

## Required Final State For A Loop

At the end of each autonomous loop:

- list best render-only receipt;
- list best spec-render receipt;
- list best token-spec receipt;
- list any end-to-end combined receipt;
- list pruned branches and reasons;
- list still-growing branches and next experiments;
- confirm RunPod tracked/live pods are empty;
- confirm balance/current spend;
- report tests run;
- provide the next continuation prompt.

## Continuation Prompt Template

```text
/goal Read and execute docs/research/SPEC_RENDER_AND_TOKEN_SPEC_DECODE_DEEP_PLAN_2026-07-09.md.

Use the branch/prune loop, not a fixed target. Start from the current receipts:
renderer-only friendly 14.3372x, hard-scene delivery 7.8721x, batch-crop tile-refine 3.4897x
preview, speculative render ladder with actual tile_refine imports and best adapted OIDN delivery
12.9695x, CX-native fixed/gated repeat 315.460911x exact, and prefix-accept adaptive token bests:
ngram repeat 152.873506x, prose 119.247118x, code 5.643504x, JSON 1.987658x; copy-match JSON
6.609782x with bounded q=2, repeat 155.149457x, code 11.117705x, prose 119.243916x; copy-runway
repeat 258.594191x, prose 194.514908x, code 14.44762x, JSON 5.649682x; JSON-template structural
proposal JSON 229.780843x exact with accepted tokens 0.998698, draft acceptance 0.573137,
target-call reduction 361.411765x, width ladder
[4096,2048,1536,1024,768,512,384,256,128,64,32,16,8,4,2], and random JSON-template pruned at
0.765712x. Random also prunes at 0.813218x for copy-match and 0.770385x for copy-runway.

Work on speculative rendering as a modality-general speculative decode problem, and work on token
speculative decode as one branch of that broader paradigm. The center is ComputeExchange-owned:
`SpecUnit -> DraftProducer -> Verifier -> AcceptancePolicy -> RepairPolicy -> SpecReceipt`. Inventory
local vLLM/Hawking/token code paths, but treat external systems as reference mines and parts shelves,
not dependency centers. Pillage kernels, schedulers, memory layouts, compatibility fixes, and API
ideas only when they help the CX-native hot path. Establish stable token baselines, run targeted
reference probes only when they answer a concrete question, grow the CX-custom prefix/spec protocol,
especially the JSON-template structural branch at the measured 4096-token knee and the copy-runway
repeat/prose/code branch, and run the cheapest render speculative-decode unit path that uses measured
draft->verify->refine/fallback receipts. Grow or prune branches by measured accepted fraction,
exactness or quality, wall-clock speed, and reproducibility. If the measured bottleneck is in the
generic runtime, start a custom kernel/scheduler/resident-worker branch instead of waiting for a
library to solve it.

Do not stop at any fixed speedup. If a branch improves and still has evidence-safe headroom, keep
splitting and growing it. If a branch loses to the current comparable receipt, fails quality/exactness
after reasonable fixes, or is dominated by logistics, prune it and move budget to the next branch.

Maintain RunPod safety exactly: check tracked pods, live pods, balance, and current spend before and
after paid work; arm watchdogs; terminate created pods; confirm tracked pods [] and live pods [];
secret-scan outside .secrets. GPU fallback is monotonic upward only: if the requested GPU is not
available, upgrade, never downgrade. Avoid full runtime tar uploads unless a 4 MiB transfer preflight
with a strict floor passes, or use a warm image/volume. Write/update the branch ledger and
docs/speed-lane-reports/spec-lab/SPEC_RENDER_AND_TOKEN_DECODE_ITERATION_2026-07-09.md with measured
receipts, pruned branches, still-growing branches, and the next continuation prompt.
```
