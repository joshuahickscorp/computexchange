#!/usr/bin/env python3
# render/measure_spark_bezel.py · final-wave Commit A remeasure.
# The wave-3 "31.5mm solid end-caps" were wrong. The truth: foam edge-to-edge, champagne pill
# BEZEL islands embedded in the foam, thin ~1mm end rails. Measure the bezel + rail + foam from
# the dead-on cl_front-foam (150mm long axis vertical), and pin ALL Spark colours to sth_front-1.
import os, sys, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import measure as M

ROOT = os.path.dirname(os.path.abspath(__file__))

# ---------- structure from cl_front-foam (device vertical, long axis = y) ----------
rgb = M.load_rgb(os.path.join(ROOT, "ref/dgx-spark/cl_front-foam.jpg"))
lab = M.srgb_to_lab(rgb.astype(float) / 255.0)
L = lab[..., 0]; bb = lab[..., 2]
std = M.local_std(L, 4)
foam = std > 9.0; gold = bb > 14.0
dev = M.close_(foam | gold, 3)
colcov = dev.mean(0); rowcov = dev.mean(1)
xs = np.where(colcov > 0.02)[0]; ys = np.where(rowcov > 0.02)[0]
dx0, dx1, dy0, dy1 = xs.min(), xs.max(), ys.min(), ys.max()
devH = dy1 - dy0  # 150mm
scale = devH / 150.0
cx = (dx0 + dx1) // 2
print(f"# device y[{dy0},{dy1}] x[{dx0},{dx1}] scale {scale:.3f}px/mm short {(dx1-dx0)/scale:.1f}mm")

champ = gold & (std < 4.0)  # smooth champagne (bezel + rails + lips), not foam
# pill centres along the long axis: 15.21mm from each end (pill_center_from_end)
pc_off = int(15.21 * scale)
pills = {"bottom": dy1 - pc_off, "top": dy0 + pc_off}

def bezel_extent(pcy):
    # window around the pill centre; bezel = champ percentile bbox (trims stray foam struts).
    # In cl_front-foam: x is the 50.5 SHORT axis, y is the 150 LONG axis.
    wy = int(30 * scale); wx = int(20 * scale)
    y0, y1 = max(dy0, pcy - wy), min(dy1, pcy + wy)
    x0, x1 = max(dx0, cx - wx), min(dx1, cx + wx)
    sub = champ[y0:y1, x0:x1]
    ys2, xs2 = np.where(sub)
    if len(ys2) < 30:
        return None
    ext_short = (np.percentile(xs2, 97) - np.percentile(xs2, 3)) / scale   # along 50.5
    ext_long = (np.percentile(ys2, 97) - np.percentile(ys2, 3)) / scale    # along 150
    return ext_short, ext_long

for name, pcy in pills.items():
    e = bezel_extent(pcy)
    if e:
        e50, e150 = e   # bezel extent along the 50.5 axis, and along the 150 axis
        # in the DEVICE front frame (150 wide x 50.5 tall): bezel is e150 wide x e50 tall,
        # around the pill (12.96 wide along 150 x 31.41 tall along 50.5)
        print(f"# bezel {name}: {e150:.1f}mm (along 150) x {e50:.1f}mm (along 50.5) · slot 12.96x31.41 "
              f"-> border ~{(e150-12.96)/2:.1f}mm side, ~{(e50-31.41)/2:.1f}mm top/bottom")

# end rail: champagne at the extreme long-axis end (first/last few mm), min smooth width
def rail_width(end):
    rows = range(dy1 - int(2*scale), dy1) if end == "bottom" else range(dy0, dy0 + int(2*scale))
    widths = []
    for y in rows:
        c = np.where(champ[y, dx0:dx1])[0]
        if len(c):
            widths.append((c.max() - c.min()) / scale)
    return np.median(widths) if widths else 0
# rail is the thin champagne strip at the very corner; measure the champagne band beyond the foam
# at the ends by scanning inboard from each end until foam starts
def end_rail(end):
    step = -1 if end == "bottom" else 1
    y = dy1 - int(1*scale) if end == "bottom" else dy0 + int(1*scale)
    # at this row near the end, champagne spans most of the width (the end rail band); the rail
    # THICKNESS along the long axis = distance from the end to where foam appears at centre x
    yy = dy1 if end == "bottom" else dy0
    d = 0
    while 0 <= yy < L.shape[0] and champ[yy, cx] and d < int(12*scale):
        yy += step; d += 1
    return d / scale
print(f"# end_rail thickness: bottom {end_rail('bottom'):.1f}mm  top {end_rail('top'):.1f}mm "
      f"(reconcile with phase-0 foam_end_band 0.99)")

# foam field extents: high-variance band (per-row/col), should now span nearly edge-to-edge
frow = foam[dy0:dy1, dx0:dx1].mean(1); fcol = foam[dy0:dy1, dx0:dx1].mean(0)
fy = np.where(frow > 0.15)[0]; fx = np.where(fcol > 0.15)[0]
print(f"# foam field: long {(fy.max()-fy.min())/scale:.1f}mm  short {(fx.max()-fx.min())/scale:.1f}mm "
      f"(edge-to-edge ~148 x 46 expected)")

# ---------- colour pins: ALL from sth_front-1, spread vs cl_side-profile + storagereview ----------
print("\n# COLOUR PINS (sth_front-1 is THE pin; others = spread)")
def patches(path, champ_box, foam_box):
    rgb2 = M.load_rgb(os.path.join(ROOT, path)); h, w = rgb2.shape[:2]
    def med(box):
        p = rgb2[int(box[1]*h):int(box[3]*h), int(box[0]*w):int(box[2]*w)]
        return M.srgb_to_lab(p.astype(float)/255.0).reshape(-1, 3)
    ch = np.median(med(champ_box), 0)
    fl = med(foam_box); Lf = fl[:, 0]
    web = fl[Lf >= np.quantile(Lf, 0.75)].mean(0)
    pore = fl[Lf <= np.quantile(Lf, 0.25)].mean(0)
    return ch, web, pore

# boxes tuned per source (champagne bezel/rail patch, foam field patch)
S = {
 "sth_front-1.jpg":       ((0.315, 0.55, 0.345, 0.64), (0.44, 0.44, 0.56, 0.60)),
 "cl_side-profile.jpg":   ((0.27, 0.32, 0.30, 0.55), (0.42, 0.42, 0.58, 0.58)),
 "storagereview_front.jpg": ((0.62, 0.45, 0.66, 0.60), (0.40, 0.42, 0.60, 0.58)),
}
for src, (cb, fb) in S.items():
    ch, web, pore = patches("ref/dgx-spark/" + src, cb, fb)
    tag = "PIN" if src.startswith("sth") else "   "
    print(f"# {tag} {src:24} champ L{ch[0]:5.1f} a{ch[1]:5.1f} b{ch[2]:5.1f} | web L{web[0]:5.1f} b{web[2]:5.1f} | pore L{pore[0]:5.1f} a{pore[1]:5.1f} b{pore[2]:5.1f}")
