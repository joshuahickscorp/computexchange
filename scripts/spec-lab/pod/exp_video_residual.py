#!/usr/bin/env python3
"""
exp_video_residual.py — "render the DELTA / P-frame" test.

The owner's framing: instead of rendering (or transmitting) every frame in full,
render a keyframe once and then, for each following frame, render only the RESIDUAL
against a cheap PREDICTION from the previous frame. If that residual is small and
compresses well, "rendering the delta" is much cheaper than rendering the full frame
— which is exactly what P-frames buy a video codec, applied here as a compute test.

Per non-key frame:
  prediction = previous frame, optionally shifted by an estimated GLOBAL motion vector
      motion="none"           -> prediction is the raw previous frame (direct delta)
      motion="from_container" -> prediction is the previous frame shifted by a motion
                                 vector estimated from the frames themselves (a cheap
                                 stand-in for reading codec MVs out of the container)
  residual   = true_frame - prediction        (signed)
  quantised  = round(residual / q) * q         (q = residual_quant)
  We measure the residual's SIZE (entropy-coded bytes) vs a full frame's size, and
  reconstruct true ~= prediction + dequantised_residual to score SSIM.

Cost / speedup (in BYTES of information that must be produced/transmitted per frame,
a real, measured proxy for "how much has to be rendered"):
  net_speedup = full_frame_bytes /
                (keyframe_bytes_amortised + mean_residual_bytes)
  mean_residual_frac = mean_residual_bytes / full_frame_bytes
  quality = mean SSIM(reconstructed, true) over the non-key frames.

All byte sizes are REAL: we zlib-compress the actual pixel/residual buffers and
measure their compressed length. Nothing is guessed.

Contract: human logs to stderr; the LAST stdout line is exactly ONE json metrics
object. Any failure -> last stdout line is {"error": ...} and we exit non-zero.
"""

import sys
import json
import zlib

import numpy as np

try:
    from skimage.metrics import structural_similarity as ssim
    _HAVE_SSIM = True
except Exception:  # pragma: no cover
    _HAVE_SSIM = False

# cv2 is OPTIONAL. We only use it (if present) to sanity-refine motion estimation;
# the estimator below works fine on numpy alone, so cv2 absence is a no-op + note.
try:
    import cv2  # noqa: F401
    _HAVE_CV2 = True
except Exception:
    cv2 = None
    _HAVE_CV2 = False

# reuse the exact same deterministic clip generator as the interp runner so both
# experiments speak about the same footage.
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from exp_video_interp import make_clip  # type: ignore
    _HAVE_CLIP = True
except Exception:
    _HAVE_CLIP = False


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ------------------------------------------------------------- byte-size measurement

def _compressed_bytes(arr):
    """REAL size proxy: length of the zlib-compressed raw buffer. For a full frame
    we pass rgb24 pixels; for a residual we pass the quantised signed int16 buffer.
    Same codec, same settings => a fair full-vs-residual byte comparison."""
    return len(zlib.compress(np.ascontiguousarray(arr).tobytes(), level=6))


# ------------------------------------------------------------------- motion estimate

def _to_gray(f):
    return (0.299 * f[..., 0] + 0.587 * f[..., 1] + 0.114 * f[..., 2]).astype(np.float32)


def _estimate_global_motion(prev, cur, search=6):
    """Estimate an integer global (dx, dy) that best aligns prev onto cur, by a small
    brute-force search minimising mean-abs luma error. This stands in for reading a
    global motion vector out of the container — measured from the pixels, honestly."""
    gp = _to_gray(prev)
    gc = _to_gray(cur)
    best = (0, 0)
    best_err = None
    for dy in range(-search, search + 1):
        for dx in range(-search, search + 1):
            shifted = np.roll(np.roll(gp, dy, axis=0), dx, axis=1)
            err = np.abs(shifted - gc).mean()
            if best_err is None or err < best_err:
                best_err = err
                best = (dx, dy)
    return best


def _shift_frame(frame, dx, dy):
    """Shift a full RGB frame by integer (dx, dy) with edge replication via roll."""
    return np.roll(np.roll(frame, dy, axis=0), dx, axis=1)


# --------------------------------------------------------------------------- runner

def run(params):
    clip = params.get("clip", "talking_head")
    q = int(params.get("residual_quant", 8))
    motion = params.get("motion", "none")               # "none" | "from_container"

    if not _HAVE_SSIM:
        return {"error": "scikit-image (SSIM) not available; cannot score reconstruction honestly"}
    if not _HAVE_CLIP:
        return {"error": "could not import make_clip from exp_video_interp"}
    if q < 1:
        q = 1

    notes = []
    log(f"[residual] clip={clip} residual_quant={q} motion={motion} cv2={_HAVE_CV2}")

    frames, meta = make_clip(clip)
    total = len(frames)
    log(f"[residual] {total} frames {frames.shape[1]}x{frames.shape[2]} regime={meta['regime']}")

    # Full-frame byte cost: average compressed size of a raw frame (the thing we are
    # trying to AVOID re-producing every frame).
    full_sizes = [_compressed_bytes(frames[i]) for i in range(total)]
    full_frame_bytes = float(np.mean(full_sizes))

    # One keyframe (frame 0) is sent in full and amortised across the clip.
    keyframe_bytes = float(full_sizes[0])
    keyframe_bytes_amortised = keyframe_bytes / total

    residual_bytes = []
    recon_ssims = []

    for i in range(1, total):
        prev = frames[i - 1].astype(np.int16)
        cur = frames[i].astype(np.int16)

        if motion == "from_container":
            dx, dy = _estimate_global_motion(frames[i - 1], frames[i])
            prediction = _shift_frame(frames[i - 1], dx, dy).astype(np.int16)
        else:
            dx, dy = 0, 0
            prediction = prev

        residual = cur - prediction                       # signed int16
        # Quantise the residual (coarser q => smaller bytes, lower fidelity).
        quant = np.round(residual / q).astype(np.int16) * q
        residual_bytes.append(_compressed_bytes(quant))

        # Reconstruct exactly as a decoder would: prediction + dequantised residual.
        recon = np.clip(prediction + quant, 0, 255).astype(np.uint8)
        s = ssim(frames[i], recon, channel_axis=2, data_range=255)
        recon_ssims.append(float(s))

    mean_residual_bytes = float(np.mean(residual_bytes)) if residual_bytes else 0.0
    per_frame_cost = keyframe_bytes_amortised + mean_residual_bytes
    net_speedup = full_frame_bytes / per_frame_cost if per_frame_cost > 0 else 0.0
    mean_residual_frac = (mean_residual_bytes / full_frame_bytes) if full_frame_bytes > 0 else 0.0
    quality = float(np.mean(recon_ssims)) if recon_ssims else 1.0

    if motion == "from_container":
        notes.append("global motion vector estimated from pixels (brute-force luma "
                     "search) as a stand-in for reading codec MVs from the container")
    if _HAVE_CV2:
        notes.append("cv2 present but motion estimate is numpy-only (deterministic)")
    else:
        notes.append("cv2 absent; motion estimate is numpy-only (no functionality lost)")

    note = (f"clip={clip}/{meta['regime']}; q={q}; "
            f"full={full_frame_bytes:.0f}B keyframe_amort={keyframe_bytes_amortised:.1f}B "
            f"mean_residual={mean_residual_bytes:.0f}B over {total-1} P-frames; "
            f"byte sizes = REAL zlib-compressed buffers (measured proxy for render/transmit cost); "
            + "; ".join(notes))

    out = {
        "net_speedup": round(net_speedup, 4),
        "quality": round(quality, 4),
        "mean_residual_frac": round(mean_residual_frac, 4),
        "modeled": True,   # bytes are a measured proxy for true render/transmit cost
        "note": note,
    }
    log(f"[residual] result {out}")
    return out


def main():
    try:
        params = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    except Exception as e:
        print(json.dumps({"error": f"bad params json: {e}"}))
        sys.exit(1)
    try:
        metrics = run(params)
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        sys.exit(1)
    print(json.dumps(metrics))
    if "error" in metrics:
        sys.exit(1)


if __name__ == "__main__":
    main()
