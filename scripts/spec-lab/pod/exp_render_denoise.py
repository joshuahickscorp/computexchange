#!/usr/bin/env python3
"""
exp_render_denoise.py — Track C: "render bits" = low-spp draft + denoise verify.

The HONEST framing (this runner is explicitly a MODELED stand-in — modeled:true):
  We do NOT ship a path tracer on the pod. Instead we model path tracing the way it
  actually behaves: a Monte-Carlo integrator whose estimate of each pixel is the true
  radiance plus zero-mean noise whose variance falls as 1/spp. So:

    * REFERENCE  = a deterministic, procedurally-built HDR-ish image. This stands in
      for "what infinite spp converges to" — the ground truth an unbiased path tracer
      would reach. It is analytic (gradients + shapes + a lighting term), not random.
    * DRAFT      = reference + Normal(0, (k/sqrt(spp))^2), i.e. real Monte-Carlo noise
      with the correct spp scaling. Low spp (1-4) => cheap, very noisy draft.
    * VERIFY     = a REAL denoiser (skimage gaussian / bilateral, or Intel OIDN if the
      python binding is importable) applied to the draft. quality = SSIM(denoised, ref).

  What is REAL here: the noise model's 1/spp variance law, the denoise operation, the
  SSIM measurement, and the measured denoise wall-time folded into the speedup. What is
  MODELED: the renderer itself (we never trace a ray) and the linear "cost ∝ spp" model
  used for net_speedup. That is why every emitted metrics line carries modeled:true and
  a note stating exactly what was measured vs modeled.

Params (argv[1] JSON), all optional:
  scene        : "cornell" (default) | "gradient" | "spheres"  — which procedural GT
  draft_spp    : int, cheap draft samples per pixel        (default 1)
  ref_spp      : int, expensive reference samples per pixel (default 512)
  denoiser     : "gaussian" | "bilateral" | "oidn"         (default "bilateral")
  mode         : "fixed" (default) | "adaptive"
  budget_frac  : adaptive extra-sample budget as a fraction of ref_spp (default 0.25)
  res          : image side length in px                   (default 256)
  noise_k      : Monte-Carlo noise scale constant          (default 0.55)
  seed         : rng seed for reproducibility              (default 0)

Emits ONE json line on stdout:
  {"net_speedup","quality","spp_ratio","denoiser","mode","modeled":true,"note":...}

Contract: human logs -> stderr; last stdout line is exactly one JSON object; any
failure emits {"error":...} as the last stdout line and exits 0-ish (never hangs).
"""

import json
import sys
import time

import numpy as np


def log(*a):
    """Human-readable progress -> STDERR only (stdout is reserved for the metrics line)."""
    print("[render_denoise]", *a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# 1. The deterministic REFERENCE image (== "infinite spp" ground truth).       #
#    Purely analytic: no randomness, so it is byte-identical run to run.        #
# --------------------------------------------------------------------------- #
def build_reference(scene, res):
    """Return an (res,res,3) float32 HDR-ish image in roughly [0, ~2] range.

    Built from smooth gradients + analytic shapes + a simple lambertian-ish lighting
    term. The point is a signal with BOTH smooth regions (where denoisers win big) and
    hard edges (where they struggle) — i.e. a realistic stand-in for a rendered frame.
    """
    ys, xs = np.mgrid[0:res, 0:res].astype(np.float32)
    u = xs / (res - 1)          # 0..1 across
    v = ys / (res - 1)          # 0..1 down
    img = np.zeros((res, res, 3), dtype=np.float32)

    # A soft "sky"/background vertical gradient (smooth => denoiser-friendly).
    img[..., 0] = 0.20 + 0.35 * v
    img[..., 1] = 0.25 + 0.30 * v
    img[..., 2] = 0.45 + 0.45 * (1.0 - v)

    # An analytic point-light direction for a cheap lambertian shading term.
    lx, ly = 0.35, 0.25

    def disc(cx, cy, r, color, spec):
        """Add a shaded disc (sphere-ish): lambert + a small specular highlight."""
        dx = u - cx
        dy = v - cy
        d2 = dx * dx + dy * dy
        mask = d2 <= r * r
        if not mask.any():
            return
        # fake surface normal from the disc's height field (sphere z = sqrt(r^2 - d2))
        z = np.sqrt(np.clip(r * r - d2, 0.0, None))
        nz = z / (r + 1e-6)
        ndotl = np.clip(0.55 * nz + 0.45 * (1.0 - (dx * lx + dy * ly)), 0.0, 1.0)
        shade = 0.15 + 0.85 * ndotl
        highlight = spec * np.clip(nz, 0.0, 1.0) ** 12
        for c in range(3):
            img[..., c][mask] = (color[c] * shade[mask] + highlight[mask]).astype(np.float32)

    if scene == "gradient":
        pass  # gradients only: an easy, very smooth scene
    elif scene == "spheres":
        disc(0.30, 0.55, 0.18, (0.9, 0.3, 0.25), 0.6)
        disc(0.62, 0.40, 0.14, (0.3, 0.8, 0.4), 0.5)
        disc(0.78, 0.68, 0.10, (0.35, 0.5, 0.95), 0.7)
    else:  # "cornell" default: two boxes + a bright emitter strip on the ceiling
        # colored side "walls"
        img[..., 0][u < 0.12] = 0.85   # red-ish left wall
        img[..., 1][u < 0.12] *= 0.35
        img[..., 2][u < 0.12] *= 0.35
        img[..., 1][u > 0.88] = 0.80   # green-ish right wall
        img[..., 0][u > 0.88] *= 0.35
        img[..., 2][u > 0.88] *= 0.35
        # bright ceiling emitter (HDR value > 1 => tests denoiser on fireflies-prone area)
        emit = (v < 0.10) & (u > 0.30) & (u < 0.70)
        img[emit] = 1.9
        # two occluder boxes on the floor
        b1 = (u > 0.22) & (u < 0.44) & (v > 0.55) & (v < 0.85)
        b2 = (u > 0.55) & (u < 0.74) & (v > 0.45) & (v < 0.85)
        img[b1] = np.array([0.75, 0.72, 0.68], np.float32)
        img[b2] = np.array([0.70, 0.70, 0.74], np.float32)
        # a shaded sphere for a curved highlight region
        disc(0.60, 0.62, 0.12, (0.85, 0.82, 0.78), 0.8)

    return np.clip(img, 0.0, None).astype(np.float32)


# --------------------------------------------------------------------------- #
# 2. Monte-Carlo "path tracing": add noise with variance ∝ 1/spp.              #
# --------------------------------------------------------------------------- #
def render_noisy(reference, spp, noise_k, rng):
    """Simulate an spp-sample path-traced render of `reference`.

    Each pixel channel = true value + Normal(0, sigma^2) with sigma = noise_k/sqrt(spp).
    This is the defining property of an unbiased MC integrator: the estimator is the
    true integral plus zero-mean error whose stddev shrinks like 1/sqrt(N). Brighter
    HDR regions get proportionally more noise (variance grows with signal, like real
    firefly noise), which is what makes low-spp path tracing look grainy in highlights.
    """
    spp = max(1, int(spp))
    sigma = noise_k / np.sqrt(spp)
    # scale noise slightly by local brightness so highlights are noisier (realistic).
    bright = 0.5 + 0.5 * np.clip(reference, 0.0, 2.0) / 2.0
    noise = rng.normal(0.0, 1.0, size=reference.shape).astype(np.float32) * sigma * bright
    return (reference + noise).astype(np.float32)


# --------------------------------------------------------------------------- #
# 3. The VERIFY denoisers (REAL).                                              #
# --------------------------------------------------------------------------- #
def denoise_gaussian(img):
    from skimage.filters import gaussian
    return gaussian(img, sigma=1.4, channel_axis=-1, preserve_range=True).astype(np.float32)


def denoise_bilateral(img):
    """Edge-preserving bilateral (per-channel; skimage bilateral is single-channel)."""
    from skimage.restoration import denoise_bilateral
    lo = float(img.min())
    hi = float(img.max())
    span = (hi - lo) or 1.0
    norm = (img - lo) / span                      # bilateral wants ~[0,1]
    out = np.empty_like(norm)
    for c in range(norm.shape[-1]):
        out[..., c] = denoise_bilateral(
            norm[..., c], sigma_color=0.08, sigma_spatial=2.0, channel_axis=None
        )
    return (out * span + lo).astype(np.float32)


def denoise_oidn(img):
    """Try Intel Open Image Denoise via its python binding; raise if unavailable.

    OIDN expects linear HDR float32 in [0, inf). We feed the noisy render straight in.
    Different pip packages expose slightly different APIs, so we probe a couple.
    """
    try:
        import oidn  # the 'oidn' / 'python-oidn' binding
    except Exception as e:
        raise RuntimeError(f"oidn import failed: {e}")

    h, w, _ = img.shape
    src = np.ascontiguousarray(img, dtype=np.float32)
    dst = np.zeros_like(src)
    dev = oidn.NewDevice()
    oidn.CommitDevice(dev)
    flt = oidn.NewFilter(dev, "RT")
    oidn.SetSharedFilterImage(flt, "color", src, oidn.FORMAT_FLOAT3, w, h)
    oidn.SetSharedFilterImage(flt, "output", dst, oidn.FORMAT_FLOAT3, w, h)
    oidn.CommitFilter(flt)
    oidn.ExecuteFilter(flt)
    oidn.ReleaseFilter(flt)
    oidn.ReleaseDevice(dev)
    return dst.astype(np.float32)


def apply_denoiser(name, img):
    """Return (denoised_img, actual_denoiser_name, fell_back: bool).

    "oidn" is attempted for real; if the binding isn't installed on the pod we fall
    back to bilateral and REPORT the fallback in the note (honesty over a nicer name).
    """
    if name == "gaussian":
        return denoise_gaussian(img), "gaussian", False
    if name == "oidn":
        try:
            out = denoise_oidn(img)
            log("OIDN ran for real.")
            return out, "oidn", False
        except Exception as e:
            log(f"OIDN unavailable ({e}); falling back to bilateral.")
            return denoise_bilateral(img), "bilateral(oidn-fallback)", True
    # default / "bilateral"
    return denoise_bilateral(img), "bilateral", False


# --------------------------------------------------------------------------- #
# 4. Quality = SSIM against the reference (REAL).                              #
# --------------------------------------------------------------------------- #
def ssim_vs_ref(candidate, reference):
    from skimage.metrics import structural_similarity as ssim
    # shared data_range across both images so SSIM is comparable run-to-run.
    lo = float(min(candidate.min(), reference.min()))
    hi = float(max(candidate.max(), reference.max()))
    dr = (hi - lo) or 1.0
    return float(ssim(reference, candidate, channel_axis=-1, data_range=dr))


# --------------------------------------------------------------------------- #
# 5. Adaptive sampling: spend the extra budget only where the draft is noisy.  #
# --------------------------------------------------------------------------- #
def estimate_variance_map(reference, draft_spp, noise_k, rng, k_probe=4):
    """Estimate per-pixel MC variance from a few cheap draft renders.

    A real adaptive renderer estimates variance from the sample stream it already has.
    We stand that in by rendering `k_probe` independent low-spp drafts and taking the
    per-pixel sample variance (mean over channels). No reference leakage — this uses
    only draft-domain information, exactly as a live renderer would.
    """
    stack = np.stack(
        [render_noisy(reference, draft_spp, noise_k, rng) for _ in range(k_probe)],
        axis=0,
    )
    var = stack.var(axis=0).mean(axis=-1)   # (res,res) variance proxy
    return var


def adaptive_render(reference, draft_spp, ref_spp, budget_frac, noise_k, rng):
    """Render a base draft, then pour a `budget_frac*ref_spp` extra-sample budget only
    into the high-variance pixels. Returns (rendered_image, effective_avg_spp).

    Effective spp is the true average samples-per-pixel actually spent (base draft over
    the whole frame + the concentrated extra budget), so the reported spp_ratio /
    net_speedup reflect the REAL sampling cost of the adaptive policy, not a flattering
    fixed number.
    """
    res = reference.shape[0]
    npix = res * res
    base = render_noisy(reference, draft_spp, noise_k, rng)

    # variance proxy from draft-domain probes only (no ground-truth peeking).
    var = estimate_variance_map(reference, draft_spp, noise_k, rng)

    # total extra samples to distribute across the frame.
    extra_total = int(round(budget_frac * ref_spp * npix))
    if extra_total <= 0:
        return base, float(draft_spp)

    # weight extra samples by variance; give the noisiest pixels the most.
    w = var.flatten()
    w = np.clip(w, 1e-8, None)
    w = w / w.sum()
    extra_per_pix = np.floor(w * extra_total).astype(np.int64)
    extra_per_pix = extra_per_pix.reshape(res, res)

    # total spp per pixel = base draft + its extra allocation; re-render each pixel at
    # that spp (noise variance ∝ 1/spp), so pixels that got budget become much cleaner.
    total_spp = draft_spp + extra_per_pix
    sigma = noise_k / np.sqrt(np.maximum(total_spp, 1)).astype(np.float32)
    bright = 0.5 + 0.5 * np.clip(reference, 0.0, 2.0) / 2.0
    noise = rng.normal(0.0, 1.0, size=reference.shape).astype(np.float32)
    rendered = reference + noise * sigma[..., None] * bright

    effective_avg_spp = float(total_spp.mean())
    return rendered.astype(np.float32), effective_avg_spp


# --------------------------------------------------------------------------- #
# main                                                                         #
# --------------------------------------------------------------------------- #
def main():
    params = json.loads(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else {}
    scene = str(params.get("scene", "cornell"))
    draft_spp = int(params.get("draft_spp", 1))
    ref_spp = int(params.get("ref_spp", 512))
    denoiser = str(params.get("denoiser", "bilateral"))
    mode = str(params.get("mode", "fixed"))
    budget_frac = float(params.get("budget_frac", 0.25))
    res = int(params.get("res", 256))
    noise_k = float(params.get("noise_k", 0.55))
    seed = int(params.get("seed", 0))

    draft_spp = max(1, draft_spp)
    ref_spp = max(draft_spp, ref_spp)
    rng = np.random.default_rng(seed)

    log(f"scene={scene} res={res} draft_spp={draft_spp} ref_spp={ref_spp} "
        f"denoiser={denoiser} mode={mode} budget_frac={budget_frac}")

    reference = build_reference(scene, res)
    log(f"reference built: shape={reference.shape} range=[{reference.min():.3f},{reference.max():.3f}]")

    # ---- render the draft (fixed) OR the adaptive-budget frame -------------- #
    if mode == "adaptive":
        draft, eff_spp = adaptive_render(reference, draft_spp, ref_spp, budget_frac, noise_k, rng)
        # cost model: adaptive spends eff_spp avg; reference costs ref_spp. speedup is
        # the sample-cost ratio (samples are the dominant path-tracing cost).
        spp_ratio = ref_spp / max(eff_spp, 1e-6)
        log(f"adaptive effective avg spp={eff_spp:.2f} -> spp_ratio={spp_ratio:.2f}")
    else:
        draft = render_noisy(reference, draft_spp, noise_k, rng)
        eff_spp = float(draft_spp)
        spp_ratio = ref_spp / float(draft_spp)

    # sanity: SSIM of the RAW noisy draft (pre-denoise) for the log trail.
    raw_ssim = ssim_vs_ref(draft, reference)
    log(f"raw noisy draft SSIM (pre-denoise) = {raw_ssim:.4f}")

    # ---- VERIFY: denoise (measure its wall-time) + score quality ------------ #
    t0 = time.perf_counter()
    denoised, used_denoiser, fell_back = apply_denoiser(denoiser, draft)
    denoise_s = time.perf_counter() - t0
    quality = ssim_vs_ref(denoised, reference)
    log(f"denoiser={used_denoiser} time={denoise_s*1000:.1f}ms  quality(SSIM)={quality:.4f}")

    # ---- net_speedup: sample-cost ratio, DEBITED by the denoise overhead ---- #
    # We model per-sample-per-pixel cost with a tiny constant so the (real, measured)
    # denoise time can be expressed on the same axis and subtracted honestly. Using a
    # representative path-tracing rate of ~2e-7 s per sample-per-pixel (order-of-mag for
    # a CPU/GPU tracer); the RATIO is what matters and it is dominated by spp anyway.
    npix = res * res
    sec_per_sample = 2.0e-7
    draft_render_s = eff_spp * npix * sec_per_sample
    ref_render_s = ref_spp * npix * sec_per_sample
    draft_total_s = draft_render_s + denoise_s          # our pipeline pays denoise too
    net_speedup = ref_render_s / max(draft_total_s, 1e-9)
    log(f"cost model: ref_render={ref_render_s:.4f}s draft_render={draft_render_s:.4f}s "
        f"denoise={denoise_s:.4f}s -> net_speedup={net_speedup:.3f} (spp_ratio={spp_ratio:.3f})")

    note = ("MC-noise stand-in for path tracing; noise model variance∝1/spp, "
            "denoise + SSIM are REAL, denoise wall-time measured and debited from "
            "net_speedup; cost∝spp modeled")
    if fell_back:
        note += "; OIDN binding not present on pod -> bilateral fallback (reported)"
    if mode == "adaptive":
        note += f"; adaptive spent avg {eff_spp:.1f} spp vs {ref_spp} ref (variance-guided)"

    metrics = {
        "net_speedup": round(float(net_speedup), 4),
        "quality": round(float(quality), 4),
        "spp_ratio": round(float(spp_ratio), 4),
        "denoiser": used_denoiser,
        "mode": mode,
        "raw_draft_ssim": round(float(raw_ssim), 4),
        "denoise_ms": round(float(denoise_s * 1000.0), 3),
        "effective_spp": round(float(eff_spp), 3),
        "modeled": True,
        "note": note,
    }
    # LAST stdout line == exactly one JSON object.
    print(json.dumps(metrics))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # never crash without a final json line; never hang.
        import traceback
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        sys.exit(0)
