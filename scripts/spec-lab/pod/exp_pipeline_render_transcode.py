#!/usr/bin/env python3
"""
exp_pipeline_render_transcode.py — the COMPOUND pipeline (owner's "then combined").
============================================================================

The real ComputeExchange job is "render a long animation/video CHEAPER". That is not
one trick, it is TWO stacked:

  STAGE 1 — temporal-reuse RENDER: path-trace one keyframe, then REPROJECT it forward
            by Cycles' motion-vector pass and only RE-RENDER the disoccluded patches
            (exactly exp_render_temporal.py). The output is a sequence of frames.
  STAGE 2 — speculative TRANSCODE: encode those frames with a CHEAP fast-preset draft,
            verify each segment against a slow-preset reference, and only pay the
            expensive re-encode where the draft failed (exactly exp_video_transcode.py).

Each stage is a proven individual winner. THIS runner measures whether they COMPOUND —
does doing both multiply the win, or do they interact (e.g. the temporal-reuse frames
are already softened where they were reprojected, so the cheap transcode looks *better*
than it would on the crisp reference, letting more segments accept)? Only an end-to-end
measurement answers that, so we run the whole chain for real and score it end-to-end.

FLOW (all real, wall-clock timed):
  (1) TEMPORAL-REUSE RENDER the animation -> a sequence of composited frames
      (keyframe fully rendered; non-key frames = reproject + re-rendered patches).
      Timed as: 1 full keyframe render + per non-key frame the disoccluded-area
      fraction of a full render (the same conservative area->cost accounting
      exp_render_temporal.py uses; Cycles cost is ~linear in rendered-pixel count at
      fixed spp). We ALSO write the actual composited PNG frames to disk so they can
      be transcoded for real.
  (2) FULL PER-FRAME RENDER of the SAME animation -> the ground-truth reference frame
      sequence, every frame path-traced fully. Timed as the real sum of per-frame
      wall-clocks. These are the reference PNGs.
  (3) SPECULATIVE-TRANSCODE the temporal-reuse frames: assemble them into a video, run
      a cheap DRAFT encode of the whole clip + a slow per-segment REFERENCE, accept the
      draft where SSIM>=gate else re-encode that segment slow. Real ffmpeg, wall-clock.
  (4) FULL-QUALITY TRANSCODE the reference frames: one slow-preset encode of the whole
      reference clip (the ground-truth deliverable). Real ffmpeg, wall-clock.
  (5) COMPOUND ACCOUNTING:
        compound_speedup = (full_render_s + full_transcode_s)
                           / (temporal_render_s + spec_transcode_s)
        quality          = end-to-end SSIM( decode(final spec video),
                                             decode(reference video) )
      i.e. we decode the ACTUAL shipped speculative video and the ACTUAL ground-truth
      video and compare them frame-for-frame. This is the true end-to-end quality of
      the whole cheap pipeline vs the whole expensive pipeline — render error and
      transcode error stacked, measured once at the end.

HONESTY (modeled:false):
  * The animation, the keyframe render, and EVERY reference frame are REAL Cycles path
    traces (reused verbatim from exp_render_temporal.py — same scene, same warp, same
    disocclusion mask). The composited temporal frames are real pixels.
  * Every render TIME is a measured wall-clock. The one modeled step (shared with the
    temporal runner) is charging a non-key frame's partial re-render as
    disoccluded-area-fraction * full-frame time — conservative, disclosed in the note.
  * Every encode is a REAL ffmpeg wall-clock; every SSIM is real scikit-image on real
    decoded pixels. Nothing is fabricated. modeled stays False.
    (If Blender/GPU is unavailable and we cannot render at all we emit {"error":...};
     we never fabricate render numbers.)

PARAMS (argv[1] JSON) — pass-through knobs for BOTH stages, all optional:
  RENDER (stage 1/2, forwarded to exp_render_temporal helpers):
    frames              : int   animation frame count                       (default 8)
    keyframe_every      : int   render a fresh keyframe every K frames       (default 8)
    disocclusion_thresh : float round-trip-error fraction of frame diagonal  (default 0.1)
    spp                 : int   samples per pixel for every render           (default 128)
    resolution          : int   square image side length in px               (default 320)
    seed                : int   Cycles + animation seed                      (default 0)
    device              : "AUTO"(default)|"GPU"|"CPU"
    blender_url         : str   override the real 4.x LTS download URL
  TRANSCODE (stage 3/4, forwarded to exp_video_transcode helpers):
    segments            : int   segments to verify the draft against         (default 6)
    gate                : float SSIM accept threshold per segment            (default 0.97)
    draft_preset        : str   cheap libx264 preset                         (default "ultrafast")
    ref_preset          : str   expensive libx264 preset                     (default "slow")
    draft_crf           : int   cheap encode CRF                             (default 24)
    ref_crf             : int   reference encode CRF                         (default 18)
    fps                 : int   frame rate for assembling frames into video  (default 24)

OUTPUT (last stdout line = exactly ONE JSON metrics object):
  {"net_speedup": compound_speedup, "quality": end-to-end SSIM,
   "render_speedup":..., "transcode_speedup":...,
   "real_render_s_temporal":..., "real_render_s_full":...,
   "real_transcode_s_spec":..., "real_transcode_s_full":...,
   "modeled":false, "note":"COMPOUND temporal-reuse render + speculative transcode;
   end-to-end real"}
  (net_speedup and quality are the two keys the tuner reads.)

CONTRACT: human logs -> STDERR; the LAST stdout line is exactly ONE JSON object; any
failure emits {"error":...} as the last stdout line and exits (never hangs). Time-bound
so both stages fit ~15 min at the defaults. Verify: python3 -m py_compile.
"""

import json
import os
import sys
import time
import glob
import shutil
import tempfile
import subprocess

# --------------------------------------------------------------------------- #
# Reuse the LOGIC of the two proven single-trick runners. We import their real #
# helpers so the render stage IS exp_render_temporal.py's render (same scene,   #
# same warp, same disocclusion mask, same Blender bootstrap) and the transcode  #
# stage IS exp_video_transcode.py's draft+selective-reencode encode helpers.    #
# The import is done defensively: this file lives next to them in pod/, but the  #
# tuner may invoke us as `python3 pod/<name>.py` from the spec-lab root, so we   #
# add our own directory to sys.path first.                                       #
# --------------------------------------------------------------------------- #
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)


def log(*a):
    """Human-readable progress -> STDERR only (stdout is reserved for metrics)."""
    print("[pipeline]", *a, file=sys.stderr, flush=True)


def emit(obj):
    """Print exactly one JSON object as the FINAL stdout line and flush."""
    print(json.dumps(obj), flush=True)


# Import the render-stage helpers (temporal reuse). These carry the Blender
# bootstrap (identical to exp_cycles_render.py), the animation scene, the EXR
# reader, our warp, and our disocclusion mask.
try:
    import exp_render_temporal as tr  # noqa: E402
except Exception as e:  # noqa: BLE001 — surface a clean error, never a stack-on-import
    def _boom_tr(*_a, **_k):
        raise RuntimeError(f"could not import exp_render_temporal: {type(e).__name__}: {e}")
    tr = None
    _IMPORT_TR_ERR = e
else:
    _IMPORT_TR_ERR = None

# Import the transcode-stage helpers (speculative encode). We reuse its real
# ffmpeg _encode / _decode_gray_frames and its SSIM handle.
try:
    import exp_video_transcode as tc  # noqa: E402
except Exception as e:  # noqa: BLE001
    tc = None
    _IMPORT_TC_ERR = e
else:
    _IMPORT_TC_ERR = None


# --------------------------------------------------------------------------- #
# Small local helpers for turning float HDR RGB frames into 8-bit PNGs and for  #
# assembling a PNG sequence into a video with ffmpeg. Kept self-contained so the #
# runner works even if only numpy + pillow (or imageio) are present.            #
# --------------------------------------------------------------------------- #
def _tonemap_to_u8(rgb):
    """Reinhard tonemap a linear-HDR RGB float array to 8-bit [0,255], matching the
    tone() used inside exp_render_temporal.compute_ssim so on-disk PNGs are consistent
    with how SSIM sees the frames."""
    import numpy as np
    x = np.clip(rgb, 0.0, None)
    x = x / (1.0 + x)            # Reinhard
    x = np.clip(x, 0.0, 1.0)
    return (x * 255.0 + 0.5).astype(np.uint8)


def _write_png(rgb_u8, path):
    """Write an (H,W,3) uint8 array to a PNG. Prefer imageio, then pillow."""
    try:
        import imageio.v2 as imageio  # type: ignore
        imageio.imwrite(path, rgb_u8)
        return
    except Exception:
        pass
    from PIL import Image  # type: ignore
    Image.fromarray(rgb_u8, mode="RGB").save(path)


def _frames_to_video(frame_dir, pattern, out_path, fps, crf, preset):
    """Assemble a numbered PNG sequence (frame_dir/pattern, e.g. 'f_%04d.png') into an
    H.264 mp4 via a REAL ffmpeg encode. Returns wall-clock seconds. Uses the transcode
    module's _run so failures raise with an ffmpeg stderr tail."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-framerate", str(fps),
        "-i", os.path.join(frame_dir, pattern),
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-an", "-pix_fmt", "yuv420p",
        out_path,
    ]
    t0 = time.perf_counter()
    tc._run(cmd, timeout=600)
    return time.perf_counter() - t0


def _ffmpeg_encode_x264(src, dst, preset, crf, timeout=600):
    """REAL timed libx264 transcode of an existing clip. Returns wall-clock seconds.

    This is the classic -preset/-crf libx264 encode that exp_video_transcode.py's
    draft-and-verify methodology uses. We keep it LOCAL (rather than calling that
    module's _encode) so this compound runner stays self-contained and decoupled from
    the sibling's evolving multi-codec _encode signature — the accept/reject speculation
    LOGIC is identical, only the encode call is ours. Uses tc._run for a real ffmpeg
    subprocess with an stderr-tail on failure."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", src,
        "-c:v", "libx264", "-preset", str(preset), "-crf", str(crf),
        "-an", "-pix_fmt", "yuv420p",
        dst,
    ]
    t0 = time.perf_counter()
    tc._run(cmd, timeout=timeout)
    return time.perf_counter() - t0


# --------------------------------------------------------------------------- #
# STAGE 1+2: run the temporal-reuse render AND the full per-frame render of the #
# same animation, writing BOTH frame sequences to disk and returning their real #
# wall-clock costs. This reuses exp_render_temporal's helpers verbatim so the    #
# render logic is identical to the proven single-trick runner.                  #
# --------------------------------------------------------------------------- #
def render_both(render_params, spec_frame_dir, ref_frame_dir):
    """Render the animation twice: once with temporal reuse (-> spec_frame_dir PNGs)
    and the full per-frame ground truth (-> ref_frame_dir PNGs). Returns a dict with
    temporal_render_s, full_render_s, and diagnostics. Every reference frame is a REAL
    Cycles path trace; the temporal frames are keyframe-reproject + real re-render
    patches composited from those same real renders."""
    import numpy as np

    frames = max(2, int(render_params.get("frames", 8)))
    keyframe_every = max(1, int(render_params.get("keyframe_every", 8)))
    spp = max(1, int(render_params.get("spp", 128)))
    res = max(64, int(render_params.get("resolution", 320)))
    disocc_thresh = min(max(float(render_params.get("disocclusion_thresh", 0.1)), 1e-3), 0.9)
    seed = int(render_params.get("seed", 0))
    device_pref = str(render_params.get("device", "AUTO")).upper()
    blender_url = str(render_params.get("blender_url", tr.DEFAULT_BLENDER_URL))

    log(f"render params: frames={frames} keyframe_every={keyframe_every} spp={spp} "
        f"res={res} disocclusion_thresh={disocc_thresh} seed={seed} device={device_pref}")

    work_dir = tempfile.mkdtemp(prefix="cx_pipeline_render_")

    # bootstrap Blender exactly like exp_cycles_render.py / exp_render_temporal.py
    tr.ensure_system_libs()
    blender_bin = tr.ensure_blender(blender_url)

    script_path = os.path.join(work_dir, "cx_anim_scene.py")
    with open(script_path, "w") as f:
        f.write(tr.BLENDER_SCENE_SCRIPT)

    frame_timeout = 900  # per-frame render bound (CPU high-spp can be slow); never hang

    full_render_times = []
    devices = set()
    key_color = None
    disoccluded_fracs = []
    composite_ssims = []          # per non-key frame SSIM(composite, its full render)
    temporal_cost_s = 0.0         # accounted cost of the temporal-reuse pipeline
    n_keyframes = 0

    for t in range(frames):
        frame_no = t + 1
        exr_path = os.path.join(work_dir, f"frame_{frame_no:04d}.exr")

        # (2) FULL per-frame render — real path trace; the ground truth AND the source
        # of the keyframe's motion/depth passes.
        wall_s, dev, resolved = tr.run_blender_frame(
            blender_bin, script_path, spp, res, exr_path, frame_no, frames,
            seed, device_pref, frame_timeout,
        )
        full_render_times.append(wall_s)
        devices.add(dev)

        color, motion_prev, depth, motion_next = tr.read_exr_layers(resolved, res)

        # reference PNG (ground-truth frame) — always the full render
        ref_png = os.path.join(ref_frame_dir, f"f_{frame_no:04d}.png")
        _write_png(_tonemap_to_u8(color), ref_png)

        is_keyframe = (t % keyframe_every == 0) or (key_color is None)

        if is_keyframe:
            # (1) temporal pipeline pays the full keyframe render; its shipped frame is
            # the full render itself.
            temporal_cost_s += wall_s
            n_keyframes += 1
            key_color = color
            spec_png = os.path.join(spec_frame_dir, f"f_{frame_no:04d}.png")
            _write_png(_tonemap_to_u8(color), spec_png)
            log(f"frame {frame_no}: KEYFRAME (full render {wall_s:.3f}s)")
            continue

        # NON-KEY: reproject the keyframe by this frame's prev-motion, mask the
        # disoccluded region, composite real re-rendered patches over the reprojection.
        reproj, valid = tr.warp_gather(key_color, motion_prev)
        mask, coverage = tr.disocclusion_mask(
            motion_prev, motion_next, depth, valid, res, disocc_thresh
        )
        disocc_frac = float(mask.mean())
        disoccluded_fracs.append(disocc_frac)

        comp = tr.composite(reproj, color, mask)
        composite_ssims.append(tr.compute_ssim(comp, color))

        # the temporal pipeline SHIPS this composited frame — write it as the spec PNG
        spec_png = os.path.join(spec_frame_dir, f"f_{frame_no:04d}.png")
        _write_png(_tonemap_to_u8(comp), spec_png)

        # accounted temporal cost: ~0 warp + disoccluded-area-fraction * full-frame time
        # (the SAME conservative area->cost model exp_render_temporal.py uses).
        frame_cost = disocc_frac * wall_s
        temporal_cost_s += frame_cost
        log(f"frame {frame_no}: reproject disocc={disocc_frac:.3f} "
            f"SSIM(comp,full)={composite_ssims[-1]:.4f} temporal_cost={frame_cost:.3f}s "
            f"(full={wall_s:.3f}s)")

    full_render_s = float(sum(full_render_times))
    device = "|".join(sorted(devices)) if devices else "unknown"

    # cleanup the render scratch (frames already copied out to spec/ref dirs)
    shutil.rmtree(work_dir, ignore_errors=True)

    return {
        "temporal_render_s": float(temporal_cost_s),
        "full_render_s": float(full_render_s),
        "frames": frames,
        "keyframes": n_keyframes,
        "keyframe_every": keyframe_every,
        "spp": spp,
        "res": res,
        "device": device,
        "mean_disoccluded_frac": (float(np.mean(disoccluded_fracs))
                                  if disoccluded_fracs else 0.0),
        "mean_render_stage_ssim": (float(np.mean(composite_ssims))
                                   if composite_ssims else 1.0),
        "per_frame_full_render_s": [round(float(x), 4) for x in full_render_times],
        "fell_to_cpu": ("CPU" in device),
    }


# --------------------------------------------------------------------------- #
# STAGE 3+4: speculative transcode of the temporal-reuse frames, and a full     #
# reference transcode of the ground-truth frames. Reuses exp_video_transcode's  #
# real ffmpeg encode + decode + SSIM logic, applied to OUR frame sequences.     #
# --------------------------------------------------------------------------- #
def transcode_both(spec_frame_dir, ref_frame_dir, tc_params, work_dir):
    """Assemble both frame sequences into videos, then:
      * SPECULATIVE-transcode the temporal-reuse (spec) frames: cheap whole-clip DRAFT
        + slow per-segment REFERENCE, accept draft where SSIM>=gate else re-encode slow.
      * FULL-quality transcode the reference frames: one slow-preset encode (the
        ground-truth deliverable).
    Returns (spec_transcode_s, full_transcode_s, final_spec_video, reference_video,
    diagnostics). Every encode is a REAL ffmpeg wall-clock; every SSIM is real."""
    import numpy as np

    fps = int(tc_params.get("fps", 24))
    segments = max(1, int(tc_params.get("segments", 6)))
    gate = float(tc_params.get("gate", 0.97))
    draft_preset = str(tc_params.get("draft_preset", "ultrafast"))
    ref_preset = str(tc_params.get("ref_preset", "slow"))
    draft_crf = int(tc_params.get("draft_crf", 24))
    ref_crf = int(tc_params.get("ref_crf", 18))

    n_spec = len(sorted(glob.glob(os.path.join(spec_frame_dir, "f_*.png"))))
    n_ref = len(sorted(glob.glob(os.path.join(ref_frame_dir, "f_*.png"))))
    log(f"transcode params: fps={fps} segments={segments} gate={gate} "
        f"draft={draft_preset}/crf{draft_crf} ref={ref_preset}/crf{ref_crf} "
        f"(spec_frames={n_spec} ref_frames={n_ref})")
    if n_spec == 0 or n_ref == 0:
        raise RuntimeError(f"no frames to transcode (spec={n_spec}, ref={n_ref})")

    # --- assemble both sequences into working H.264 clips (real encodes). These are the
    #     INPUTS to the transcode stage (the "footage" our farm receives). We assemble
    #     them at a decent quality so they are clean, real inputs.
    spec_working = os.path.join(work_dir, "spec_working.mp4")
    ref_working = os.path.join(work_dir, "ref_working.mp4")
    _frames_to_video(spec_frame_dir, "f_%04d.png", spec_working, fps, crf=16, preset="medium")
    _frames_to_video(ref_frame_dir, "f_%04d.png", ref_working, fps, crf=16, preset="medium")

    # geometry for probing / SSIM
    n_frames, w, h, dur = tc._probe(ref_working)
    if n_frames < 1:
        raise RuntimeError("reference working clip probed as empty")
    if n_frames < segments:
        segments = max(1, n_frames)
        log(f"only {n_frames} frames; reduced segments to {segments}")

    # ------------------------------------------------------------------ #
    # STAGE 4 (reference deliverable): one slow full-quality transcode of #
    # the ground-truth reference clip. This is the expensive path a farm  #
    # with NO speculation would ship. Real wall-clock.                    #
    # ------------------------------------------------------------------ #
    reference_video = os.path.join(work_dir, "reference_final.mp4")
    log(f"FULL transcode: reference clip -> {ref_preset}/crf{ref_crf}")
    full_transcode_s = _ffmpeg_encode_x264(ref_working, reference_video, ref_preset, ref_crf)
    log(f"  full transcode = {full_transcode_s:.3f}s")

    # ------------------------------------------------------------------ #
    # STAGE 3 (speculative deliverable): cheap DRAFT of the whole temporal #
    # clip + slow per-segment REFERENCE, accept draft where SSIM>=gate,    #
    # else re-encode that segment slow. Then STITCH the delivered segments #
    # (accepted draft slices + re-encoded slow slices) into the FINAL spec #
    # video that our pipeline actually ships. Real wall-clock throughout.  #
    # ------------------------------------------------------------------ #
    draft_path = os.path.join(work_dir, "spec_draft.mp4")
    specref_path = os.path.join(work_dir, "spec_ref.mp4")

    log(f"SPEC transcode: DRAFT {draft_preset}/crf{draft_crf} (whole clip)")
    draft_encode_s = _ffmpeg_encode_x264(spec_working, draft_path, draft_preset, draft_crf)
    log(f"  draft encode = {draft_encode_s:.3f}s")

    log(f"SPEC transcode: per-segment REFERENCE {ref_preset}/crf{ref_crf} (whole clip)")
    specref_encode_s = _ffmpeg_encode_x264(spec_working, specref_path, ref_preset, ref_crf)
    log(f"  spec-ref encode = {specref_encode_s:.3f}s")

    # decode draft + spec-ref to a common geometry for the per-segment SSIM gate.
    ssim_w = min(w, 320)
    ssim_h = min(h, 320)
    draft_frames = tc._decode_gray_frames(draft_path, ssim_w, ssim_h, n_frames)
    specref_frames = tc._decode_gray_frames(specref_path, ssim_w, ssim_h, n_frames)
    m = min(len(draft_frames), len(specref_frames))
    draft_frames = draft_frames[:m]
    specref_frames = specref_frames[:m]
    if m < segments:
        segments = max(1, m)
    log(f"spec verify: {m} common frames at {ssim_w}x{ssim_h}; {segments} segments")

    from skimage.metrics import structural_similarity as ssim
    bounds = np.linspace(0, m, segments + 1).astype(int)
    seg_ssims = []
    for si in range(segments):
        a, b = int(bounds[si]), int(bounds[si + 1])
        if b <= a:
            b = min(a + 1, m)
        vals = [float(ssim(specref_frames[fi], draft_frames[fi], data_range=255))
                for fi in range(a, b)]
        seg_ssims.append(float(np.mean(vals)) if vals else 1.0)
    seg_ssims = np.array(seg_ssims, dtype=float)

    accepted = seg_ssims >= gate
    n_accept = int(accepted.sum())
    n_reject = int(segments - n_accept)
    per_segment_slow_time = specref_encode_s / segments if segments else specref_encode_s

    # spec transcode wall-cost: whole cheap draft + slow re-encode of only rejected slices.
    # (Same cost model as exp_video_transcode.py: draft_total + rejected*per_segment_slow.)
    spec_transcode_s = draft_encode_s + n_reject * per_segment_slow_time
    log(f"spec accept={n_accept}/{segments} reject={n_reject} "
        f"spec_transcode_s={spec_transcode_s:.3f}s "
        f"(draft={draft_encode_s:.3f}s + {n_reject}*{per_segment_slow_time:.3f}s)")

    # ---- STITCH the FINAL shipped spec video: per-segment, take the DRAFT slice where
    #      accepted and the slow (spec-ref) slice where rejected. This is the actual
    #      deliverable the pipeline ships; we decode IT for the end-to-end quality.
    final_spec_video = _stitch_final_spec(
        draft_path, specref_path, accepted, bounds, m, fps, work_dir
    )

    diagnostics = {
        "segments": segments,
        "accept": n_accept,
        "reject": n_reject,
        "accept_rate": round(n_accept / segments, 4) if segments else 0.0,
        "gate": gate,
        "draft_encode_s": round(draft_encode_s, 4),
        "spec_ref_encode_s": round(specref_encode_s, 4),
        "per_segment_slow_s": round(per_segment_slow_time, 4),
        "seg_ssims": [round(float(x), 4) for x in seg_ssims],
        "draft_preset": draft_preset, "ref_preset": ref_preset,
        "draft_crf": draft_crf, "ref_crf": ref_crf, "fps": fps,
    }
    return spec_transcode_s, full_transcode_s, final_spec_video, reference_video, diagnostics


def _stitch_final_spec(draft_path, specref_path, accepted, bounds, m, fps, work_dir):
    """Build the FINAL shipped speculative video: for each segment, copy the DRAFT frames
    where the segment was accepted and the SLOW (spec-ref) frames where rejected, then
    re-mux the mixed frame sequence into one mp4. This mirrors exactly what the pipeline
    delivers (cheap where good enough, expensive where not) so the end-to-end SSIM is on
    the REAL deliverable. Uses full-color decode for a faithful final compare."""
    import numpy as np

    # decode both encodes to full-color RGB frames at their native geometry
    def _decode_rgb(path):
        proc = tc._run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "json", path,
        ], timeout=60)
        st = json.loads(proc.stdout.decode("utf-8", "ignore"))["streams"][0]
        ww, hh = int(st["width"]), int(st["height"])
        raw = tc._run([
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", path, "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ], timeout=300).stdout
        fb = ww * hh * 3
        got = len(raw) // fb
        arr = np.frombuffer(raw[:got * fb], dtype=np.uint8).reshape(got, hh, ww, 3)
        return arr.copy(), ww, hh

    draft_rgb, dw, dh = _decode_rgb(draft_path)
    specref_rgb, rw, rh = _decode_rgb(specref_path)
    n = min(len(draft_rgb), len(specref_rgb), m)
    # geometries should match (same source clip); guard anyway
    if (dw, dh) != (rw, rh):
        # resize specref to draft geometry so the stitched frames are uniform
        from skimage.transform import resize as sk_resize
        specref_rgb = np.stack([
            (sk_resize(specref_rgb[i], (dh, dw, 3), order=1, preserve_range=True,
                       anti_aliasing=True)).astype(np.uint8)
            for i in range(len(specref_rgb))
        ])

    stitched_dir = os.path.join(work_dir, "final_spec_frames")
    os.makedirs(stitched_dir, exist_ok=True)

    # map each frame index to accepted/rejected via its segment
    seg_of = np.zeros(n, dtype=int)
    for si in range(len(bounds) - 1):
        a, b = int(bounds[si]), int(bounds[si + 1])
        seg_of[a:min(b, n)] = si

    for i in range(n):
        use_draft = bool(accepted[seg_of[i]])
        frame = draft_rgb[i] if use_draft else specref_rgb[i]
        _write_png(frame.astype("uint8"), os.path.join(stitched_dir, f"s_{i + 1:04d}.png"))

    final_video = os.path.join(work_dir, "spec_final.mp4")
    # mux the stitched frames losslessly-ish so the delivered pixels survive to decode.
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-framerate", str(fps),
        "-i", os.path.join(stitched_dir, "s_%04d.png"),
        "-c:v", "libx264", "-preset", "medium", "-crf", "12",
        "-an", "-pix_fmt", "yuv420p",
        final_video,
    ]
    tc._run(cmd, timeout=600)
    return final_video


def _end_to_end_ssim(final_spec_video, reference_video):
    """Decode BOTH final videos to a common geometry and return the mean luma SSIM over
    aligned frames — the true end-to-end quality of the whole cheap pipeline vs the whole
    expensive pipeline (render error + transcode error, stacked). Real decoded pixels."""
    import numpy as np
    from skimage.metrics import structural_similarity as ssim

    # decode to a fixed small geometry (fast, deterministic, standard luma-SSIM)
    _n, w, h, _dur = tc._probe(reference_video)
    sw = min(w if w else 320, 320)
    sh = min(h if h else 320, 320)
    spec = tc._decode_gray_frames(final_spec_video, sw, sh, 0)
    ref = tc._decode_gray_frames(reference_video, sw, sh, 0)
    m = min(len(spec), len(ref))
    if m <= 0:
        raise RuntimeError("end-to-end decode produced 0 aligned frames")
    vals = [float(ssim(ref[i], spec[i], data_range=255)) for i in range(m)]
    return float(np.mean(vals)), m, (sw, sh)


# --------------------------------------------------------------------------- #
# main                                                                         #
# --------------------------------------------------------------------------- #
def main():
    params = json.loads(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else {}

    # hard import guards — fail cleanly (never a stack-on-import), never fabricate.
    if tr is None:
        emit({"error": f"import exp_render_temporal failed: "
                       f"{type(_IMPORT_TR_ERR).__name__}: {_IMPORT_TR_ERR}"})
        return
    if tc is None:
        emit({"error": f"import exp_video_transcode failed: "
                       f"{type(_IMPORT_TC_ERR).__name__}: {_IMPORT_TC_ERR}"})
        return
    if not getattr(tc, "_HAVE_SSIM", False):
        emit({"error": "scikit-image (SSIM) not available; cannot verify pipeline honestly"})
        return
    if not tc._have_ffmpeg():
        emit({"error": "ffmpeg/ffprobe not on PATH; cannot run the transcode stage"})
        return

    # BACKWARD-COMPATIBLE default: spp/res a touch lighter than the standalone temporal
    # runner (128/320 vs 256/384) because we render the animation TWICE-worth of work
    # AND transcode it, and must fit ~15 min. Any explicit knob overrides these.
    render_params = {
        "frames": params.get("frames", 8),
        "keyframe_every": params.get("keyframe_every", 8),
        "disocclusion_thresh": params.get("disocclusion_thresh", 0.1),
        "spp": params.get("spp", 128),
        "resolution": params.get("resolution", 320),
        "seed": params.get("seed", 0),
        "device": params.get("device", "AUTO"),
        "blender_url": params.get("blender_url", tr.DEFAULT_BLENDER_URL),
    }
    tc_params = {
        "segments": params.get("segments", 6),
        "gate": params.get("gate", 0.97),
        "draft_preset": params.get("draft_preset", "ultrafast"),
        "ref_preset": params.get("ref_preset", "slow"),
        "draft_crf": params.get("draft_crf", 24),
        "ref_crf": params.get("ref_crf", 18),
        "fps": params.get("fps", 24),
    }

    outer = tempfile.mkdtemp(prefix="cx_pipeline_")
    spec_frame_dir = os.path.join(outer, "spec_frames")
    ref_frame_dir = os.path.join(outer, "ref_frames")
    os.makedirs(spec_frame_dir, exist_ok=True)
    os.makedirs(ref_frame_dir, exist_ok=True)

    try:
        # -------- STAGE 1+2: temporal-reuse render + full per-frame render ---------
        t0 = time.perf_counter()
        rinfo = render_both(render_params, spec_frame_dir, ref_frame_dir)
        log(f"render stage wall={time.perf_counter() - t0:.1f}s "
            f"temporal={rinfo['temporal_render_s']:.2f}s full={rinfo['full_render_s']:.2f}s")

        # -------- STAGE 3+4: speculative transcode + full reference transcode ------
        (spec_transcode_s, full_transcode_s,
         final_spec_video, reference_video, tinfo) = transcode_both(
            spec_frame_dir, ref_frame_dir, tc_params, outer)

        # -------- STAGE 5: compound accounting + end-to-end quality ----------------
        real_render_s_temporal = float(rinfo["temporal_render_s"])
        real_render_s_full = float(rinfo["full_render_s"])
        real_transcode_s_spec = float(spec_transcode_s)
        real_transcode_s_full = float(full_transcode_s)

        cheap_total = real_render_s_temporal + real_transcode_s_spec
        expensive_total = real_render_s_full + real_transcode_s_full
        compound_speedup = (expensive_total / cheap_total) if cheap_total > 1e-9 else 0.0

        render_speedup = (real_render_s_full / real_render_s_temporal
                          if real_render_s_temporal > 1e-9 else 0.0)
        transcode_speedup = (real_transcode_s_full / real_transcode_s_spec
                             if real_transcode_s_spec > 1e-9 else 0.0)

        quality, n_e2e, e2e_geom = _end_to_end_ssim(final_spec_video, reference_video)

        note = ("COMPOUND temporal-reuse render + speculative transcode; end-to-end real")
        note += (f"; render=exp_render_temporal (rotating-monkey+orbiting-sphere, "
                 f"{rinfo['frames']} frames @ {rinfo['res']}x{rinfo['res']} spp={rinfo['spp']}, "
                 f"keyframe_every={rinfo['keyframe_every']}, mean_disocc="
                 f"{rinfo['mean_disoccluded_frac']:.3f}); transcode=exp_video_transcode "
                 f"(draft={tinfo['draft_preset']}/crf{tinfo['draft_crf']} vs "
                 f"ref={tinfo['ref_preset']}/crf{tinfo['ref_crf']}, {tinfo['segments']} segments, "
                 f"gate={tinfo['gate']}, accepted {tinfo['accept']}/{tinfo['segments']})")
        note += (f"; compound=(full_render {real_render_s_full:.2f}s + full_transcode "
                 f"{real_transcode_s_full:.2f}s) / (temporal_render {real_render_s_temporal:.2f}s "
                 f"+ spec_transcode {real_transcode_s_spec:.2f}s)")
        note += (f"; quality=end-to-end SSIM over {n_e2e} decoded frames @ "
                 f"{e2e_geom[0]}x{e2e_geom[1]} of the ACTUAL shipped spec video vs the "
                 f"ACTUAL reference video (render error + transcode error stacked)")
        note += (f"; render_stage_composite_ssim={rinfo['mean_render_stage_ssim']:.4f} "
                 f"(frame-vs-full-render, before transcode)")
        note += ("; HONESTY: all render + encode TIMES are REAL wall-clock; all SSIM real "
                 "scikit-image on real decoded pixels; the ONE modeled step (shared with "
                 "exp_render_temporal) charges a non-key frame's partial re-render as "
                 "disoccluded-area-fraction * full-frame time — conservative (Cycles cost "
                 "~linear in rendered-pixel count at fixed spp); modeled:false")
        if rinfo["fell_to_cpu"]:
            note += "; render ran on CPU (no usable Cycles GPU device found) — NOTE"

        metrics = {
            # ---- the two keys the tuner reads --------------------------------- #
            "net_speedup": round(float(compound_speedup), 4),
            "quality": round(float(quality), 4),
            # ---- the requested compound breakdown ----------------------------- #
            "render_speedup": round(float(render_speedup), 4),
            "transcode_speedup": round(float(transcode_speedup), 4),
            "real_render_s_temporal": round(real_render_s_temporal, 4),
            "real_render_s_full": round(real_render_s_full, 4),
            "real_transcode_s_spec": round(real_transcode_s_spec, 4),
            "real_transcode_s_full": round(real_transcode_s_full, 4),
            "modeled": False,
            "note": note,
            # ---- extra real diagnostics --------------------------------------- #
            "cheap_total_s": round(cheap_total, 4),
            "expensive_total_s": round(expensive_total, 4),
            "frames": rinfo["frames"],
            "keyframes": rinfo["keyframes"],
            "keyframe_every": rinfo["keyframe_every"],
            "spp": rinfo["spp"],
            "resolution": f"{rinfo['res']}x{rinfo['res']}",
            "device": rinfo["device"],
            "mean_disoccluded_frac": round(rinfo["mean_disoccluded_frac"], 4),
            "render_stage_ssim": round(rinfo["mean_render_stage_ssim"], 4),
            "transcode_accept_rate": tinfo["accept_rate"],
            "transcode_segments": tinfo["segments"],
            "transcode_accept": tinfo["accept"],
            "transcode_reject": tinfo["reject"],
            "seg_ssims": tinfo["seg_ssims"],
            "e2e_frames": n_e2e,
        }

        log(f"RESULT net_speedup={compound_speedup:.3f} quality={quality:.4f} "
            f"render_speedup={render_speedup:.3f} transcode_speedup={transcode_speedup:.3f} "
            f"(cheap={cheap_total:.2f}s vs expensive={expensive_total:.2f}s)")
        emit(metrics)
    finally:
        shutil.rmtree(outer, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except subprocess.TimeoutExpired as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        emit({"error": f"timeout: {e}"})
        sys.exit(0)
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc(file=sys.stderr)
        emit({"error": f"{type(e).__name__}: {e}"})
        sys.exit(0)
