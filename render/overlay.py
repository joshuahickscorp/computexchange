#!/usr/bin/env python3
# overlay.py · reproduction-grade verification. Composite a verify render (transparent,
# from build_scene.py --verify) at 50 percent over the official press reference, aligned
# by device bounding box, so every silhouette or feature divergence is visible and
# measurable. Reference imagery is for VIEWING ONLY · never a texture, never traced.
#
#   python3 render/overlay.py studio   # or: spark
#
# Writes render/verify/<device>-front-overlay.png (the 50 percent composite) and
# render/verify/<device>-front-sidebyside.png. Prints the measured bbox deltas.
import sys
from PIL import Image
import numpy as np

DEV = sys.argv[1] if len(sys.argv) > 1 else "studio"
NAME = "mac-studio" if DEV == "studio" else "dgx-spark"
REF = f"render/ref/{NAME}-front-ref.jpg"
REN = f"render/verify/{NAME}-front.png"


def alpha_bbox(rgba):
    a = np.asarray(rgba)[:, :, 3]
    ys, xs = np.where(a > 20)
    return xs.min(), ys.min(), xs.max(), ys.max()


def ref_bbox(rgb):
    """The device is darker than the near-white studio background · threshold and
    take the bbox of the dark region (robust to the subtle bg gradient)."""
    g = np.asarray(rgb.convert("L")).astype(np.int16)
    mask = g < 236
    colcount = mask.sum(axis=0)
    rowcount = mask.sum(axis=1)
    cthr = max(3, int(0.02 * rgb.height))
    rthr = max(3, int(0.02 * rgb.width))
    cols = np.where(colcount > cthr)[0]
    rows = np.where(rowcount > rthr)[0]
    return cols.min(), rows.min(), cols.max(), rows.max()


def main():
    ref = Image.open(REF).convert("RGB")
    ren = Image.open(REN).convert("RGBA")

    rx0, ry0, rx1, ry1 = ref_bbox(ref)
    ax0, ay0, ax1, ay1 = alpha_bbox(ren)
    ref_w, ref_h = rx1 - rx0, ry1 - ry0
    ren_w, ren_h = ax1 - ax0, ay1 - ay0

    # scale the render so its device WIDTH matches the reference device width, then
    # align by device-bbox TOP-CENTER · any height/silhouette mismatch stays visible.
    scale = ref_w / ren_w
    new_size = (int(ren.width * scale), int(ren.height * scale))
    ren_s = ren.resize(new_size, Image.LANCZOS)
    # device bbox in the scaled render
    sax0, say0 = ax0 * scale, ay0 * scale
    scx = (ax0 + ax1) / 2 * scale
    # paste so scaled device top-center lands on ref device top-center
    ref_cx = (rx0 + rx1) / 2
    px = int(ref_cx - scx)
    py = int(ry0 - say0)

    # aspect-ratio finding
    ref_ar = ref_w / ref_h
    ren_ar = ren_w / ren_h
    print(f"[{NAME}] reference device bbox {ref_w}x{ref_h} (AR {ref_ar:.3f})")
    print(f"[{NAME}] render    device bbox {ren_w}x{ren_h} (AR {ren_ar:.3f})")
    print(f"[{NAME}] width-matched · height delta {(ren_h*scale - ref_h):+.0f}px "
          f"({(ren_ar/ref_ar - 1)*100:+.1f}% aspect)")

    # 50 percent composite
    base = ref.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    layer.paste(ren_s, (px, py), ren_s)
    lp = np.asarray(layer).astype(np.float32)
    lp[:, :, 3] *= 0.5
    layer = Image.fromarray(lp.astype(np.uint8), "RGBA")
    out = Image.alpha_composite(base, layer)
    # draw the two bboxes for reference
    out.convert("RGB").save(f"render/verify/{NAME}-front-overlay.png")

    # side by side (ref | render scaled to same height)
    rh = ref.height
    rn = ren.resize((int(ren.width * rh / ren.height), rh), Image.LANCZOS)
    sbs = Image.new("RGB", (ref.width + rn.width + 20, rh), (12, 12, 12))
    sbs.paste(ref, (0, 0))
    sbs.paste(rn.convert("RGB"), (ref.width + 20, 0))
    sbs.save(f"render/verify/{NAME}-front-sidebyside.png")
    print(f"[{NAME}] wrote overlay + sidebyside to render/verify/")


if __name__ == "__main__":
    main()
