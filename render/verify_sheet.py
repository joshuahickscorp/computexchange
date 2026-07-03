#!/usr/bin/env python3
# render/verify_sheet.py · phase-3 self-grade compare sheet for one device front.
#
# Panels: reference | matched-camera render | 50% blend | silhouette-difference heatmap,
# with a delta table drawn in the margin: contour max deviation (% of width), corner-radius
# delta, feature-position deltas, and the render clip percentage. I grade myself against
# this; a loop closes when the deltas say so. No banned vocabulary is emitted here.
#
#   python3 render/verify_sheet.py studio    # reads render/verify/mac-studio-front.png
#
# Pure numpy + Pillow. Reuses render/measure.py primitives.

import os, sys, math
import numpy as np
from PIL import Image, ImageDraw
import measure as M

ROOT = os.path.dirname(os.path.abspath(__file__))
WC = 760   # canonical device width in px for the panels

CFG = {
    "studio": dict(ref="ref/mac-studio/apple_front.jpg", render="verify/mac-studio-front.png",
                   width_mm=197.0, spec_aspect=197.0/95.0, out="verify/loop-studio.png"),
}

def load_ref_silhouette(path):
    rgb = M.load_rgb(path)
    ds, s = M.downscale(rgb, 1600)
    bg = M.bg_color(ds)
    mask = M.silhouette(ds, M.pred_neutral_bg(bg, 16))
    return ds, mask

def load_render(path):
    im = Image.open(os.path.join(ROOT, path)).convert("RGBA")
    a = np.asarray(im)
    rgb = a[..., :3]; alpha = a[..., 3] > 128
    return rgb, alpha

def crop_to(mask, rgb):
    x0, y0, x1, y1 = M.bbox_of(mask)
    return rgb[y0:y1, x0:x1], mask[y0:y1, x0:x1], (x1 - x0, y1 - y0)

def resize_w(rgb, mask, wc):
    h, w = mask.shape
    hc = max(1, int(round(h * wc / w)))
    r = np.asarray(Image.fromarray(rgb).resize((wc, hc), Image.LANCZOS))
    m = np.asarray(Image.fromarray(mask.astype(np.uint8) * 255).resize((wc, hc), Image.NEAREST)) > 128
    return r, m

def composite_white(rgb, mask):
    out = rgb.copy()
    out[~mask] = 255
    return out

def measure_front(rgb, mask, width_mm):
    """aspect, top-corner R (mm), and the front-face dark features (USB-C/SD) as (x_mm, w, h)."""
    x0, y0, x1, y1 = M.bbox_of(mask)
    W = x1 - x0; H = y1 - y0
    mmpp = width_mm / W
    out = dict(aspect=W / H, mmpp=mmpp, W=W, H=H)
    f = M.fit_corner(mask, "tl", frac=0.18)
    out["corner_R"] = f[2] * mmpp if f else None
    # dark features in the lower third
    sub = rgb[y0:y1, x0:x1]
    L = M.srgb_to_lab(sub.astype(float) / 255.0)[..., 0]
    band = np.zeros_like(L, bool); band[int(H*0.55):int(H*0.95), :] = True
    dark = (L < 55) & band
    blobs = [b for b in M.label_blobs(dark) if b['area'] > 0.0002 * W * H]
    feats = []
    for b in blobs:
        bw = b['x1']-b['x0']; bh = b['y1']-b['y0']
        feats.append(dict(x_mm=(b['cx']-W/2)*mmpp, w=bw*mmpp, h=bh*mmpp, ar=bh/max(1,bw)))
    feats.sort(key=lambda d: d['x_mm'])
    out["feats"] = feats
    return out

def contour_dev(m_ref, m_ren):
    """Silhouette agreement between two same-size masks. Returns max horizontal edge
    deviation (% width) + where it occurs, the mean edge deviation (% width), and the
    XOR-area fraction (% of the union) · a fairer aggregate than the single worst row."""
    h = min(m_ref.shape[0], m_ren.shape[0]); w = m_ref.shape[1]
    devs = []; ymax = 0; dmax = 0.0
    marg = max(2, int(0.02 * h))   # skip the extreme top/bottom rows (AA + segmentation noise)
    for y in range(marg, h - marg):
        xr = np.where(m_ref[y])[0]; xn = np.where(m_ren[y])[0]
        if len(xr) and len(xn):
            d = max(abs(xr.min()-xn.min()), abs(xr.max()-xn.max()))
            devs.append(d)
            if d > dmax: dmax = d; ymax = y
    xor = (m_ref ^ m_ren).sum(); uni = (m_ref | m_ren).sum()
    return dict(maxpct=100.0*dmax/w, myfrac=ymax/max(1, h), meanpct=100.0*np.mean(devs)/w,
                xorpct=100.0*xor/max(1, uni))

def clip_pct(rgb, mask):
    dev = rgb[mask].astype(float) / 255.0
    if len(dev) == 0: return 0.0
    return 100.0 * np.mean(np.any(dev >= 0.98, axis=1))

def draw_table(lines, w, h):
    im = Image.new("RGB", (w, h), (18, 18, 20)); d = ImageDraw.Draw(im)
    y = 8
    for ln, col in lines:
        d.text((10, y), ln, fill=col); y += 16
    return im

def build(device):
    cfg = CFG[device]
    ref_ds, ref_mask = load_ref_silhouette(cfg["ref"])
    ren_rgb, ren_mask = load_render(cfg["render"])

    ref_c, ref_m, ref_wh = crop_to(ref_mask, ref_ds)
    ren_c, ren_m, ren_wh = crop_to(ren_mask, ren_rgb)

    m_ref = measure_front(ref_c, ref_m, cfg["width_mm"])
    m_ren = measure_front(composite_white(ren_c, ren_m), ren_m, cfg["width_mm"])

    # panels at common device width
    refP, refMs = resize_w(ref_c, ref_m, WC)
    renP, renMs = resize_w(composite_white(ren_c, ren_m), ren_m, WC)
    Hc = max(refP.shape[0], renP.shape[0])
    def pad(img, fill):
        c = np.full((Hc, WC, 3), fill, np.uint8); c[:img.shape[0]] = img; return c
    def padm(m):
        c = np.zeros((Hc, WC), bool); c[:m.shape[0]] = m; return c
    refP, renP = pad(refP, 255), pad(renP, 255)
    refMs, renMs = padm(refMs), padm(renMs)
    blend = (0.5*refP + 0.5*renP).astype(np.uint8)
    heat = np.full((Hc, WC, 3), 255, np.uint8)
    both = refMs & renMs; diff = refMs ^ renMs
    heat[both] = (210, 210, 215); heat[diff] = (230, 40, 40)

    cd_ = contour_dev(refMs, renMs)
    clp = clip_pct(ren_c, ren_m)

    def fmt(v, u=""):
        return f"{v:.2f}{u}" if isinstance(v, float) else str(v)
    def dcol(delta, tol):
        return (90, 220, 120) if abs(delta) <= tol else (240, 180, 60) if abs(delta) <= 2*tol else (240, 90, 90)
    lines = [(f"DELTA TABLE · {device} front · loop self-grade", (255,255,255)), ("", (0,0,0))]
    da = m_ren["aspect"] - m_ref["aspect"]
    lines.append((f"aspect  ref {m_ref['aspect']:.3f}  ren {m_ren['aspect']:.3f}  d {da:+.3f}", dcol(da,0.02)))
    lines.append((f"        spec {cfg['spec_aspect']:.3f}", (170,170,170)))
    if m_ref["corner_R"] and m_ren["corner_R"]:
        dr = m_ren["corner_R"] - m_ref["corner_R"]
        lines.append((f"top corner R  ref {m_ref['corner_R']:.1f}  ren {m_ren['corner_R']:.1f}  d {dr:+.1f}mm", dcol(dr,1.0)))
    lines.append((f"contour  max {cd_['maxpct']:.1f}% @y{cd_['myfrac']:.0%}  mean {cd_['meanpct']:.1f}%  XOR-area {cd_['xorpct']:.1f}%", dcol(cd_['xorpct'],2.0)))
    lines.append((f"clip (>=0.98)  {clp:.2f}%  {'PASS' if clp<1.0 else 'FAIL'}", (90,220,120) if clp<1.0 else (240,90,90)))
    lines.append(("", (0,0,0)))
    lines.append(("front features (x from center, w x h mm, aspect):", (255,255,255)))
    for tag, mm_ in (("ref", m_ref), ("ren", m_ren)):
        for fdd in mm_["feats"][:4]:
            lines.append((f"  {tag}  x{fdd['x_mm']:+6.1f}  {fdd['w']:4.1f}x{fdd['h']:4.1f}  ar{fdd['ar']:.2f}", (180,190,200)))

    # compose: 4 panels in a row, table strip below
    gap = 8
    row = np.full((Hc, WC*4 + gap*3, 3), 24, np.uint8)
    for i, P in enumerate((refP, renP, blend, heat)):
        row[:, i*(WC+gap):i*(WC+gap)+WC] = P
    labels = ["reference", "render (matched)", "50% blend", "silhouette diff"]
    rowim = Image.fromarray(row); dr = ImageDraw.Draw(rowim)
    for i, lb in enumerate(labels):
        dr.rectangle([i*(WC+gap), 0, i*(WC+gap)+WC, 20], fill=(24,24,28))
        dr.text((i*(WC+gap)+6, 4), lb, fill=(240,240,240))
    table = draw_table(lines, row.shape[1], 22*len(lines)+16)
    sheet = Image.new("RGB", (row.shape[1], Hc + table.height + gap), (24,24,24))
    sheet.paste(rowim, (0, 0)); sheet.paste(table, (0, Hc + gap))
    outp = os.path.join(ROOT, cfg["out"])
    sheet.save(outp)
    print(f"aspect ref {m_ref['aspect']:.3f} ren {m_ren['aspect']:.3f} | corner ref "
          f"{m_ref['corner_R']:.2f} ren {m_ren['corner_R']:.2f} | max {cd_['maxpct']:.1f}%@y{cd_['myfrac']:.0%} "
          f"mean {cd_['meanpct']:.1f}% XOR {cd_['xorpct']:.1f}% | clip {clp:.2f}%")
    print("wrote", os.path.relpath(outp, ROOT))
    return dict(aspect_ref=m_ref["aspect"], aspect_ren=m_ren["aspect"], cd=cd_, clip=clp,
                corner_ref=m_ref["corner_R"], corner_ren=m_ren["corner_R"],
                feats_ref=m_ref["feats"], feats_ren=m_ren["feats"])

if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "studio")
