#!/usr/bin/env bash
# run.sh — fire the deterministic spec-lab experiment ladder on ONE money-safe GPU.
#
#   export RUNPOD_API_KEY=...            # required
#   bash scripts/spec-lab/run.sh         # full ladder, 180-min hard cap
#   MAX_MIN=90 bash scripts/spec-lab/run.sh
#   bash scripts/spec-lab/run.sh --only A1-ngram,A3-bytes   # a subset
#   bash scripts/spec-lab/run.sh --dry-run                  # print the DAG, no GPU
#
# Money-safety: the pod is tracked to disk before anything else, torn down on every
# exit path, and hard-killed at MAX_MIN. If a run is ever interrupted uncleanly:
#   python3 scripts/spec-lab/runpod.py cleanup     # nuke any tracked pod
#
# Results stream to docs/speed-lane-reports/spec-lab/ledger.jsonl (resumable: a rung
# already PASSED is skipped on re-run).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[ -n "${RUNPOD_API_KEY:-}" ] || { echo "RUNPOD_API_KEY unset"; exit 1; }
[ -f "$HOME/.ssh/id_ed25519.pub" ] || { echo "need ~/.ssh/id_ed25519(.pub) — add the pubkey in the RunPod console too"; exit 1; }
command -v python3 >/dev/null || { echo "python3 required"; exit 1; }

MAX_MIN="${MAX_MIN:-180}"
exec python3 "$HERE/orchestrator.py" --max-minutes "$MAX_MIN" "$@"
