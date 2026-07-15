#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV="${PASS6_VENV:-$ROOT/.venv-pass6}"
PY="${PYTHON:-python3}"
"$PY" -m venv "$VENV"
source "$VENV/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r "$ROOT/pass6/requirements-pass6.in"
python -m pip freeze > "$ROOT/requirements.lock"
echo "Pass-6 environment ready: $VENV"
echo "Lock written only after successful installation: $ROOT/requirements.lock"
