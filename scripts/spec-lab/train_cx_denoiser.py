#!/usr/bin/env python3
"""train_cx_denoiser.py — Track 2 M1: train the small albedo-demodulated,
kernel-predicting U-Net on Noise2Noise pairs minted by exp_mint_denoise_pairs.py.

Noise2Noise (Lehtinen et al. 2018): with TWO independent noisy realizations of the same
frame, regressing one toward the other converges to the clean image — no clean target
needed. We predict a KxK gather kernel from noisy_a (+ guides), reconstruct, and regress
the reconstruction toward noisy_b (and, symmetrically, b->a). All the denoiser math lives
in cx_denoiser_model.py so training, eval, and ONNX export share ONE definition.

Outputs: a .pt checkpoint (state_dict + arch config) AND an .onnx export of the
kernel-predicting net (the artifact the M2 Rust `ort` bridge consumes; the gather is
hand-written in Rust there, so only the net is in the ONNX graph).

CLI:
  python3 train_cx_denoiser.py --data-dir DIR --out-ckpt cx_denoiser.pt \
        [--epochs 40] [--batch-size 16] [--base 48] [--kernel 5] [--lr 1e-3] \
        [--loss smape|l1_tone] [--device auto|cuda|cpu] [--onnx cx_denoiser.onnx] \
        [--val-fraction 0.1] [--max-patches N] [--emit-json]
"""

import argparse
import glob
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import numpy as np  # noqa: E402
import torch  # noqa: E402
from torch.utils.data import DataLoader, Dataset  # noqa: E402

import cx_denoiser_model as m  # noqa: E402


def log(*a):
    print("[train_cx]", *a, file=sys.stderr, flush=True)


def _chw(a):
    """(H,W,C) -> (C,H,W) float32 tensor-ready array."""
    return np.ascontiguousarray(np.transpose(a, (2, 0, 1))).astype(np.float32)


class PatchDataset(Dataset):
    """Loads the .npz N2N patches. Each item: noisy_a, noisy_b, albedo, normal, depth
    (all CHW float32). Depth is forced to 1 channel."""

    def __init__(self, files):
        self.files = files

    def __len__(self):
        return len(self.files)

    def __getitem__(self, i):
        d = np.load(self.files[i])
        depth = d["depth"]
        if depth.ndim == 2:
            depth = depth[..., None]
        return {
            "noisy_a": torch.from_numpy(_chw(d["noisy_a"])),
            "noisy_b": torch.from_numpy(_chw(d["noisy_b"])),
            "albedo": torch.from_numpy(_chw(d["albedo"])),
            "normal": torch.from_numpy(_chw(d["normal"])),
            "depth": torch.from_numpy(_chw(depth[..., :1])),
        }


def pick_device(pref):
    if pref == "cpu":
        return torch.device("cpu")
    if pref == "cuda" or (pref == "auto" and torch.cuda.is_available()):
        if torch.cuda.is_available():
            return torch.device("cuda")
    if pref == "auto" and getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def train_one_epoch(model, loader, opt, device, loss_kind):
    model.train()
    tot, nb = 0.0, 0
    for batch in loader:
        a = batch["noisy_a"].to(device)
        b = batch["noisy_b"].to(device)
        albedo = batch["albedo"].to(device)
        normal = batch["normal"].to(device)
        depth = batch["depth"].to(device)
        opt.zero_grad()
        # Symmetric N2N: predict from a->target b, and from b->target a; average.
        pred_a = m.denoise(model, a, albedo, normal, depth)
        loss1 = m.n2n_loss(pred_a, b, loss_kind)
        pred_b = m.denoise(model, b, albedo, normal, depth)
        loss2 = m.n2n_loss(pred_b, a, loss_kind)
        loss = 0.5 * (loss1 + loss2)
        loss.backward()
        opt.step()
        tot += float(loss.item())
        nb += 1
    return tot / max(nb, 1)


@torch.no_grad()
def validate(model, loader, device, loss_kind):
    model.eval()
    tot, nb = 0.0, 0
    for batch in loader:
        a = batch["noisy_a"].to(device)
        b = batch["noisy_b"].to(device)
        albedo = batch["albedo"].to(device)
        normal = batch["normal"].to(device)
        depth = batch["depth"].to(device)
        pred_a = m.denoise(model, a, albedo, normal, depth)
        tot += float(m.n2n_loss(pred_a, b, loss_kind).item())
        nb += 1
    return tot / max(nb, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True, help="mint out_dir (contains patches/)")
    ap.add_argument("--out-ckpt", default=os.path.join(HERE, "cx_denoiser.pt"))
    ap.add_argument("--onnx", default="", help="also export ONNX to this path")
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--base", type=int, default=48, help="U-Net base width")
    ap.add_argument("--kernel", type=int, default=5, help="gather kernel size K")
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--loss", choices=["smape", "l1_tone"], default="smape")
    ap.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    ap.add_argument("--val-fraction", type=float, default=0.1)
    ap.add_argument("--max-patches", type=int, default=0, help="0 = all")
    ap.add_argument("--num-workers", type=int, default=2)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--emit-json", action="store_true",
                    help="print one JSON summary line to stdout at the end")
    args = ap.parse_args()

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    patch_dir = os.path.join(args.data_dir, "patches")
    files = sorted(glob.glob(os.path.join(patch_dir, "*.npz")))
    if not files:
        raise SystemExit(f"no patches in {patch_dir} (run exp_mint_denoise_pairs first)")
    if args.max_patches > 0:
        files = files[: args.max_patches]

    # Split by whole SOURCE FRAME, never by individual patch. Crops within a frame are
    # drawn independently (exp_mint_denoise_pairs.py's rng.randint per crop, no tiling/
    # min-distance constraint) and noisy_b is one fixed full-frame array reused across
    # every crop of that frame, so a patch-level shuffle+split lets "val" crops overlap
    # train crops and share literal noisy_b target pixels -- val loss then measures
    # memorization of the training frames, not generalization (confirmed: this is what
    # produced a spurious best_val=2e-06 on a 4-frame single-scene run).
    rng = np.random.RandomState(args.seed)
    manifest_path = os.path.join(args.data_dir, "manifest.json")
    with open(manifest_path) as fh:
        manifest = json.load(fh)
    patch_frame = {p["file"]: p["frame"] for p in manifest["patches"]}
    frame_ids = sorted({patch_frame[os.path.basename(f)] for f in files
                        if os.path.basename(f) in patch_frame})
    if len(frame_ids) > 1:
        shuffled_frames = list(frame_ids)
        rng.shuffle(shuffled_frames)
        n_val_frames = max(1, round(len(shuffled_frames) * args.val_fraction))
        n_val_frames = min(n_val_frames, len(shuffled_frames) - 1)  # keep >=1 train frame
        val_frame_set = set(shuffled_frames[:n_val_frames])
        val_files = [f for f in files if patch_frame.get(os.path.basename(f)) in val_frame_set]
        train_files = [f for f in files if patch_frame.get(os.path.basename(f)) not in val_frame_set]
        rng.shuffle(train_files)
        log(f"frame-aware split: {len(frame_ids)} source frames, "
            f"{len(val_frame_set)} held out for val -> "
            f"train={len(train_files)} val={len(val_files)} patches")
    else:
        log("only 1 source frame in this mint run -- no frame-aware val split possible; "
            "training with no val set rather than a leaked one")
        train_files = list(files)
        rng.shuffle(train_files)
        val_files = []

    device = pick_device(args.device)
    loss_kind = args.loss
    log(f"device={device} train={len(train_files)} val={len(val_files)} "
        f"base={args.base} K={args.kernel} loss={loss_kind}")

    model = m.KPCNDenoiser(base=args.base, kernel_size=args.kernel).to(device)
    n_params = m.count_params(model)
    log(f"model params = {n_params:,} ({n_params/1e6:.2f}M)")

    dl_kw = dict(batch_size=args.batch_size, num_workers=args.num_workers,
                 pin_memory=(device.type == "cuda"))
    train_loader = DataLoader(PatchDataset(train_files), shuffle=True, drop_last=False, **dl_kw)
    val_loader = (DataLoader(PatchDataset(val_files), shuffle=False, **dl_kw)
                  if val_files else None)

    opt = torch.optim.Adam(model.parameters(), lr=args.lr)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=max(1, args.epochs))

    t0 = time.time()
    best_val = float("inf")
    history = []
    for ep in range(args.epochs):
        tr = train_one_epoch(model, train_loader, opt, device, loss_kind)
        va = validate(model, val_loader, device, loss_kind) if val_loader else tr
        sched.step()
        history.append({"epoch": ep, "train": round(tr, 6), "val": round(va, 6)})
        log(f"epoch {ep+1}/{args.epochs} train={tr:.5f} val={va:.5f} "
            f"lr={opt.param_groups[0]['lr']:.2e}")
        if va <= best_val:
            best_val = va
            _save_ckpt(model, args, n_params, args.out_ckpt)
    train_s = time.time() - t0

    # Always leave a final checkpoint even if val never improved (tiny datasets).
    if not os.path.isfile(args.out_ckpt):
        _save_ckpt(model, args, n_params, args.out_ckpt)

    onnx_path = args.onnx
    onnx_ok = False
    if onnx_path:
        try:
            # Reload best weights before export so the ONNX matches the .pt.
            state = torch.load(args.out_ckpt, map_location="cpu")
            export_model = m.KPCNDenoiser(base=args.base, kernel_size=args.kernel)
            export_model.load_state_dict(state["state_dict"])
            m.export_onnx(export_model, onnx_path, device="cpu")
            onnx_ok = os.path.isfile(onnx_path)
            log(f"ONNX exported -> {onnx_path} ok={onnx_ok}")
        except Exception as e:  # noqa: BLE001
            log(f"ONNX export failed (non-fatal): {type(e).__name__}: {e}")

    summary = {
        "ok": True, "checkpoint": args.out_ckpt, "onnx": onnx_path if onnx_ok else "",
        "params": n_params, "params_m": round(n_params / 1e6, 3),
        "epochs": args.epochs, "batch_size": args.batch_size, "base": args.base,
        "kernel": args.kernel, "loss": loss_kind, "device": str(device),
        "n_train": len(train_files), "n_val": len(val_files),
        "best_val_loss": round(best_val, 6), "train_s": round(train_s, 1),
        "history": history, "modeled": False,
    }
    if args.emit_json:
        print(json.dumps(summary), flush=True)
    else:
        log(f"DONE best_val={best_val:.5f} train_s={train_s:.1f} ckpt={args.out_ckpt}")


def _save_ckpt(model, args, n_params, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    torch.save({
        "state_dict": model.state_dict(),
        "arch": {"base": args.base, "kernel": args.kernel,
                 "guide_ch": m.GUIDE_CH, "radiance_ch": m.RADIANCE_CH},
        "params": n_params, "demod_eps": m.DEMOD_EPS,
    }, path)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}), flush=True)
        sys.exit(1)
