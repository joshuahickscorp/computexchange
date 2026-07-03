#!/usr/bin/env python3
# render/rig_patches.py · wave-0 in-rig tone gate (D1).
# Measures each pinned material patch IN the portrait rig and compares to its reference Lab
# target shifted by ONE global exposure offset. dE76 tolerance 4 per patch (foam pore 6).
#   python3 render/rig_patches.py --offset -18            # measure calib renders, table
#   python3 render/rig_patches.py --refs                  # print reference Labs (from crops)
# Boxes are normalized image coords (x0,y0,x1,y1) on the fixed portrait framing.

import os, sys
import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.abspath(__file__))
EVID = os.path.join(ROOT, "measure_evidence")
CAL  = os.path.join(ROOT, "calib")

def arg(name, d=None):
    a = sys.argv
    if name in a:
        i = a.index(name)
        return a[i+1] if i+1 < len(a) and not a[i+1].startswith("--") else True
    return d

def srgb_to_lab(rgb01):
    def inv(c): return np.where(c <= 0.04045, c/12.92, ((c+0.055)/1.055)**2.4)
    r, g, b = [inv(rgb01[..., i]) for i in range(3)]
    X = r*0.4124+g*0.3576+b*0.1805; Y = r*0.2126+g*0.7152+b*0.0722; Z = r*0.0193+g*0.1192+b*0.9505
    X /= 0.95047; Z /= 1.08883
    def f(t): return np.where(t > 0.008856, np.cbrt(t), 7.787*t+16/116)
    fX, fY, fZ = f(X), f(Y), f(Z)
    return np.stack([116*fY-16, 500*(fX-fY), 200*(fY-fZ)], -1)

def lab_of(rgb_u8):
    p = rgb_u8.reshape(-1, 3).astype(float)/255.0
    return srgb_to_lab(p.reshape(1, -1, 3)).reshape(-1, 3)

def box_px(im, box):
    w, h = im.size
    x0, y0, x1, y1 = box
    return (int(x0*w), int(y0*h), int(x1*w), int(y1*h))

def crop_arr(path, box=None):
    im = Image.open(path).convert("RGB")
    if box:
        im = im.crop(box_px(im, box))
    return np.asarray(im, dtype=np.uint8)

def smooth_lab(path, box):
    lab = lab_of(crop_arr(path, box))
    return np.median(lab, axis=0)                 # median rejects specular outliers

def quartile_lab(path, box, which):
    lab = lab_of(crop_arr(path, box))
    L = lab[:, 0]
    if which == "web":
        m = L >= np.quantile(L, 0.75)
    else:                                          # pore
        m = L <= np.quantile(L, 0.25)
    return lab[m].mean(axis=0)

def dE(a, b):
    return float(np.sqrt(((np.array(a)-np.array(b))**2).sum()))

# ---- reference Labs -------------------------------------------------------------------
# alu + champ are already pinned rows in MEASUREMENTS.md; intake + foam web/pore are derived
# here from the same evidence crops those rows were measured from (traceable, no new source).
def reference_labs():
    # intake: measure the PERFORATED MESH ONLY (lower band, below the red measurement line).
    # The full crop is ~60% bright front-face silver above the mesh, which inflated the old
    # mean to L77.7 (unrepresentative of the mesh's apparent tone). AUTOPSY: superseded.
    intake = np.median(lab_of(crop_arr(os.path.join(EVID, "mac_intake_band.png"),
                                       (0.20, 0.60, 0.80, 0.93))), axis=0)
    foam_crop = os.path.join(EVID, "dgx_foam_patch.png")
    fl = lab_of(crop_arr(foam_crop)); L = fl[:, 0]
    web = fl[L >= np.quantile(L, 0.75)].mean(axis=0)
    pore = fl[L <= np.quantile(L, 0.25)].mean(axis=0)
    return {
        "studio_alu":   np.array([84.32, 0.03, -1.12]),   # MEASUREMENTS row (apple_front)
        "studio_intake": intake,                          # from mac_intake_band.png
        "spark_champ":  np.array([72.52, 7.78, 42.78]),   # MEASUREMENTS row (storagereview)
        "spark_web":    web,                              # from dgx_foam_patch.png top quartile
        "spark_pore":   pore,                             # from dgx_foam_patch.png bottom quartile
    }

# ---- in-rig patch definitions (normalized boxes on the calib renders) -----------------
PATCHES = [
    dict(name="studio_alu",    file="mac-studio-front.png", box=(0.42, 0.44, 0.56, 0.60), kind="smooth", tol=4),
    dict(name="studio_intake", file="mac-studio-front.png", box=(0.32, 0.805, 0.62, 0.845), kind="smooth", tol=4),
    dict(name="spark_champ",   file="dgx-spark-q34.png",    box=(0.42, 0.22, 0.62, 0.32), kind="smooth", tol=4),
    dict(name="spark_web",     file="dgx-spark-q34.png",    box=(0.14, 0.48, 0.40, 0.72), kind="web",    tol=4),
    dict(name="spark_pore",    file="dgx-spark-q34.png",    box=(0.14, 0.48, 0.40, 0.72), kind="pore",   tol=6),
]

def measure(offset):
    refs = reference_labs()
    print(f"# in-rig tone gate · global offset O = {offset:+.1f} L · dE76 vs (ref_L+O, ref_a, ref_b)")
    print(f"{'patch':16} {'refL':>6} {'tgtL':>6} {'measL':>6} {'meas_a':>6} {'meas_b':>6} {'dE':>6} {'tol':>4}  verdict")
    allpass = True
    for p in PATCHES:
        path = os.path.join(CAL, p["file"])
        if not os.path.exists(path):
            print(f"{p['name']:16} MISSING {p['file']}"); allpass = False; continue
        if p["kind"] == "smooth":
            meas = smooth_lab(path, p["box"])
        else:
            meas = quartile_lab(path, p["box"], p["kind"])
        ref = refs[p["name"]]
        tgt = np.array([ref[0] + offset, ref[1], ref[2]])
        d = dE(meas, tgt)
        ok = d <= p["tol"]
        allpass = allpass and ok
        print(f"{p['name']:16} {ref[0]:6.1f} {tgt[0]:6.1f} {meas[0]:6.1f} {meas[1]:6.1f} {meas[2]:6.1f} {d:6.2f} {p['tol']:4}  {'PASS' if ok else 'FAIL'}")
    # natural offset diagnostic: per-patch (measL - refL), to help pick a self-consistent O
    print("# natural per-patch (measL - refL):")
    for p in PATCHES:
        path = os.path.join(CAL, p["file"])
        if not os.path.exists(path): continue
        meas = smooth_lab(path, p["box"]) if p["kind"] == "smooth" else quartile_lab(path, p["box"], p["kind"])
        print(f"#   {p['name']:16} {meas[0]-refs[p['name']][0]:+6.1f}")
    print("ALL PASS" if allpass else "SOME FAIL")
    return allpass

if __name__ == "__main__":
    if arg("--refs"):
        for k, v in reference_labs().items():
            print(f"{k:16} L={v[0]:6.2f} a={v[1]:6.2f} b={v[2]:6.2f}")
    else:
        off = float(arg("--offset", "0"))
        measure(off)
