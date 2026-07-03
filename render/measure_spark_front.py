#!/usr/bin/env python3
# render/measure_spark_front.py · wave-3 Spark front-structure remeasure.
# Autopsy of foam_field 148x46 (edge-to-edge assumption leaked in). The real front is:
# solid champagne END-CAPS at both ends, a BOUNDED center foam field with champagne margins.
# Measure from cl_front-foam (dead-on, device stood vertical -> long 150mm axis is vertical).
# Segmentation: device = high-local-variance foam OR gold(b*) champagne, vs neutral grey bg.
import os, numpy as np
from PIL import Image
import measure as M

ROOT = os.path.dirname(os.path.abspath(__file__))
P = os.path.join(ROOT, "ref/dgx-spark/cl_front-foam.jpg")

rgb = M.load_rgb(P)
H, W = rgb.shape[:2]
lab = M.srgb_to_lab(rgb.astype(float)/255.0)
L = lab[..., 0]; b = lab[..., 2]
std = M.local_std(L, 4)

foam = std > 9.0                      # textured foam -> high local std
gold = b > 14.0                       # champagne + foam struts are gold (b*)
dev = M.close_(foam | gold, 3)
# robust device bbox: use dense rows/cols (>2% coverage) to reject stray bg speckle
colcov = dev.mean(axis=0); rowcov = dev.mean(axis=1)
xcols = np.where(colcov > 0.02)[0]; yrows = np.where(rowcov > 0.02)[0]
dx0, dx1 = xcols.min(), xcols.max(); dy0, dy1 = yrows.min(), yrows.max()
dev_h = dy1 - dy0                      # long axis (150mm), vertical
dev_w = dx1 - dx0                      # short axis (50.5mm), horizontal
scale = dev_h / 150.0                  # px per mm (long-edge anchor)
print(f"# device bbox y[{dy0},{dy1}] x[{dx0},{dx1}]  {dev_w}x{dev_h}px  scale {scale:.3f} px/mm")
print(f"# short-axis check: {dev_w/scale:.1f} mm (spec 50.5)")

# foam field = the central high-texture band. Per-row MEDIAN std across the device width:
# smooth champagne caps read low, foam reads high. Threshold at the profile midpoint.
def band(std2d, a0, a1, b0, b1, axis):
    # median std along `axis` within the device box; return contiguous run over midpoint T
    sub = std2d[a0:a1, b0:b1]
    prof = np.median(sub, axis=axis)             # axis=1 -> per-row; axis=0 -> per-col
    prof = np.convolve(prof, np.ones(7)/7, mode="same")
    T = (prof.min() + prof.max()) / 2.0
    on = prof > T
    # largest contiguous True run
    best = (0, 0); i = 0
    while i < len(on):
        if on[i]:
            j = i
            while j < len(on) and on[j]: j += 1
            if j - i > best[1] - best[0]: best = (i, j)
            i = j
        else:
            i += 1
    return best, T, prof.min(), prof.max()

(ry0, ry1), Tr, rmin, rmax = band(std, dy0, dy1, dx0, dx1, axis=1)
(cx0, cx1), Tc, cmin, cmax = band(std, dy0, dy1, dx0, dx1, axis=0)
fy0, fy1 = dy0 + ry0, dy0 + ry1
fx0, fx1 = dx0 + cx0, dx0 + cx1
print(f"# row-std profile min {rmin:.1f} max {rmax:.1f} T {Tr:.1f}; col-std min {cmin:.1f} max {cmax:.1f} T {Tc:.1f}")
print(f"# foam bbox y[{fy0:.0f},{fy1:.0f}] x[{fx0:.0f},{fx1:.0f}]")

cap_top = (fy0 - dy0)/scale
cap_bot = (dy1 - fy1)/scale
foam_long = (fy1 - fy0)/scale
foam_short = (fx1 - fx0)/scale
margin_l = (fx0 - dx0)/scale
margin_r = (dx1 - fx1)/scale
print(f"endcap_width (long-axis ends): top {cap_top:.1f} mm  bottom {cap_bot:.1f} mm  mean {(cap_top+cap_bot)/2:.1f}")
print(f"foam_field_span (long, between caps): {foam_long:.1f} mm  (was 148.02 edge-to-edge)")
print(f"foam_field_short: {foam_short:.1f} mm  (was 46.34)")
print(f"foam_margins (short-axis lips): left {margin_l:.1f} mm  right {margin_r:.1f} mm  mean {(margin_l+margin_r)/2:.1f}")

M.save_crop(rgb, (dx0-10, dy0-10, dx1+10, dy1+10), "wave3-spark-front-struct.png",
            lines=[(dx0, int(fy0), dx1, int(fy0), (255,0,0)), (dx0, int(fy1), dx1, int(fy1), (255,0,0)),
                   (int(fx0), dy0, int(fx0), dy1, (0,255,0)), (int(fx1), dy0, int(fx1), dy1, (0,255,0))])
print("# crop wave3-spark-front-struct.png (red=cap/foam long bounds, green=foam short bounds)")
