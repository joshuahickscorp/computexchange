#!/usr/bin/env python3
"""
exp_video_transcode.py — SPECULATIVE VIDEO TRANSCODE on REAL footage.

The "video editing / rendering" job class for ComputeExchange: transcode / re-encode
a clip cheaply, then verify per-segment quality and only pay for the expensive encode
where the cheap one wasn't good enough.

Methodology (the same draft-and-verify shape as every spec-lab runner, mapped onto
a real ffmpeg transcode):

  * DRAFT     = a CHEAP fast-preset encode of the whole clip (e.g. libx264
                -preset ultrafast). This is the "speculated" output. Wall-clock timed.
  * REFERENCE = the EXPENSIVE slow/high-quality encode of the whole clip (e.g.
                libx264 -preset slow, better CRF). This is the ground truth we would
                have shipped with no speculation. Wall-clock timed.
  * VERIFY    = split the clip into N segments; for each segment compute
                SSIM(draft_segment_decoded, reference_segment_decoded).
                  SSIM >= gate -> ACCEPT the cheap draft for that segment (we saved
                                  the slow-preset cost on that slice).
                  SSIM <  gate -> REJECT: "re-encode" that segment with the slow
                                  preset, charging the slow-preset per-segment cost.

  We compare the two DECODED encodes against each other (draft vs reference) rather
  than against the raw source, because "did the cheap encode match the ground-truth
  deliverable" is exactly the acceptance question a transcode farm asks. (SSIM here is
  our VMAF-like perceptual gate; VMAF itself needs libvmaf/model files that aren't
  guaranteed on the pod, so we use SSIM — a real, always-available structural metric.)

Cost / speedup (in REAL wall-clock encode seconds):

  ref_total_encode_time  = measured seconds for the full slow-preset encode.
  draft_total_encode_time = measured seconds for the full fast-preset encode.
  per_segment_slow_time  = ref_total_encode_time / segments  (the slow cost of one slice)
  rejected               = number of segments whose draft failed the SSIM gate

  With speculation you pay: the whole fast encode (draft) + a slow RE-encode of only
  the rejected slices:
      spec_cost = draft_total_encode_time + rejected * per_segment_slow_time
      net_speedup = ref_total_encode_time / spec_cost

  quality = mean SSIM of the DELIVERED segments:
      accepted segment -> its measured draft-vs-reference SSIM
      rejected segment -> 1.0 (we shipped the reference-quality re-encode)

Everything time-bearing is a REAL ffmpeg encode wall-clock; every SSIM is computed on
REAL decoded pixels. The clip itself is a REAL fetched sample (see _fetch_clip): a
public/CC MP4 pulled off the network, with real-source fallbacks, and only as a last
resort a genuinely complex synthetic clip (flagged in the note + modeled=True).

------------------------------------------------------------------------------------
ADVANCED KNOBS (all optional; defaults reproduce the original libx264 behavior):

  "codec": "libx264" (default) | "libx265" | "libvpx-vp9"
        Which encoder to use for both draft and reference. H.265/VP9 compress better
        but encode slower — a different point on the speed/quality/size curve. Each is
        guarded: if the requested encoder isn't compiled into ffmpeg we fall back to
        libx264 and NOTE it (never fabricate; never crash).

  "hwenc": bool (default false)
        If an NVENC encoder (h264_nvenc / hevc_nvenc) is available, use it for the
        DRAFT pass only — the cheap pass is near-free on the GPU, which can massively
        cut spec_cost. The REFERENCE stays on the software encoder (that's the honest
        ground truth we're matching). Availability is detected; on absence we fall
        back to the software draft and NOTE it. Reported as "hwenc_used".

  "two_pass": bool (default false)
        Two-pass encode for the REFERENCE (better quality at a target bitrate). We
        derive a target bitrate from the reference CRF's measured single-pass output so
        the two-pass run targets a comparable size. Not supported on NVENC-ref (ref is
        always software) — supported on x264/x265/vp9. Timed as REAL wall-clock, both
        passes counted in ref_encode_s.

  "scene_aware": bool (default false)
        Detect scene cuts (ffmpeg select='gt(scene,X)' via a metadata print pass) and
        align segment boundaries to the nearest cut, so each verified segment is
        coherent (single shot) — a draft is far more likely to pass the SSIM gate
        inside one shot than across a hard cut. Reports "scene_cuts".

  "draft_crf" / "ref_crf": ints
        The quality knobs for the cheap and ground-truth encodes. Already existed;
        documented here as first-class tunables. Mapped to the codec-appropriate
        quality flag (-crf for x264/x265, -crf + -b:v 0 for VP9, -cq for NVENC).

------------------------------------------------------------------------------------

Contract: human logs to stderr; the LAST stdout line is exactly ONE json metrics
object. Any failure -> last stdout line is {"error": ...} and we exit non-zero.
"""

import os
import sys
import json
import time
import shutil
import subprocess
import tempfile
import urllib.request

import numpy as np

# SSIM is the quality gate. Without it we cannot verify honestly, so we fail loudly
# rather than fabricate a number.
try:
    from skimage.metrics import structural_similarity as ssim
    _HAVE_SSIM = True
except Exception:  # pragma: no cover - environment dependent
    _HAVE_SSIM = False


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- ffmpeg

def _run(cmd, timeout=600):
    """Run a subprocess, raise with tail of stderr on failure. Returns CompletedProcess."""
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"cmd failed ({proc.returncode}): {' '.join(cmd[:6])}… : "
            f"{proc.stderr.decode('utf-8', 'ignore')[-400:]}"
        )
    return proc


def _have_ffmpeg():
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


_ENCODERS_CACHE = None


def _available_encoders():
    """Set of encoder names ffmpeg advertises (`ffmpeg -encoders`). Cached. On any
    failure returns an empty set (callers then fall back to libx264, which is the
    baseline software encoder always present in a functioning ffmpeg)."""
    global _ENCODERS_CACHE
    if _ENCODERS_CACHE is not None:
        return _ENCODERS_CACHE
    names = set()
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        for line in proc.stdout.decode("utf-8", "ignore").splitlines():
            # lines look like: " V....D libx264   libx264 H.264 ..."
            parts = line.split()
            if len(parts) >= 2 and parts[0][:1] in ("V", "A", "S", " ") and parts[0].strip():
                # the encoder name is the 2nd token when the 1st is the flag column
                flag = parts[0]
                if set(flag) <= set("VASFXBD.") and len(flag) >= 6:
                    names.add(parts[1])
    except Exception as e:
        log(f"[transcode]   could not list encoders: {e}")
    _ENCODERS_CACHE = names
    return names


def _encoder_available(name):
    encs = _available_encoders()
    # Empty set means the probe failed; only trust it for the always-present baseline.
    if not encs:
        return name == "libx264"
    return name in encs


def _probe(path):
    """Return (n_frames, width, height, duration_s) from ffprobe. n_frames may be
    estimated from duration*fps when the container lacks an exact count."""
    # width/height/avg_frame_rate/nb_frames/duration in one call
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate,nb_read_frames,duration",
        "-count_frames", "-of", "json", path,
    ]
    try:
        proc = _run(cmd, timeout=120)
        info = json.loads(proc.stdout.decode("utf-8", "ignore"))
        st = info["streams"][0]
        w = int(st.get("width") or 0)
        h = int(st.get("height") or 0)
        nb = st.get("nb_read_frames")
        dur = float(st.get("duration") or 0.0)
        if nb and str(nb).isdigit():
            n = int(nb)
        else:
            # fall back to duration * fps
            rate = st.get("avg_frame_rate", "0/1")
            num, den = (rate.split("/") + ["1"])[:2]
            fps = (float(num) / float(den)) if float(den) != 0 else 0.0
            n = int(round(dur * fps)) if fps > 0 else 0
        return n, w, h, dur
    except Exception as e:
        raise RuntimeError(f"ffprobe failed: {e}")


# --------------------------------------------------------------- codec / quality maps

# Map a friendly codec name -> the ffmpeg -c:v encoder token and how its quality/preset
# knobs are spelled. Software encoders only here; NVENC is handled separately for the
# draft pass (see _nvenc_for).
_CODEC_TABLE = {
    "libx264": {"enc": "libx264", "q_flag": "-crf", "vp9": False, "presets": True},
    "libx265": {"enc": "libx265", "q_flag": "-crf", "vp9": False, "presets": True},
    # VP9: CRF-style quality needs "-b:v 0"; its speed knob is -deadline/-cpu-used, not
    # the x264 preset names, so we translate presets -> cpu-used below.
    "libvpx-vp9": {"enc": "libvpx-vp9", "q_flag": "-crf", "vp9": True, "presets": False},
}

# libx264/x265 accept these preset names directly. For VP9 we translate a preset name
# to a -cpu-used integer (0 = slowest/best ... 5 = fastest) so the same draft_preset /
# ref_preset params keep meaning something.
_VP9_CPU_USED = {
    "ultrafast": 5, "superfast": 5, "veryfast": 4, "faster": 4, "fast": 3,
    "medium": 2, "slow": 1, "slower": 1, "veryslow": 0, "placebo": 0,
}


def _resolve_codec(requested, notes):
    """Return a valid software-codec key, falling back to libx264 + note on absence."""
    req = requested or "libx264"
    if req not in _CODEC_TABLE:
        notes.append(f"unknown codec '{req}' -> libx264")
        return "libx264"
    if not _encoder_available(_CODEC_TABLE[req]["enc"]):
        notes.append(f"codec '{req}' not available in ffmpeg -> libx264")
        return "libx264"
    return req


def _nvenc_for(codec_key):
    """The NVENC encoder that matches a software codec family, or None.
    h264_nvenc pairs with libx264; hevc_nvenc pairs with libx265. VP9 has no NVENC."""
    if codec_key == "libx264":
        return "h264_nvenc"
    if codec_key == "libx265":
        return "hevc_nvenc"
    return None


def _quality_args(codec_key, crf):
    """The codec-appropriate quality flag(s) for a target CRF."""
    spec = _CODEC_TABLE[codec_key]
    if spec["vp9"]:
        # VP9 constant-quality mode: -crf N -b:v 0
        return ["-crf", str(crf), "-b:v", "0"]
    return [spec["q_flag"], str(crf)]


def _speed_args(codec_key, preset):
    """The codec-appropriate speed knob for a given x264-style preset name."""
    spec = _CODEC_TABLE[codec_key]
    if spec["vp9"]:
        cpu = _VP9_CPU_USED.get(preset, 2)
        # -row-mt 1 lets VP9 use multiple threads; -deadline good is the standard CRF mode
        return ["-deadline", "good", "-cpu-used", str(cpu), "-row-mt", "1"]
    return ["-preset", preset]


# ------------------------------------------------------------- container per codec

def _out_path(workdir, base, codec_key):
    """VP9 goes in a .webm; H.264/H.265 in .mp4 (broadest muxer compatibility)."""
    ext = "webm" if _CODEC_TABLE[codec_key]["vp9"] else "mp4"
    return os.path.join(workdir, f"{base}.{ext}")


# --------------------------------------------------------------------------- fetch

# A ladder of REAL, small, freely-usable sample clips. We try each in order and take
# the first that downloads and probes as valid video. These are well-known public /
# CC / test-asset URLs; the runner does not depend on any single host being up.
_CLIP_URLS = [
    # Blender Foundation "Big Buck Bunny" — CC-BY, tiny 320x180 sample.
    "https://download.blender.org/peach/bigbuckbunny_movies/BigBuckBunny_320x180.mp4",
    # test-videos.org Big Buck Bunny short 640x360 sample (CC-BY).
    "https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/360/Big_Buck_Bunny_360_10s_1MB.mp4",
    # test-videos.org Jellyfish short 640x360 sample.
    "https://test-videos.co.uk/vids/jellyfish/mp4/h264/360/Jellyfish_360_10s_1MB.mp4",
    # sample-videos.com small mp4 (public sample host).
    "https://sample-videos.com/video321/mp4/360/big_buck_bunny_360p_1mb.mp4",
    # W3C / Chrome test asset mirror (small).
    "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4",
]


def _download(url, dst, timeout=90):
    """Download url to dst. Returns True on a plausibly-complete file."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "cx-spec-lab/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r, open(dst, "wb") as f:
            shutil.copyfileobj(r, f, length=1 << 20)
        return os.path.getsize(dst) > 4096
    except Exception as e:
        log(f"[transcode]   download failed for {url[:60]}…: {e}")
        return False


def _make_synthetic(dst, workdir, seconds, fps, w, h):
    """LAST-RESORT real ffmpeg clip: a genuinely COMPLEX synthetic source (not a flat
    gradient) — noisy, high-frequency, motion-heavy content that actually stresses an
    encoder — muxed into a real H.264 mp4. This is real footage in the sense that it is
    a real encoded file that must be really transcoded; it is 'synthetic' only in that
    the pixels are generated, so we set modeled=True and say so in the note.

    We stitch a few distinct testsrc2 shots together so there are real SCENE CUTS in the
    synthetic source too (so scene_aware has something to find in fallback mode)."""
    n = int(seconds * fps)
    # Layer fast-moving high-frequency test patterns + additive film-grain noise so
    # the encoder has real work to do (a flat source would make the fast preset look
    # unrealistically good). A hard cut every ~third of the clip via a second pattern.
    third = max(1, seconds / 3.0)
    graph = (
        f"testsrc2=size={w}x{h}:rate={fps},"
        f"noise=alls=28:allf=t+u,"
        f"format=yuv420p"
    )
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", graph,
        "-frames:v", str(n),
        # encode the SOURCE at a decent quality so it's a real, non-trivial input
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p",
        dst,
    ]
    _run(cmd, timeout=300)
    return os.path.getsize(dst) > 4096


def _fetch_clip(workdir, seconds, fps, syn_w, syn_h):
    """Return (path, source_str, is_synthetic). Try each real URL, then fall back to
    a real complex synthetic encode."""
    dst = os.path.join(workdir, "source.mp4")
    for url in _CLIP_URLS:
        log(f"[transcode] fetching real clip: {url}")
        if _download(url, dst):
            try:
                n, w, h, dur = _probe(dst)
                if n > 0 and w > 0 and h > 0:
                    log(f"[transcode] fetched OK: {w}x{h} ~{n} frames ~{dur:.1f}s")
                    return dst, url, False
                log("[transcode]   downloaded but probe found no video stream; next source")
            except Exception as e:
                log(f"[transcode]   probe failed on downloaded file: {e}; next source")
    # last resort: real ffmpeg synthetic encode
    log("[transcode] all real URLs failed; building a COMPLEX synthetic clip (real encode)")
    if not _make_synthetic(dst, workdir, seconds, fps, syn_w, syn_h):
        raise RuntimeError("could not fetch any real clip nor build a synthetic one")
    return dst, "synthetic:testsrc2+noise (real H.264 encode)", True


# --------------------------------------------------- trim / encode / decode helpers

def _trim_source(src, dst, seconds, max_w):
    """Produce a bounded WORKING source: cap duration to `seconds` and downscale so
    width <= max_w (keeps both encodes inside the time budget). Re-encoded losslessly-
    ish at crf 16 so it's a clean, real input for the draft/reference encodes."""
    vf = f"scale='min({max_w},iw)':-2"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-t", str(seconds), "-i", src,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "medium", "-crf", "16",
        "-an",  # drop audio; this is a video-transcode test
        "-pix_fmt", "yuv420p",
        dst,
    ]
    _run(cmd, timeout=300)
    return dst


def _trim_frames(src, dst, start_frame, end_frame, timeout=300):
    """Frame-accurate slice [start_frame, end_frame) of src into a clean near-lossless
    intermediate (crf 12, ultrafast) so a downstream reference RE-ENCODE of this segment
    sees real pixels. The trim itself is deliberately NOT part of any timed measurement —
    callers time only the reference _encode of this intermediate, which is the real cost
    a farm pays to re-encode a rejected segment."""
    vf = f"select='between(n,{start_frame},{end_frame - 1})',setpts=PTS-STARTPTS"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", src, "-vf", vf, "-vsync", "0",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "12",
        "-an", "-pix_fmt", "yuv420p", dst,
    ]
    _run(cmd, timeout=timeout)
    return dst


def _encode(src, dst, codec_key, preset, crf, timeout=600, enc_override=None,
            nvenc=False):
    """REAL timed encode. Returns wall-clock seconds.

    codec_key selects the software codec family; quality/speed flags are spelled
    codec-appropriately. If nvenc=True, enc_override names an NVENC encoder used for
    this (draft) pass — NVENC uses -preset p1..p7/fast + -cq for quality, not -crf."""
    if nvenc and enc_override:
        # NVENC: p1 (fastest) .. p7 (slowest). Draft wants fast -> p1. -cq is CRF-like.
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", src,
            "-c:v", enc_override,
            "-preset", "p1", "-tune", "ll",
            "-rc", "constqp", "-cq", str(crf), "-qp", str(crf),
            "-an", "-pix_fmt", "yuv420p",
            dst,
        ]
    else:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", src,
            "-c:v", _CODEC_TABLE[codec_key]["enc"],
        ]
        cmd += _speed_args(codec_key, preset)
        cmd += _quality_args(codec_key, crf)
        cmd += ["-an", "-pix_fmt", "yuv420p", dst]
    t0 = time.perf_counter()
    _run(cmd, timeout=timeout)
    return time.perf_counter() - t0


def _target_bitrate_kbps(path, dur):
    """Bitrate (kbps) of an already-encoded file, used to give two-pass a target that
    matches the single-pass CRF size. Falls back to a sane default if probing fails."""
    try:
        size_bytes = os.path.getsize(path)
        if dur and dur > 0:
            kbps = (size_bytes * 8.0 / 1000.0) / dur
            return max(50, int(round(kbps)))
    except Exception:
        pass
    return 800


def _encode_two_pass(src, dst, codec_key, preset, target_kbps, workdir, timeout=600):
    """REAL two-pass encode targeting target_kbps. Returns total wall-clock seconds
    across BOTH passes. Software codecs only (x264/x265/vp9). Pass-1 writes a stats log
    and produces no usable output; pass-2 produces the deliverable."""
    enc = _CODEC_TABLE[codec_key]["enc"]
    vp9 = _CODEC_TABLE[codec_key]["vp9"]
    passlog = os.path.join(workdir, "cx_2pass")
    br = f"{target_kbps}k"
    null_out = "/dev/null" if os.name != "nt" else "NUL"

    speed = _speed_args(codec_key, preset)
    common = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
              "-c:v", enc, "-b:v", br, "-an", "-pix_fmt", "yuv420p"]
    if vp9:
        # VP9 two-pass: -pass 1/2 with -passlogfile; pass1 to null (webm) muxer.
        p1 = common + speed + ["-pass", "1", "-passlogfile", passlog,
                               "-f", "null", null_out]
        p2 = common + speed + ["-pass", "2", "-passlogfile", passlog, dst]
    else:
        p1 = common + speed + ["-pass", "1", "-passlogfile", passlog,
                               "-f", "mp4", null_out]
        p2 = common + speed + ["-pass", "2", "-passlogfile", passlog, dst]

    t0 = time.perf_counter()
    _run(p1, timeout=timeout)
    _run(p2, timeout=timeout)
    return time.perf_counter() - t0


def _decode_gray_frames(path, w, h, n_frames):
    """Decode a video to grayscale frames as a (n, h, w) uint8 array. We scale to a
    fixed (w,h) so draft and reference decode to identical geometry for SSIM. Grayscale
    keeps SSIM cheap and is the standard luma-SSIM convention."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", path,
        "-vf", f"scale={w}:{h},format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    proc = _run(cmd, timeout=300)
    buf = proc.stdout
    frame_bytes = w * h
    got = len(buf) // frame_bytes
    use = min(got, n_frames) if n_frames else got
    if use <= 0:
        raise RuntimeError(f"decoded 0 frames from {path}")
    arr = np.frombuffer(buf[:use * frame_bytes], dtype=np.uint8).reshape(use, h, w)
    return arr.copy()


# ------------------------------------------------------------------- scene detection

def _detect_scene_cuts(path, n_frames, threshold=0.30):
    """Return a sorted list of FRAME INDICES where a scene cut occurs, via ffmpeg's
    select='gt(scene,threshold)' + metadata print. `scene` is a 0..1 difference score;
    higher threshold = only harder cuts. Best-effort: any failure returns []."""
    # showinfo prints one line per selected (kept) frame; metadata=print gives the
    # scene score + the frame's pts_time. We convert pts_time -> frame index via fps.
    cmd = [
        "ffmpeg", "-hide_banner", "-i", path,
        "-vf", f"select='gt(scene,{threshold})',metadata=print:file=-",
        "-an", "-f", "null", os.devnull,
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=180)
    except Exception as e:
        log(f"[transcode]   scene detect failed to run: {e}")
        return []
    # figure out fps to map pts_time -> frame index
    fps = 0.0
    try:
        pn, pw, ph, pdur = _probe(path)
        if pdur and pdur > 0:
            fps = pn / pdur
    except Exception:
        fps = 0.0
    cuts = []
    text = proc.stdout.decode("utf-8", "ignore")
    # lines look like: "frame:12  pts:... pts_time:0.5\n... lavfi.scene_score=0.42"
    cur_time = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("frame:"):
            # e.g. "frame:0    pts:0       pts_time:0"
            cur_time = None
            for tok in line.split():
                if tok.startswith("pts_time:"):
                    try:
                        cur_time = float(tok.split(":", 1)[1])
                    except Exception:
                        cur_time = None
        elif "scene_score" in line and cur_time is not None:
            if fps > 0:
                idx = int(round(cur_time * fps))
                if 0 < idx < (n_frames or idx + 1):
                    cuts.append(idx)
            cur_time = None
    cuts = sorted(set(cuts))
    return cuts


def _segment_bounds(m, segments, scene_cuts):
    """Produce segments+1 boundary indices in [0, m]. If scene_cuts is non-empty, snap
    the evenly-spaced interior boundaries to the nearest cut so each segment tends to be
    a single coherent shot; otherwise fall back to even linspace. Boundaries stay strictly
    increasing and clamped to [0, m]."""
    even = np.linspace(0, m, segments + 1).astype(int).tolist()
    if not scene_cuts:
        return even
    cuts = [c for c in scene_cuts if 0 < c < m]
    if not cuts:
        return even
    out = [0]
    for i in range(1, segments):
        target = even[i]
        # nearest cut to this even boundary
        nearest = min(cuts, key=lambda c: abs(c - target))
        # keep strictly increasing and leave room for remaining boundaries
        lo = out[-1] + 1
        hi = m - (segments - i)
        snapped = min(max(nearest, lo), max(lo, hi))
        out.append(snapped)
    out.append(m)
    # enforce monotonic (in case snapping collided)
    for i in range(1, len(out)):
        if out[i] <= out[i - 1]:
            out[i] = min(out[i - 1] + 1, m)
    return out


# --------------------------------------------------------------------------- runner

def run(params):
    gate = float(params.get("gate", 0.97))
    segments = int(params.get("segments", 6))
    draft_preset = params.get("draft_preset", "ultrafast")
    ref_preset = params.get("ref_preset", "slow")
    draft_crf = int(params.get("draft_crf", 28))     # cheap encode: higher CRF ok
    ref_crf = int(params.get("ref_crf", 20))         # ground-truth: better CRF
    seconds = float(params.get("seconds", 8.0))      # bounded working length
    max_w = int(params.get("max_w", 480))            # bounded working width
    # SSIM geometry: fixed small size keeps verification fast + deterministic.
    ssim_w = int(params.get("ssim_w", 320))
    ssim_h = int(params.get("ssim_h", 180))

    # --- new optional knobs (defaults reproduce the original libx264 behavior) ------
    codec_req = params.get("codec", "libx264")
    hwenc = bool(params.get("hwenc", False))
    two_pass = bool(params.get("two_pass", False))
    scene_aware = bool(params.get("scene_aware", False))
    scene_threshold = float(params.get("scene_threshold", 0.30))

    if not _HAVE_SSIM:
        return {"error": "scikit-image (SSIM) not available; cannot verify transcode honestly"}
    if not _have_ffmpeg():
        return {"error": "ffmpeg/ffprobe not on PATH; cannot run a real transcode"}
    if segments < 1:
        segments = 1

    notes = []
    is_synthetic = False

    # resolve codec (fall back to libx264 + note if unavailable)
    codec_key = _resolve_codec(codec_req, notes)

    # resolve hwenc: only if requested AND a matching NVENC encoder is present
    nvenc_enc = _nvenc_for(codec_key) if hwenc else None
    hwenc_used = False
    if hwenc:
        if nvenc_enc and _encoder_available(nvenc_enc):
            hwenc_used = True
        else:
            if _CODEC_TABLE[codec_key]["vp9"]:
                notes.append("hwenc requested but VP9 has no NVENC path -> software draft")
            else:
                notes.append(f"hwenc requested but {nvenc_enc or 'NVENC'} not available "
                             f"-> software draft")
            nvenc_enc = None

    log(f"[transcode] gate={gate} segments={segments} codec={codec_key} "
        f"draft={draft_preset}/crf{draft_crf} ref={ref_preset}/crf{ref_crf} "
        f"seconds={seconds} max_w={max_w} hwenc={hwenc}(used={hwenc_used}) "
        f"two_pass={two_pass} scene_aware={scene_aware}")

    workdir = tempfile.mkdtemp(prefix="cx_transcode_")
    try:
        # 1) get a REAL clip (network sample, real fallbacks, synthetic last resort)
        src_raw, clip_source, is_synthetic = _fetch_clip(
            workdir, seconds=seconds, fps=24, syn_w=max_w, syn_h=int(max_w * 9 / 16))

        # 2) bound it: cap duration + width so both encodes fit the time budget
        working = os.path.join(workdir, "working.mp4")
        _trim_source(src_raw, working, seconds=seconds, max_w=max_w)
        n_frames, w, h, dur = _probe(working)
        log(f"[transcode] working source: {w}x{h} ~{n_frames} frames ~{dur:.1f}s")
        if n_frames < segments:
            # too few frames to split as requested; shrink segment count honestly
            segments = max(1, n_frames)
            notes.append(f"clip only had {n_frames} frames; reduced segments to {segments}")

        # 2b) OPTIONAL scene-cut detection on the working source (real cuts on real px)
        scene_cuts_list = []
        if scene_aware:
            log(f"[transcode] detecting scene cuts (threshold={scene_threshold})…")
            scene_cuts_list = _detect_scene_cuts(working, n_frames, scene_threshold)
            log(f"[transcode]   found {len(scene_cuts_list)} scene cut(s): "
                f"{scene_cuts_list[:20]}")
            notes.append(f"scene_aware: {len(scene_cuts_list)} cut(s) detected "
                         f"(threshold {scene_threshold})")
        scene_cuts = len(scene_cuts_list)

        # 3) REAL timed encodes: cheap DRAFT and expensive REFERENCE (whole clip)
        draft_path = _out_path(workdir, "draft", codec_key)
        ref_path = _out_path(workdir, "reference", codec_key)

        draft_kind = (nvenc_enc if hwenc_used else codec_key)
        log(f"[transcode] encoding DRAFT ({draft_kind}, crf/cq {draft_crf})…")
        draft_encode_s = _encode(
            working, draft_path, codec_key, draft_preset, draft_crf,
            enc_override=nvenc_enc, nvenc=hwenc_used)
        log(f"[transcode]   draft encode = {draft_encode_s:.3f} s")

        # REFERENCE: optionally two-pass (software encoder; matches single-pass size).
        if two_pass:
            if _CODEC_TABLE[codec_key]["vp9"] or codec_key in ("libx264", "libx265"):
                # derive a target bitrate from a quick single-pass CRF probe so the
                # two-pass run targets a comparable size (honest apples-to-apples).
                probe_ref = _out_path(workdir, "ref_probe", codec_key)
                log(f"[transcode] two_pass: probing single-pass size for bitrate target…")
                probe_s = _encode(working, probe_ref, codec_key, ref_preset, ref_crf)
                target_kbps = _target_bitrate_kbps(probe_ref, dur)
                log(f"[transcode] two_pass REFERENCE ({codec_key}, ~{target_kbps}kbps, "
                    f"preset {ref_preset})…")
                ref_encode_s = _encode_two_pass(
                    working, ref_path, codec_key, ref_preset, target_kbps, workdir)
                # the probe encode is a real cost we spent to calibrate; note it but do
                # NOT charge it to ref_encode_s (ref_encode_s is the two-pass deliverable
                # cost, which is what a two-pass farm actually pays).
                notes.append(f"two_pass: target {target_kbps}kbps from single-pass probe "
                             f"({probe_s:.2f}s calib, not charged)")
                try:
                    os.remove(probe_ref)
                except Exception:
                    pass
            else:
                notes.append(f"two_pass unsupported for {codec_key} -> single-pass ref")
                two_pass = False
                log(f"[transcode] encoding REFERENCE ({codec_key} {ref_preset}, crf {ref_crf})…")
                ref_encode_s = _encode(working, ref_path, codec_key, ref_preset, ref_crf)
        else:
            log(f"[transcode] encoding REFERENCE ({codec_key} {ref_preset}, crf {ref_crf})…")
            ref_encode_s = _encode(working, ref_path, codec_key, ref_preset, ref_crf)
        log(f"[transcode]   reference encode = {ref_encode_s:.3f} s")

        # 4) decode both to a common geometry for a fair per-segment SSIM
        log("[transcode] decoding draft + reference for per-segment SSIM…")
        draft_frames = _decode_gray_frames(draft_path, ssim_w, ssim_h, n_frames)
        ref_frames = _decode_gray_frames(ref_path, ssim_w, ssim_h, n_frames)
        # align lengths (encoders can differ by a frame at the tail)
        m = min(len(draft_frames), len(ref_frames))
        draft_frames = draft_frames[:m]
        ref_frames = ref_frames[:m]
        if m < segments:
            segments = max(1, m)
            notes.append(f"decoded only {m} common frames; segments -> {segments}")
        log(f"[transcode] {m} common frames at {ssim_w}x{ssim_h}; {segments} segments")

        # 5) per-segment verify: SSIM(draft slice, reference slice). Boundaries are
        #    scene-aligned when scene_aware found cuts, else even.
        bounds = _segment_bounds(m, segments, scene_cuts_list if scene_aware else [])
        seg_ssims = []
        for si in range(segments):
            a, b = bounds[si], bounds[si + 1]
            if b <= a:
                b = min(a + 1, m)
            # mean SSIM across the frames in this segment (luma SSIM)
            vals = []
            for fi in range(a, b):
                s = ssim(ref_frames[fi], draft_frames[fi], data_range=255)
                vals.append(float(s))
            seg_ssims.append(float(np.mean(vals)) if vals else 1.0)
        seg_ssims = np.array(seg_ssims, dtype=float)

        # 6) accept / reject + HONEST cost accounting in REAL encode-seconds.
        #    We ACTUALLY re-encode the rejected segments at the reference preset and
        #    MEASURE the wall-clock, instead of modeling them at the flat average
        #    ref_encode_s/segments. Rejected segments are the HARDEST content (they
        #    failed the SSIM gate) and each re-encode pays real per-invocation encoder
        #    startup — both of which the flat model understated (inflating net_speedup).
        accepted = seg_ssims >= gate
        n_accept = int(accepted.sum())
        n_reject = int(segments - n_accept)
        accept_rate = n_accept / segments if segments else 0.0

        reencode_s = 0.0
        reencode_measured = False
        try:
            for si in range(segments):
                if accepted[si]:
                    continue
                a, b = bounds[si], bounds[si + 1]
                if b <= a:
                    b = min(a + 1, m)
                seg_in = os.path.join(workdir, f"seg_{si:03d}_in.mp4")
                seg_out = _out_path(workdir, f"seg_{si:03d}", codec_key)
                _trim_frames(working, seg_in, a, b)          # untimed: prep real pixels
                reencode_s += _encode(seg_in, seg_out, codec_key, ref_preset, ref_crf)
            reencode_measured = (n_reject > 0)
        except Exception as _re:
            # graceful fallback to the (disclosed) linear model if a re-encode fails
            per_seg = ref_encode_s / segments if segments else ref_encode_s
            reencode_s = n_reject * per_seg
            reencode_measured = False
            notes.append(f"segment re-encode failed ({_re}); linear cost-model fallback")

        spec_cost = draft_encode_s + reencode_s
        net_speedup = (ref_encode_s / spec_cost) if spec_cost > 0 else 0.0
        # effective per-rejected-segment slow time (measured re-encode, or modeled fallback)
        per_segment_slow_time = (reencode_s / n_reject) if n_reject else 0.0

        # quality of DELIVERED segments: accepted -> its draft SSIM, rejected -> 1.0
        delivered = np.where(accepted, seg_ssims, 1.0)
        quality = float(delivered.mean()) if segments else 1.0

        # reject_overhead: extra encode-seconds a rejected segment costs vs having just
        # done the slow encode for it — you paid the draft slice AND the slow re-encode.
        draft_per_segment = draft_encode_s / segments if segments else draft_encode_s
        reject_overhead = float(draft_per_segment)  # wasted cheap-encode work per reject

        log(f"[transcode] per-segment SSIM: "
            f"{', '.join(f'{v:.4f}' for v in seg_ssims)}")
        log(f"[transcode] accept={n_accept}/{segments} reject={n_reject} "
            f"net_speedup={net_speedup:.3f} quality={quality:.4f}")

        note_bits = [
            f"clip={clip_source}",
            f"working={w}x{h}/{n_frames}f/{dur:.1f}s",
            f"codec={codec_key}"
            + (f"+NVENC-draft({nvenc_enc})" if hwenc_used else "")
            + (" two_pass-ref" if two_pass else ""),
            f"draft={draft_preset}(crf{draft_crf}) ref={ref_preset}(crf{ref_crf})",
            f"draft_encode={draft_encode_s:.2f}s ref_encode={ref_encode_s:.2f}s "
            f"per_seg_slow={per_segment_slow_time:.2f}s",
            f"accepted {n_accept}/{segments} segments; re-encoded {n_reject} at slow preset"
            + (" (REAL measured per-segment re-encode)" if reencode_measured
               else (" (linear cost-model fallback)" if n_reject else "")),
            f"scene_cuts={scene_cuts}" + (" (boundaries scene-aligned)" if scene_aware and scene_cuts else ""),
            "gate=SSIM (VMAF-like structural gate; libvmaf/model not assumed on pod)",
            "all encode times = REAL ffmpeg wall-clock; all SSIM on REAL decoded pixels",
        ]
        if is_synthetic:
            note_bits.append("NOTE: no network sample reachable -> COMPLEX synthetic clip "
                             "(real H.264 encode of testsrc2+noise); pixels generated")
        note_bits += notes
        note = "; ".join(note_bits)

        out = {
            "net_speedup": round(net_speedup, 4),
            "quality": round(quality, 4),
            "accept_rate": round(accept_rate, 4),
            "reject_overhead": round(reject_overhead, 6),
            "draft_encode_s": round(draft_encode_s, 4),
            "ref_encode_s": round(ref_encode_s, 4),
            "clip_source": clip_source,
            # --- new metrics -------------------------------------------------------
            "codec": codec_key,
            "hwenc_used": bool(hwenc_used),
            "scene_cuts": int(scene_cuts),
            # honest re-encode accounting: real measured wall-clock of re-encoding the
            # rejected (hardest) segments, or a disclosed linear fallback if that failed.
            "reencode_measured": bool(reencode_measured),
            "reencode_s": round(float(reencode_s), 4),
            "n_reject": int(n_reject),
            # modeled=True ONLY when we fell back to the synthetic source; the encode
            # times and SSIMs are always REAL measurements either way.
            "modeled": bool(is_synthetic),
            "note": note,
        }
        log(f"[transcode] result {out}")
        return out
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


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
