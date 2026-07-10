#!/usr/bin/env python3
"""
run_local_metal_anchor.py — the LOCAL (zero-dollar) Metal render lane driver.
================================================================================

WHY: the CX fleet is Apple Silicon Macs. For the render lane to be a CX product
the anchor stack must run and be MEASURED on Metal — not just on rented CUDA.
This driver runs the SAME anchor-vs-reference protocol as the cloud lane by
invoking pod/exp_render_stack.py as a subprocess against a LOCAL Blender binary
on the local Mac's Metal GPU. No pod is ever provisioned; no RunPod API is ever
called; the only network use is the one-time public scene download
(download.blender.org) and, if needed, pip wheels for the EXR reader.

HOW IT REUSES THE POD RUNNER (no fork, no copy):
  pod/exp_render_stack.py hardcodes pod paths (BLENDER_DIR=/root/blender,
  WORK_DIR=/tmp/render_stack, _CACHE_ROOT=/models|/root/spec-lab). Instead of
  editing the runner, this driver launches a tiny SHIM subprocess that imports
  the module, patches those module constants to local-Mac paths from env vars,
  then calls the runner's main() under the runner's own error contract (last
  stdout line is exactly ONE JSON object; failures emit {"error":...} exit 0).
  Because BLENDER_BIN is patched to an existing local binary, ensure_blender()
  returns immediately — the Linux tarball download never triggers.

METAL DEVICE: the runner's embedded scene script picks its GPU via the
  OPTIX>CUDA>HIP>ONEAPI>METAL ladder (METAL rung added guardedly for this lane;
  macOS headless gotcha handled: get_devices_for_type('METAL') must be called
  or prefs.devices can stay empty). require_gpu=True is set by DEFAULT here so
  the runner's fail-loud path fires if no GPU lands — never a silent CPU run.

PYTHON DEPS: the runner needs numpy/PIL/skimage plus an EXR reader. This Mac's
  python may lack the OpenEXR bindings (the imageio fallback cannot read
  Blender multilayer EXRs without extra backends). The driver therefore
  provisions, WITHOUT touching the user's environment:
    * if only OpenEXR is missing -> pip install --target <cache>/pysite OpenEXR
      and prepend it to PYTHONPATH for the shim subprocess only;
    * if more is missing        -> a private venv at <cache>/venv.

DEFAULT CONFIG (TINY, sized so an M3 Pro finishes in roughly 5-15 minutes):
  classroom @ 960x540, frames=2 (the runner clamps frames to >=2 — a single
  frame cannot form the animated protocol; the task's "1 frame" is therefore
  honestly delivered as the 2-frame minimum), keyframe_every=1 (all-anchor,
  zero reprojection — the pure anchor-vs-reference measurement, same mode as
  cloud RUN 3), ref_spp=512, draft_spp=64, repair OFF, hole_fill="inpaint"
  (kf=1 has no reprojected frames so no holes ever exist; this skips the
  pointless fixed-overhead calibration render and keeps modeled=false — the
  receipt is FULLY MEASURED).

OUTPUT: last stdout line = the runner's standard metrics JSON (augmented with
  "evidence"); on success a ledger row labeled MEASURED/local-metal is appended
  to docs/speed-lane-reports/spec-lab/local_metal_ledger.jsonl. If no local
  Blender exists the driver emits {"error":...,"status":"PENDING-OWNER-HARDWARE"}
  and exits 0 — an honest absence, never a fabricated number.

USAGE:
  python3 scripts/spec-lab/run_local_metal_anchor.py               # real run
  python3 scripts/spec-lab/run_local_metal_anchor.py --dry-run     # plumbing only
  python3 scripts/spec-lab/run_local_metal_anchor.py --ref-spp 128 --draft-spp 32
"""

import argparse
import datetime
import json
import os
import platform
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
POD_DIR = os.path.join(HERE, "pod")
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
DEFAULT_LEDGER = os.path.join(
    REPO_ROOT, "docs", "speed-lane-reports", "spec-lab", "local_metal_ledger.jsonl")
DEFAULT_CACHE_ROOT = os.path.expanduser("~/.cache/cx-spec-lab")

# Local Blender discovery order (repo convention: memory/blender-asset-render —
# Blender is installed but NOT on PATH on the fleet Macs; the .app binary is the
# canonical location used by every scripts/cx_*.py asset renderer).
BLENDER_ENV_VAR = "CX_LOCAL_BLENDER"
DEFAULT_BLENDER_CANDIDATES = (
    "/Applications/Blender.app/Contents/MacOS/Blender",
)

# Imports the pod runner actually needs in the SHIM's python. OpenEXR is listed
# because without it read_exr_layers() falls to imageio, which cannot open
# Blender MULTILAYER EXRs without extra backends (verified locally 2026-07-10).
REQUIRED_IMPORTS = ("numpy", "PIL", "skimage", "OpenEXR")

EVIDENCE_LABEL = "MEASURED/local-metal"


def log(*a):
    print("[local-metal]", *a, file=sys.stderr, flush=True)


def emit(obj):
    """Runner contract mirrored: exactly ONE JSON object as the final stdout line."""
    print(json.dumps(obj), flush=True)


# --------------------------------------------------------------------------- #
# Blender discovery                                                            #
# --------------------------------------------------------------------------- #
def _runnable(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def discover_blender(explicit=None, environ=None, candidates=None, which=shutil.which):
    """Return the first runnable local Blender binary, or None (honest absence).

    Order: explicit arg > $CX_LOCAL_BLENDER > the repo-convention .app binary >
    `blender` on PATH. Pure plumbing — unit-testable without Blender."""
    environ = os.environ if environ is None else environ
    candidates = DEFAULT_BLENDER_CANDIDATES if candidates is None else candidates
    ordered = [explicit, environ.get(BLENDER_ENV_VAR), *candidates]
    for cand in ordered:
        if _runnable(cand):
            return cand
    on_path = which("blender")
    if _runnable(on_path):
        return on_path
    return None


def blender_version(blender_bin, runner=subprocess.run):
    """First line of `blender --version` (best-effort; never fatal)."""
    try:
        proc = runner([blender_bin, "--version"], capture_output=True,
                      text=True, timeout=60)
        first = (proc.stdout or "").strip().splitlines()
        return first[0] if first else "unknown"
    except Exception as e:  # noqa: BLE001 — diagnostics only
        return f"unknown ({type(e).__name__})"


# --------------------------------------------------------------------------- #
# Config                                                                       #
# --------------------------------------------------------------------------- #
def build_config(overrides=None):
    """The TINY local default config for exp_render_stack.py, plus overrides.

    Defaults are the honest local protocol: all-anchor kf=1 (pure
    anchor-vs-reference, cloud RUN 3's mode), fully measured (no modeled step),
    require_gpu=True so a missing Metal GPU fails LOUD instead of silently
    benchmarking the CPU."""
    cfg = {
        "scene": "classroom",
        "resolution": "960x540",
        "frames": 2,               # runner enforces >=2 (animated protocol minimum)
        "keyframe_every": 1,       # all-anchor: zero reprojection, pure anchor-vs-ref
        "ref_spp": 512,
        "draft_spp": 64,
        "adaptive_threshold": 0.01,
        "adaptive_min_samples": 16,
        "denoiser": "oidn",
        "denoise_guides": True,
        "light_tree": True,
        "bounces": 12,
        "hole_fill": "inpaint",    # kf=1 => no holes exist; skips the pointless
                                   # O-calibration render; modeled stays false
        "repair_enabled": False,
        "cam_motion": 1.0,
        "seed": 0,
        "device": "GPU",
        "require_gpu": True,       # fail-loud: never a silent CPU benchmark
    }
    for k, v in (overrides or {}).items():
        if v is not None:
            cfg[k] = v
    # mirror the runner's own clamp so the config we LOG matches what runs.
    cfg["frames"] = max(2, int(cfg["frames"]))
    return cfg


# --------------------------------------------------------------------------- #
# Python environment for the shim (never mutates the user's env)               #
# --------------------------------------------------------------------------- #
def deps_missing(python_bin, extra_env=None, runner=subprocess.run):
    """Which of REQUIRED_IMPORTS fail to import in python_bin? ([] = all good)."""
    # one interpreter launch probes every import.
    probe = (
        "import json,sys\n"
        "missing=[]\n"
        f"for m in {list(REQUIRED_IMPORTS)!r}:\n"
        "    try:\n"
        "        __import__(m)\n"
        "    except Exception:\n"
        "        missing.append(m)\n"
        "print(json.dumps(missing))\n"
    )
    env = dict(os.environ)
    env.update(extra_env or {})
    try:
        proc = runner([python_bin, "-c", probe], capture_output=True, text=True,
                      timeout=120, env=env)
        return json.loads((proc.stdout or "[]").strip().splitlines()[-1])
    except Exception:  # noqa: BLE001 — an unprobeable python is fully missing
        return list(REQUIRED_IMPORTS)


def ensure_python_env(cache_root, base_python=None, runner=subprocess.run):
    """Return (python_bin, extra_env, note) able to import REQUIRED_IMPORTS.

    Tiered, never touching the user's environment:
      0) base python already has everything -> use it directly.
      1) ONLY OpenEXR missing -> pip install --target <cache>/pysite OpenEXR and
         prepend to PYTHONPATH for the shim subprocess only.
      2) more missing -> private venv at <cache>/venv with all four deps.
    Raises RuntimeError (honest) if no tier can be satisfied."""
    base_python = base_python or sys.executable
    missing = deps_missing(base_python, runner=runner)
    if not missing:
        return base_python, {}, f"base python {base_python} has all deps"

    if missing == ["OpenEXR"]:
        pysite = os.path.join(cache_root, "pysite")
        extra = {"PYTHONPATH": pysite + os.pathsep + os.environ.get("PYTHONPATH", "")}
        if deps_missing(base_python, extra_env=extra, runner=runner) == []:
            return base_python, extra, f"base python + cached pysite {pysite}"
        os.makedirs(pysite, exist_ok=True)
        log(f"installing OpenEXR bindings into private pysite {pysite} (user env untouched)")
        proc = runner([base_python, "-m", "pip", "install", "--quiet",
                       "--target", pysite, "OpenEXR"],
                      capture_output=True, text=True, timeout=900)
        if proc.returncode == 0 and deps_missing(base_python, extra_env=extra,
                                                 runner=runner) == []:
            return base_python, extra, f"base python + fresh pysite {pysite}"
        log(f"pysite tier failed (rc={proc.returncode}); falling back to private venv")

    venv_dir = os.path.join(cache_root, "venv")
    venv_python = os.path.join(venv_dir, "bin", "python")
    if not _runnable(venv_python):
        log(f"creating private venv {venv_dir}")
        proc = runner([base_python, "-m", "venv", venv_dir],
                      capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise RuntimeError(f"venv creation failed: {(proc.stderr or '')[-400:]}")
    still = deps_missing(venv_python, runner=runner)
    if still:
        pkgs = [{"numpy": "numpy", "PIL": "pillow", "skimage": "scikit-image",
                 "OpenEXR": "OpenEXR"}[m] for m in still]
        log(f"installing {pkgs} into private venv (user env untouched)")
        proc = runner([venv_python, "-m", "pip", "install", "--quiet", *pkgs],
                      capture_output=True, text=True, timeout=1800)
        if proc.returncode != 0:
            raise RuntimeError(f"venv pip install failed: {(proc.stderr or '')[-400:]}")
    still = deps_missing(venv_python, runner=runner)
    if still:
        raise RuntimeError(
            f"could not satisfy python deps {still} in private venv {venv_dir}; "
            f"install manually, e.g.: {venv_python} -m pip install "
            "numpy pillow scikit-image OpenEXR")
    return venv_python, {}, f"private venv {venv_dir}"


# --------------------------------------------------------------------------- #
# The shim — imports the pod runner and repoints its pod paths to local ones.   #
# Env-driven so the shim source is a constant (unit-testable: it must compile). #
# --------------------------------------------------------------------------- #
SHIM_SOURCE = r'''
import json, os, sys

POD_DIR = os.environ["CX_SHIM_POD_DIR"]
sys.path.insert(0, POD_DIR)
import exp_render_stack as ers  # noqa: E402

# Repoint the pod-rooted constants to the local Mac (module globals are read at
# call time, so patching here reaches every helper). SCENES_DIR is derived from
# _CACHE_ROOT at import time, so it must be patched explicitly too.
ers.BLENDER_DIR = os.environ["CX_SHIM_BLENDER_DIR"]
ers.BLENDER_BIN = os.environ["CX_SHIM_BLENDER_BIN"]
ers.WORK_DIR    = os.environ["CX_SHIM_WORK_DIR"]
ers._CACHE_ROOT = os.environ["CX_SHIM_CACHE_ROOT"]
ers.SCENES_DIR  = os.environ["CX_SHIM_SCENES_DIR"]

sys.argv = ["exp_render_stack.py", os.environ["CX_SHIM_CONFIG_JSON"]]

# EXACT mirror of the runner's __main__ contract: last stdout line is ONE JSON
# object; any failure emits {"error":...} and exits 0 (never hangs, never lies).
try:
    ers.main()
except Exception as e:  # noqa: BLE001
    import traceback
    traceback.print_exc(file=sys.stderr)
    ers.emit({"error": f"{type(e).__name__}: {e}"})
    sys.exit(0)
'''


def parse_last_json_line(text):
    """The runner's contract: LAST stdout line that parses as a JSON object."""
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def run_anchor(config, blender_bin, python_bin, extra_env, cache_root,
               timeout_s=7200, runner=subprocess.run):
    """Run exp_render_stack.py via the shim on the LOCAL Blender. Returns the
    runner's metrics dict (which may honestly be {"error": ...})."""
    work_dir = os.path.join(cache_root, "work", "render_stack")
    os.makedirs(work_dir, exist_ok=True)
    shim_path = os.path.join(work_dir, "cx_local_shim.py")
    with open(shim_path, "w") as f:
        f.write(SHIM_SOURCE)

    env = dict(os.environ)
    env.update(extra_env or {})
    env.update({
        "CX_SHIM_POD_DIR": POD_DIR,
        "CX_SHIM_BLENDER_DIR": os.path.dirname(blender_bin),
        "CX_SHIM_BLENDER_BIN": blender_bin,
        "CX_SHIM_WORK_DIR": work_dir,
        "CX_SHIM_CACHE_ROOT": cache_root,
        "CX_SHIM_SCENES_DIR": os.path.join(cache_root, "scenes"),
        "CX_SHIM_CONFIG_JSON": json.dumps(config),
    })
    log(f"launching shim: python={python_bin} blender={blender_bin}")
    log(f"config: {json.dumps(config)}")
    # stderr is INHERITED so the runner's live progress logs stream through;
    # stdout is captured for the single metrics line.
    proc = runner([python_bin, shim_path], env=env, stdout=subprocess.PIPE,
                  stderr=None, text=True, timeout=timeout_s)
    metrics = parse_last_json_line(proc.stdout)
    if metrics is None:
        return {"error": f"shim produced no JSON metrics line "
                         f"(rc={proc.returncode}, stdout tail: "
                         f"{(proc.stdout or '')[-400:]!r})"}
    return metrics


# --------------------------------------------------------------------------- #
# Ledger                                                                       #
# --------------------------------------------------------------------------- #
def host_info(blender_bin=None, blender_ver=None):
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "blender_bin": blender_bin,
        "blender_version": blender_ver,
    }


def ledger_row(metrics, config, host, evidence=EVIDENCE_LABEL):
    return {
        "event": "local_metal_anchor_receipt",
        "evidence": evidence,
        "row": metrics,
        "config": config,
        "host": host,
        "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def append_ledger(path, row):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(row) + "\n")
    return path


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--blender", help="explicit local Blender binary")
    p.add_argument("--cache-root", default=DEFAULT_CACHE_ROOT)
    p.add_argument("--ledger", default=DEFAULT_LEDGER)
    p.add_argument("--no-ledger", action="store_true",
                   help="do not append to the ledger (smoke runs)")
    p.add_argument("--dry-run", action="store_true",
                   help="discovery + config only; do not render")
    p.add_argument("--timeout-s", type=int, default=7200)
    p.add_argument("--frames", type=int)
    p.add_argument("--keyframe-every", type=int, dest="keyframe_every")
    p.add_argument("--ref-spp", type=int, dest="ref_spp")
    p.add_argument("--draft-spp", type=int, dest="draft_spp")
    p.add_argument("--resolution")
    p.add_argument("--scene")
    p.add_argument("--config-json", help="raw JSON overrides merged LAST")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    overrides = {k: getattr(args, k) for k in
                 ("frames", "keyframe_every", "ref_spp", "draft_spp",
                  "resolution", "scene")}
    if args.config_json:
        overrides.update(json.loads(args.config_json))
    config = build_config(overrides)

    blender_bin = discover_blender(explicit=args.blender)
    if blender_bin is None:
        emit({
            "error": "no local Blender binary found "
                     f"(checked --blender, ${BLENDER_ENV_VAR}, "
                     f"{DEFAULT_BLENDER_CANDIDATES[0]}, and `blender` on PATH). "
                     "Install Blender 4.2 LTS (macOS Apple Silicon) from "
                     "https://www.blender.org/download/lts/4-2/ so the binary "
                     "exists at /Applications/Blender.app/Contents/MacOS/Blender, "
                     "then rerun.",
            "status": "PENDING-OWNER-HARDWARE",
        })
        return 0

    ver = blender_version(blender_bin)
    log(f"local Blender: {blender_bin} ({ver})")

    if args.dry_run:
        emit({"dry_run": True, "blender_bin": blender_bin,
              "blender_version": ver, "config": config,
              "ledger": args.ledger, "cache_root": args.cache_root})
        return 0

    os.makedirs(args.cache_root, exist_ok=True)
    try:
        python_bin, extra_env, env_note = ensure_python_env(args.cache_root)
    except RuntimeError as e:
        emit({"error": f"python env for the shim could not be provisioned: {e}"})
        return 0
    log(f"shim python: {python_bin} ({env_note})")

    try:
        metrics = run_anchor(config, blender_bin, python_bin, extra_env,
                             args.cache_root, timeout_s=args.timeout_s)
    except subprocess.TimeoutExpired:
        emit({"error": f"local anchor run timed out after {args.timeout_s}s"})
        return 0

    if "error" in metrics:
        emit(metrics)  # honest failure, verbatim; nothing is ledgered
        return 0

    device = str(metrics.get("device", ""))
    if "METAL" not in device.upper():
        # A receipt not traced on the Metal GPU must NEVER be labeled local-metal.
        emit({"error": f"run completed but device={device!r} is not a Metal GPU; "
                       "refusing to ledger a mislabeled receipt", "row": metrics})
        return 0

    metrics_out = dict(metrics)
    metrics_out["evidence"] = EVIDENCE_LABEL
    if not args.no_ledger:
        row = ledger_row(metrics, config, host_info(blender_bin, ver))
        append_ledger(args.ledger, row)
        log(f"ledger row appended -> {args.ledger}")
    emit(metrics_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
