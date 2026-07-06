#!/usr/bin/env bash
#
# Computexchange — prove-openai-sdk: drives the REAL, official `openai` Python
# package (installed from PyPI, unmodified) against a running control plane via
# base_url override, to prove the OpenAI-compatible Batch API (control/openai.go)
# actually works for the SDK buyers already have — not just for hand-crafted curl
# requests. This is the proof artifact named by docs/internal/CREED_AND_PATH_TO_TEN.md
# ("Buyer developer experience" 7→8, "Harden the OpenAI drop-in against the real
# SDK"): files.create → batches.create → retrieve → files.content, end to end,
# plus the two named hardening checks — an unsupported endpoint and an unrecognized
# model must raise a REAL, TYPED openai exception (not silently substitute a model,
# not hand back a shape the SDK can't parse into e.body["message"]/e.code/e.type).
#
# SEPARATE from prove-local.sh on purpose (same reasoning as bench-local.sh):
# prove-local's own OpenAI section drives the flow via raw curl/urllib, which
# proves the WIRE FORMAT is right but not that the real SDK's request/response
# handling (multipart encoding, status->exception mapping, body parsing) agrees
# with it. This script is the SDK-fidelity check; prove-local is still the
# correctness matrix. Run either, both, or neither.
#
# Usage:  scripts/prove-openai-sdk.sh <control_url> <api_key>
#   e.g.  scripts/prove-openai-sdk.sh http://localhost:18080 dev-api-key-0001
#
# Honest by construction: if the real `openai` PyPI package cannot be installed
# in this environment (no network / no pip), this SKIPS (exit 0) with a loud,
# unambiguous message — it never silently downgrades to a curl-based stand-in and
# calls that "SDK-proven".

set -euo pipefail

CONTROL_URL="${1:?usage: prove-openai-sdk.sh <control_url> <api_key>}"
API_KEY="${2:?usage: prove-openai-sdk.sh <control_url> <api_key>}"

say()  { printf '\033[1;36m[prove-openai-sdk]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  ⚠\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m  ✗ %s\033[0m\n' "$*" >&2; }

command -v python3 >/dev/null 2>&1 || { warn "python3 not found — SKIPPING (cannot even attempt)"; exit 0; }

VENV_DIR="$(mktemp -d)"
trap 'rm -rf "$VENV_DIR"' EXIT

say "provisioning a throwaway venv + installing the REAL openai PyPI package"
if ! python3 -m venv "$VENV_DIR/venv" >/dev/null 2>&1; then
  warn "could not create a venv — SKIPPING real-SDK proof (not faking it with curl)"
  exit 0
fi
# shellcheck disable=SC1091
source "$VENV_DIR/venv/bin/activate"
if ! pip install --quiet --upgrade pip >/dev/null 2>&1 || ! pip install --quiet openai >/dev/null 2>&1; then
  warn "could not install the real 'openai' package from PyPI (no network?) — SKIPPING"
  warn "this environment could NOT prove SDK fidelity; only the curl-based prove-local check ran"
  exit 0
fi
INSTALLED_VER="$(python3 -c 'import openai; print(openai.__version__)')"
ok "real openai package installed (PyPI, unmodified): v$INSTALLED_VER"

say "running the real SDK against $CONTROL_URL (files.create -> batches.create -> retrieve -> files.content)"
CONTROL_URL="$CONTROL_URL" API_KEY="$API_KEY" python3 - <<'PY'
import io, json, os, sys, time

from openai import OpenAI, BadRequestError, NotFoundError

base_url = os.environ["CONTROL_URL"].rstrip("/") + "/v1"
client = OpenAI(api_key=os.environ["API_KEY"], base_url=base_url)

failures = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        failures.append(name)

# --- 1. The documented happy path: files.create -> batches.create -> retrieve -> files.content
batch_input = (
    '{"custom_id":"a","method":"POST","url":"/v1/embeddings","body":{"model":"text-embedding-3-small","input":"alpha prove-openai-sdk line"}}\n'
    '{"custom_id":"b","method":"POST","url":"/v1/embeddings","body":{"model":"text-embedding-3-small","input":"beta prove-openai-sdk line"}}\n'
)
buf = io.BytesIO(batch_input.encode()); buf.name = "batch_input.jsonl"
f = client.files.create(file=buf, purpose="batch")
check("files.create returns a file object", f.object == "file" and f.id.startswith("file-"), f"id={f.id}")

b = client.batches.create(input_file_id=f.id, endpoint="/v1/embeddings", completion_window="24h")
check("batches.create returns a batch object", b.object == "batch" and b.id.startswith("batch-"), f"id={b.id}")

final = None
for _ in range(120):
    b = client.batches.retrieve(b.id)
    if b.status in ("completed", "failed", "expired", "cancelled"):
        final = b
        break
    time.sleep(2)
check("batches.retrieve reaches a terminal state", final is not None, f"last status={getattr(b,'status',None)}")
check("batch completed (not failed)", final is not None and final.status == "completed", f"status={getattr(final,'status',None)}")

if final is not None and final.status == "completed":
    content = client.files.content(final.output_file_id)
    lines = [l for l in content.text.splitlines() if l.strip()]
    check("files.content returns 2 output lines", len(lines) == 2, f"got {len(lines)}")
    cids = set()
    dims_ok = True
    for l in lines:
        d = json.loads(l)
        if d["response"]["status_code"] != 200:
            dims_ok = False
        emb = d["response"]["body"]["data"][0]["embedding"]
        if len(emb) != 384:
            dims_ok = False
        cids.add(d["custom_id"])
    check("all outputs are 200 with 384-dim embeddings", dims_ok)
    check("custom_ids round-trip (a, b)", cids == {"a", "b"}, f"got {cids}")

# --- 2. Hardening check: unsupported endpoint -> a REAL typed error, not a crash.
buf2 = io.BytesIO(b'{"custom_id":"a","method":"POST","url":"/v1/embeddings","body":{"model":"text-embedding-3-small","input":"x"}}\n')
buf2.name = "in.jsonl"
f2 = client.files.create(file=buf2, purpose="batch")
try:
    client.batches.create(input_file_id=f2.id, endpoint="/v1/completions", completion_window="24h")
    check("unsupported endpoint raises BadRequestError", False, "(no exception raised)")
except BadRequestError as e:
    check("unsupported endpoint raises BadRequestError", True)
    check("e.body is a dict (not a bare string)", isinstance(e.body, dict), f"body={e.body!r}")
    ok_msg = isinstance(e.body, dict) and isinstance(e.body.get("message"), str)
    check('e.body["message"] accessible (OpenAI-docs-recommended access pattern)', ok_msg, f"body={e.body!r}")
except Exception as e:
    check("unsupported endpoint raises BadRequestError", False, f"got {type(e).__name__}: {e}")

# --- 3. Hardening check: an unrecognized model -> a real typed 404, NEVER a
# silent substitution onto a fallback model (the exact gap this rung fixes).
buf3 = io.BytesIO(b'{"custom_id":"a","method":"POST","url":"/v1/embeddings","body":{"model":"totally-bogus-model-xyz","input":"x"}}\n')
buf3.name = "in3.jsonl"
f3 = client.files.create(file=buf3, purpose="batch")
try:
    b3 = client.batches.create(input_file_id=f3.id, endpoint="/v1/embeddings", completion_window="24h")
    check("unrecognized model is REJECTED, not silently substituted", False, f"batch silently created: {b3.id}")
except NotFoundError as e:
    check("unrecognized model is REJECTED, not silently substituted", True, f"status={e.status_code}")
    check('e.code == "model_not_found"', e.code == "model_not_found", f"code={e.code!r}")
except Exception as e:
    check("unrecognized model is REJECTED, not silently substituted", False, f"got {type(e).__name__}: {e}")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL REAL-SDK CHECKS PASSED")
PY
RC=$?

if [ "$RC" = "0" ]; then
  ok "real openai SDK (v$INSTALLED_VER) proved the full batch flow + both hardening checks"
else
  fail "real openai SDK run failed — see output above"
fi
exit "$RC"
