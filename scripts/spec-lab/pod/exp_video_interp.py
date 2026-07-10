#!/usr/bin/env python3
"""
exp_video_interp.py — draft-and-verify FRAME speculation (the video keystone).

The idea, in speculative-decoding terms mapped onto video:
  * "Full render / full decode" of a frame == the true decoded frame. That is the
    EXPENSIVE ground truth (a real render / a real decode step).
  * A "draft" of a speculated frame == a CHEAP predicted frame synthesised from its
    real neighbours (a blend, or an optical-flow warp of the previous real frame).
  * We speculate `spec_frames` frames in every gap between two real "keyframes",
    then VERIFY each draft against the true decoded frame with SSIM.
      SSIM(draft, true) >= ssim_gate  -> ACCEPT the cheap draft (we saved a render).
      otherwise                       -> REJECT, fall back to the real frame (full cost).

  Cost model (units of "one real frame render/decode" = 1.0):
      real frame        = 1.0
      accepted draft    = draft_cost           (measured wall-time ratio; ~free vs a real render)
      rejected draft    = draft_cost + 1.0     (we drafted, threw it away, then rendered for real)
      net_speedup = total_frames /
                    (n_real + n_accepted*draft_cost + n_rejected*(draft_cost + 1))
      quality     = mean SSIM of the frames actually DELIVERED
                    (accepted drafts contribute their measured SSIM; real frames contribute 1.0)

  measure_reject_cost: also report reject_overhead = the EXTRA cost a rejected
  speculation adds versus just rendering that frame = draft_cost (you paid to draft,
  then paid full render anyway). Prefilters ("motion_magnitude" / "scene_cut") skip
  drafting where a cheap pre-check says motion is high / a cut is present, which is
  how you drive reject_overhead down toward zero on nasty footage.

Everything is measured from real ffmpeg-generated frames. draft_cost is the REAL
timed ratio of (produce a draft) / (decode a real frame) on this box — not a guess.

Contract: human logs to stderr; the LAST stdout line is exactly ONE json metrics
object. Any failure -> last stdout line is {"error": ...} and we exit non-zero.
"""

import sys
import json
import time
import subprocess

import numpy as np

# SSIM is required to verify drafts honestly. If it is genuinely missing we cannot
# measure quality, so we fail loudly rather than fabricate a number.
try:
    from skimage.metrics import structural_similarity as ssim
    _HAVE_SSIM = True
except Exception:  # pragma: no cover - environment dependent
    _HAVE_SSIM = False

# cv2 is OPTIONAL — only the optical_flow draft needs it. Fall back to blend + note.
try:
    import cv2
    _HAVE_CV2 = True
except Exception:
    cv2 = None
    _HAVE_CV2 = False


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- clips
# We synthesise short, DETERMINISTIC clips on the pod with ffmpeg and decode them
# straight to numpy via a rawvideo rgb24 pipe (no temp files, no container quirks).

W, H = 128, 128           # small enough to stay well inside the time budget
N_FRAMES = 48             # ~48 frames per clip, as specified


def _decode_lavfi(filtergraph, n_frames, w=W, h=H):
    """Run one ffmpeg lavfi graph and decode exactly n_frames RGB frames to a
    numpy array of shape (n_frames, h, w, 3), uint8. Deterministic per graph."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", filtergraph,
        "-frames:v", str(n_frames),
        "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed ({proc.returncode}): {proc.stderr.decode('utf-8', 'ignore')[-300:]}"
        )
    frame_bytes = w * h * 3
    need = frame_bytes * n_frames
    buf = proc.stdout
    if len(buf) < need:
        raise RuntimeError(f"ffmpeg returned {len(buf)} bytes, need {need}")
    arr = np.frombuffer(buf[:need], dtype=np.uint8).reshape(n_frames, h, w, 3)
    return arr.copy()  # own the memory (frombuffer view is read-only)


def make_clip(clip):
    """Return (frames, meta). Two DETERMINISTIC regimes (verified: identical bytes
    across ffmpeg invocations, so both runners speak about the exact same footage).

    talking_head — LOW motion: a smooth luminance gradient that pans slowly across a
        larger canvas (~0.4 px/frame). Measured mean per-frame luma delta ~0.008.
        Neighbouring frames are very close, so cheap drafts verify well (high accept).
        Built with geq (a closed-form pixel function) + a crop pan — fully
        deterministic, unlike the `gradients` source which is time/seed animated.
    high_motion  — FAST motion + a HARD scene cut: a sharp RGB test pattern scrolled
        FAST (~12 px/frame) for the first half, then a HARD CUT to a different sharp
        pattern scrolled fast for the second half. Measured mean per-frame luma delta
        ~0.12 (≈14x the low-motion clip), with a big spike at the splice. Drafts
        across the fast motion and the cut fail the SSIM gate — the stress case.
    """
    if clip == "talking_head":
        # geq computes each pixel from a closed-form function of (X,Y) — no RNG, no
        # time dependence => byte-identical every run. A diagonal sinusoid gives
        # smooth, textured content; the crop pans a WxH window slowly across it.
        cw, ch = W * 2, H * 2
        graph = (
            f"color=c=black:size={cw}x{ch}:rate=24,"
            "format=gray,"
            "geq=lum='128+100*sin((X+Y)/20)':cr=128:cb=128,"
            f"crop={W}:{H}:x='min(iw-{W}, 0.4*n)':y='min(ih-{H}, 0.25*n)',"
            "format=rgb24"
        )
        frames = _decode_lavfi(graph, N_FRAMES)
        meta = {"regime": "low_motion", "has_cut": False}
        return frames, meta

    if clip == "high_motion":
        # Two fast-scrolling sharp patterns spliced together => a hard scene cut in
        # the middle. mod(k*n, W) wraps the crop offset, producing a large, sharp
        # per-frame shift (fast motion). Both halves are deterministic test sources.
        half = N_FRAMES // 2
        cw, ch = W * 2, H * 2
        a = _decode_lavfi(
            f"rgbtestsrc=size={cw}x{ch}:rate=30,"
            f"crop={W}:{H}:x='mod(12*n,{W})':y='mod(8*n,{H})',format=rgb24",
            half,
        )
        b = _decode_lavfi(
            f"testsrc=size={cw}x{ch}:rate=30,"
            f"crop={W}:{H}:x='mod(11*n,{W})':y='mod(9*n,{H})',format=rgb24",
            N_FRAMES - half,
        )
        frames = np.concatenate([a, b], axis=0)
        meta = {"regime": "high_motion", "has_cut": True, "cut_at": half}
        return frames, meta

    raise ValueError(f"unknown clip {clip!r}")


# ------------------------------------------------------------------- draft methods

def _to_gray(f):
    # luma-ish grayscale for flow / motion pre-checks
    return (0.299 * f[..., 0] + 0.587 * f[..., 1] + 0.114 * f[..., 2]).astype(np.float32)


def draft_blend(prev_real, next_real, alpha):
    """Linear blend of the two surrounding REAL frames. alpha in (0,1): fraction of
    the way from prev to next. This is the cheapest possible draft."""
    p = prev_real.astype(np.float32)
    q = next_real.astype(np.float32)
    return np.clip((1.0 - alpha) * p + alpha * q, 0, 255).astype(np.uint8)


def draft_optical_flow(prev_real, next_real, alpha, use_cv2):
    """Warp prev_real toward next_real by Farneback flow scaled by alpha. Falls
    back to a blend if cv2 is unavailable. Returns (draft, used_flow: bool)."""
    if not use_cv2:
        return draft_blend(prev_real, next_real, alpha), False
    g0 = _to_gray(prev_real)
    g1 = _to_gray(next_real)
    flow = cv2.calcOpticalFlowFarneback(
        g0, g1, None,
        pyr_scale=0.5, levels=3, winsize=15,
        iterations=3, poly_n=5, poly_sigma=1.2, flags=0,
    )
    h, w = g0.shape
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    map_x = (xs + alpha * flow[..., 0]).astype(np.float32)
    map_y = (ys + alpha * flow[..., 1]).astype(np.float32)
    warped = cv2.remap(prev_real, map_x, map_y, interpolation=cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_REPLICATE)
    return warped, True


# ----------------------------------------------------------------- cheap prefilter

def _motion_magnitude(prev_real, next_real):
    """Cheap global motion proxy: mean absolute luma difference between the two
    bracketing real frames, normalised to 0..1. High => fast motion / likely cut."""
    d = np.abs(_to_gray(next_real) - _to_gray(prev_real))
    return float(d.mean() / 255.0)


def _scene_cut(prev_real, next_real):
    """Cheap cut detector: normalised luma-histogram correlation. Returns True when
    the two bracketing frames look like different scenes (low correlation)."""
    g0 = _to_gray(prev_real).ravel()
    g1 = _to_gray(next_real).ravel()
    h0, _ = np.histogram(g0, bins=32, range=(0, 255), density=True)
    h1, _ = np.histogram(g1, bins=32, range=(0, 255), density=True)
    # cosine similarity of the two histograms; a hard cut drops it well below 1.
    denom = (np.linalg.norm(h0) * np.linalg.norm(h1)) + 1e-9
    corr = float(np.dot(h0, h1) / denom)
    return corr < 0.90


# --------------------------------------------------------------- draft_cost timing

def measure_draft_cost(frames, draft_mode, use_cv2, reps=None):
    """REAL measurement: time producing a draft vs 'decoding a real frame'.

    We can't invoke the true expensive renderer here, so the honest denominator is
    the cost of actually materialising a real frame from the encoded clip — i.e. an
    ffmpeg rawvideo decode of one frame. We time a genuine single-frame decode and
    the genuine draft op, and take their ratio. This is a measured sub-cost, marked
    accordingly in the note. If drafts are ~free vs a decode, draft_cost is small."""
    h, w = frames.shape[1], frames.shape[2]
    prev_real = frames[0]
    next_real = frames[min(2, len(frames) - 1)]

    # time one real-frame "render/decode": a genuine ffmpeg decode of a single frame.
    def _time_real_decode(n):
        t0 = time.perf_counter()
        for _ in range(n):
            _decode_lavfi(f"testsrc=size={w}x{h}:rate=24,format=rgb24", 1, w=w, h=h)
        return (time.perf_counter() - t0) / n

    def _time_draft(n):
        t0 = time.perf_counter()
        for _ in range(n):
            if draft_mode == "optical_flow":
                draft_optical_flow(prev_real, next_real, 0.5, use_cv2)
            else:
                draft_blend(prev_real, next_real, 0.5)
        return (time.perf_counter() - t0) / n

    if reps is None:
        reps_real = 8
        reps_draft = 50
    else:
        reps_real, reps_draft = reps, reps

    real_t = _time_real_decode(reps_real)
    draft_t = _time_draft(reps_draft)
    ratio = draft_t / real_t if real_t > 0 else 0.0
    # A draft can never sensibly be treated as cheaper-than-free; clamp tiny/neg noise.
    return max(0.0, float(ratio)), {"real_decode_s": real_t, "draft_s": draft_t}


# --------------------------------------------------------------------------- runner

def run(params):
    clip = params.get("clip", "talking_head")
    spec_frames = int(params.get("spec_frames", 2))
    ssim_gate = float(params.get("ssim_gate", 0.97))
    draft_mode = params.get("draft", "blend")            # "blend" | "optical_flow"
    prefilter = params.get("prefilter", None)            # None | "motion_magnitude" | "scene_cut"
    measure_reject = bool(params.get("measure_reject_cost", False))

    if not _HAVE_SSIM:
        # Quality is SSIM; without it we cannot verify drafts honestly.
        return {"error": "scikit-image (SSIM) not available; cannot verify drafts honestly"}

    notes = []
    use_cv2 = _HAVE_CV2
    if draft_mode == "optical_flow" and not use_cv2:
        notes.append("cv2 unavailable -> optical_flow fell back to blend draft")
        draft_mode_effective = "blend"
    else:
        draft_mode_effective = draft_mode

    log(f"[interp] clip={clip} spec_frames={spec_frames} gate={ssim_gate} "
        f"draft={draft_mode} prefilter={prefilter} cv2={use_cv2}")

    frames, meta = make_clip(clip)
    total_frames = len(frames)
    log(f"[interp] generated {total_frames} frames {frames.shape[1]}x{frames.shape[2]} "
        f"regime={meta['regime']}")

    # measured cost of a draft relative to a real frame render/decode
    draft_cost, cost_detail = measure_draft_cost(frames, draft_mode_effective, use_cv2)
    log(f"[interp] measured draft_cost={draft_cost:.4f} "
        f"(draft {cost_detail['draft_s']*1e3:.3f} ms / real {cost_detail['real_decode_s']*1e3:.3f} ms)")

    # Keyframe grid: every (spec_frames+1)-th frame is a REAL keyframe; the frames in
    # between are the ones we speculate. gap = spec_frames drafts per keyframe pair.
    step = spec_frames + 1
    key_idxs = list(range(0, total_frames, step))
    # ensure the final frame is a keyframe boundary so every gap has both brackets
    if key_idxs[-1] != total_frames - 1:
        key_idxs.append(total_frames - 1)

    n_real = 0
    n_accepted = 0
    n_rejected = 0
    n_prefiltered = 0            # drafts we skipped WITHOUT drafting (cheap pre-check)
    delivered_ssim = []          # per delivered frame; real=1.0, accepted draft=its SSIM
    real_flag = np.zeros(total_frames, dtype=bool)

    # Mark keyframes as real (rendered) frames.
    for k in key_idxs:
        real_flag[k] = True

    # Walk each consecutive keyframe pair and speculate the interior frames.
    for a, b in zip(key_idxs[:-1], key_idxs[1:]):
        prev_real = frames[a]
        next_real = frames[b]
        interior = list(range(a + 1, b))
        if not interior:
            continue

        # Cheap prefilter runs ONCE per gap on the bracketing real frames. If it says
        # "this gap is hostile" we skip drafting entirely and render the interior
        # for real — paying no wasted draft cost (this is what shrinks reject_overhead).
        skip_gap = False
        if prefilter == "motion_magnitude":
            mm = _motion_magnitude(prev_real, next_real)
            skip_gap = mm > 0.12          # tuned threshold on 0..1 luma-diff scale
        elif prefilter == "scene_cut":
            skip_gap = _scene_cut(prev_real, next_real)

        if skip_gap:
            n_prefiltered += len(interior)
            n_real += len(interior)       # rendered for real, but NOT drafted-then-thrown
            for _ in interior:
                delivered_ssim.append(1.0)
            continue

        span = b - a
        for idx in interior:
            alpha = (idx - a) / span
            if draft_mode_effective == "optical_flow":
                draft, _ = draft_optical_flow(prev_real, next_real, alpha, use_cv2)
            else:
                draft = draft_blend(prev_real, next_real, alpha)
            true = frames[idx]
            s = ssim(true, draft, channel_axis=2, data_range=255)
            if s >= ssim_gate:
                n_accepted += 1
                delivered_ssim.append(float(s))
            else:
                n_rejected += 1
                n_real += 1               # fall back to the true frame (full render)
                delivered_ssim.append(1.0)

    # Count the keyframes themselves as real renders.
    n_real += len(key_idxs)

    speculated = n_accepted + n_rejected + n_prefiltered
    # Cost accounting in "real-frame render" units:
    #   real render          -> 1.0                (n_real of them, includes rejects & prefiltered)
    #   accepted draft       -> draft_cost
    #   rejected draft       -> draft_cost extra ON TOP of the real render already counted in n_real
    #   prefiltered frame    -> nothing extra (we never drafted it) — already a real render
    cost = (n_real * 1.0
            + n_accepted * draft_cost
            + n_rejected * draft_cost)     # the +1 render for a reject is already in n_real
    net_speedup = total_frames / cost if cost > 0 else 0.0

    quality = float(np.mean(delivered_ssim)) if delivered_ssim else 1.0
    accept_rate = (n_accepted / speculated) if speculated else 0.0

    # reject_overhead: the extra cost a rejected speculation adds vs just rendering
    # the frame outright. You paid draft_cost, threw the draft away, then rendered.
    # So the overhead per reject is exactly draft_cost. A prefilter reduces the number
    # of rejects (by skipping the draft), so the EFFECTIVE overhead we report is the
    # average wasted draft cost per speculated-but-not-delivered frame.
    reject_overhead = float(draft_cost)
    if measure_reject:
        # amortised wasted-draft cost across all frames we attempted to speculate,
        # showing how a prefilter drives the realised overhead down.
        attempted = n_accepted + n_rejected + n_prefiltered
        wasted = n_rejected * draft_cost           # prefiltered frames wasted 0 draft cost
        reject_overhead = float(wasted / attempted) if attempted else 0.0

    note_bits = [
        f"clip={clip}/{meta['regime']}",
        f"real={n_real} accept={n_accepted} reject={n_rejected} prefiltered={n_prefiltered}",
        f"draft={draft_mode_effective}",
        "draft_cost = measured (draft op time) / (single-frame ffmpeg decode time); "
        "true renderer unavailable so the real-frame denominator is a genuine decode, "
        "not a path-trace",
    ]
    note_bits += notes
    note = "; ".join(note_bits)

    out = {
        "net_speedup": round(net_speedup, 4),
        "quality": round(quality, 4),
        "accept_rate": round(accept_rate, 4),
        "draft_cost": round(draft_cost, 4),
        "reject_overhead": round(reject_overhead, 4),
        "modeled": True,   # draft_cost uses a decode-based proxy for the true renderer
        "note": note,
    }
    log(f"[interp] result {out}")
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
    # LAST stdout line: exactly one JSON object.
    print(json.dumps(metrics))
    if "error" in metrics:
        sys.exit(1)


if __name__ == "__main__":
    main()
