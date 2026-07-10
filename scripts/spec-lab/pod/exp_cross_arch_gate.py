#!/usr/bin/env python3
"""exp_cross_arch_gate.py — CROSS-ARCHITECTURE CONSISTENCY GATE runner (Metal vs CUDA).
================================================================================

WHY THIS EXISTS (Generalization Plan 2026-07-10, objective 2 "Any silicon"):
The CX fleet is Apple Silicon (Cycles/Metal); rented GPUs are CUDA. If a Mac drafts
and a CUDA box verifies — or produces the reference — every delivery-quality claim
must hold ACROSS architectures. Cycles is NOT expected byte-identical across device
kernels; the honest question is HOW CLOSE, per grading tile, and whether the
cross-arch worst tile clears the strict delivery gate (worst-tile SSIM >= 0.95).

THREE-STAGE DESIGN (docs/research/CROSS_ARCH_GATE_DESIGN.md):
  stage 1  mode="self_consistency" (LOCAL Metal, $0, RUN FOR REAL):
           render the SAME tiny reference-recipe config THREE times on one box —
             A  (seed_a)          the canonical render
             A2 (seed_a, again)   a fresh subprocess, byte-identical config+seed
             B  (seed_b)          byte-identical config, different seed
           per-tile SSIM(A,A2) = SAME-SEED determinism (the ceiling);
           per-tile SSIM(A,B)  = CROSS-SEED Monte-Carlo noise floor (the floor).
           Exports the canonical EXR (render A) + a tamper-evident manifest
           (config, config_hash, EXR sha256, producer arch, measured baselines)
           that the CUDA half reproduces bit-for-bit-of-config.
  stage 2  mode="replica" (CUDA pod, fired LATER by the money-safe driver):
           validates the shipped manifest + EXR hashes, re-renders the IDENTICAL
           config (same seed_a) on this box, per-tile SSIM(shipped, replica) =
           the CROSS-ARCH delta; optionally also renders seed_b here for the
           verifier-side cross-seed floor. Emits the stage-3 gate report.
  stage 3  gate_report() (pure, unit-tested): same-arch baseline vs cross-arch
           delta; gate_pass = cross-arch worst-tile >= DELIVERY_WORST_TILE.
           The same-seed baseline is the comparison CEILING, the cross-seed
           baseline the FLOOR: cross_arch below the floor = systematic arch bias
           (worse than reseeding); between floor and ceiling = arches behave like
           different noise realizations of the same estimator.

WHAT IS RENDERED (the canonical, deterministic-config recipe):
  exp_render_stack.py's EXACT reference recipe (run_blender_frame is_ref=True):
  fixed spp, adaptive OFF, denoiser OFF, guides OFF, light-tree left at the
  scene's .blend default, the SAME deterministic camera path. No denoiser =>
  no OIDN-backend confound; what is compared is the path tracer itself.
  The anchor-stack (denoised draft) cross-arch comparison is a named FOLLOW-UP,
  not silently mixed into this gate.

HONESTY CONTRACT (same as exp_render_stack.py / exp_reference_consistency_probe.py):
  * Human logs -> STDERR; the LAST stdout line is exactly ONE JSON object.
  * Any failure emits {"error": ...} as the last stdout line and exits 0 —
    never fabricate a number.
  * require_gpu is fail-loud (functional 64x64@1spp probe, CPU devices disabled,
    every render refuses CPU fallback) — reused verbatim from exp_render_stack.
  * Every SSIM / wall / hash value is MEASURED on real pixels. modeled=false.
  * The cross-arch comparison is meaningful ONLY because the replica config is
    BUILT FROM the manifest by the same code path that built the producer's
    config (render_kwargs_from_config), and the manifest is hash-validated
    (config_hash + EXR sha256) before a cent is spent.

CONFIG (argv[1] JSON):
  shared: mode="self_consistency"|"replica", device="AUTO", require_gpu=false,
          gpu_probe_timeout_s=300, blender_url=<4.2 LTS>.
  self_consistency: scene="classroom", resolution="960x540", frame=1, nframes=2,
          ref_spp=512, bounces=12, cam_motion=1.0, seed_a=0, seed_b=12345,
          export=true, export_root=<EXPORT_ROOT>/<config_hash>.
  replica: manifest_path (required), exr_path (default: manifest sibling),
          render_cross_seed_baseline=true.
"""

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time

# The stack runner + the sibling probes live in this same pod/ directory (the driver
# scp's the whole directory); we REUSE exp_render_stack for the Blender bootstrap /
# scene cache / deterministic camera path / money-safe render driver / EXR reader /
# grading-grid tiling + SSIM, exp_cross_denoiser_probe for the unit-tested tile
# flatten/rank math, and exp_reference_consistency_probe for the seed-parity assert —
# verbatim, so this gate adds no divergent copy.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import exp_render_stack as ers  # noqa: E402
import exp_cross_denoiser_probe as xdp  # noqa: E402
import exp_reference_consistency_probe as xrc  # noqa: E402

WORK_DIR = "/tmp/cross_arch_gate"
# Where stage 1 writes the canonical EXR + manifest. Pod default matches the driver's
# scp layout; the LOCAL shim patches this to ~/.cache/cx-spec-lab/cross_arch_export.
EXPORT_ROOT = "/root/spec-lab/cross_arch_export"

# The strict delivery gate. SINGLE source of truth is
# cx_integrated_speculation.DELIVERY_WORST_TILE (= 0.95); that module lives one
# directory up and is NOT scp'd into pod/, so we mirror the literal here and the
# LOCAL unit test asserts this mirror still equals the real constant.
DELIVERY_WORST_TILE = 0.95

MANIFEST_KIND = "cx_cross_arch_manifest"
MANIFEST_VERSION = 1
CANONICAL_EXR_NAME = "canonical_ref.exr"
# The COMPLETE set of knobs that determine the canonical render (given the pinned
# Blender build + the pinned embedded scene script). Everything else (paths, device,
# timeouts) is execution plumbing that must NOT enter the hash.
CANONICAL_CONFIG_KEYS = (
    "scene", "resolution", "frame", "nframes", "ref_spp", "bounces",
    "cam_motion", "seed_a", "seed_b",
)


def log(*a):
    """Human-readable progress -> STDERR only (stdout is reserved for the JSON line)."""
    print("[cross_arch_gate]", *a, file=sys.stderr, flush=True)


def emit(obj):
    """Print exactly one JSON object as the FINAL stdout line and flush."""
    print(json.dumps(obj), flush=True)


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested locally; no numpy/skimage/Blender needed).          #
# --------------------------------------------------------------------------- #
def canonical_config(params):
    """Extract + type-normalize the canonical render config from a params dict.
    Types are forced (int/float/str) so the SAME semantic config always hashes the
    SAME regardless of JSON int/float spelling. Raises on missing keys/bad values."""
    try:
        cfg = {
            "scene": str(params["scene"]),
            "resolution": str(params["resolution"]).lower(),
            "frame": int(params["frame"]),
            "nframes": int(params["nframes"]),
            "ref_spp": int(params["ref_spp"]),
            "bounces": int(params["bounces"]),
            "cam_motion": float(params["cam_motion"]),
            "seed_a": int(params["seed_a"]),
            "seed_b": int(params["seed_b"]),
        }
    except KeyError as e:
        raise RuntimeError(f"canonical config missing required key: {e}")
    rx, _, ry = cfg["resolution"].partition("x")
    if not (rx.isdigit() and ry.isdigit()):
        raise RuntimeError(f"bad resolution {cfg['resolution']!r}; expected WxH")
    if not (1 <= cfg["frame"] <= cfg["nframes"]):
        raise RuntimeError(
            f"frame must be in [1, nframes={cfg['nframes']}], got {cfg['frame']}")
    if cfg["seed_a"] == cfg["seed_b"]:
        raise RuntimeError(
            f"seed_a and seed_b must differ (both {cfg['seed_a']}); the cross-seed "
            f"baseline would be a trivial 1.0")
    if cfg["ref_spp"] < 1 or cfg["bounces"] < 1:
        raise RuntimeError("ref_spp and bounces must be >= 1")
    return cfg


def config_hash(cfg):
    """sha256 of the canonical config, key-order independent (sorted, compact JSON).
    Only CANONICAL_CONFIG_KEYS enter the hash — plumbing keys are rejected."""
    extra = sorted(set(cfg) - set(CANONICAL_CONFIG_KEYS))
    missing = sorted(set(CANONICAL_CONFIG_KEYS) - set(cfg))
    if extra or missing:
        raise RuntimeError(
            f"config_hash takes EXACTLY the canonical keys; extra={extra} missing={missing}")
    blob = json.dumps(cfg, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def build_manifest(cfg, exr_sha256, producer, baselines):
    """The tamper-evident record stage 2 validates before spending a cent."""
    return {
        "kind": MANIFEST_KIND,
        "version": MANIFEST_VERSION,
        "config": dict(cfg),
        "config_hash": config_hash(cfg),
        "exr_file": CANONICAL_EXR_NAME,
        "exr_sha256": str(exr_sha256),
        "canonical_seed": int(cfg["seed_a"]),
        "producer": dict(producer),
        "baselines": dict(baselines),
        "grading": {
            "grid": int(ers.GRADING_TILE_GRID),
            "tone": "x/(1+x) clipped to [0,1] (exp_render_stack._tone)",
            "ssim": "skimage structural_similarity channel_axis=-1 data_range=1.0 "
                    "on exp_render_stack._tile_rects tiles",
        },
        "recipe": "exp_render_stack reference recipe (is_ref=True): fixed spp, "
                  "adaptive OFF, denoiser OFF, guides OFF, light-tree at scene "
                  "default, deterministic camera path",
    }


def validate_manifest(manifest, exr_sha256=None):
    """Refuse a stale/tampered/mismatched manifest BEFORE any render money is spent.
    Returns the canonical config on success; raises RuntimeError with the exact
    failed check otherwise."""
    if manifest.get("kind") != MANIFEST_KIND:
        raise RuntimeError(
            f"manifest kind {manifest.get('kind')!r} != {MANIFEST_KIND!r}")
    if int(manifest.get("version", -1)) != MANIFEST_VERSION:
        raise RuntimeError(
            f"manifest version {manifest.get('version')!r} != {MANIFEST_VERSION}")
    cfg = canonical_config(manifest.get("config") or {})
    want = config_hash(cfg)
    got = manifest.get("config_hash")
    if got != want:
        raise RuntimeError(
            f"manifest config_hash mismatch: recorded {got!r} != recomputed {want!r} "
            f"(config was edited after export)")
    if int(manifest.get("canonical_seed", -1)) != cfg["seed_a"]:
        raise RuntimeError("manifest canonical_seed != config seed_a")
    if not manifest.get("exr_sha256"):
        raise RuntimeError("manifest missing exr_sha256")
    if exr_sha256 is not None and exr_sha256 != manifest["exr_sha256"]:
        raise RuntimeError(
            f"shipped EXR sha256 {exr_sha256!r} != manifest {manifest['exr_sha256']!r} "
            f"(the canonical EXR was corrupted or substituted in transit)")
    baselines = manifest.get("baselines") or {}
    for k in ("same_seed", "cross_seed"):
        if "worst_tile_ssim" not in (baselines.get(k) or {}):
            raise RuntimeError(f"manifest baselines missing {k}.worst_tile_ssim")
    return cfg


def render_kwargs_from_config(cfg, *, blend, seed, device_pref, timeout_s, require_gpu):
    """The ONE code path that turns a canonical config into run_blender_frame kwargs —
    used by BOTH the producer renders (stage 1) and the replica render (stage 2), so
    the two architectures render a structurally identical recipe. is_ref=True makes
    the scene script FORCE adaptive OFF + denoiser OFF and SKIP the anchor-only
    light-tree lever (its block is gated `if (not IS_REF)`)."""
    rx, _, ry = str(cfg["resolution"]).lower().partition("x")
    return dict(
        blend=blend,
        res_x=max(16, int(rx)), res_y=max(16, int(ry)),
        spp=int(cfg["ref_spp"]), is_ref=True,
        frame=int(cfg["frame"]), nframes=int(cfg["nframes"]),
        cam_motion=float(cfg["cam_motion"]), seed=int(seed),
        bounces=int(cfg["bounces"]), device_pref=device_pref,
        timeout_s=int(timeout_s), require_gpu=bool(require_gpu),
    )


def assert_identical_config(cfg_x, cfg_y):
    """The SAME-SEED pair (A vs A2) must be identical in EVERY key including seed —
    the determinism measurement is meaningless otherwise. Raises on any diff."""
    keys = set(cfg_x) | set(cfg_y)
    diffs = sorted(k for k in keys if cfg_x.get(k) != cfg_y.get(k))
    if diffs:
        raise RuntimeError(
            f"same-seed determinism pair must be IDENTICAL configs; differing keys: {diffs}")
    return []


def classify_seed_effect(same_seed_max_abs, cross_seed_max_abs,
                         epsilon=1e-4, ratio=10.0):
    """MEASURED-2026-07-10 GOTCHA, detected structurally: on Blender 4.2.1 Cycles
    (Metal, and consistent with the banked CUDA reference-consistency probe where
    different-seed 4096-spp renders came out pixel-identical), `scene.cycles.seed`
    does NOT perturb the sample sequence for ANY offered sampling pattern
    (AUTOMATIC / TABULATED_SOBOL / BLUE_NOISE) — different seeds reproduce the SAME
    Monte-Carlo realization to float epsilon (max|d| ~1e-6 even at 8 spp where the
    image is visibly noisy). When that happens the "cross-seed noise floor" is
    DEGENERATE (it re-measures determinism, not statistics) and must not be used to
    classify a cross-arch delta as within-noise/systematic.

    Pure classification from the two measured RAW max-abs pixel diffs:
      "effective" — the cross-seed pair differs clearly beyond the same-seed float
                    jitter (> ratio * same-seed max AND > epsilon absolute; real MC
                    noise at any practical spp is >= ~1e-2, four orders above both).
      "inert"     — the seed knob did not change the realization; the cross-seed
                    baseline is degenerate."""
    same = float(same_seed_max_abs)
    cross = float(cross_seed_max_abs)
    if cross > max(ratio * same, epsilon):
        return "effective"
    return "inert"


def comparison_block(ssim_mat, global_ssim, worst_tile, p5_tile, grid):
    """PURE assembly of one A-vs-B comparison: full JSON-safe tile list + scalars +
    the identity of the worst (argmin-SSIM) tile. Reuses xdp.flatten_tiles /
    xdp.topk_indices (already unit-tested NaN-skipping rank math)."""
    flat = xdp.flatten_tiles(ssim_mat)
    n_tiles = grid * grid
    if len(flat) != n_tiles:
        raise ValueError(f"ssim map has {len(flat)} tiles, expected {n_tiles}")
    dissim = [(1.0 - v) if v is not None else None for v in flat]
    top = xdp.topk_indices(dissim, 1)
    worst_entry = (
        {"tile": [int(top[0] // grid), int(top[0] % grid)],
         "ssim": round(float(flat[top[0]]), 6)}
        if top else None)
    return {
        "global_ssim": round(float(global_ssim), 6),
        "worst_tile_ssim": round(float(worst_tile), 6),
        "p5_tile_ssim": round(float(p5_tile), 6),
        "tiles": [round(v, 6) if v is not None else None for v in flat],
        "worst_tile": worst_entry,
        "n_valid_tiles": len(xdp._finite_indices(flat)),
    }


def gate_report(producer_baselines, cross_arch=None, verifier_cross_seed=None,
                gate=DELIVERY_WORST_TILE):
    """STAGE 3, pure: same-arch baseline vs cross-arch delta -> the honest gate verdict.

    producer_baselines: stage 1's {"same_seed": {...}, "cross_seed": {...}} blocks
      (each with worst_tile_ssim; same_seed may carry pixel_exact).
    cross_arch: stage 2's SSIM(shipped_canonical, replica) block, or None (pending).
    verifier_cross_seed: stage 2's optional replica-side cross-seed block, or None.

    Semantics (all comparisons on the SAME grading tiling):
      * gate_pass = cross-arch worst-tile >= gate. The gated comparison is the
        SAME-SEED cross-arch pair (identical config, identical seed, different
        silicon) — the exact "Mac produced the reference, CUDA reproduces it"
        delivery question.
      * same-seed same-arch = the determinism CEILING; cross-seed same-arch = the
        Monte-Carlo noise FLOOR. cross-arch between them = the arches behave like
        different noise realizations of the same estimator; cross-arch BELOW the
        floor = a systematic arch bias worse than reseeding — flagged explicitly.
    """
    same_seed = dict(producer_baselines["same_seed"])
    cross_seed = dict(producer_baselines["cross_seed"])
    ss_worst = float(same_seed["worst_tile_ssim"])
    cs_worst = float(cross_seed["worst_tile_ssim"])
    # MEASURED gotcha (see classify_seed_effect): when the seed knob is inert the
    # cross-seed "floor" degenerates into a second determinism measurement and MUST
    # NOT drive the within-noise / systematic-bias classification.
    seed_inert = bool(cross_seed.get("degenerate_seed_inert", False))

    report = {
        "kind": "cross_arch_gate_report",
        "delivery_worst_tile_gate": float(gate),
        "gated_comparison": "same-seed cross-arch (identical config+seed, different silicon)",
        "same_arch_same_seed_worst_tile": round(ss_worst, 6),
        "same_arch_same_seed_pixel_exact": same_seed.get("pixel_exact"),
        "same_arch_cross_seed_worst_tile": round(cs_worst, 6),
        "same_arch_cross_seed_clears_gate": bool(cs_worst >= gate),
        "same_arch_cross_seed_degenerate_seed_inert": seed_inert,
        "verifier_cross_seed_worst_tile": (
            round(float(verifier_cross_seed["worst_tile_ssim"]), 6)
            if verifier_cross_seed else None),
    }

    if cross_arch is None:
        if seed_inert:
            pending_interp = (
                f"SAME-ARCH BASELINE ESTABLISHED (DETERMINISM ONLY), CROSS-ARCH "
                f"PENDING: on one box the same config+seed reproduces at worst-tile "
                f"{ss_worst:.4f}"
                f"{' (pixel-exact)' if same_seed.get('pixel_exact') else ''}. The "
                f"cross-seed pair ALSO reproduced (worst-tile {cs_worst:.4f}) because "
                f"cycles.seed is MEASURED-INERT on this Blender build — different "
                f"seeds do not change the Monte-Carlo realization, so NO same-arch "
                f"noise floor exists; the determinism ceiling is the only same-arch "
                f"baseline. The gated cross-arch number does not exist until the CUDA "
                f"half renders the shipped manifest; nothing here predicts it.")
        else:
            pending_interp = (
                f"SAME-ARCH BASELINE ESTABLISHED, CROSS-ARCH PENDING: on one box the "
                f"same config agrees with itself at worst-tile {ss_worst:.4f} (same "
                f"seed{', pixel-exact' if same_seed.get('pixel_exact') else ''}) and "
                f"{cs_worst:.4f} (different seed = the Monte-Carlo noise floor at this "
                f"spp). The gated cross-arch number does not exist until the CUDA half "
                f"renders the shipped manifest; nothing here predicts it.")
        report.update({
            "status": "PENDING-CUDA-HALF",
            "label": "MEASURED (same-arch half only; cross-arch half not yet run)",
            "cross_arch_worst_tile": None,
            "cross_arch_global_ssim": None,
            "gate_pass": None,
            "cross_arch_within_same_arch_noise": None,
            "systematic_arch_bias_suspected": None,
            "interpretation": pending_interp,
        })
        return report

    xa_worst = float(cross_arch["worst_tile_ssim"])
    xa_global = float(cross_arch["global_ssim"])
    gate_pass = bool(xa_worst >= gate)
    # With an inert seed there is NO Monte-Carlo floor to be "within" — the
    # classification would compare against a determinism re-measurement and call
    # every real cross-arch delta "systematic bias". Report None instead, honestly.
    within_noise = None if seed_inert else bool(xa_worst >= cs_worst)
    report.update({
        "status": "COMPLETE",
        "label": "MEASURED (both halves: producer baselines + cross-arch replica)",
        "cross_arch_worst_tile": round(xa_worst, 6),
        "cross_arch_global_ssim": round(xa_global, 6),
        "gate_pass": gate_pass,
        "cross_arch_gate_margin": round(xa_worst - gate, 6),
        "cross_arch_within_same_arch_noise": within_noise,
        "systematic_arch_bias_suspected": (
            None if seed_inert else bool(xa_worst < cs_worst)),
    })

    if seed_inert:
        if gate_pass:
            interp = (
                f"CROSS-ARCH GATE PASS: worst-tile SSIM(shipped, replica) = "
                f"{xa_worst:.4f} >= gate {gate:.2f}. NOTE: cycles.seed is "
                f"MEASURED-INERT on this Blender build, so no same-arch Monte-Carlo "
                f"noise floor exists to bracket the delta; the same-arch baseline is "
                f"the determinism ceiling {ss_worst:.4f} (same config reproduces "
                f"itself). Any gap below that ceiling is a kernel-level cross-arch "
                f"difference by construction — currently inside the delivery "
                f"tolerance. At this config a Mac-produced reference and a CUDA "
                f"verification are interchangeable at the strict delivery tier.")
        else:
            interp = (
                f"CROSS-ARCH GATE FAIL: worst-tile SSIM(shipped, replica) = "
                f"{xa_worst:.4f} < gate {gate:.2f}. cycles.seed is MEASURED-INERT on "
                f"this Blender build, so this delta cannot be Monte-Carlo reseeding "
                f"noise — the same config reproduces itself at {ss_worst:.4f} on one "
                f"box. The gap IS a kernel-level architecture difference. A "
                f"Mac-produced reference and a CUDA verification are NOT "
                f"interchangeable at this config; cross-arch delivery claims must "
                f"not be made until the source of the deviation is found.")
        if verifier_cross_seed is not None:
            interp += (
                f" Verifier-side cross-seed check: "
                f"{float(verifier_cross_seed['worst_tile_ssim']):.4f} "
                f"(degenerate too if the seed is inert on the verifier's build).")
        report["interpretation"] = interp
        return report

    if gate_pass and within_noise:
        interp = (
            f"CROSS-ARCH GATE PASS, WITHIN NOISE: worst-tile SSIM(shipped, replica) = "
            f"{xa_worst:.4f} >= gate {gate:.2f}, and it is at or above the producer's "
            f"own cross-seed floor {cs_worst:.4f} — the two architectures disagree no "
            f"more than one architecture disagrees with itself across seeds. At this "
            f"config a Mac-produced reference and a CUDA verification are "
            f"interchangeable at the strict delivery tier.")
    elif gate_pass:
        interp = (
            f"CROSS-ARCH GATE PASS, BUT BELOW THE SAME-ARCH NOISE FLOOR: worst-tile "
            f"{xa_worst:.4f} >= gate {gate:.2f}, yet it is BELOW the producer's "
            f"cross-seed floor {cs_worst:.4f} — the architectures differ by more than "
            f"reseeding does, i.e. a real (kernel-level) deviation exists, currently "
            f"inside the delivery tolerance. Track it: a harder scene/config could "
            f"push it through the gate.")
    elif not report["same_arch_cross_seed_clears_gate"]:
        interp = (
            f"CROSS-ARCH GATE FAIL AT THIS CONFIG — BUT SO DOES SAME-ARCH RESEEDING: "
            f"worst-tile {xa_worst:.4f} < gate {gate:.2f}, and the producer's own "
            f"cross-seed floor {cs_worst:.4f} also fails the gate at this spp. The "
            f"failure is dominated by Monte-Carlo noise at this budget, not "
            f"(necessarily) by architecture: at this spp not even the same box "
            f"clears the gate against a reseeded self. The honest verdict is "
            f"gate_pass=false AT THIS CONFIG, with the arch-vs-noise split requiring "
            f"a higher-spp (more converged) canonical render. Compare against the "
            f"same-seed ceiling {ss_worst:.4f}: cross-arch at the SAME seed landing "
            f"near the cross-seed floor means the seed's noise realization does NOT "
            f"transfer across kernels.")
    else:
        interp = (
            f"CROSS-ARCH GATE FAIL: worst-tile SSIM(shipped, replica) = {xa_worst:.4f} "
            f"< gate {gate:.2f} while the producer's cross-seed floor {cs_worst:.4f} "
            f"CLEARS the gate — the architectures genuinely disagree beyond both the "
            f"delivery tolerance and the Monte-Carlo noise floor. A Mac-produced "
            f"reference and a CUDA verification are NOT interchangeable at this "
            f"config; cross-arch delivery claims must not be made until the source "
            f"of the kernel-level deviation is found.")
    if verifier_cross_seed is not None:
        interp += (
            f" Verifier-side cross-seed floor: {float(verifier_cross_seed['worst_tile_ssim']):.4f}.")
    report["interpretation"] = interp
    return report


def baselines_from_metrics(metrics):
    """Extract the gate_report producer_baselines from a stage-1 metrics dict
    (ledger row shape). Single-sourced so the local `report` subcommand and the
    pod-side stage 2 read the same fields."""
    comps = metrics.get("comparisons") or {}
    if "same_seed" not in comps or "cross_seed" not in comps:
        raise RuntimeError("stage-1 metrics missing comparisons.same_seed/cross_seed")
    same_seed = dict(comps["same_seed"])
    if same_seed.get("pixel_exact") is None:
        same_seed["pixel_exact"] = metrics.get("same_seed_pixel_exact")
    return {"same_seed": same_seed, "cross_seed": dict(comps["cross_seed"])}


# --------------------------------------------------------------------------- #
# Render plumbing shared by both modes.                                         #
# --------------------------------------------------------------------------- #
def _blender_version(blender_bin):
    """First line of `blender --version` (best-effort; never fatal)."""
    try:
        proc = subprocess.run([blender_bin, "--version"], capture_output=True,
                              text=True, timeout=60)
        first = (proc.stdout or "").strip().splitlines()
        return first[0] if first else "unknown"
    except Exception as e:  # noqa: BLE001 — diagnostics only
        return f"unknown ({type(e).__name__})"


def _bootstrap(params):
    """Shared stage-1/stage-2 bootstrap: libs + Blender + functional GPU gate.
    Returns (blender_bin, blender_version, require_gpu, device_pref)."""
    device_pref = str(params.get("device", "AUTO")).upper()
    require_gpu = bool(params.get("require_gpu", False))
    gpu_probe_timeout_s = int(params.get(
        "gpu_probe_timeout_s", os.environ.get("CX_GPU_PROBE_TIMEOUT_S", 300)))
    blender_url = str(params.get("blender_url", ers.DEFAULT_BLENDER_URL))

    ers.ensure_system_libs()
    ers.ensure_pydeps()
    blender_bin = ers.ensure_blender(blender_url)
    if require_gpu:
        ers.require_gpu_probe(blender_bin, timeout_s=gpu_probe_timeout_s)
    return blender_bin, _blender_version(blender_bin), require_gpu, device_pref


def _render_one(blender_bin, script_path, out_name, kwargs):
    """One canonical render -> (wall_s, device, color[H,W,3])."""
    wall, dev, exr = ers.run_blender_frame(
        blender_bin, script_path,
        out_exr=os.path.join(WORK_DIR, out_name), **kwargs)
    color = ers.read_exr_layers(exr, kwargs["res_x"], kwargs["res_y"])[0]
    return wall, dev, color, exr


def _guard_devices(devices, require_gpu):
    """Belt-and-suspenders (run_blender_frame already refuses per-render): a CPU
    device string must never reach a GPU-required receipt."""
    if require_gpu and any(d.startswith("CPU") or d == "unknown" for d in devices):
        raise RuntimeError(
            f"require_gpu set but render device set is {sorted(devices)!r}; "
            f"refusing a CPU-fallback receipt")


def _score(color_x, color_y):
    """Grading-grid SSIM between two frames: ([grid,grid] map, global, worst, p5)."""
    mat = ers.per_tile_ssim_map(color_x, color_y)
    g, w, p5 = ers.compute_ssim_global_and_tiles(color_x, color_y)
    return mat, g, w, p5


def diff_stats(color_x, color_y):
    """Raw-pixel (linear HDR, pre-tonemap) agreement stats between two frames.
    These are what expose the seed-inertness gotcha: SSIM saturates at 1.0 long
    before max_abs_diff distinguishes float jitter (~1e-6) from real Monte-Carlo
    re-realization (>= ~1e-2)."""
    import numpy as np
    d = np.abs(np.asarray(color_x, dtype=np.float64)
               - np.asarray(color_y, dtype=np.float64))
    return {
        "pixel_exact": bool(np.array_equal(color_x, color_y)),
        "max_abs_diff": float(d.max()),
        "mean_abs_diff": float(d.mean()),
    }


# --------------------------------------------------------------------------- #
# Stage 1 — Metal-vs-Metal self-consistency + canonical export.                 #
# --------------------------------------------------------------------------- #
def run_self_consistency(params):
    import numpy as np  # local import so a missing numpy errors cleanly

    cfg = canonical_config({
        "scene": params.get("scene", "classroom"),
        "resolution": params.get("resolution", "960x540"),
        "frame": params.get("frame", 1),
        "nframes": params.get("nframes", 2),
        "ref_spp": params.get("ref_spp", 512),
        "bounces": params.get("bounces", 12),
        "cam_motion": params.get("cam_motion", 1.0),
        "seed_a": params.get("seed_a", 0),
        "seed_b": params.get("seed_b", 12345),
    })
    do_export = bool(params.get("export", True))
    export_root = str(params.get("export_root", EXPORT_ROOT))
    chash = config_hash(cfg)
    log(f"stage 1 (self_consistency): config={json.dumps(cfg, sort_keys=True)} "
        f"config_hash={chash}")

    os.makedirs(WORK_DIR, exist_ok=True)
    blender_bin, bl_version, require_gpu, device_pref = _bootstrap(params)
    blend, scene_key, fallback_note = ers.resolve_scene(cfg["scene"])
    if scene_key != cfg["scene"]:
        raise RuntimeError(
            f"scene fell back from {cfg['scene']!r} to {scene_key!r}; a fallback scene "
            f"must never enter a hash-pinned cross-arch manifest — request the "
            f"resolved scene explicitly")

    script_path = os.path.join(WORK_DIR, "cx_cross_arch_scene.py")
    with open(script_path, "w") as f:
        f.write(ers.BLENDER_SCENE_SCRIPT)

    timeout_s = int(params.get("render_timeout_s", 3600))
    kw = lambda seed: render_kwargs_from_config(  # noqa: E731
        cfg, blend=blend, seed=seed, device_pref=device_pref,
        timeout_s=timeout_s, require_gpu=require_gpu)
    cfg_a, cfg_a2, cfg_b = kw(cfg["seed_a"]), kw(cfg["seed_a"]), kw(cfg["seed_b"])
    # HONESTY GATES before any render: A vs A2 identical in EVERYTHING (the
    # determinism pair); A vs B differ ONLY in seed (the noise-floor pair; reuses the
    # reference-consistency probe's already-tested assert).
    assert_identical_config(cfg_a, cfg_a2)
    diffs = xrc.assert_seed_only_diff(cfg_a, cfg_b)
    log(f"config parity confirmed: A==A2 exactly; A vs B differ only in {diffs}")

    wall_a, dev_a, color_a, exr_a = _render_one(blender_bin, script_path, "ref_a.exr", cfg_a)
    wall_a2, dev_a2, color_a2, _ = _render_one(blender_bin, script_path, "ref_a2.exr", cfg_a2)
    wall_b, dev_b, color_b, _ = _render_one(blender_bin, script_path, "ref_b.exr", cfg_b)
    devices = sorted({dev_a, dev_a2, dev_b})
    _guard_devices(devices, require_gpu)

    t0 = time.perf_counter()
    ss_stats = diff_stats(color_a, color_a2)
    cs_stats = diff_stats(color_a, color_b)
    pixel_exact = ss_stats["pixel_exact"]
    mat_ss, g_ss, w_ss, p5_ss = _score(color_a, color_a2)
    mat_cs, g_cs, w_cs, p5_cs = _score(color_a, color_b)
    scoring_s = time.perf_counter() - t0
    seed_effect = classify_seed_effect(ss_stats["max_abs_diff"],
                                       cs_stats["max_abs_diff"])
    log(f"scoring done in {scoring_s:.1f}s; same-seed worst={w_ss:.4f} "
        f"(pixel_exact={pixel_exact}, max|d|={ss_stats['max_abs_diff']:.3g}) "
        f"cross-seed worst={w_cs:.4f} (max|d|={cs_stats['max_abs_diff']:.3g}) "
        f"seed_effect={seed_effect}")
    if seed_effect == "inert":
        log("WARN: cycles.seed is INERT on this build — the cross-seed pair "
            "reproduced the SAME Monte-Carlo realization (float-epsilon diff); the "
            "cross-seed baseline is DEGENERATE (a second determinism measurement, "
            "NOT a noise floor) and is flagged as such in the manifest + report")

    grid = ers.GRADING_TILE_GRID
    same_seed_block = comparison_block(mat_ss, g_ss, w_ss, p5_ss, grid)
    same_seed_block.update(ss_stats)
    cross_seed_block = comparison_block(mat_cs, g_cs, w_cs, p5_cs, grid)
    cross_seed_block.update(cs_stats)
    if seed_effect == "inert":
        cross_seed_block["degenerate_seed_inert"] = True
    baselines = {"same_seed": same_seed_block, "cross_seed": cross_seed_block}

    producer = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "device": ",".join(devices),
        "blender_version": bl_version,
    }

    export_dir = manifest_path = exr_export_path = None
    exr_sha = sha256_file(exr_a)
    if do_export:
        export_dir = os.path.join(export_root, chash)
        os.makedirs(export_dir, exist_ok=True)
        exr_export_path = os.path.join(export_dir, CANONICAL_EXR_NAME)
        shutil.copyfile(exr_a, exr_export_path)
        if sha256_file(exr_export_path) != exr_sha:
            raise RuntimeError("canonical EXR copy corrupted (sha256 mismatch after copy)")
        manifest = build_manifest(cfg, exr_sha, producer, baselines)
        manifest_path = os.path.join(export_dir, "manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
        # Fail-closed: re-validate our own export exactly as the replica will.
        validate_manifest(manifest, exr_sha256=sha256_file(exr_export_path))
        log(f"canonical export ready -> {export_dir}")

    note = (
        f"CROSS-ARCH GATE stage 1 (self-consistency + canonical export) on "
        f"'{scene_key}' frame {cfg['frame']}/{cfg['nframes']} ({cfg['resolution']}), "
        f"THREE real renders on one box, all the EXACT exp_render_stack.py reference "
        f"recipe ({cfg['ref_spp']} spp fixed, adaptive OFF, denoiser OFF, guides OFF, "
        f"light-tree at the scene default, deterministic camera path): A (seed="
        f"{cfg['seed_a']}), A2 (seed={cfg['seed_a']}, fresh subprocess — the SAME-SEED "
        f"determinism pair) and B (seed={cfg['seed_b']} — the CROSS-SEED Monte-Carlo "
        f"noise floor). per-tile SSIM on the {grid}x{grid} grading grid (same _tone/"
        f"_tile_rects as the delivery gate). The canonical EXR is render A; the "
        f"manifest pins config_hash + EXR sha256 so the CUDA half reproduces the "
        f"IDENTICAL config or refuses. All values MEASURED on real pixels; "
        f"modeled=false. Config parity asserted in code before any render. "
        f"seed_effect={seed_effect}: raw max|d| same-seed {ss_stats['max_abs_diff']:.3g} "
        f"vs cross-seed {cs_stats['max_abs_diff']:.3g}"
        + (" — cycles.seed is INERT on this build (different seeds reproduce the "
           "same Monte-Carlo realization to float epsilon; independently confirmed "
           "at 8 spp where the image is visibly noisy), so the cross-seed baseline "
           "is DEGENERATE and flagged; the determinism ceiling is the only "
           "same-arch baseline." if seed_effect == "inert" else ".")
    )
    if fallback_note:
        note += " SCENE NOTE: " + fallback_note + "."

    metrics = {
        "probe": "cross_arch_gate",
        "mode": "self_consistency",
        "label": "MEASURED",
        "grid": int(grid),
        "config": cfg,
        "config_hash": chash,
        "delivery_worst_tile_gate": float(DELIVERY_WORST_TILE),
        "comparisons": baselines,
        "same_seed_pixel_exact": pixel_exact,
        "seed_effect": seed_effect,
        "gate_report": gate_report(baselines, cross_arch=None),
        "producer": producer,
        "device": ",".join(devices),
        "wall_ref_a_s": round(float(wall_a), 3),
        "wall_ref_a2_s": round(float(wall_a2), 3),
        "wall_ref_b_s": round(float(wall_b), 3),
        "scoring_s": round(float(scoring_s), 3),
        "exr_sha256": exr_sha,
        "export_dir": export_dir,
        "manifest_path": manifest_path,
        "canonical_exr_path": exr_export_path,
        "modeled": False,
        "note": note,
    }
    log(f"RESULT same_seed worst={w_ss:.4f} pixel_exact={pixel_exact} | "
        f"cross_seed worst={w_cs:.4f} | device={metrics['device']}")
    return metrics


# --------------------------------------------------------------------------- #
# Stage 2 — replica on the OTHER architecture, scored vs the shipped canonical. #
# --------------------------------------------------------------------------- #
def run_replica(params):
    manifest_path = params.get("manifest_path")
    if not manifest_path:
        raise RuntimeError("replica mode requires manifest_path")
    with open(manifest_path) as f:
        manifest = json.load(f)
    exr_path = params.get("exr_path") or os.path.join(
        os.path.dirname(os.path.abspath(manifest_path)), manifest.get("exr_file", ""))
    if not os.path.isfile(exr_path):
        raise RuntimeError(f"shipped canonical EXR not found at {exr_path!r}")
    # Validate hashes BEFORE any render is paid for: config untampered, EXR intact.
    shipped_sha = sha256_file(exr_path)
    cfg = validate_manifest(manifest, exr_sha256=shipped_sha)
    chash = manifest["config_hash"]
    render_cross_seed = bool(params.get("render_cross_seed_baseline", True))
    log(f"stage 2 (replica): manifest validated (config_hash={chash}, "
        f"exr sha256 ok); producer={manifest['producer'].get('device')} on "
        f"{manifest['producer'].get('machine')}")

    os.makedirs(WORK_DIR, exist_ok=True)
    blender_bin, bl_version, require_gpu, device_pref = _bootstrap(params)
    blend, scene_key, fallback_note = ers.resolve_scene(cfg["scene"])
    if scene_key != cfg["scene"]:
        raise RuntimeError(
            f"scene fell back from {cfg['scene']!r} to {scene_key!r}; the replica must "
            f"render the manifest's exact scene or refuse")

    script_path = os.path.join(WORK_DIR, "cx_cross_arch_scene.py")
    with open(script_path, "w") as f:
        f.write(ers.BLENDER_SCENE_SCRIPT)

    timeout_s = int(params.get("render_timeout_s", 3600))
    kw = lambda seed: render_kwargs_from_config(  # noqa: E731
        cfg, blend=blend, seed=seed, device_pref=device_pref,
        timeout_s=timeout_s, require_gpu=require_gpu)

    rx, _, ry = str(cfg["resolution"]).lower().partition("x")
    res_x, res_y = int(rx), int(ry)
    shipped_color = ers.read_exr_layers(exr_path, res_x, res_y)[0]

    wall_r, dev_r, replica_color, _ = _render_one(
        blender_bin, script_path, "replica_a.exr", kw(cfg["seed_a"]))
    devices = {dev_r}

    walls = {"replica_a": wall_r}
    verifier_block = None
    if render_cross_seed:
        wall_vb, dev_vb, color_vb, _ = _render_one(
            blender_bin, script_path, "replica_b.exr", kw(cfg["seed_b"]))
        devices.add(dev_vb)
        walls["replica_b"] = wall_vb
    _guard_devices(devices, require_gpu)

    t0 = time.perf_counter()
    grid = ers.GRADING_TILE_GRID
    cross_stats = diff_stats(shipped_color, replica_color)
    mat_x, g_x, w_x, p5_x = _score(shipped_color, replica_color)
    cross_block = comparison_block(mat_x, g_x, w_x, p5_x, grid)
    cross_block.update(cross_stats)
    if render_cross_seed:
        v_stats = diff_stats(replica_color, color_vb)
        mat_v, g_v, w_v, p5_v = _score(replica_color, color_vb)
        verifier_block = comparison_block(mat_v, g_v, w_v, p5_v, grid)
        verifier_block.update(v_stats)
        # Same structural detection on the verifier's build: replica vs its own
        # reseed at float-epsilon = the seed is inert HERE too (recorded, honest).
        if classify_seed_effect(0.0, v_stats["max_abs_diff"]) == "inert":
            verifier_block["degenerate_seed_inert"] = True
    scoring_s = time.perf_counter() - t0

    report = gate_report(manifest["baselines"], cross_arch=cross_block,
                         verifier_cross_seed=verifier_block)
    log(f"scoring done in {scoring_s:.1f}s; cross-arch worst={w_x:.4f} "
        f"gate_pass={report['gate_pass']} within_noise="
        f"{report['cross_arch_within_same_arch_noise']}")

    note = (
        f"CROSS-ARCH GATE stage 2 (replica) on '{scene_key}' frame "
        f"{cfg['frame']}/{cfg['nframes']} ({cfg['resolution']}): the shipped canonical "
        f"EXR (sha256-validated, produced on {manifest['producer'].get('device')} / "
        f"{manifest['producer'].get('machine')}, {manifest['producer'].get('blender_version')}) "
        f"vs a replica rendered HERE from the IDENTICAL hash-pinned config (seed="
        f"{cfg['seed_a']}, {cfg['ref_spp']} spp reference recipe, same deterministic "
        f"camera path, same embedded scene script). per-tile SSIM on the {grid}x{grid} "
        f"grading grid = the CROSS-ARCHITECTURE delta; gate_pass = cross-arch "
        f"worst-tile >= {DELIVERY_WORST_TILE}. "
        + (f"A second replica render (seed={cfg['seed_b']}) measures the verifier-side "
           f"cross-seed noise floor. " if render_cross_seed else "")
        + "All values MEASURED on real pixels; modeled=false. The comparison is valid "
          "ONLY because the manifest's config_hash and EXR sha256 were validated before "
          "any render."
    )
    if fallback_note:
        note += " SCENE NOTE: " + fallback_note + "."

    metrics = {
        "probe": "cross_arch_gate",
        "mode": "replica",
        "label": "MEASURED",
        "grid": int(grid),
        "config": cfg,
        "config_hash": chash,
        "delivery_worst_tile_gate": float(DELIVERY_WORST_TILE),
        "cross_arch": cross_block,
        "verifier_cross_seed": verifier_block,
        "gate_report": report,
        "producer": manifest["producer"],
        "replica": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "device": ",".join(sorted(devices)),
            "blender_version": bl_version,
        },
        "device": ",".join(sorted(devices)),
        "walls_s": {k: round(float(v), 3) for k, v in walls.items()},
        "scoring_s": round(float(scoring_s), 3),
        "shipped_exr_sha256": shipped_sha,
        "modeled": False,
        "note": note,
    }
    log(f"RESULT cross_arch worst={w_x:.4f} global={g_x:.4f} "
        f"gate_pass={report['gate_pass']} device={metrics['device']}")
    return metrics


# --------------------------------------------------------------------------- #
# main                                                                          #
# --------------------------------------------------------------------------- #
def main():
    params = json.loads(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else {}
    mode = str(params.get("mode", "self_consistency"))
    if mode == "self_consistency":
        metrics = run_self_consistency(params)
    elif mode == "replica":
        metrics = run_replica(params)
    else:
        raise RuntimeError(
            f"unknown mode {mode!r}; expected 'self_consistency' or 'replica'")
    emit(metrics)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 — the contract: error key, exit 0, never fabricate
        import traceback
        traceback.print_exc(file=sys.stderr)
        emit({"error": f"{type(e).__name__}: {e}"})
        sys.exit(0)
