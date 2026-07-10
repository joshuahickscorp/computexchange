# CX Render Autoprobe - 2026-07-09

## Summary

- Final lane: `apple_silicon`
- Reason: Darwin arm64 host with Metal candidate
- Overall status: `passed_with_skips`
- Ledger: `docs/speed-lane-reports/spec-lab/cx_render_autoprobe_ledger.jsonl`

## Platform

- OS: `Darwin 25.6.0`
- Machine: `arm64`
- Python: `3.14.5`
- Apple Silicon: `True`
- NVIDIA CUDA detected: `False`

## Runtime Tars

| Role | Exists | Size | Path |
|---|---:|---:|---|
| `hopper_sm90` | `True` | `201026571` | `/Users/scammermike/Downloads/computexchange/.artifacts/cycles/runtime/cx-cycles-hopper-sm90-runtime-20260708.tar.gz` |
| `hopper_sm90_batch` | `True` | `201027940` | `/Users/scammermike/Downloads/computexchange/.artifacts/cycles/runtime/cx-cycles-hopper-sm90-batch-runtime-20260708.tar.gz` |
| `ada_sm89_batch` | `True` | `200734833` | `/Users/scammermike/Downloads/computexchange/.artifacts/cycles/runtime/cx-cycles-ada-sm89-batch-runtime-20260708.tar.gz` |

## Cloud Safety State

- RunPod key present: `True`
- SSH pubkey present: `True`
- Tracked pods: `[]`
- Balance: `{'clientBalance': 43.2230308581, 'currentSpendPerHr': 0.01}`
- Live pods: `[]`

## Local Proof Stages

| Stage | OK | Elapsed | Note |
|---|---:|---:|---|
| `renderer_local_tests` | `None` | `None` | --no-run-local-renderer |

## Cloud Proof

Cloud was not treated as proven unless the script actually provisioned and tore down a pod.

```json
{
  "ok": null,
  "reason": "cloud provisioning requires --allow-cloud",
  "status": "skipped"
}
```

## Interpretation

- This autoprobe is a routing and receipt layer, not a renderer victory claim.
- On Apple Silicon it proves the native Rust renderer lane and records CUDA/cloud absence.
- CUDA quality claims must come from the quality/speculative ledgers, with global and worst-tile gates.
