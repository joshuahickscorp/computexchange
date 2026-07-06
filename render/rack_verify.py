#!/usr/bin/env python3
# render/rack_verify.py · the NUMERIC ORACLE for the rack goal-loop.
# Runs clipcheck + in-rig Lab patch reads on a rack render and verdicts against the pinned
# targets with the RACK offset (O_rack, measured gate-5a: dark object sits ABOVE its
# flat-studio reference · opposite the desktops' O=-12). Exit 0 = all gates PASS.
#
#   python3 render/rack_verify.py render/rack_previews/frame-frame-front.png --shot frame-front
#   python3 render/rack_verify.py <render.png> --shot <name> [--offset 6.0]
#
# Patch boxes are normalized (x0,y0,x1,y1) per shot on the FIXED framing of build_rack.py.
# Add a shot's patches here when its framing is locked; a framing change re-verifies boxes.
# Dash gate: middot only.

import sys
import numpy as np
from PIL import Image

def arg(name, d=None):
    a = sys.argv
    if name in a:
        i = a.index(name)
        return a[i + 1] if i + 1 < len(a) and not a[i + 1].startswith("--") else True
    return d

O_RACK = float(arg("--offset", 0.0))   # AUTOPSY 2026-07-05: the gate-5a "+6" came from a box
# contaminated by the brighter side-channel wall behind the rail. Clean flange box reads
# L16.6 in-rig vs ref L16 -> natural offset ~+0.6 ~= 0. Dark-regime L is compressive · a dark
# object largely TRACKS its reference tone under the hero rig. Working O_rack = 0.0; final
# derivation lands on the RM44 broad front face (the honest patch analog) at the part wave.
CLIP_THR, CLIP_FAIL = 0.98, 1.0

# ---- reference pins (reference-side Lab · RACK-BUILD-PLAN section 0) ----------------------
PINS = {
    "powder_black": (16.0, 0.3, -1.0),   # RM44 lid/ear band L15-18 · rm44_front_A
    "switch_white": (74.0, 0.6, -2.5),   # CRS354 lit top band · crs354_sth_front
    "port_cavity":  (25.0, 1.5, 3.0),    # RJ45 recess · dark-not-black, warm
    "ups_black":    (16.0, 0.3, -1.0),   # same powder class · LOW-CONF pin (440px photo)
}
TOL = {"powder_black": 5.0, "switch_white": 4.0, "port_cavity": 6.0, "ups_black": 6.0}
# NOTE tolerance powder_black 5 (dark-regime L reads are noisier) · switch_white 4 (bright,
# desktop-standard) · cavity/ups 6 (position-dominated / low-conf pin).

# ---- per-shot patch boxes (normalized) -----------------------------------------------------
SHOTS = {
    # empty frame probe (gate 5a framing · 1400x2000 or preview-scaled)
    "frame-front": [
        ("powder_black", (0.618, 0.25, 0.624, 0.75), "right rail flange, key side"),
        # AUTOPSY 2026-07-05 (R0.2): box was (0.655..0.683) · widening the corner post front face
        # 30->45mm moved the post inner edge into that band -> it sampled the bright post face (L58),
        # a false FAIL (material unchanged). Re-derived to a NARROW flange strip x0.618-0.624; the
        # oracle uses per-pixel MEDIAN and the rail is PERFORATED, so a wide box goes bimodal
        # (holes L8 vs flange L18) · the narrow strip lands median L18.1 dE2.08 PASS. Same
        # contamination class as the gate-5a +6 autopsy. FRAGILE by construction (perforated rail) ·
        # the durable powder_black patch moves to the RM44 solid front face at the node wave.
    ],
    # RM44 node solo front (Wave 1.5 · durable powder patch = the SOLID ear L-face · stable low-std
    # read, NOT the perforated mesh or edge-highlight border · lands L15.7 on the L16 pin at the
    # calibrated rig key14/rim9/fill5.3, the dark-object read the frame proof established).
    "node-front": [
        ("powder_black", (0.165, 0.46, 0.185, 0.56), "RM44 left ear front face, solid powder"),
    ],
    "switch-front": [
        ("switch_white", (0.44, 0.46, 0.56, 0.52), "CRS354 front face, white powder"),
    ],
    # full assembly front (gate 6 framing · boxes land when framing locks)
    # "front": [ ... ],
    # "q34":   [ ... ],
}

def srgb_to_lab(rgb01):
    def inv(c): return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = [inv(rgb01[..., i]) for i in range(3)]
    X = r*0.4124 + g*0.3576 + b*0.1805
    Y = r*0.2126 + g*0.7152 + b*0.0722
    Z = r*0.0193 + g*0.1192 + b*0.9505
    X /= 0.95047; Z /= 1.08883
    def f(t): return np.where(t > 0.008856, np.cbrt(t), 7.787*t + 16/116)
    fX, fY, fZ = f(X), f(Y), f(Z)
    return np.stack([116*fY - 16, 500*(fX - fY), 200*(fY - fZ)], -1)

def main():
    path = sys.argv[1]
    shot = str(arg("--shot", "frame-front"))
    a = np.asarray(Image.open(path).convert("RGB"), float) / 255.0
    h, w = a.shape[:2]

    # clip gate (device pixels = brighter than near-black bg)
    dev = a[a.max(2) > 0.03]
    clip = 100.0 * np.mean(np.any(dev >= CLIP_THR, axis=1)) if len(dev) else 0.0
    peak = float(dev.max()) if len(dev) else 0.0
    clip_ok = clip < CLIP_FAIL
    print(f"# rack_verify · {path} · shot={shot} · O_rack={O_RACK:+.1f}")
    print(f"clip {clip:.3f}% (>= {CLIP_THR}) peak {peak:.3f}  {'PASS' if clip_ok else 'FAIL'}")

    allpass = clip_ok
    patches = SHOTS.get(shot, [])
    if not patches:
        print(f"(no patch boxes defined for shot '{shot}' · clip gate only)")
    print(f"{'patch':14} {'refL':>6} {'tgtL':>6} {'measL':>6} {'a':>6} {'b':>6} {'dE':>6} {'tol':>4}  verdict")
    for name, box, note in patches:
        x0, y0, x1, y1 = box
        crop = a[int(y0*h):int(y1*h), int(x0*w):int(x1*w)].reshape(-1, 3)
        lab = srgb_to_lab(crop.reshape(1, -1, 3)).reshape(-1, 3)
        meas = np.median(lab, axis=0)
        ref = PINS[name]
        tgt = np.array([ref[0] + O_RACK, ref[1], ref[2]])
        dE = float(np.sqrt(((meas - tgt) ** 2).sum()))
        ok = dE <= TOL[name]
        allpass = allpass and ok
        print(f"{name:14} {ref[0]:6.1f} {tgt[0]:6.1f} {meas[0]:6.1f} {meas[1]:6.1f} {meas[2]:6.1f} "
              f"{dE:6.2f} {TOL[name]:4.1f}  {'PASS' if ok else 'FAIL'}  · {note}")
    print("ALL PASS" if allpass else "SOME FAIL")
    return 0 if allpass else 1

if __name__ == "__main__":
    sys.exit(main())
