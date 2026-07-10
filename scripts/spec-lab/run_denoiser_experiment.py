#!/usr/bin/env python3
"""run_denoiser_experiment.py — Track 2 M0+M1 money-safe driver (launch-ready).

Provisions ONE reachable+CUDA-verified GPU, arms the pod-side self-destruct watchdog
IMMEDIATELY, ships the spec-lab code, installs deps, then runs the full M0+M1 loop on the
pod: mint Noise2Noise pairs -> train the small kernel-predicting U-Net -> eval cx vs OIDN
on the shared worst-tile SSIM harness. The trained .pt + .onnx are pulled back locally
(the owned artifact the M2 Rust bridge will consume). Pod is torn down in `finally`.

Safety contract (identical to run_ultimate_no_reprojection.py): register_cleanup wires
teardown to every exit path; arm_remote_watchdog is a hard backstop that self-terminates
the pod even if THIS process dies; provision_reachable proves SSH + CUDA before use.

DEFAULT held-out design (the honest M1): train on one scene, EVAL ON AN UNSEEN SCENE.
--same-scene instead mints one scene and holds out frames for a gentler first read.

  python3 run_denoiser_experiment.py                 # classroom -> bmw27 held-out
  python3 run_denoiser_experiment.py --same-scene    # classroom held-out frames
  python3 run_denoiser_experiment.py --train-scene classroom --eval-scene bmw27 \
        --resolution 960x540 --spp 32 --ref-spp 2048 --frames 4 --eval-frames 2 \
        --n-crops 192 --crop-size 128 --epochs 40 --batch-size 16 --timeout-s 1800
"""

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import runpod  # noqa: E402

# gpu-provisioning-policy (task #20 rewrite, 2026-07-09): base tier A100 then H100 —
# cheapest first, then availability, COMMUNITY then SECURE at each rung; if neither
# base is available UPGRADE to H200. NEVER downgrade to L40S/RTX A6000/A40/CPU.
# Blackwell (B200/B300, sm_100/sm_120) stays out until a Blackwell-capable Blender is
# proven on-box (Blender 4.2 ships no kernels — silent CPU fallback burned $0.58 on
# 2026-07-09). H100/H200 (sm_90) carry a first-render PTX-JIT caveat: give the
# functional GPU probe JIT headroom via the runner param gpu_probe_timeout_s (see
# run_integrated_production_benchmark.py; 2026-07-09 two-pod H100 probe-timeout
# evidence).
GPU_PLAN = [
    ("NVIDIA A100 80GB PCIe", "COMMUNITY"),
    ("NVIDIA A100 80GB PCIe", "SECURE"),
    ("NVIDIA H100 80GB HBM3", "COMMUNITY"),
    ("NVIDIA H100 80GB HBM3", "SECURE"),
    ("NVIDIA H200", "COMMUNITY"),
    ("NVIDIA H200", "SECURE"),
]
# Same PyTorch image the render lab already uses (torch preinstalled, CUDA-matched).
POD_IMAGE = "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"
POD_DISK_GB = 80
LEDGER = os.path.join(REPO, "docs/speed-lane-reports/spec-lab/denoiser_ledger.jsonl")
ARTIFACT_DIR = os.path.join(REPO, "docs/speed-lane-reports/spec-lab/denoiser_artifacts")

REMOTE_ROOT = "/root/spec-lab"
TRAIN_DATA = f"{REMOTE_ROOT}/denoise_train"
EVAL_DATA = f"{REMOTE_ROOT}/denoise_eval"
CKPT = f"{REMOTE_ROOT}/cx_denoiser.pt"
ONNX = f"{REMOTE_ROOT}/cx_denoiser.onnx"


def log(m):
    print(f"[denoiser-exp {time.strftime('%H:%M:%S')}] {m}", flush=True)


def ledger_append(rec):
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}) + "\n")


def run_remote_json(pod, cmd, timeout):
    """Run a remote command, return the parsed LAST json stdout line (the emit contract)."""
    rc, out, err = runpod.ssh(pod, cmd, timeout=timeout)
    tail = [ln for ln in (out or "").splitlines() if ln.strip()]
    if not tail:
        return {"error": f"no stdout (rc={rc}); stderr_tail={(err or '')[-500:]}"}
    try:
        return json.loads(tail[-1])
    except Exception as e:  # noqa: BLE001
        return {"error": f"unparseable final line: {e}; last={tail[-1][:400]}; "
                         f"stderr_tail={(err or '')[-300:]}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-scene", default="classroom")
    ap.add_argument("--eval-scene", default="bmw27")
    ap.add_argument("--same-scene", action="store_true",
                    help="mint ONE scene and hold out frames (gentler first read)")
    ap.add_argument("--resolution", default="960x540")
    ap.add_argument("--spp", type=int, default=32)
    ap.add_argument("--ref-spp", type=int, default=2048)
    ap.add_argument("--frames", type=int, default=4, help="train frames")
    ap.add_argument("--eval-frames", type=int, default=2)
    ap.add_argument("--n-crops", type=int, default=192)
    ap.add_argument("--crop-size", type=int, default=128)
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--base", type=int, default=48)
    ap.add_argument("--kernel", type=int, default=5)
    ap.add_argument("--timeout-s", type=int, default=1800, help="per-step ssh timeout")
    ap.add_argument("--watchdog-ttl-s", type=int, default=7200,
                    help="hard pod self-destruct backstop")
    args = ap.parse_args()

    runpod.register_cleanup()
    bal0 = runpod.balance()["clientBalance"]
    log(f"balance ${bal0:.2f}")

    log("provisioning...")
    pod = runpod.provision_reachable(GPU_PLAN, POD_IMAGE, disk_gb=POD_DISK_GB)
    log(f"pod {pod['gpu']} {pod['id']} @ {pod['ip']}:{pod['port']}")
    ledger_append({"event": "pod_up", "pod": pod, "args": vars(args)})

    # HARD BACKSTOP first — self-terminates on the pod even if this process dies.
    runpod.arm_remote_watchdog(pod, args.watchdog_ttl_s)

    try:
        # ---- ship code ------------------------------------------------------
        rc, _, err = runpod.ssh(pod, f"mkdir -p {REMOTE_ROOT}", timeout=60)
        if rc != 0:
            raise RuntimeError(f"mkdir failed: {err[:200]}")
        ok, serr = runpod.scp_to(pod, os.path.join(HERE, "pod"), f"{REMOTE_ROOT}/")
        if not ok:
            raise RuntimeError(f"scp pod/ failed: {serr[:200]}")
        for fn in ("cx_denoiser_model.py", "train_cx_denoiser.py", "eval_cx_denoiser.py"):
            ok, serr = runpod.scp_to(pod, os.path.join(HERE, fn), f"{REMOTE_ROOT}/{fn}")
            if not ok:
                raise RuntimeError(f"scp {fn} failed: {serr[:200]}")

        # ---- deps: torch is in the image; add EXR/imaging/oidn/onnx ---------
        # OIDN comparison goes through Blender's own bundled Cycles/compositor
        # denoiser (denoise_oidn() in eval_cx_denoiser.py), not a standalone 'oidn'
        # pip binding -- that binding is unreliable and is not installed here.
        log("installing deps (OpenEXR/Imath/imageio/skimage/onnx)...")
        rc, out, err = runpod.ssh(
            pod,
            "pip install --break-system-packages --no-cache-dir "
            "OpenEXR Imath imageio numpy pillow scikit-image opencv-python-headless "
            "onnx 2>&1 | tail -3",
            timeout=1200)
        log(f"deps rc={rc}: {(out or '').strip()[-200:]}")

        # ---- M0: mint training data ----------------------------------------
        eval_frac_train = 0.0 if not args.same_scene else 0.34
        train_cfg = {
            "scene": args.train_scene, "resolution": args.resolution, "spp": args.spp,
            "ref_spp": args.ref_spp, "frames": args.frames,
            "eval_fraction": eval_frac_train, "n_crops": args.n_crops,
            "crop_size": args.crop_size, "seed": 0, "out_dir": TRAIN_DATA,
        }
        log(f"MINT train {train_cfg}")
        t0 = time.time()
        mint_train = run_remote_json(
            pod,
            f"cd {REMOTE_ROOT} && python3 pod/exp_mint_denoise_pairs.py "
            f"'{json.dumps(train_cfg)}'",
            timeout=args.timeout_s)
        ledger_append({"event": "mint_train", "wall_s": round(time.time() - t0, 1),
                       "result": mint_train})
        if mint_train.get("error"):
            raise RuntimeError(f"mint(train) failed: {mint_train['error']}")
        log(f"  -> {mint_train.get('n_patches')} patches, "
            f"render {mint_train.get('render_noisy_s')}s (+ref {mint_train.get('render_ref_s')}s)")

        # eval data dir: same as train dir for --same-scene, else a fresh scene
        eval_data_dir = TRAIN_DATA if args.same_scene else EVAL_DATA
        if not args.same_scene:
            eval_cfg = {
                "scene": args.eval_scene, "resolution": args.resolution,
                "spp": args.spp, "ref_spp": args.ref_spp, "frames": args.eval_frames,
                "eval_fraction": 1.0, "n_crops": 0, "crop_size": args.crop_size,
                "seed": 1000, "out_dir": EVAL_DATA,
            }
            log(f"MINT eval (held-out scene) {eval_cfg}")
            t0 = time.time()
            mint_eval = run_remote_json(
                pod,
                f"cd {REMOTE_ROOT} && python3 pod/exp_mint_denoise_pairs.py "
                f"'{json.dumps(eval_cfg)}'",
                timeout=args.timeout_s)
            ledger_append({"event": "mint_eval", "wall_s": round(time.time() - t0, 1),
                           "result": mint_eval})
            if mint_eval.get("error"):
                raise RuntimeError(f"mint(eval) failed: {mint_eval['error']}")
            log(f"  -> {mint_eval.get('n_eval_frames')} eval frames w/ references")

        # ---- M1: train the denoiser ----------------------------------------
        log(f"TRAIN epochs={args.epochs} batch={args.batch_size} base={args.base}")
        t0 = time.time()
        train_res = run_remote_json(
            pod,
            f"cd {REMOTE_ROOT} && python3 train_cx_denoiser.py "
            f"--data-dir {TRAIN_DATA} --out-ckpt {CKPT} --onnx {ONNX} "
            f"--epochs {args.epochs} --batch-size {args.batch_size} --base {args.base} "
            f"--kernel {args.kernel} --device auto --emit-json",
            timeout=args.timeout_s)
        ledger_append({"event": "train", "wall_s": round(time.time() - t0, 1),
                       "result": train_res})
        if train_res.get("error"):
            raise RuntimeError(f"train failed: {train_res['error']}")
        log(f"  -> params={train_res.get('params_m')}M best_val={train_res.get('best_val_loss')} "
            f"train_s={train_res.get('train_s')} onnx={'yes' if train_res.get('onnx') else 'no'}")

        # ---- M1 go/no-go: eval cx vs OIDN ----------------------------------
        log(f"EVAL cx vs OIDN on {eval_data_dir}")
        t0 = time.time()
        eval_res = run_remote_json(
            pod,
            f"cd {REMOTE_ROOT} && python3 eval_cx_denoiser.py "
            f"--ckpt {CKPT} --data-dir {eval_data_dir} --device auto",
            timeout=args.timeout_s)
        ledger_append({"event": "eval", "wall_s": round(time.time() - t0, 1),
                       "result": eval_res})
        if eval_res.get("error"):
            log(f"  -> EVAL ERROR: {str(eval_res['error'])[:400]}")
        else:
            log(f"  -> VERDICT={eval_res.get('verdict')} "
                f"cx.worst={eval_res.get('cx', {}).get('worst_tile')} "
                f"oidn.worst={eval_res.get('oidn', {}).get('worst_tile')} "
                f"delta={eval_res.get('worst_tile_delta_cx_minus_oidn')}")

        # ---- pull the owned artifact (.pt + .onnx) back locally --------------
        os.makedirs(ARTIFACT_DIR, exist_ok=True)
        for remote, local in ((CKPT, "cx_denoiser.pt"), (ONNX, "cx_denoiser.onnx")):
            try:
                import subprocess
                r = subprocess.run(
                    ["scp", *runpod.SSH_OPTS, "-P", str(pod["port"]),
                     f"root@{pod['ip']}:{remote}", os.path.join(ARTIFACT_DIR, local)],
                    capture_output=True, text=True, timeout=300)
                if r.returncode == 0:
                    log(f"pulled {local} -> {ARTIFACT_DIR}")
            except Exception as e:  # noqa: BLE001
                log(f"artifact pull {local} failed (non-fatal): {e}")

        # final one-line summary for the caller
        print(json.dumps({
            "verdict": eval_res.get("verdict"),
            "cx": eval_res.get("cx"),
            "oidn": eval_res.get("oidn"),
            "worst_tile_delta_cx_minus_oidn": eval_res.get("worst_tile_delta_cx_minus_oidn"),
            "params_m": train_res.get("params_m"),
            "n_patches": mint_train.get("n_patches"),
            "artifacts": ARTIFACT_DIR,
        }), flush=True)
    finally:
        log("tearing down...")
        try:
            runpod.terminate(pod["id"])
        except Exception as e:  # noqa: BLE001
            log(f"terminate error (verify in console): {e}")
        b2 = runpod.balance()["clientBalance"]
        ledger_append({"event": "pod_down", "balance_after": b2})
        log(f"pod down. balance ${b2:.2f} (spent ${bal0 - b2:.2f})")


if __name__ == "__main__":
    main()
