#!/usr/bin/env python3
# render/measure_fillet.py · wave-1 top-edge fillet remeasure + autopsy.
# Fit the top-corner arc of the dimensions.com REAR elevation (left drawing, 197mm wide) to
# recover the true top-edge fillet radius, which the front-outline fit conflated with the
# 31.4mm plan corner. Cross-check reported separately against apple_front's rolloff band.
import os, numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.abspath(__file__))
P = os.path.join(ROOT, "ref/mac-studio/dim_back-side.svg.png")

def kasa(xs, ys):
    x = np.asarray(xs, float); y = np.asarray(ys, float)
    A = np.c_[2*x, 2*y, np.ones(len(x))]
    b = x*x + y*y
    cx, cy, c = np.linalg.lstsq(A, b, rcond=None)[0]
    r = np.sqrt(c + cx*cx + cy*cy)
    rr = np.sqrt((x-cx)**2 + (y-cy)**2)
    return cx, cy, r, float(np.sqrt(np.mean((rr-r)**2)))

im = np.asarray(Image.open(P).convert("RGB"), dtype=np.int16)
stroke = im.min(axis=2) < 180          # any non-white (cyan strokes)
# left (rear) elevation only: strokes with x in the left drawing
H, W = stroke.shape
ys, xs = np.where(stroke)
left = xs < W * 0.62
xs, ys = xs[left], ys[left]
x_left, x_right = xs.min(), xs.max()
y_top, y_bot = ys.min(), ys.max()
width_px = x_right - x_left
scale = width_px / 197.0               # px per mm (rear width = 197mm)
print(f"# rear elevation bbox x[{x_left},{x_right}] y[{y_top},{y_bot}] width {width_px}px  scale {scale:.3f} px/mm")

# outer boundary near the top-left corner: leftmost stroke per row + topmost stroke per col,
# within a corner neighborhood, then fit the arc.
def corner_fit(nb):
    pts_x, pts_y = [], []
    # left outer edge: for rows y_top..y_top+nb, leftmost stroke x
    for y in range(y_top, y_top + nb):
        row = np.where(stroke[y, x_left:x_left + nb])[0]
        if len(row): pts_x.append(x_left + row[0]); pts_y.append(y)
    # top outer edge: for cols x_left..x_left+nb, topmost stroke y
    for x in range(x_left, x_left + nb):
        col = np.where(stroke[y_top:y_top + nb, x])[0]
        if len(col): pts_x.append(x); pts_y.append(y_top + col[0])
    # keep only points on the curved part: within radius nb of the ideal corner
    px = np.array(pts_x, float); py = np.array(pts_y, float)
    d = np.sqrt((px - x_left)**2 + (py - y_top)**2)
    m = d < nb
    cx, cy, r, rms = kasa(px[m], py[m])
    return r, rms, m.sum()

for nb in (24, 32, 40, 50):
    r, rms, n = corner_fit(nb)
    print(f"# nb={nb:3d}px  R = {r/scale:5.2f} mm   (rms {rms/scale:.2f} mm, {n} pts)")

# also do the top-RIGHT corner as an independent check
def corner_fit_tr(nb):
    pts_x, pts_y = [], []
    for y in range(y_top, y_top + nb):
        row = np.where(stroke[y, x_right - nb:x_right])[0]
        if len(row): pts_x.append(x_right - nb + row[-1]); pts_y.append(y)
    for x in range(x_right - nb, x_right):
        col = np.where(stroke[y_top:y_top + nb, x])[0]
        if len(col): pts_x.append(x); pts_y.append(y_top + col[0])
    px = np.array(pts_x, float); py = np.array(pts_y, float)
    d = np.sqrt((px - x_right)**2 + (py - y_top)**2)
    m = d < nb
    cx, cy, r, rms = kasa(px[m], py[m])
    return r/scale, rms/scale
r_tr, rms_tr = corner_fit_tr(40)
print(f"# top-RIGHT corner nb=40: R = {r_tr:.2f} mm (rms {rms_tr:.2f})")
