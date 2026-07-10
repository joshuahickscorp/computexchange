#!/usr/bin/env python3
"""cx_denoiser_model.py — the OWNED denoiser: a small, albedo-demodulated,
kernel-predicting two-branch U-Net (KPCN pattern), shared by train + eval + export.

Track 2 (docs/research/ORIGINAL_ENGINE_THREE_TRACKS_2026-07-07.md) M0+M1. This module
is architecture only — no I/O, no Blender, no pod deps — so it imports on any box that
has torch, and every smoke test / training / eval / ONNX-export path uses the SAME
forward math (the whole point: one kernel-apply + one demod/remod definition, so the
number we grade is the number we ship).

The pipeline (KPCN, Bako et al. 2017; matches OIDN 3's public albedo-demod design):
    demod   = radiance / (albedo + eps)            # divide OUT the texture detail
    guides  = [albedo(3), normal(3), depth'(1)]    # 7-ch guide vector (our exact AOVs)
    kernel  = net(demod, guides)                   # per-pixel KxK softmax gather kernel
    out_dm  = apply_kernel(demod, kernel)          # local weighted reconstruction
    out     = out_dm * (albedo + eps)              # re-modulate the texture back on

The NET's only output is the KxK kernel (M2 hand-writes the gather as a Metal/CUDA
kernel in Rust, so ONNX export is the net alone — apply_kernel/demod/remod live here in
Python now and in Rust later). Training is Noise2Noise: predict from noisy_a, regress the
kernel-reconstructed output toward the *independent* noisy_b — no clean target needed.
"""

import torch
import torch.nn as nn
import torch.nn.functional as F

# Guide layout is fixed everywhere: albedo(3) + normal(3) + depth(1) = 7 channels.
GUIDE_CH = 7
RADIANCE_CH = 3
DEFAULT_KERNEL = 5
DEMOD_EPS = 1e-2  # albedo-demodulation floor; round-trip is EXACT regardless of value.


# --------------------------------------------------------------------------- #
# Albedo demodulation (exact round-trip) + guide preparation.                  #
# --------------------------------------------------------------------------- #
def demodulate(radiance, albedo, eps=DEMOD_EPS):
    """radiance / (albedo + eps). Divides the high-freq texture OUT so the net only
    has to denoise smooth illumination — the single biggest KPCN quality lever."""
    return radiance / (albedo.clamp_min(0.0) + eps)


def remodulate(x, albedo, eps=DEMOD_EPS):
    """Inverse of demodulate: x * (albedo + eps). demodulate->remodulate is EXACT."""
    return x * (albedo.clamp_min(0.0) + eps)


def prepare_guides(albedo, normal, depth):
    """Stack the AOVs into the fixed 7-ch guide tensor with a stable depth transform.

    albedo [B,3,H,W] in ~[0,1], normal [B,3,H,W] in ~[-1,1], depth [B,1,H,W] in world
    units (0..large). Depth is squashed to (-1,1) via d/(1+|d|) so its scale never
    swamps the conv activations (done HERE, not at mint time, so train==eval==export)."""
    depth = depth / (1.0 + depth.abs())
    return torch.cat([albedo, normal, depth], dim=1)


# --------------------------------------------------------------------------- #
# Kernel apply — the KPCN local weighted gather (the piece M2 owns in Rust).   #
# --------------------------------------------------------------------------- #
def apply_kernel(radiance, kernel, K):
    """Per-pixel KxK weighted gather.

    radiance [B,C,H,W], kernel [B,K*K,H,W] (already softmax-normalized over the K*K
    dim). Returns [B,C,H,W]. Uses F.unfold so the tap ordering (row-major) is identical
    in training, eval, and any Rust re-implementation that mirrors this loop."""
    B, C, H, W = radiance.shape
    pad = K // 2
    patches = F.unfold(radiance, kernel_size=K, padding=pad)      # [B, C*K*K, H*W]
    patches = patches.view(B, C, K * K, H * W)
    kw = kernel.view(B, 1, K * K, H * W)
    out = (patches * kw).sum(dim=2)                               # [B, C, H*W]
    return out.view(B, C, H, W)


# --------------------------------------------------------------------------- #
# The two-branch kernel-predicting U-Net.                                       #
# --------------------------------------------------------------------------- #
def _conv_block(cin, cout):
    return nn.Sequential(
        nn.Conv2d(cin, cout, 3, padding=1), nn.ReLU(inplace=True),
        nn.Conv2d(cout, cout, 3, padding=1), nn.ReLU(inplace=True),
    )


class KPCNDenoiser(nn.Module):
    """Predicts a per-pixel KxK softmax gather kernel from demodulated radiance + guides.

    Two branches, as specified: a SHALLOW guide branch (albedo/normal/depth features)
    and a DEEPER radiance branch (a small U-Net over the demodulated radiance fused with
    the guide features). Output is ONLY the kernel — the gather/demod/remod are external
    (Python here, Rust at M2). Target size ~0.5-3M params (verified by count_params())."""

    def __init__(self, guide_ch=GUIDE_CH, radiance_ch=RADIANCE_CH, base=48,
                 kernel_size=DEFAULT_KERNEL):
        super().__init__()
        self.K = kernel_size
        # --- shallow guide branch ---
        self.guide = nn.Sequential(
            nn.Conv2d(guide_ch, base, 3, padding=1), nn.ReLU(inplace=True),
            nn.Conv2d(base, base, 3, padding=1), nn.ReLU(inplace=True),
        )
        # --- deeper radiance branch (U-Net, 2 down / 2 up) fed radiance + guide feats ---
        cin = radiance_ch + base
        self.enc1 = _conv_block(cin, base)
        self.enc2 = _conv_block(base, base * 2)
        self.bott = _conv_block(base * 2, base * 4)
        self.dec2 = _conv_block(base * 4 + base * 2, base * 2)
        self.dec1 = _conv_block(base * 2 + base, base)
        self.pool = nn.MaxPool2d(2)
        self.head = nn.Conv2d(base, kernel_size * kernel_size, 3, padding=1)

    def forward(self, demod_radiance, guides):
        # Pad to a multiple of 4 so the two 2x pools divide evenly; crop back after.
        _, _, H, W = demod_radiance.shape
        ph = (4 - H % 4) % 4
        pw = (4 - W % 4) % 4
        if ph or pw:
            demod_radiance = F.pad(demod_radiance, (0, pw, 0, ph), mode="reflect")
            guides = F.pad(guides, (0, pw, 0, ph), mode="reflect")

        g = self.guide(guides)
        x = torch.cat([demod_radiance, g], dim=1)
        e1 = self.enc1(x)                    # [B, base,   H,   W]
        e2 = self.enc2(self.pool(e1))        # [B, 2base,  H/2, W/2]
        b = self.bott(self.pool(e2))         # [B, 4base,  H/4, W/4]
        d2 = F.interpolate(b, size=e2.shape[-2:], mode="nearest")
        d2 = self.dec2(torch.cat([d2, e2], dim=1))
        d1 = F.interpolate(d2, size=e1.shape[-2:], mode="nearest")
        d1 = self.dec1(torch.cat([d1, e1], dim=1))
        logits = self.head(d1)               # [B, K*K, H(pad), W(pad)]
        if ph or pw:
            logits = logits[..., :H, :W]
        return F.softmax(logits, dim=1)      # per-pixel kernel, sums to 1 over taps


def count_params(model):
    return sum(p.numel() for p in model.parameters())


# --------------------------------------------------------------------------- #
# Full denoise pipeline (used identically by training-forward and eval).       #
# --------------------------------------------------------------------------- #
def denoise(model, noisy, albedo, normal, depth, eps=DEMOD_EPS):
    """demod -> net kernel -> apply -> remod. Returns denoised radiance [B,3,H,W].

    Differentiable end-to-end, so the Noise2Noise loss flows through the kernel apply."""
    demod = demodulate(noisy, albedo, eps)
    guides = prepare_guides(albedo, normal, depth)
    kernel = model(demod, guides)
    out_dm = apply_kernel(demod, kernel, model.K)
    return remodulate(out_dm, albedo, eps)


# --------------------------------------------------------------------------- #
# Losses — SMAPE (HDR-robust, KPCN's default) and a tonemapped-L1 alternative. #
# --------------------------------------------------------------------------- #
def _tone(x):
    """Reinhard tonemap x/(1+x) — SAME map the SSIM grader uses, for consistency."""
    return x.clamp_min(0.0) / (1.0 + x.clamp_min(0.0))


def smape_loss(pred, target, eps=1e-2):
    """Symmetric MAPE: |p-t| / (|p|+|t|+eps). Scale-robust on raw HDR radiance."""
    return (torch.abs(pred - target) / (torch.abs(pred) + torch.abs(target) + eps)).mean()


def tonemapped_l1(pred, target):
    return torch.abs(_tone(pred) - _tone(target)).mean()


def n2n_loss(pred, noisy_target, kind="smape"):
    """Noise2Noise: regress the reconstruction toward an INDEPENDENT noisy frame.
    Since the two noise realizations are zero-mean-independent, the minimiser is the
    clean image — no clean target required for training."""
    if kind == "l1_tone":
        return tonemapped_l1(pred, noisy_target)
    return smape_loss(pred, noisy_target)


def export_onnx(model, path, sample_h=128, sample_w=128, device="cpu"):
    """Export the KERNEL-PREDICTING NET (only) to ONNX for the later Rust `ort` bridge.
    The gather/demod/remod are intentionally NOT in the graph — M2 hand-writes the
    gather as a custom Metal/CUDA kernel. Dynamic H/W so any frame size runs."""
    model.eval()
    demod = torch.randn(1, RADIANCE_CH, sample_h, sample_w, device=device)
    guides = torch.randn(1, GUIDE_CH, sample_h, sample_w, device=device)
    torch.onnx.export(
        model, (demod, guides), path,
        input_names=["demod_radiance", "guides"], output_names=["kernel"],
        dynamic_axes={"demod_radiance": {0: "b", 2: "h", 3: "w"},
                      "guides": {0: "b", 2: "h", 3: "w"},
                      "kernel": {0: "b", 2: "h", 3: "w"}},
        opset_version=17,
    )
