#!/usr/bin/env bash
#
# setup_base.sh — deterministic, IDEMPOTENT base install for the spec-lab pod.
#
# Run ONCE on a fresh RunPod GPU box (Python 3.12, torch 2.8+cu128) before the
# experiment ladder walks. Re-running is safe: pip installs are idempotent-ish
# (already-satisfied pins are no-ops), the exports/mkdir are harmless to repeat.
#
# Hard requirements (a failure here MUST fail the script): vLLM, transformers
# when pinned, numpy, pillow, scikit-image. Defaults preserve the proven
# vllm==0.11.0 / transformers==4.57.1 stack, but SPEC_LAB_VLLM_VERSION and
# SPEC_LAB_TRANSFORMERS_VERSION can select a version-upgrade branch.
#
# Invoked by the orchestrator as:  bash /root/spec-lab/pod/setup_base.sh
# The last line printed on success is exactly: SETUP_BASE_DONE
#
# NOTE on `set -e`: we enable it for the REQUIRED section so a broken vllm /
# transformers install aborts loudly, and we deliberately guard every OPTIONAL
# step with `|| true` so their failures can't trip the errexit.

set -euo pipefail

echo "[setup_base] starting deterministic base install" >&2

# ---------------------------------------------------------------------------
# 1. Model cache + HF transport. HF_HUB_ENABLE_HF_TRANSFER=0 avoids the rust
#    hf_transfer accelerator, which is flaky on some pods and pulls an extra
#    dep; the plain python downloader is slower but reliable. HF_HOME points at
#    the big /models volume so weights survive between runners in one session.
# ---------------------------------------------------------------------------
export HF_HOME=/models/hf
export HF_HUB_ENABLE_HF_TRANSFER=0
mkdir -p /models/hf
echo "[setup_base] HF_HOME=$HF_HOME HF_HUB_ENABLE_HF_TRANSFER=$HF_HUB_ENABLE_HF_TRANSFER" >&2

# Persist the exports for any later interactive/ssh shell on this pod (best
# effort — never fatal). The runners also get these from the orchestrator env,
# so this is belt-and-suspenders only.
{
  echo "export HF_HOME=/models/hf"
  echo "export HF_HUB_ENABLE_HF_TRANSFER=0"
} >> "$HOME/.bashrc" 2>/dev/null || true

# ---------------------------------------------------------------------------
# 2. System packages: ffmpeg for the video runners (decode/encode + probing).
#    apt is noisy and occasionally races on pods; this whole step is optional
#    (the video runners degrade gracefully without ffmpeg), so it must not abort
#    the script. Hence the `|| true` and the errexit-safe subshell.
# ---------------------------------------------------------------------------
echo "[setup_base] apt: ffmpeg + headless-Blender X/GL libs + unzip (optional, best-effort)…" >&2
# ffmpeg for the video runners; the libx*/libgl libs let headless Blender (which the
# Cycles runner self-downloads) actually launch; unzip for the production-scene render
# runner (exp_cycles_render_prod.py fetches classroom/bmw27 .zip scenes — it uses python's
# zipfile so unzip is belt-and-suspenders only). All best-effort (|| true).
(apt-get update && apt-get install -y ffmpeg libxi6 libxxf86vm1 libxfixes3 libxrender1 libgl1 unzip) >/dev/null 2>&1 || true
if command -v ffmpeg >/dev/null 2>&1; then
  echo "[setup_base] ffmpeg present: $(ffmpeg -version 2>/dev/null | head -n1)" >&2
else
  echo "[setup_base] ffmpeg NOT installed (optional) — video runners will fall back" >&2
fi

# ---------------------------------------------------------------------------
# 3. REQUIRED python deps. Ordering is load-bearing for the default pin:
#
#      pip install vllm==0.11.0            # this pulls transformers 5.x
#      pip install transformers==4.57.1    # RE-PIN back down — 5.x breaks the
#                                          # tokenizer path vLLM 0.11 expects
#
#    Version-upgrade branches can set:
#
#      SPEC_LAB_VLLM_VERSION=latest
#      SPEC_LAB_TRANSFORMERS_VERSION=auto
#
#    so vLLM owns its compatible transformers dependency instead of re-pinning.
#    Installing transformers first would just get clobbered by the vllm step,
#    so any re-pin MUST come AFTER vllm. Do not reorder.
#
#    --break-system-packages: RunPod's base image marks the env EXTERNALLY-
#    MANAGED; we intentionally install into it (this is a throwaway pod).
#    --no-cache-dir: keeps the image/disk small on the 60GB pod volume.
# ---------------------------------------------------------------------------
PIP="pip install --break-system-packages --no-cache-dir"

VLLM_VERSION="${SPEC_LAB_VLLM_VERSION:-0.11.0}"
TRANSFORMERS_VERSION="${SPEC_LAB_TRANSFORMERS_VERSION:-4.57.1}"

case "$VLLM_VERSION" in
  latest|unpinned|auto)
    VLLM_SPEC="vllm"
    ;;
  *)
    VLLM_SPEC="vllm==$VLLM_VERSION"
    ;;
esac

echo "[setup_base] pip: $VLLM_SPEC (REQUIRED)…" >&2
$PIP --upgrade "$VLLM_SPEC"

case "$TRANSFORMERS_VERSION" in
  ""|auto|skip|none)
    echo "[setup_base] pip: transformers re-pin skipped (SPEC_LAB_TRANSFORMERS_VERSION=$TRANSFORMERS_VERSION)" >&2
    ;;
  latest|unpinned)
    echo "[setup_base] pip: transformers latest (REQUIRED after vllm)…" >&2
    $PIP --upgrade transformers
    ;;
  *)
    echo "[setup_base] pip: transformers==$TRANSFORMERS_VERSION (REQUIRED after vllm)…" >&2
    $PIP --upgrade "transformers==$TRANSFORMERS_VERSION"
    ;;
esac

echo "[setup_base] pip: numpy pillow scikit-image (REQUIRED)…" >&2
$PIP numpy pillow scikit-image

# ---------------------------------------------------------------------------
# 4. OPTIONAL python deps: opencv (optical flow for the video/render runners).
#    Best-effort ONLY — the runners fall back to numpy when these are missing,
#    so their failure must never fail setup. opencv-python-headless avoids the
#    GUI/GL system libs that aren't present on a headless pod.
# ---------------------------------------------------------------------------
echo "[setup_base] pip: opencv-python-headless (OPTIONAL, best-effort)…" >&2
$PIP opencv-python-headless >/dev/null 2>&1 || \
  echo "[setup_base] opencv install failed (optional) — runners fall back to numpy" >&2

# OpenEXR + Imath — needed to read Blender's multilayer-EXR Vector/Z passes in the
# temporal render runner (it prefers OpenEXR, else falls back to imageio which lacks
# an EXR backend on this image). Best-effort; the temporal runner errors cleanly if absent.
echo "[setup_base] pip: OpenEXR Imath (OPTIONAL, for the temporal-reuse render runner)…" >&2
$PIP OpenEXR Imath >/dev/null 2>&1 || \
  echo "[setup_base] OpenEXR install failed (optional) — temporal runner will report the EXR-read error" >&2

# oidn (Intel Open Image Denoise) — OPTIONAL neural denoiser for the render
# track. Almost never present as a pip wheel; guarded so it can't abort setup.
echo "[setup_base] pip: oidn (OPTIONAL, usually unavailable — best-effort)…" >&2
$PIP oidn >/dev/null 2>&1 || \
  echo "[setup_base] oidn not installed (optional) — render runner uses its numpy denoiser" >&2

# ---------------------------------------------------------------------------
# 5. Report the installed versions of the REQUIRED stack so the ledger has a
#    provenance record, then print the sentinel the orchestrator greps for.
# ---------------------------------------------------------------------------
echo "[setup_base] installed versions:" >&2
python3 - <<'PYEOF' >&2 || true
import importlib
for mod in ("torch", "vllm", "transformers", "numpy", "PIL", "skimage", "cv2"):
    try:
        m = importlib.import_module(mod)
        v = getattr(m, "__version__", "?")
        print(f"  {mod:14s} {v}")
    except Exception as e:
        print(f"  {mod:14s} NOT IMPORTABLE ({type(e).__name__})")
PYEOF

echo "SETUP_BASE_DONE"
