# Renderer 5x Push And 3AM Restart - 2026-07-09

## Summary

The immediate post-overnight push tried to get a fresh renderer-only row above the `5x` floor.
The current proven renderer-only ceiling remains the existing measured quality-ladder row:

- `scene_world_volume.xml`, raw `64 spp` vs `4096 spp`;
- global SSIM `1.0`;
- worst-tile SSIM `1.0`;
- measured speedup `7.4286x`;
- source ledger: `docs/speed-lane-reports/spec-lab/cycles_quality_ladder_ledger.jsonl`;
- speculative gate report: `docs/speed-lane-reports/spec-lab/SPECULATIVE_RENDER_LADDER_2026-07-09.md`.

That row clears the `5x` floor. The attempted fresh rows in this mini-loop did not complete because
runtime-root upload stalled after reachable pods were provisioned.

## Secret And Restart Setup

RunPod key persistence:

- File: `.secrets/runpod.env`.
- Mode: `600`.
- Git ignore: `.gitignore` ignores `.secrets/`.
- `scripts/spec-lab/runpod.py` reads `RUNPOD_API_KEY` from `.secrets/runpod.env` when the
  environment variable is absent.

Automation:

- Codex automation id: `cx-renderer-spec-decode-3am-restart`.
- Schedule: July 9, 2026 at `03:00` America/Toronto.
- Secret handling: automation prompt does not include the key; it relies on `.secrets/runpod.env`.

## Branches Tried Now

### Branch A: Ada/L40S Raw Renderer-Only Retry

Intent:

- Run raw renderer-only quality ladder on L40S.
- Scenes: `scene_monkey.xml`, `scene_world_volume.xml`.
- Reference: `4096 spp`.
- Drafts: `16,32,64,128 spp`.

Outcome:

- Provisioned reachable CUDA L40S.
- Remote watchdog armed.
- Upload of `.artifacts/cycles/runtime/cx-cycles-ada-sm89-batch-runtime-20260708.tar.gz` stalled.
- Branch cut with `Ctrl-C`.
- Cleanup signal terminated tracked pod.
- Final run log reported balance `$44.19`, spend about `$0.08`.

### Branch B: Hopper/H100 Raw Renderer-Only Retry

Intent:

- Run raw renderer-only quality ladder on H100/Hopper.
- Scenes: `scene_world_volume.xml`, `scene_monkey.xml`, `scene_caustics.xml`.
- Reference: `4096 spp`.
- Drafts: `4,8,16,32,64 spp`.

Outcome:

- H200 community had no capacity.
- Provisioned reachable CUDA H100 NVL.
- Remote watchdog armed.
- Upload of `.artifacts/cycles/runtime/cx-cycles-hopper-sm90-batch-runtime-20260708.tar.gz`
  stalled.
- Branch cut with `Ctrl-C`.
- Cleanup signal terminated tracked pod.
- Final run log reported balance `$44.04`, spend about `$0.15`.

## Final Safety State

Independent post-cut autoprobe:

- live pods: `[]`;
- tracked pods: `[]`;
- balance: `$43.8935218191`;
- runtime tars present locally.

## Current Decision

Keep:

- the measured raw `7.4286x` delivery row as the current renderer-only floor-clearing receipt;
- OIDN `4 spp` monkey row as the best new draft/denoise proof from the prior live run;
- no-build runtime-root architecture as the right CUDA execution path.

Cut for now:

- ad hoc cold tar upload when SCP stalls after a reachable pod is provisioned.

Next branch for 3AM:

- avoid rediscovering this upload wall;
- use the new transfer-speed preflight before uploading the full runtime root;
- prefer a pushed image, persistent volume, or remote pull/cache if the transfer preflight fails;
- if upload is healthy, run H100/Hopper raw drafts `1,2,4,8,16,32,64 spp` against `4096 spp`
  reference on `scene_world_volume.xml`, `scene_caustics.xml`, and a harder CX scene;
- then run OIDN and tile-refinement branches;
- only after renderer evidence lands, restart spec decode.

Additional analysis:

- `docs/speed-lane-reports/spec-lab/EMPIRICAL_RENDERER_10X_PATH_2026-07-09.md`

## Claim Boundary

- CX already has a measured raw renderer-only delivery-tier row above `5x` (`7.4286x`) in the
  existing quality ladder.
- This mini-loop did not add a new above-`5x` row because both paid retries hit transport stalls
  before render work.
- Cloud safety held: both branches were cut, cleaned up, and independently checked.
