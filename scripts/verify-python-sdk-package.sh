#!/usr/bin/env bash
# Prove the documented local SDK install works without PYTHONPATH or an editable
# checkout. This is intentionally safe for CI: it uses a throwaway virtualenv,
# imports from outside the repository, and deletes all build state on exit.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/cx-python-sdk.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

"$PYTHON_BIN" -m venv "$WORK/venv"
PY="$WORK/venv/bin/python"
cp -R "$ROOT/sdk/python" "$WORK/sdk-python"
# The source checkout may contain artifacts from an earlier local build. Remove
# them only from the throwaway copy so the wheel is always built from source.
rm -rf \
  "$WORK/sdk-python/build" \
  "$WORK/sdk-python/dist" \
  "$WORK/sdk-python/computeexchange.egg-info"
find "$WORK/sdk-python" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$WORK/sdk-python" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete

"$PY" -m pip install --disable-pip-version-check --quiet "$WORK/sdk-python"
"$PY" -m pip check

cd "$WORK"
CX_SDK_SOURCE_ROOTS="$ROOT/sdk/python:$WORK/sdk-python" \
  PYTHONNOUSERSITE=1 \
  "$PY" -m unittest discover \
    -s "$ROOT/sdk/python/tests" \
    -p 'test_*.py' \
    -v

"$PY" - <<'PY'
from computeexchange import Client, __version__

client = Client("https://computexchange.net", "cx_test_not_sent")
print(f"installed computeexchange {__version__} from {client.__class__.__module__}")
PY
