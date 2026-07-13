# Heterogeneous fleet proof harness

`scripts/fleet_proof.py` turns a physical fleet run into a reproducible proof bundle. It is intentionally inventory-driven: it never discovers, rents, or provisions hardware. A live run only starts/checks the exact machines and pod IDs in the supplied JSON, submits the configured workloads, maps completed receipts back to the workers observed at readiness, and cleans up.

This closes the *proof scaffolding* gap. It does not itself prove that two Macs or a production CUDA lane exist. The runnable fixture is synthetic and must never be cited as field evidence.

## Run it locally now

The mock inventory models two distinct Apple machines and two distinct RunPod/CUDA workers. It drives the same identity, lane, receipt, settlement, cleanup, ledger, and artifact assertions used by live mode, without SSH, network calls, credentials, or spend.

```sh
python3 scripts/fleet_proof.py validate \
  --inventory docs/fleet-proof/mock.inventory.json \
  --mode mock

python3 scripts/fleet_proof.py run \
  --inventory docs/fleet-proof/mock.inventory.json \
  --mode mock

python3 -m unittest -v scripts/test_fleet_proof.py
```

A no-execution plan of the future physical inventory is also safe now:

```sh
python3 scripts/fleet_proof.py run \
  --inventory docs/fleet-proof/two-apple-two-cuda.inventory.example.json \
  --mode dry-run
```

`dry-run` validates cardinality and writes a plan ledger, but does not run a command or contact a provider. Live mode rejects every `REPLACE_WITH_...` marker.

## Prepare the physical inventory

When the Studio is present, copy `two-apple-two-cuda.inventory.example.json` to an ignored location such as `.artifacts/fleet-proof.inventory.json` and replace every marker. Add or remove CUDA node objects to express any `N`; then update the explicit minima under `requirements`.

Each node declares:

- a stable inventory ID, substrate, and lane;
- a provider resource ID: the Mac platform UUID for owned Apple hardware or the exact RunPod pod ID for ephemeral CUDA;
- a local or SSH transport;
- the expected engine, hardware class, and pinned output-determining build hash;
- optional idempotent start, mandatory readiness, and paired cleanup commands.

Do not store tokens or API keys in JSON. Commands should read credentials from local/remote environment files. The driver only injects `CX_FLEET_*` run metadata. Live ephemeral RunPod entries require `RUNPOD_API_KEY` (or the explicitly named environment variable) *before any node command runs*, so teardown capability is established first.

The current vLLM runner remains soak-gated rather than a production dispatch lane. A real CUDA receipt will therefore remain impossible until that gate is intentionally promoted; the harness reports failure instead of converting readiness into a product claim.

## Readiness contract

Every node's `commands.ready` may print diagnostic lines, but its final non-empty stdout line must be one JSON object:

```json
{
  "schema_version": 1,
  "ok": true,
  "node_id": "apple-studio",
  "machine_id": "REAL-STABLE-PHYSICAL-ID",
  "provider_resource_id": "SAME-VALUE-AS-INVENTORY",
  "worker_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
  "substrate": "apple",
  "lane": "metal_candle",
  "engine": "candle",
  "build_hash": "REAL-REGISTERED-BUILD-HASH",
  "hardware_class": "apple_silicon_max"
}
```

The probe must derive, not restate, identity:

- On Apple, read `IOPlatformUUID` (or another stable platform identity) and the server-bound `worker_id` from the live agent status/registration.
- On CUDA, read the GPU UUID with `nvidia-smi`, the live worker ID, and the exact pod ID supplied by the external lifecycle step.
- Read engine, hardware class, and build hash from the registration/control-plane source. `GET /admin/workers` currently omits engine/build hash, so use an authoritative read-only DB/query helper until that admin projection is expanded. Do not echo the inventory's expected values and call that observation.

The harness rejects reused worker IDs, mismatched resource IDs, wrong lanes/classes/builds, and fewer distinct physical machine IDs than declared. Two processes on one Mac therefore cannot satisfy a two-machine requirement.

## Workload and collector contracts

`workflow.commands.submit` runs locally after all nodes pass readiness. The driver exposes `CX_FLEET_READINESS_JSON` and requires this final JSON line:

```json
{
  "schema_version": 1,
  "ok": true,
  "proof_run_id": "VALUE-FROM-CX_FLEET_PROOF_RUN_ID",
  "workload_id": "physical-proof-wave-1",
  "job_ids": ["apple-job-id", "cuda-job-id"]
}
```

Use separate Apple and CUDA jobs if one current job contract cannot legitimately span both engines. The proof binds them with one `workload_id`; it never implies that a single task migrated between incompatible lanes.

`workflow.commands.collect` runs locally until it returns `ok: true` or the configured attempts expire. It receives `CX_FLEET_SUBMISSION_JSON`, `CX_FLEET_WORKLOAD_ID`, and `CX_FLEET_JOB_IDS`. Its final JSON line is:

```json
{
  "schema_version": 1,
  "ok": true,
  "proof_run_id": "VALUE-FROM-CX_FLEET_PROOF_RUN_ID",
  "workload_id": "physical-proof-wave-1",
  "receipts": [
    {
      "job_id": "apple-job-id",
      "task_id": "completed-task-id",
      "worker_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
      "status": "complete",
      "artifact_sha256": "64_HEX_CHARACTERS_FROM_THE_COMMITTED_RESULT",
      "verification_class": "candle|REAL-REGISTERED-BUILD-HASH",
      "settled": true
    }
  ]
}
```

The collector should query authoritative completed task/result/ledger rows, not agent self-report. The harness then independently requires:

- every task belongs to a submitted job and every task ID is unique;
- every receipt worker was observed during readiness;
- every inventoried node contributes the configured minimum receipts;
- distinct receipt workers and their mapped physical machines meet the minima;
- distinct workers per substrate and lane meet their minima;
- artifact digests are SHA-256-shaped and verification classes match readiness;
- settlement is true when the inventory makes it a gate.

## Live execution and cleanup

No command below provisions a pod. First create external CUDA workers with the existing money-capped lifecycle or another deliberately approved process, then put the returned pod IDs and SSH endpoints into the ignored inventory.

```sh
export RUNPOD_API_KEY=...  # environment only

python3 scripts/fleet_proof.py validate \
  --inventory .artifacts/fleet-proof.inventory.json \
  --mode live

python3 scripts/fleet_proof.py run \
  --inventory .artifacts/fleet-proof.inventory.json \
  --mode live
```

Cleanup order is deliberate:

1. The optional workflow cleanup cancels unfinished submitted work. It can read `submission.json` beneath `CX_FLEET_PROOF_ARTIFACT_DIR`.
2. Node cleanup commands stop agents/servers in parallel.
3. Every ephemeral RunPod resource is terminated by its exact inventoried pod ID through a `podTerminate` mutation. There is no create/deploy mutation in the driver.

All three steps run from `finally`, including readiness, submission, collection, and assertion failures. A failed provider teardown makes the overall run `FAIL` with `manual-action-required`; it is never reported as a clean run. The emergency retry is:

```sh
python3 scripts/fleet_proof.py cleanup \
  --inventory .artifacts/fleet-proof.inventory.json \
  --mode live
```

After any teardown failure, verify the provider console and `python3 scripts/spec-lab/runpod.py pods`. Do not delete the proof directory until every explicit pod ID is absent.

## Proof artifacts

Each run writes `.artifacts/fleet-proof/<run-id>/` by default:

- `inventory.snapshot.json` — redacted canonical run inventory;
- `events.jsonl` — append-only machine-readable lifecycle/assertion ledger;
- `readiness.json`, `submission.json`, `collected.json` — protocol evidence;
- `commands/<scope>/...` — redacted stdout/stderr plus return code, duration, timeout flag, and hashes;
- `summary.json` — final status, observations, annotated receipts, every assertion, and cleanup state;
- `artifact_manifest.json` — SHA-256 and byte size for every other proof artifact.

The snapshot records the current Git commit when available. Raw secrets are never intentionally written, common bearer/API-key forms are redacted, and live inventory should still contain references to environment variables rather than literal credentials.

## What counts as the real acceptance receipt

One green mock run proves only the harness. The first physical acceptance run must show at least two distinct Apple platform IDs and two distinct CUDA GPU/pod IDs, four server-bound worker UUIDs, real completed task/result hashes from every worker, exact verification classes, settlement rows, and confirmed teardown. Repeat it across ten agent/server restarts and preserve each immutable run directory before calling restart stability proven.

For a platform/runtime 5/5 claim, extend that matrix to at least two workers per supported runtime class, 100 held-out manifests per class, ten restarts, no per-machine hand edits, and independent clean-checkout reproduction. This work is independent of token speculative decode; it depends only on the shared worker identity, receipt, artifact, and settlement contracts.
