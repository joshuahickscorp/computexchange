#!/usr/bin/env python3
"""No-build standalone Cycles quality ladder on RunPod.

This answers a different question from sample fan-out: with a prebuilt Cycles
runtime root, how far can we lower samples before global or worst-tile quality
falls apart? It keeps the pod lifecycle identical to the existing spec-lab
drivers: tracked pod state, remote watchdog, ledger rows, and finally teardown.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

import cycles_fork  # noqa: E402
import runpod  # noqa: E402


POD_IMAGE = "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"
POD_DISK_GB = 90
WATCHDOG_TTL_S = 7200
LEDGER = os.path.join(
    REPO,
    "docs/speed-lane-reports/spec-lab/cycles_quality_ladder_ledger.jsonl",
)
HOPPER_TAR = os.path.join(
    REPO,
    ".artifacts/cycles/runtime/cx-cycles-hopper-sm90-batch-runtime-20260708.tar.gz",
)
ADA_TAR = os.path.join(
    REPO,
    ".artifacts/cycles/runtime/cx-cycles-ada-sm89-batch-runtime-20260708.tar.gz",
)

# ARCH-PINNED capability tiers, NOT price ladders — gpu-provisioning-policy exception
# clause: each tier exists solely to run the matching prebuilt cx-cycles runtime
# tarball above (HOPPER_TAR = sm_90 cubins, ADA_TAR = sm_89 cubins). A cubin only
# loads on its own arch, so the SKU set IS the experiment substrate and the policy's
# A100->H100->H200 ladder does not apply (an A100 sm_80 cannot load either tarball).
# All HOPPER rungs are policy-tier (H100/H200) anyway; the ADA rungs are allowed ONLY
# under this exception (test_cycles_quality_ladder.py asserts the ADA contents).
# NOTE: these tars are CX-FORK builds with real sm_90/sm_89 cubins — unrelated to the
# official Blender 4.2 tarball, whose sm_90 coverage is doubted (2026-07-09 two-pod
# H100 probe-timeout evidence; see run_integrated_production_benchmark.py).
HOPPER_GPU_PLAN = [
    ("NVIDIA H200", "COMMUNITY"),
    ("NVIDIA H100 NVL", "COMMUNITY"),
    ("NVIDIA H100 80GB HBM3", "COMMUNITY"),
    ("NVIDIA H100 PCIe", "COMMUNITY"),
    ("NVIDIA H200", "SECURE"),
    ("NVIDIA H100 NVL", "SECURE"),
    ("NVIDIA H100 80GB HBM3", "SECURE"),
    ("NVIDIA H100 PCIe", "SECURE"),
]
ADA_GPU_PLAN = [
    ("NVIDIA GeForce RTX 4090", "COMMUNITY"),
    ("NVIDIA GeForce RTX 4090", "SECURE"),
    ("NVIDIA L40", "COMMUNITY"),
    ("NVIDIA L40", "SECURE"),
    ("NVIDIA L40S", "COMMUNITY"),
    ("NVIDIA L40S", "SECURE"),
    ("NVIDIA RTX 6000 Ada Generation", "COMMUNITY"),
    ("NVIDIA RTX 6000 Ada Generation", "SECURE"),
    ("NVIDIA RTX 5000 Ada Generation", "COMMUNITY"),
    ("NVIDIA RTX 4000 Ada Generation", "COMMUNITY"),
    ("NVIDIA L4", "SECURE"),
]


def log(message: str) -> None:
    print(f"[cycles-quality {time.strftime('%H:%M:%S')}] {message}", flush=True)


def append_ledger(record: dict) -> None:
    cycles_fork.append_ledger(record, ledger=LEDGER)


def parse_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def parse_int_csv(value: str) -> list[int]:
    return [int(part) for part in parse_csv(value)]


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "default"


def default_tar_for_tier(tier: str) -> str:
    return ADA_TAR if tier == "ada" else HOPPER_TAR


def gpu_plan_for_tier(tier: str) -> list[tuple[str, str]]:
    return ADA_GPU_PLAN if tier == "ada" else HOPPER_GPU_PLAN


def run_stage(pod: dict, name: str, cmd: str, timeout_s: int) -> dict:
    log(f"STAGE {name}: start (timeout {timeout_s}s)")
    t0 = time.time()
    try:
        rc, out, err = runpod.ssh(pod, cmd, timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001
        rec = {
            "stage": name,
            "ok": False,
            "elapsed_s": round(time.time() - t0, 1),
            "out_tail": "",
            "err_tail": f"{type(exc).__name__}: {exc}"[-2400:],
        }
        append_ledger({"event": "stage", **rec})
        return rec
    rec = {
        "stage": name,
        "ok": rc == 0,
        "elapsed_s": round(time.time() - t0, 1),
        "out_tail": (out or "")[-20000:],
        "err_tail": (err or "")[-2400:],
    }
    append_ledger({"event": "stage", **rec})
    log(f"STAGE {name}: rc={rc} elapsed={rec['elapsed_s']}s ok={rec['ok']}")
    if rec["out_tail"].strip():
        log(f"{name} stdout tail:\n{rec['out_tail']}")
    if not rec["ok"] and rec["err_tail"].strip():
        log(f"{name} stderr tail:\n{rec['err_tail']}")
    return rec


def transfer_preflight(
    pod: dict,
    size_mb: int,
    min_mbps: float,
    timeout_s: int,
) -> dict:
    if size_mb <= 0:
        rec = {
            "stage": "transfer_preflight",
            "ok": True,
            "elapsed_s": 0.0,
            "out_tail": "disabled",
            "err_tail": "",
            "mbps": None,
        }
        append_ledger({"event": "stage", **rec})
        return rec
    log(
        "STAGE transfer_preflight: "
        f"start ({size_mb} MiB, min {min_mbps:.3f} MiB/s, timeout {timeout_s}s)"
    )
    fd, local_path = tempfile.mkstemp(prefix="cx-transfer-preflight-", suffix=".bin")
    remote_path = "/tmp/cx_transfer_preflight.bin"
    t0 = time.time()
    try:
        with os.fdopen(fd, "wb") as f:
            chunk = b"cx-transfer-preflight\n" * 32768
            remaining = size_mb * 1024 * 1024
            while remaining > 0:
                part = chunk[: min(len(chunk), remaining)]
                f.write(part)
                remaining -= len(part)
        ok, err = runpod.scp_to(pod, local_path, remote_path, timeout=timeout_s)
        elapsed = max(time.time() - t0, 1e-6)
        mbps = size_mb / elapsed
        rec = {
            "stage": "transfer_preflight",
            "ok": bool(ok and mbps >= min_mbps),
            "elapsed_s": round(elapsed, 1),
            "out_tail": f"size_mib={size_mb} mbps={mbps:.4f}",
            "err_tail": (err or "")[-2400:],
            "mbps": round(mbps, 4),
            "min_mbps": min_mbps,
        }
        if ok:
            runpod.ssh(pod, f"rm -f {remote_path}", timeout=30)
    except Exception as exc:  # noqa: BLE001
        rec = {
            "stage": "transfer_preflight",
            "ok": False,
            "elapsed_s": round(time.time() - t0, 1),
            "out_tail": "",
            "err_tail": f"{type(exc).__name__}: {exc}"[-2400:],
            "mbps": None,
            "min_mbps": min_mbps,
        }
    finally:
        try:
            os.unlink(local_path)
        except OSError:
            pass
    append_ledger({"event": "stage", **rec})
    log(
        "STAGE transfer_preflight: "
        f"elapsed={rec['elapsed_s']}s ok={rec['ok']} mbps={rec.get('mbps')}"
    )
    return rec


def upload_prebuilt_root(
    pod: dict,
    tar_path: str,
    root: str,
    upload_timeout_s: int,
) -> list[dict]:
    log(f"STAGE upload_prebuilt_root: start ({tar_path})")
    t0 = time.time()
    remote_tar = "/tmp/cx_cycles_quality_root.tar.gz"
    ok, err = runpod.scp_to(pod, tar_path, remote_tar, timeout=upload_timeout_s)
    upload_rec = {
        "stage": "upload_prebuilt_root",
        "ok": ok,
        "elapsed_s": round(time.time() - t0, 1),
        "out_tail": tar_path if ok else "",
        "err_tail": (err or "")[-2400:],
    }
    append_ledger({"event": "stage", **upload_rec})
    log(
        "STAGE upload_prebuilt_root: "
        f"elapsed={upload_rec['elapsed_s']}s ok={upload_rec['ok']}"
    )
    if not ok:
        return [upload_rec]
    cmd = (
        "set -e -o pipefail; "
        "rm -rf " + cycles_fork.shell(root) + "; "
        "mkdir -p " + cycles_fork.shell(root) + "; "
        "tar -xf " + cycles_fork.shell(remote_tar) + " -C " + cycles_fork.shell(root) + "; "
        "test -x " + cycles_fork.shell(cycles_fork.binary_path(root)) + "; "
        "test -d " + cycles_fork.shell(os.path.join(root, "examples")) + "; "
        "du -sh " + cycles_fork.shell(root) + "; "
        "echo CX_CYCLES_PREBUILT_ROOT_OK=1"
    )
    return [upload_rec, run_stage(pod, "extract_prebuilt_root", cmd, 1800)]


def synthetic_scene_cmd(root: str, name: str = "cx_many_glass.xml") -> str:
    """Create a heavier XML scene from built-in Cycles example assets."""
    scene = os.path.join(root, "examples", name)
    script = f"""
from pathlib import Path

root = Path({root!r})
out = Path({scene!r})
grid = [-2, -1, 0, 1, 2]
parts = [
    '<cycles>',
    '<camera width="1280" height="720" />',
    '<transform translate="0 1.7 -7.5" rotate="12 1 0 0">',
    '  <camera type="perspective" />',
    '</transform>',
    '<integrator max_bounce="12" transparent_max_bounce="8" />',
    '<background>',
    '  <sky_texture name="sky" sky_type="hosek_wilkie" />',
    '  <background name="bg" strength="3.0" />',
    '  <connect from="sky color" to="bg color" />',
    '  <connect from="bg background" to="output surface" />',
    '</background>',
    '<shader name="glass">',
    '  <noise_texture name="noise" scale="4.0"/>',
    '  <glass_bsdf name="bsdf" distribution="beckmann" IOR="1.45" roughness="0.28" />',
    '  <connect from="noise color" to="bsdf color" />',
    '  <connect from="bsdf bsdf" to="output surface" />',
    '</shader>',
    '<shader name="floor">',
    '  <checker_texture name="checker" scale="6.0" color1="0.75, 0.75, 0.75" color2="0.08, 0.12, 0.16" />',
    '  <glossy_bsdf name="floor_bsdf" distribution="beckmann" roughness="0.18"/>',
    '  <connect from="checker color" to="floor_bsdf color" />',
    '  <connect from="floor_bsdf bsdf" to="output surface" />',
    '</shader>',
]
for i, (x, z) in enumerate((x, z) for x in grid for z in grid):
    rot = 20 + i * 13
    y = 0.0 if (i % 3) else 0.2
    parts.extend([
        f'<transform translate="{{x * 0.9:.2f}} {{y:.2f}} {{z * 0.65:.2f}}" '
        f'rotate="{{rot % 360}} 0 1 0" scale="0.34 0.34 0.34">',
        '  <transform rotate="180 0 1 1">',
        '    <state interpolation="smooth" shader="glass">',
        '      <include src="./objects/suzanne.xml" />',
        '    </state>',
        '  </transform>',
        '</transform>',
    ])
light_positions = [
    (-3.5, 4.0, -2.0, '0.9 0.35 0.2', 250),
    (3.5, 4.0, -2.0, '0.2 0.45 0.9', 250),
    (0.0, 5.0, 2.0, '0.9 0.9 0.8', 360),
    (-1.2, 2.7, 2.2, '0.35 0.9 0.55', 120),
    (1.2, 2.7, 2.2, '0.9 0.5 0.9', 120),
]
for i, (x, y, z, color, strength) in enumerate(light_positions):
    parts.extend([
        f'<shader name="light_shader_{{i}}">',
        f'  <emission name="emission" color="{{color}}" strength="{{strength}}" />',
        '  <connect from="emission emission" to="output surface" />',
        '</shader>',
        f'<state shader="light_shader_{{i}}">',
        f'  <light type="area" co="{{x}} {{y}} {{z}}" size="0.65" '
        'dir="0.0, -1.0, 0.0" axisu="1.0, 0.0, 0.0" axisv="0.0, 0.0, 1.0" />',
        '</state>',
    ])
parts.extend([
    '<transform rotate="90 1 0 0">',
    '  <transform translate="0 0 1.05">',
    '    <state shader="floor">',
    '      <mesh P="-8 8 0  8 8 0  8 -8 0  -8 -8 0" nverts="4" verts="0 1 2 3" />',
    '    </state>',
    '  </transform>',
    '</transform>',
    '</cycles>',
])
out.write_text("\\n".join(parts) + "\\n")
print(f"CX_SYNTHETIC_SCENE={{out}}")
print(f"CX_SYNTHETIC_SCENE_BYTES={{out.stat().st_size}}")
"""
    return "set -e; cd " + cycles_fork.shell(root) + "; python3 - <<'PY'\n" + script + "PY\n"


def render_arg(device: str, disable_adaptive_sampling: bool) -> str:
    parts = []
    if device:
        parts.extend(["--device", cycles_fork.shell(device)])
    if disable_adaptive_sampling:
        parts.append("--disable-adaptive-sampling")
    return " ".join(parts)


def quality_ladder_cmd(
    root: str,
    scene: str,
    ref_samples: int,
    draft_samples: list[int],
    device: str,
    disable_adaptive_sampling: bool,
    with_oidn: bool = False,
    oidn_device: str = "cpu",
    grid: int = 8,
) -> str:
    scene_name = safe_name(os.path.basename(scene).replace(".xml", ""))
    prefix = f"/tmp/cx_quality_{scene_name}_{ref_samples}"
    q_root = cycles_fork.shell(root)
    q_bin = cycles_fork.shell(cycles_fork.binary_path(root))
    q_scene = cycles_fork.shell("examples/" + scene)
    ref_exr = f"{prefix}_ref.exr"
    ref_png = f"{prefix}_ref.png"
    draft_pairs = [
        (samples, f"{prefix}_draft_{samples}.exr", f"{prefix}_draft_{samples}.png")
        for samples in draft_samples
    ]
    denoise_pairs = [
        (samples, f"{prefix}_draft_{samples}_oidn.exr", f"{prefix}_draft_{samples}_oidn.png")
        for samples in draft_samples
    ]
    common_args = render_arg(device, disable_adaptive_sampling)
    if common_args:
        common_args += " "

    parts = [
        "set -e -o pipefail; export DEBIAN_FRONTEND=noninteractive; ",
        "apt-get update >/dev/null 2>&1; ",
        "apt-get install -y openimageio-tools python3-numpy python3-pil time "
        ">/dev/null 2>&1; ",
        "cd " + q_root + "; ",
        "rm -f " + cycles_fork.shell(prefix) + "_* /tmp/cx_quality_time_*.txt; ",
        "export LD_LIBRARY_PATH=" + cycles_fork.shell(os.path.join(root, "install", "lib"))
        + ":${LD_LIBRARY_PATH:-}; ",
        "export PYTHONPATH=/usr/lib/python3/dist-packages:"
        "/usr/local/lib/python3.12/dist-packages:"
        "/usr/local/lib/python3.11/dist-packages:${PYTHONPATH:-}; ",
        "test -f " + q_scene + "; ",
        f"echo CX_QUALITY_SCENE={scene}; ",
        f"echo CX_QUALITY_REF_SAMPLES={ref_samples}; ",
        "echo CX_QUALITY_DEVICE=" + cycles_fork.shell(device or "default") + "; ",
        f"echo CX_QUALITY_DISABLE_ADAPTIVE={int(disable_adaptive_sampling)}; ",
        f"echo CX_QUALITY_WITH_OIDN={int(with_oidn)}; ",
        "/usr/bin/time -f 'CX_REF_TIME_S=%e' -o /tmp/cx_quality_time_ref.txt ",
        q_bin + " " + common_args + f"--samples {ref_samples} --output "
        + cycles_fork.shell(ref_exr) + " " + q_scene
        + " >/tmp/cx_quality_ref.log 2>&1; ",
        "cat /tmp/cx_quality_time_ref.txt; ",
        "oiiotool " + cycles_fork.shell(ref_exr) + " -d uint8 -o "
        + cycles_fork.shell(ref_png) + " >/tmp/cx_quality_ref_png.log 2>&1; ",
    ]
    for samples, exr, png in draft_pairs:
        parts.extend([
            f"/usr/bin/time -f 'CX_DRAFT_TIME_S_{samples}=%e' "
            f"-o /tmp/cx_quality_time_draft_{samples}.txt ",
            q_bin + " " + common_args + f"--samples {samples} --output "
            + cycles_fork.shell(exr) + " " + q_scene
            + f" >/tmp/cx_quality_draft_{samples}.log 2>&1; ",
            f"cat /tmp/cx_quality_time_draft_{samples}.txt; ",
            "oiiotool " + cycles_fork.shell(exr) + " -d uint8 -o "
            + cycles_fork.shell(png)
            + f" >/tmp/cx_quality_draft_{samples}_png.log 2>&1; ",
        ])

    if with_oidn:
        denoise_py = f"""
import ctypes
import json
import os
import time

import OpenEXR
import Imath
import numpy as np

root = {root!r}
oidn_device = {oidn_device!r}
drafts = {[(s, p, out) for (s, p, _), (_, out, _) in zip(draft_pairs, denoise_pairs)]!r}
lib_path = os.path.join(root, "install", "lib", "libOpenImageDenoise.so.2")

OIDN_DEVICE_TYPE_DEFAULT = 0
OIDN_DEVICE_TYPE_CPU = 1
OIDN_DEVICE_TYPE_CUDA = 3
OIDN_FORMAT_FLOAT3 = 3
OIDN_ERROR_NONE = 0

device_type = {{
    "default": OIDN_DEVICE_TYPE_DEFAULT,
    "cpu": OIDN_DEVICE_TYPE_CPU,
    "cuda": OIDN_DEVICE_TYPE_CUDA,
}}.get(oidn_device)
if device_type is None:
    raise SystemExit(f"bad oidn device {{oidn_device!r}}")

if oidn_device == "cuda":
    os.environ["OIDN_DEFAULT_DEVICE"] = "cuda"
elif oidn_device == "cpu":
    os.environ["OIDN_DEFAULT_DEVICE"] = "cpu"

lib = ctypes.CDLL(lib_path)
c_void_p = ctypes.c_void_p
c_char_p = ctypes.c_char_p
c_size_t = ctypes.c_size_t
c_int = ctypes.c_int
c_bool = ctypes.c_bool

lib.oidnNewDevice.argtypes = [c_int]
lib.oidnNewDevice.restype = c_void_p
lib.oidnCommitDevice.argtypes = [c_void_p]
lib.oidnNewBuffer.argtypes = [c_void_p, c_size_t]
lib.oidnNewBuffer.restype = c_void_p
lib.oidnGetBufferData.argtypes = [c_void_p]
lib.oidnGetBufferData.restype = c_void_p
lib.oidnNewFilter.argtypes = [c_void_p, c_char_p]
lib.oidnNewFilter.restype = c_void_p
lib.oidnSetFilterImage.argtypes = [
    c_void_p, c_char_p, c_void_p, c_int,
    c_size_t, c_size_t, c_size_t, c_size_t, c_size_t,
]
lib.oidnSetFilterBool.argtypes = [c_void_p, c_char_p, c_bool]
lib.oidnCommitFilter.argtypes = [c_void_p]
lib.oidnExecuteFilter.argtypes = [c_void_p]
lib.oidnGetDeviceError.argtypes = [c_void_p, ctypes.POINTER(c_char_p)]
lib.oidnGetDeviceError.restype = c_int
lib.oidnReleaseBuffer.argtypes = [c_void_p]
lib.oidnReleaseFilter.argtypes = [c_void_p]
lib.oidnReleaseDevice.argtypes = [c_void_p]

def check(device, where):
    msg = c_char_p()
    err = lib.oidnGetDeviceError(device, ctypes.byref(msg))
    if err != OIDN_ERROR_NONE:
        text = msg.value.decode("utf-8", "replace") if msg.value else ""
        raise RuntimeError(f"OIDN error after {{where}}: code={{err}} {{text}}")

def read_exr(path):
    f = OpenEXR.InputFile(path)
    header = f.header()
    dw = header["dataWindow"]
    w = dw.max.x - dw.min.x + 1
    h = dw.max.y - dw.min.y + 1
    names = list(header["channels"].keys())
    pt = Imath.PixelType(Imath.PixelType.FLOAT)

    def find_channel(suffixes):
        for suffix in suffixes:
            for name in names:
                if name.endswith(suffix):
                    return name
        return None

    chans = [
        find_channel([".Combined.R", "Combined.R", ".R", "R"]),
        find_channel([".Combined.G", "Combined.G", ".G", "G"]),
        find_channel([".Combined.B", "Combined.B", ".B", "B"]),
    ]
    if not all(chans):
        raise RuntimeError(f"could not find RGB channels in {{path}}; channels={{names[:20]}}")
    arrays = [
        np.frombuffer(f.channel(name, pt), dtype=np.float32).reshape(h, w).copy()
        for name in chans
    ]
    f.close()
    return np.ascontiguousarray(np.stack(arrays, axis=-1), dtype=np.float32)

def write_exr(path, arr):
    arr = np.ascontiguousarray(arr, dtype=np.float32)
    h, w, c = arr.shape
    if c < 3:
        raise RuntimeError(f"expected RGB array, got shape {{arr.shape}}")
    header = OpenEXR.Header(w, h)
    pt = Imath.PixelType(Imath.PixelType.FLOAT)
    header["channels"] = {{name: Imath.Channel(pt) for name in ("R", "G", "B")}}
    out = OpenEXR.OutputFile(path, header)
    out.writePixels({{
        "R": arr[..., 0].tobytes(),
        "G": arr[..., 1].tobytes(),
        "B": arr[..., 2].tobytes(),
    }})
    out.close()

device = lib.oidnNewDevice(device_type)
if not device:
    msg = c_char_p()
    lib.oidnGetDeviceError(c_void_p(), ctypes.byref(msg))
    raise RuntimeError("oidnNewDevice failed: " + (
        msg.value.decode("utf-8", "replace") if msg.value else "no message"
    ))
lib.oidnCommitDevice(device)
check(device, "commit device")

try:
    for samples, src, dst in drafts:
        color = read_exr(src)
        h, w, _ = color.shape
        nbytes = int(color.nbytes)
        out_arr = np.empty_like(color)
        color_buf = lib.oidnNewBuffer(device, nbytes)
        output_buf = lib.oidnNewBuffer(device, nbytes)
        if not color_buf or not output_buf:
            raise RuntimeError("oidnNewBuffer failed")
        try:
            ctypes.memmove(lib.oidnGetBufferData(color_buf), color.ctypes.data, nbytes)
            filt = lib.oidnNewFilter(device, b"RT")
            if not filt:
                raise RuntimeError("oidnNewFilter failed")
            try:
                lib.oidnSetFilterImage(
                    filt, b"color", color_buf, OIDN_FORMAT_FLOAT3,
                    w, h, 0, 0, 0,
                )
                lib.oidnSetFilterImage(
                    filt, b"output", output_buf, OIDN_FORMAT_FLOAT3,
                    w, h, 0, 0, 0,
                )
                lib.oidnSetFilterBool(filt, b"hdr", True)
                t0 = time.time()
                lib.oidnCommitFilter(filt)
                lib.oidnExecuteFilter(filt)
                elapsed = time.time() - t0
                check(device, f"execute samples={{samples}}")
            finally:
                lib.oidnReleaseFilter(filt)
            ctypes.memmove(out_arr.ctypes.data, lib.oidnGetBufferData(output_buf), nbytes)
            write_exr(dst, out_arr)
            with open(f"/tmp/cx_quality_time_oidn_{{samples}}.txt", "w") as f:
                f.write(f"CX_OIDN_TIME_S_{{samples}}={{elapsed:.6f}}\\n")
            print(
                "CX_OIDN_ROW=" + json.dumps(
                    {{
                        "samples": samples,
                        "oidn_device": oidn_device,
                        "oidn_time_s": round(elapsed, 6),
                        "output": dst,
                    }},
                    sort_keys=True,
                )
            )
        finally:
            lib.oidnReleaseBuffer(color_buf)
            lib.oidnReleaseBuffer(output_buf)
finally:
    lib.oidnReleaseDevice(device)
"""
        parts.append(
            "python3 -m pip install --break-system-packages --no-cache-dir "
            "OpenEXR Imath >/tmp/cx_quality_pip_oidn.log 2>&1; "
            "python3 - <<'PY'\n" + denoise_py + "PY\n"
        )
        for samples, _, png in denoise_pairs:
            exr = f"{prefix}_draft_{samples}_oidn.exr"
            parts.extend([
                "cat /tmp/cx_quality_time_oidn_" + str(samples) + ".txt; ",
                "oiiotool " + cycles_fork.shell(exr) + " -d uint8 -o "
                + cycles_fork.shell(png)
                + f" >/tmp/cx_quality_draft_{samples}_oidn_png.log 2>&1; ",
            ])

    metrics_py = f"""
from PIL import Image
import json
import numpy as np
from pathlib import Path

scene = {scene!r}
ref_samples = {ref_samples}
raw_drafts = {[(s, p) for s, _, p in draft_pairs]!r}
oidn_drafts = {[(s, p) for s, _, p in denoise_pairs] if with_oidn else []!r}
ref_png = {ref_png!r}
grid = {grid}
disable_adaptive = {disable_adaptive_sampling!r}
device = {device or "default"!r}
oidn_device = {oidn_device!r}

def read_time(path, key):
    for line in Path(path).read_text().splitlines():
        if line.startswith(key + "="):
            return float(line.split("=", 1)[1])
    raise SystemExit(f"missing {{key}} in {{path}}")

def load(path):
    arr = np.asarray(Image.open(path).convert("RGB"), dtype=np.float64) / 255.0
    return 0.2126 * arr[..., 0] + 0.7152 * arr[..., 1] + 0.0722 * arr[..., 2]

def ssim(a, b):
    c1 = 0.01 ** 2
    c2 = 0.03 ** 2
    mux = float(a.mean())
    muy = float(b.mean())
    vx = float(((a - mux) ** 2).mean())
    vy = float(((b - muy) ** 2).mean())
    cov = float(((a - mux) * (b - muy)).mean())
    denom = (mux * mux + muy * muy + c1) * (vx + vy + c2)
    if denom == 0:
        return 1.0 if np.array_equal(a, b) else 0.0
    return ((2 * mux * muy + c1) * (2 * cov + c2)) / denom

ref = load(ref_png)
ref_time = read_time("/tmp/cx_quality_time_ref.txt", "CX_REF_TIME_S")
h, w = ref.shape
drafts = []
for samples, png in raw_drafts:
    drafts.append((samples, "raw", png, 0.0))
for samples, png in oidn_drafts:
    drafts.append((
        samples,
        "oidn",
        png,
        read_time(f"/tmp/cx_quality_time_oidn_{{samples}}.txt", f"CX_OIDN_TIME_S_{{samples}}"),
    ))
for samples, variant, png, denoise_time in drafts:
    draft = load(png)
    if draft.shape != ref.shape:
        raise SystemExit(f"shape mismatch for {{png}}: {{draft.shape}} vs {{ref.shape}}")
    tile_scores = []
    for yi in range(grid):
        y0 = yi * h // grid
        y1 = (yi + 1) * h // grid
        for xi in range(grid):
            x0 = xi * w // grid
            x1 = (xi + 1) * w // grid
            if y1 > y0 and x1 > x0:
                tile_scores.append(ssim(ref[y0:y1, x0:x1], draft[y0:y1, x0:x1]))
    tiles = np.asarray(tile_scores, dtype=np.float64)
    delta = np.abs(ref - draft)
    draft_time = read_time(
        f"/tmp/cx_quality_time_draft_{{samples}}.txt",
        f"CX_DRAFT_TIME_S_{{samples}}",
    )
    total_time = draft_time + denoise_time
    row = {{
        "scene": scene,
        "device": device,
        "variant": variant,
        "oidn_device": oidn_device if variant == "oidn" else None,
        "disable_adaptive_sampling": disable_adaptive,
        "ref_samples": ref_samples,
        "samples": samples,
        "ref_time_s": round(ref_time, 4),
        "draft_time_s": round(draft_time, 4),
        "denoise_time_s": round(denoise_time, 6),
        "total_time_s": round(total_time, 6),
        "speedup_vs_ref": round(ref_time / total_time, 4) if total_time > 0 else None,
        "quality": round(float(ssim(ref, draft)), 9),
        "worst_tile_ssim": round(float(tiles.min()) if len(tiles) else 1.0, 9),
        "p5_tile_ssim": round(float(np.percentile(tiles, 5)) if len(tiles) else 1.0, 9),
        "tile_count": int(len(tiles)),
        "png_mae": round(float(delta.mean()), 9),
        "png_maxe": round(float(delta.max()), 9),
    }}
    if row["quality"] >= 0.98 and row["worst_tile_ssim"] >= 0.95:
        row["tier"] = "delivery"
    elif row["quality"] >= 0.90 and row["worst_tile_ssim"] >= 0.85:
        row["tier"] = "preview"
    else:
        row["tier"] = "fail"
    print("CX_QUALITY_ROW=" + json.dumps(row, sort_keys=True))
"""
    parts.extend([
        "python3 - <<'PY'\n" + metrics_py + "PY\n",
        "ls -lh " + " ".join(
            cycles_fork.shell(path)
            for path in [ref_exr, ref_png, *[p for _, p, _ in draft_pairs]]
        ) + "; ",
        f"echo CX_QUALITY_LADDER_OK scene={scene} ref_samples={ref_samples}",
    ])
    return "".join(parts)


def parse_quality_rows(stage: dict) -> list[dict]:
    rows = []
    for match in re.finditer(r"^CX_QUALITY_ROW=(\{.*\})$", stage.get("out_tail", ""), re.M):
        rows.append(json.loads(match.group(1)))
    return rows


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {"row_count": 0}
    delivery = [row for row in rows if row.get("tier") == "delivery"]
    preview = [row for row in rows if row.get("tier") in ("delivery", "preview")]
    best_delivery = max(delivery, key=lambda r: r.get("speedup_vs_ref") or 0, default=None)
    best_preview = max(preview, key=lambda r: r.get("speedup_vs_ref") or 0, default=None)
    best_quality = max(rows, key=lambda r: r.get("quality") or 0)
    return {
        "row_count": len(rows),
        "best_delivery": best_delivery,
        "best_preview": best_preview,
        "best_quality": best_quality,
        "scenes": sorted({row["scene"] for row in rows}),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--remote-root", default=cycles_fork.DEFAULT_REMOTE_ROOT)
    parser.add_argument("--gpu-tier", choices=("hopper", "ada"), default="hopper")
    parser.add_argument("--prebuilt-root-tar", default="")
    parser.add_argument("--scene", default="scene_world_volume.xml,scene_cube_volume.xml")
    parser.add_argument("--include-synthetic-scene", action="store_true")
    parser.add_argument("--synthetic-name", default="cx_many_glass.xml")
    parser.add_argument("--ref-samples", type=int, default=4096)
    parser.add_argument("--draft-samples", default="64,128,256,512,1024")
    parser.add_argument("--device", default="CUDA")
    parser.add_argument("--allow-adaptive-sampling", action="store_true",
                        help="use Cycles default adaptive behavior instead of fixed samples")
    parser.add_argument("--with-oidn", action="store_true",
                        help="also run standalone OIDN on each draft and score the denoised output")
    parser.add_argument("--oidn-device", choices=("cpu", "cuda", "default"), default="cpu",
                        help="OIDN device type for --with-oidn; CPU is the stable first probe")
    parser.add_argument("--min-balance", type=float, default=4.0)
    parser.add_argument("--max-minutes", type=int, default=90)
    parser.add_argument("--stage-timeout-s", type=int, default=3600)
    parser.add_argument("--upload-timeout-s", type=int, default=900,
                        help="timeout for full runtime-root tar upload")
    parser.add_argument("--transfer-preflight-mb", type=int, default=4,
                        help="MiB to upload before the full runtime tar; 0 disables")
    parser.add_argument("--min-transfer-mbps", type=float, default=0.25,
                        help="minimum preflight throughput in MiB/s before full tar upload")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    tar_path = args.prebuilt_root_tar or default_tar_for_tier(args.gpu_tier)
    if not os.path.isfile(tar_path):
        raise SystemExit(f"prebuilt runtime tar not found: {tar_path}")
    scenes = parse_csv(args.scene)
    if args.include_synthetic_scene:
        scenes.append(args.synthetic_name)
    draft_samples = parse_int_csv(args.draft_samples)
    if not draft_samples:
        raise SystemExit("--draft-samples must include at least one sample count")
    if args.ref_samples <= max(draft_samples):
        raise SystemExit("--ref-samples must be larger than all --draft-samples")

    manifest = {
        "root": args.remote_root,
        "gpu_tier": args.gpu_tier,
        "prebuilt_root_tar": tar_path,
        "scene": args.scene,
        "include_synthetic_scene": args.include_synthetic_scene,
        "scenes": scenes,
        "ref_samples": args.ref_samples,
        "draft_samples": draft_samples,
        "device": args.device or "default",
        "disable_adaptive_sampling": not args.allow_adaptive_sampling,
        "with_oidn": args.with_oidn,
        "oidn_device": args.oidn_device,
        "min_balance": args.min_balance,
        "max_minutes": args.max_minutes,
        "upload_timeout_s": args.upload_timeout_s,
        "transfer_preflight_mb": args.transfer_preflight_mb,
        "min_transfer_mbps": args.min_transfer_mbps,
    }
    if args.dry_run:
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return

    runpod.register_cleanup()
    bal0 = runpod.balance()["clientBalance"]
    log(f"balance ${bal0:.2f}; floor ${args.min_balance:.2f}")
    if bal0 <= args.min_balance:
        log("balance already at/below floor; aborting before provisioning")
        print(json.dumps({"ok": False, "error": "balance_floor", "balance": bal0}), flush=True)
        return

    pod = None
    records: list[dict] = []
    rows: list[dict] = []
    result = None
    deadline = time.time() + args.max_minutes * 60
    try:
        pod = runpod.provision_reachable(
            gpu_plan_for_tier(args.gpu_tier),
            POD_IMAGE,
            disk_gb=POD_DISK_GB,
            require_cuda=True,
            name="cx-cycles-quality",
        )
        append_ledger({"event": "pod_up", "pod": pod, "manifest": manifest})
        log(f"pod {pod['gpu']} {pod['id']} @ {pod['ip']}:{pod['port']}")
        runpod.arm_remote_watchdog(pod, min(WATCHDOG_TTL_S, args.max_minutes * 60 + 900))

        preflight = transfer_preflight(
            pod,
            args.transfer_preflight_mb,
            args.min_transfer_mbps,
            min(args.upload_timeout_s, 120),
        )
        records.append(preflight)
        if not preflight["ok"]:
            raise RuntimeError("transfer preflight failed; refusing full runtime tar upload")

        for rec in upload_prebuilt_root(
            pod,
            tar_path,
            args.remote_root,
            args.upload_timeout_s,
        ):
            records.append(rec)
            if not rec["ok"]:
                raise RuntimeError(f"prebuilt-root stage {rec['stage']} failed")

        smoke = run_stage(
            pod,
            "binary_smoke",
            cycles_fork.binary_smoke_stage(args.remote_root).cmd,
            900,
        )
        records.append(smoke)
        if not smoke["ok"]:
            raise RuntimeError("binary smoke failed")

        if args.include_synthetic_scene:
            rec = run_stage(
                pod,
                "create_synthetic_scene",
                synthetic_scene_cmd(args.remote_root, args.synthetic_name),
                120,
            )
            records.append(rec)
            if not rec["ok"]:
                raise RuntimeError("synthetic scene creation failed")

        for scene in scenes:
            if time.time() > deadline:
                log("deadline reached before next scene; stopping ladder")
                break
            bal = runpod.balance()["clientBalance"]
            if bal <= args.min_balance:
                log(f"balance ${bal:.2f} at/below floor ${args.min_balance:.2f}; stopping")
                break
            stage = run_stage(
                pod,
                f"quality_ladder_{safe_name(scene)}",
                quality_ladder_cmd(
                    args.remote_root,
                    scene,
                    args.ref_samples,
                    draft_samples,
                    args.device,
                    not args.allow_adaptive_sampling,
                    args.with_oidn,
                    args.oidn_device,
                ),
                args.stage_timeout_s,
            )
            records.append(stage)
            scene_rows = parse_quality_rows(stage)
            rows.extend(scene_rows)
            append_ledger({"event": "quality_rows", "scene": scene, "rows": scene_rows})
            if not stage["ok"] or "CX_QUALITY_LADDER_OK" not in stage.get("out_tail", ""):
                raise RuntimeError(f"quality ladder failed for {scene}")

        result = {
            "ok": bool(rows),
            "pod_gpu": pod["gpu"],
            "pod_cloud": pod["cloud"],
            "manifest": manifest,
            "summary": summarize(rows),
            "rows": rows,
            "stages": [{k: r[k] for k in ("stage", "ok", "elapsed_s")} for r in records],
        }
    except Exception as exc:  # noqa: BLE001
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "manifest": manifest,
            "summary": summarize(rows),
            "rows": rows,
            "stages": [{k: r[k] for k in ("stage", "ok", "elapsed_s")} for r in records],
        }
        log(f"ERROR {result['error']}")
    finally:
        if pod:
            log("tearing down")
            try:
                runpod.terminate(pod["id"])
            except Exception as exc:  # noqa: BLE001
                log(f"terminate error: {exc}")
        append_ledger({"event": "result", "result": result})
        try:
            b2 = runpod.balance()["clientBalance"]
        except Exception as exc:  # noqa: BLE001
            append_ledger({
                "event": "pod_down",
                "balance_after": None,
                "balance_error": f"{type(exc).__name__}: {exc}",
            })
            log(f"pod down; balance check failed: {type(exc).__name__}: {exc}")
        else:
            append_ledger({"event": "pod_down", "balance_after": b2})
            log(f"pod down. balance ${b2:.2f} (spent ${bal0 - b2:.2f})")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
