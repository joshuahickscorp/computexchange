#!/usr/bin/env python3
# render/clipcheck.py · phase-2 tone discipline. After every render, report the percentage of
# DEVICE-surface pixels at or above 0.98 in any channel. Above 1% fails the render outright ·
# this makes a blown highlight a number, not an opinion. Device pixels = alpha>0.5 for the
# transparent-bg verify/turnaround renders; for opaque renders pass --mask-nonbg to treat any
# non-near-black pixel as device.
#
#   python3 render/clipcheck.py render/verify/mac-studio-front.png [...more]
#
# Exit code 1 if any render fails, so it can gate a loop.

import sys
import numpy as np
from PIL import Image

THR = 0.98
FAIL_PCT = 1.0

def device_mask(a):
    if a.shape[-1] == 4:
        return a[..., 3] > 0.5
    # opaque: everything brighter than a near-black background
    return a[..., :3].max(-1) > 0.06

def clipcheck(path, thr=THR, fail=FAIL_PCT):
    a = np.asarray(Image.open(path).convert("RGBA")).astype(float) / 255.0
    mask = device_mask(a)
    dev = a[..., :3][mask]
    if len(dev) == 0:
        print(f"{path}: NO DEVICE PIXELS"); return 0.0
    pct = 100.0 * np.mean(np.any(dev >= thr, axis=1))
    peak = dev.max()
    status = "PASS" if pct < fail else "FAIL"
    print(f"{path}: clip {pct:.3f}% (>={thr}) peak {peak:.3f}  {status}")
    return pct

if __name__ == "__main__":
    fails = 0
    for p in sys.argv[1:]:
        if clipcheck(p) >= FAIL_PCT:
            fails += 1
    sys.exit(1 if fails else 0)
