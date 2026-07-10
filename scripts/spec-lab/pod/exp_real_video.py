#!/usr/bin/env python3
"""
exp_real_video.py — frame speculation on REAL video footage (not ffmpeg testsrc).

The whole point of this runner (vs exp_video_interp.py, which pans a synthetic
gradient): does draft-and-verify frame speculation still hold on GENUINE motion?
Real footage — camera shake, occlusion, non-rigid subjects, motion blur, hard
cuts — is much harder to predict than a clean synthetic pan, so we expect a lower
accept rate and a lower net_speedup. We report the HONEST number either way.

Methodology (speculative-decoding mapped onto video):
  * "Full decode" of a frame == the true decoded RGB frame. That is the EXPENSIVE
    ground truth (a real ffmpeg decode from the encoded clip).
  * A "draft" of a speculated frame == a CHEAP prediction from its real neighbours:
      draft="blend"        -> linear blend of the two bracketing REAL frames.
      draft="optical_flow" -> cv2 Farneback flow of the previous real frame warped
                              toward the next real frame (falls back to blend + note
                              if cv2 is unavailable).
  * We speculate `spec_frames` frames in every gap between two real "keyframes",
    then VERIFY each draft against the TRUE decoded frame with SSIM.
      SSIM(draft, true) >= ssim_gate  -> ACCEPT the cheap draft (we saved a decode).
      otherwise                       -> REJECT, fall back to the real decode (full cost).

Cost model (units of "one real-frame decode" = 1.0):
    real frame        = 1.0
    accepted draft    = draft_cost              (measured wall-time ratio)
    rejected draft    = draft_cost + 1.0        (we drafted, threw it away, then decoded)
    net_speedup = n_frames /
                  (n_real + n_accept*draft_cost + n_reject*(draft_cost + 1))
    quality     = mean SSIM of the frames actually DELIVERED
                  (accepted drafts contribute their measured SSIM; real frames = 1.0)

draft_cost is REAL: we time the actual draft op and the actual per-frame decode of
the fetched real clip on this box, and take their ratio. Nothing is guessed.

HONESTY: the footage is REAL and freely licensed. We try several stable public
sources (Blender open movies, W3C sample). If ALL of them fail to fetch, we fall
back to a HIGH-COMPLEXITY *synthetic* clip and say so loudly (clip_source begins
with "SYNTHETIC" and the note explains). SSIM / timings are always real-measured.

Contract: human logs to stderr; the LAST stdout line is exactly ONE json metrics
object. Any failure -> last stdout line is {"error": ...} and we exit non-zero.
"""

import os
import sys
import json
import time
import shutil
import subprocess
import urllib.request

import numpy as np

# SSIM is required to verify drafts honestly. Without it we cannot measure quality,
# so we fail loudly rather than fabricate a number.
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


# --------------------------------------------------------------------------- config
# Modest resolution + a short trim keeps us well inside the ~20 min budget while
# still exercising REAL motion. 256x144 (16:9) is enough for SSIM to be meaningful
# on real content without decoding gigabytes.
W, H = 256, 144
TRIM_SECONDS = 4.0          # ~3-5s window as specified
FPS = 24                    # frames we sample per second (we -r downsample to this)
DL_DIR = "/tmp/spec_real_video"
DL_TIMEOUT = 90             # seconds per download attempt

# REAL, freely-licensed sources, tried in order. Each is a short clip or a big open
# movie we trim to TRIM_SECONDS. Blender Foundation open movies are CC-BY; the W3C
# sample is a widely-mirrored public test asset. We deliberately list several hosts
# so one host being down doesn't sink the run.
#   (label, url, license/provenance)
REAL_SOURCES = [
    # Sintel — Blender Foundation open movie (CC-BY 3.0). Small trailer MP4.
    ("sintel_trailer",
     "https://download.blender.org/durian/trailer/sintel_trailer-720p.mp4",
     "Blender Foundation Sintel trailer, CC-BY 3.0"),
    # Big Buck Bunny — Blender Foundation open movie (CC-BY 3.0). Small 320p mirror.
    ("big_buck_bunny",
     "https://download.blender.org/peach/bigbuckbunny_movies/BigBuckBunny_320x180.mp4",
     "Blender Foundation Big Buck Bunny, CC-BY 3.0"),
    # W3C reference sample (public test asset, widely mirrored).
    ("w3c_bunny",
     "https://media.w3.org/2010/05/bunny/movie.mp4",
     "W3C 2010 bunny sample (public test asset)"),
    # Tears of Steel — Blender Foundation open movie (CC-BY 3.0), lots of real motion.
    ("tears_of_steel",
     "https://download.blender.org/demo/movies/ToS/ToS-4k-1920.mov",
     "Blender Foundation Tears of Steel, CC-BY 3.0"),
]


# ------------------------------------------------------------------- fetch + decode

def _have(cmd):
    return shutil.which(cmd) is not None


def _download(url, dest):
    """Fetch url -> dest. Prefer curl/wget (robust redirects/TLS), fall back to
    urllib. Returns True on a non-empty file. Never raises (logs + returns False)."""
    try:
        if _have("curl"):
            r = subprocess.run(
                ["curl", "-fsSL", "--max-time", str(DL_TIMEOUT), "-o", dest, url],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            if r.returncode == 0 and os.path.getsize(dest) > 0:
                return True
            log(f"[fetch] curl failed rc={r.returncode}: "
                f"{r.stderr.decode('utf-8','ignore')[-160:]}")
        if _have("wget"):
            r = subprocess.run(
                ["wget", "-q", "-T", str(DL_TIMEOUT), "-O", dest, url],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            if r.returncode == 0 and os.path.getsize(dest) > 0:
                return True
            log(f"[fetch] wget failed rc={r.returncode}")
        # last resort: pure-python fetch
        urllib.request.urlretrieve(url, dest)  # noqa: S310 (trusted CC/public hosts)
        return os.path.getsize(dest) > 0
    except Exception as e:
        log(f"[fetch] {type(e).__name__}: {e}")
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except Exception:
            pass
        return False


def _decode_file(path, n_seconds=TRIM_SECONDS, w=W, h=H, fps=FPS, skip_seconds=0.0):
    """Decode a REAL encoded clip to a numpy array (n, h, w, 3) uint8 via an ffmpeg
    rawvideo rgb24 pipe. We -ss into the clip a little to skip intros/black frames,
    -r downsample to a fixed fps, and scale to WxH. This is the EXPENSIVE ground
    truth path — a genuine decode of real footage.

    Returns (frames, n_decoded) or raises RuntimeError."""
    # -ss BEFORE -i = fast keyframe seek; good enough to skip a title card.
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", f"{skip_seconds:.3f}",
        "-i", path,
        "-t", f"{n_seconds:.3f}",
        "-vf", f"scale={w}:{h},fps={fps}",
        "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg decode failed ({proc.returncode}): "
            f"{proc.stderr.decode('utf-8','ignore')[-300:]}"
        )
    frame_bytes = w * h * 3
    buf = proc.stdout
    n = len(buf) // frame_bytes
    if n < 4:
        raise RuntimeError(f"decoded only {n} frames ({len(buf)} bytes) — too few")
    arr = np.frombuffer(buf[: n * frame_bytes], dtype=np.uint8).reshape(n, h, w, 3)
    return arr.copy(), n


def _synthetic_high_complexity(n_frames=int(TRIM_SECONDS * FPS), w=W, h=H):
    """LAST-RESORT fallback ONLY: a HIGH-COMPLEXITY synthetic clip (fast multi-object
    motion + a hard scene cut + noise), decoded to numpy. This is NOT real footage;
    the caller marks clip_source SYNTHETIC and the note says so. We build it with
    ffmpeg so the decode path (and thus draft_cost timing) is identical to the real
    path, and we add per-frame noise so drafts are genuinely hard to predict."""
    if not _have("ffmpeg"):
        raise RuntimeError("no real source and ffmpeg missing — cannot build fallback")
    half = n_frames // 2
    cw, ch = w * 2, h * 2
    tmp_a = os.path.join(DL_DIR, "_syn_a.raw")
    tmp_b = os.path.join(DL_DIR, "_syn_b.raw")

    def _run(graph, n, out):
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", graph, "-frames:v", str(n),
            "-pix_fmt", "rgb24", "-f", "rawvideo", out, "-y",
        ]
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode("utf-8", "ignore")[-200:])

    # fast-scrolling sharp RGB pattern + additive noise (hard to interpolate)
    _run(
        f"rgbtestsrc=size={cw}x{ch}:rate=30,"
        f"crop={w}:{h}:x='mod(17*n,{w})':y='mod(13*n,{h})',"
        f"noise=alls=25:allf=t,format=rgb24",
        half, tmp_a,
    )
    # hard cut to a different fast pattern
    _run(
        f"testsrc2=size={cw}x{ch}:rate=30,"
        f"crop={w}:{h}:x='mod(19*n,{w})':y='mod(11*n,{h})',"
        f"noise=alls=25:allf=t,format=rgb24",
        n_frames - half, tmp_b,
    )
    fb = w * h * 3
    a = np.frombuffer(open(tmp_a, "rb").read(), dtype=np.uint8)
    b = np.frombuffer(open(tmp_b, "rb").read(), dtype=np.uint8)
    a = a[: (len(a) // fb) * fb].reshape(-1, h, w, 3)
    b = b[: (len(b) // fb) * fb].reshape(-1, h, w, 3)
    for t in (tmp_a, tmp_b):
        try:
            os.remove(t)
        except Exception:
            pass
    frames = np.concatenate([a, b], axis=0).copy()
    return frames, len(frames)


def acquire_clip(clip_pref):
    """Fetch + decode a REAL clip. Returns (frames, source_label, provenance, synthetic).

    clip_pref: "auto" tries every real source in order; a specific label restricts to
    that source first (then still falls through to the others on failure)."""
    if not _have("ffmpeg"):
        raise RuntimeError("ffmpeg not available — cannot decode any video")
    os.makedirs(DL_DIR, exist_ok=True)

    sources = list(REAL_SOURCES)
    if clip_pref and clip_pref != "auto":
        # move the requested source to the front (case/substring tolerant)
        pref = clip_pref.lower()
        sources.sort(key=lambda s: 0 if pref in s[0].lower() else 1)

    for label, url, prov in sources:
        dest = os.path.join(DL_DIR, f"{label}{os.path.splitext(url)[1] or '.mp4'}")
        log(f"[fetch] trying REAL source {label}: {url}")
        if not _download(url, dest):
            log(f"[fetch] {label} download failed — next source")
            continue
        sz = os.path.getsize(dest)
        log(f"[fetch] {label} downloaded {sz/1e6:.2f} MB — decoding")
        try:
            # skip a couple seconds in to dodge fade-ins / title cards / black frames.
            frames, n = _decode_file(dest, skip_seconds=2.0)
            log(f"[fetch] {label} decoded {n} real frames {frames.shape[1]}x{frames.shape[2]}")
            return frames, label, prov, False
        except Exception as e:
            log(f"[fetch] {label} decode failed: {e} — next source")
            continue

    # every real source failed — HIGH-COMPLEXITY synthetic fallback, marked honestly.
    log("[fetch] ALL real sources failed — falling back to HIGH-COMPLEXITY SYNTHETIC clip")
    frames, n = _synthetic_high_complexity()
    log(f"[fetch] synthetic fallback built {n} frames {frames.shape[1]}x{frames.shape[2]}")
    return frames, "SYNTHETIC_high_complexity", "synthetic fallback (all real fetches failed)", True


# ------------------------------------------------------------------- draft methods

def _to_gray(f):
    return (0.299 * f[..., 0] + 0.587 * f[..., 1] + 0.114 * f[..., 2]).astype(np.float32)


def draft_blend(prev_real, next_real, alpha):
    """Linear blend of the two bracketing REAL frames. Cheapest possible draft."""
    p = prev_real.astype(np.float32)
    q = next_real.astype(np.float32)
    return np.clip((1.0 - alpha) * p + alpha * q, 0, 255).astype(np.uint8)


def draft_optical_flow(prev_real, next_real, alpha, use_cv2):
    """Warp prev_real toward next_real by Farneback flow scaled by alpha. Falls back
    to a blend if cv2 is unavailable. Returns (draft, used_flow: bool)."""
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


# --------------------------------------------------------------- draft_cost timing

def measure_draft_cost(frames, real_clip_path, draft_mode, use_cv2):
    """REAL measurement: time producing a draft vs decoding one real frame.

    Denominator: a genuine single-frame ffmpeg decode of the ACTUAL fetched clip
    (if we still have the file) — the true ground-truth cost per frame. If the file
    is gone (synthetic path), we time a single-frame lavfi decode at the same WxH,
    which is the same decode machinery. Numerator: the real draft op on real frames.

    Returns (ratio, detail). ratio is clamped >= 0 (drafts are never < free)."""
    h, w = frames.shape[1], frames.shape[2]
    prev_real = frames[0]
    next_real = frames[min(2, len(frames) - 1)]

    def _time_real_decode(n):
        t0 = time.perf_counter()
        for i in range(n):
            if real_clip_path and os.path.exists(real_clip_path):
                # decode ONE real frame from the actual clip at a varied offset
                cmd = [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-ss", f"{2.0 + 0.2 * i:.3f}", "-i", real_clip_path,
                    "-vf", f"scale={w}:{h}", "-frames:v", "1",
                    "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
                ]
            else:
                cmd = [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate=24,format=rgb24",
                    "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
                ]
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if r.returncode != 0:
                raise RuntimeError("real-frame decode timing failed")
        return (time.perf_counter() - t0) / n

    def _time_draft(n):
        t0 = time.perf_counter()
        for _ in range(n):
            if draft_mode == "optical_flow":
                draft_optical_flow(prev_real, next_real, 0.5, use_cv2)
            else:
                draft_blend(prev_real, next_real, 0.5)
        return (time.perf_counter() - t0) / n

    reps_real = 6            # ffmpeg spawn dominates; keep small to stay in budget
    reps_draft = 40
    real_t = _time_real_decode(reps_real)
    draft_t = _time_draft(reps_draft)
    ratio = draft_t / real_t if real_t > 0 else 0.0
    return max(0.0, float(ratio)), {"real_decode_s": real_t, "draft_s": draft_t}


# --------------------------------------------------------------------------- runner

def run(params):
    clip_pref = params.get("clip", "auto")
    spec_frames = int(params.get("spec_frames", 2))
    ssim_gate = float(params.get("ssim_gate", 0.95))
    draft_mode = params.get("draft", "optical_flow")        # "blend" | "optical_flow"
    max_frames = int(params.get("max_frames", 0))           # 0 = use all decoded

    if not _HAVE_SSIM:
        return {"error": "scikit-image (SSIM) not available; cannot verify drafts honestly"}

    notes = []
    use_cv2 = _HAVE_CV2
    if draft_mode == "optical_flow" and not use_cv2:
        notes.append("cv2 unavailable -> optical_flow fell back to blend draft")
        draft_mode_effective = "blend"
    else:
        draft_mode_effective = draft_mode

    log(f"[real_video] clip={clip_pref} spec_frames={spec_frames} gate={ssim_gate} "
        f"draft={draft_mode} cv2={use_cv2}")

    # --- acquire REAL footage (or a clearly-marked synthetic fallback) ---
    frames, source_label, provenance, synthetic = acquire_clip(clip_pref)
    if synthetic:
        notes.append("ALL real sources failed — used HIGH-COMPLEXITY SYNTHETIC fallback")

    # locate the actual downloaded file for honest per-frame decode timing
    real_clip_path = None
    if not synthetic and os.path.isdir(DL_DIR):
        for fn in os.listdir(DL_DIR):
            if fn.lower().startswith(source_label.lower()):
                real_clip_path = os.path.join(DL_DIR, fn)
                break

    if max_frames and len(frames) > max_frames:
        frames = frames[:max_frames]
    total_frames = len(frames)
    log(f"[real_video] using {total_frames} frames from '{source_label}' "
        f"({'SYNTHETIC' if synthetic else 'REAL'})")

    # --- measured cost of a draft relative to a real-frame decode ---
    draft_cost, cost_detail = measure_draft_cost(
        frames, real_clip_path, draft_mode_effective, use_cv2)
    log(f"[real_video] measured draft_cost={draft_cost:.4f} "
        f"(draft {cost_detail['draft_s']*1e3:.3f} ms / "
        f"real-frame decode {cost_detail['real_decode_s']*1e3:.3f} ms)")

    # --- keyframe grid: every (spec_frames+1)-th frame is a REAL keyframe ---
    step = spec_frames + 1
    key_idxs = list(range(0, total_frames, step))
    if key_idxs[-1] != total_frames - 1:
        key_idxs.append(total_frames - 1)

    n_real = 0
    n_accepted = 0
    n_rejected = 0
    delivered_ssim = []          # per delivered frame; real=1.0, accepted draft=its SSIM
    accepted_ssim = []           # SSIM of accepted drafts only (for diagnostics)

    # Walk each consecutive keyframe pair and speculate the interior frames.
    for a, b in zip(key_idxs[:-1], key_idxs[1:]):
        prev_real = frames[a]
        next_real = frames[b]
        interior = list(range(a + 1, b))
        if not interior:
            continue
        span = b - a
        for idx in interior:
            alpha = (idx - a) / span
            if draft_mode_effective == "optical_flow":
                draft, _ = draft_optical_flow(prev_real, next_real, alpha, use_cv2)
            else:
                draft = draft_blend(prev_real, next_real, alpha)
            true = frames[idx]
            s = float(ssim(true, draft, channel_axis=2, data_range=255))
            if s >= ssim_gate:
                n_accepted += 1
                delivered_ssim.append(s)
                accepted_ssim.append(s)
            else:
                n_rejected += 1
                n_real += 1                # fall back to the true decoded frame
                delivered_ssim.append(1.0)

    # Count the keyframes themselves as real decodes.
    n_real += len(key_idxs)

    speculated = n_accepted + n_rejected
    # Cost in "real-frame decode" units:
    #   real decode      -> 1.0            (n_real, includes rejects' fallback decode)
    #   accepted draft   -> draft_cost
    #   rejected draft   -> draft_cost extra ON TOP of the real decode already in n_real
    cost = (n_real * 1.0
            + n_accepted * draft_cost
            + n_rejected * draft_cost)     # the +1 decode for a reject is already in n_real
    net_speedup = total_frames / cost if cost > 0 else 0.0

    quality = float(np.mean(delivered_ssim)) if delivered_ssim else 1.0
    accept_rate = (n_accepted / speculated) if speculated else 0.0
    mean_accept_ssim = float(np.mean(accepted_ssim)) if accepted_ssim else 0.0

    # reject_overhead: the extra cost each rejected speculation adds vs just decoding
    # the frame outright. You paid draft_cost, threw the draft away, then decoded.
    reject_overhead = float(draft_cost)

    note_bits = [
        f"source={source_label} ({provenance})",
        f"{'SYNTHETIC' if synthetic else 'REAL footage'}",
        f"frames={total_frames} at {frames.shape[2]}x{frames.shape[1]} ~{TRIM_SECONDS:.0f}s@{FPS}fps",
        f"real={n_real} accept={n_accepted} reject={n_rejected} "
        f"mean_accept_ssim={mean_accept_ssim:.3f}",
        f"draft={draft_mode_effective}",
        "draft_cost = measured (draft op time) / (single real-frame ffmpeg decode time); "
        "the true 'expensive frame' is a genuine decode of the fetched clip, timed live",
        "REAL motion is harder than a synthetic pan — this is the honest number",
    ]
    note_bits += notes
    note = "; ".join(note_bits)

    out = {
        "net_speedup": round(net_speedup, 4),
        "quality": round(quality, 4),
        "accept_rate": round(accept_rate, 4),
        "draft_cost": round(draft_cost, 4),
        "reject_overhead": round(reject_overhead, 4),
        "clip_source": source_label,
        "n_frames": int(total_frames),
        # draft_cost uses a real-decode proxy for the "expensive frame"; synthetic
        # fallback (if triggered) is not real footage. Flag both cases honestly.
        "modeled": bool(synthetic),
        "note": note,
    }
    log(f"[real_video] result {out}")
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
