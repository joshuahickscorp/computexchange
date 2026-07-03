#!/usr/bin/env python3
# render/portrait_compare.py · one final compare per portrait against its nearest reference
# angle. Front + detail shots here; the q34 shots are the settlement gates (settle_sheet.py).
#   python3 render/portrait_compare.py

import os
import numpy as np
from PIL import Image, ImageDraw
import measure as M

ROOT = os.path.dirname(os.path.abspath(__file__))

# portrait -> (reference image, crop-region-of-reference or None for whole, label)
JOBS = [
    ("mac-studio-front", "ref/mac-studio/apple_front.jpg", None, "Mac Studio front vs apple_front"),
    ("mac-studio-detail", "ref/mac-studio/apple_front.jpg", (0.30, 0.55, 0.62, 0.95), "Studio detail (front ports) vs apple_front ports"),
    ("dgx-spark-front", "ref/dgx-spark/sth_front-1.jpg", None, "DGX Spark front vs sth_front-1"),
    ("dgx-spark-detail", "ref/dgx-spark/storagereview_front.jpg", (0.30, 0.40, 0.62, 0.62), "Spark detail (foam corner) vs storagereview foam"),
]

def dev_crop(path):
    a = np.asarray(Image.open(os.path.join(ROOT, path)).convert("RGBA"))
    m = a[..., 3] > 128
    if m.sum() < 100:
        return Image.fromarray(a[..., :3])
    x0, y0, x1, y1 = M.bbox_of(m)
    return Image.fromarray(a[y0:y1, x0:x1, :3])

def ref_crop(path, region):
    im = Image.open(os.path.join(ROOT, path)).convert("RGB")
    if region:
        w, h = im.size
        im = im.crop((int(region[0]*w), int(region[1]*h), int(region[2]*w), int(region[3]*h)))
    return im

def build():
    for name, ref, region, label in JOBS:
        rp = os.path.join(ROOT, f"portraits/{name}.png")
        if not os.path.exists(rp):
            print("skip (missing)", name); continue
        ren = dev_crop(f"portraits/{name}.png")
        rf = ref_crop(ref, region)
        H = 620
        def fit(im):
            return im.resize((int(im.width*H/im.height), H), Image.LANCZOS)
        rf, ren = fit(rf), fit(ren)
        sheet = Image.new("RGB", (rf.width+ren.width+12, H+50), (22, 22, 26))
        d = ImageDraw.Draw(sheet)
        d.text((6, 6), label, fill=(255, 255, 255))
        sheet.paste(rf, (0, 28)); sheet.paste(ren, (rf.width+12, 28))
        d.text((6, H+32), "reference", fill=(210, 210, 210))
        d.text((rf.width+18, H+32), "render portrait", fill=(210, 210, 210))
        out = f"measure_evidence/compare-{name}.png"
        sheet.save(os.path.join(ROOT, out)); print("wrote", out)

if __name__ == "__main__":
    build()
