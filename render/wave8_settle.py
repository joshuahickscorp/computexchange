#!/usr/bin/env python3
# render/wave8_settle.py · the five wave-8 settlement sheets (reference | render), plus detail
# crops. Run after the re-shoot populates render/portraits/.
import os
import numpy as np
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.abspath(__file__))
POR = os.path.join(ROOT, "portraits")
EVID = os.path.join(ROOT, "measure_evidence")

# (render portrait, reference image, optional ref crop (x0,y0,x1,y1 frac), label)
JOBS = [
    ("mac-studio-front", "ref/mac-studio/apple_front.jpg", None, "Studio FRONT vs apple_front"),
    ("mac-studio-q34", "ref/mac-studio/apple_lifestyle_3q.jpg", (0.30, 0.42, 0.66, 0.78), "Studio 3/4 vs apple_lifestyle_3q"),
    ("dgx-spark-front", "ref/dgx-spark/sth_front-1.jpg", None, "Spark FRONT vs sth_front-1"),
    ("dgx-spark-q34", "ref/dgx-spark/nv_hero_3q.png", None, "Spark 3/4 vs nv_hero_3q"),
    ("dgx-spark-top", "ref/dgx-spark/cl_side-profile.jpg", None, "Spark TOP vs cl_side-profile (rebuilt vent)"),
]

def dev_crop(path):
    a = np.asarray(Image.open(path).convert("RGBA"))
    if a.shape[2] == 4 and (a[..., 3] > 128).sum() > 100:
        m = a[..., 3] > 128
    else:
        L = a[..., :3].mean(2); m = L > 12
    ys, xs = np.where(m)
    return Image.fromarray(a[ys.min():ys.max(), xs.min():xs.max(), :3])

def ref_crop(path, region):
    im = Image.open(path).convert("RGB")
    if region:
        w, h = im.size
        im = im.crop((int(region[0]*w), int(region[1]*h), int(region[2]*w), int(region[3]*h)))
    return im

def build():
    for name, ref, region, label in JOBS:
        rp = os.path.join(POR, name + ".png")
        if not os.path.exists(rp):
            print("skip (missing)", name); continue
        ren = dev_crop(rp)
        rf = ref_crop(os.path.join(ROOT, ref), region)
        H = 620
        def fit(im): return im.resize((int(im.width*H/im.height), H), Image.LANCZOS)
        rf, ren = fit(rf), fit(ren)
        s = Image.new("RGB", (rf.width+ren.width+12, H+46), (22, 22, 26))
        d = ImageDraw.Draw(s)
        d.text((6, 6), "WAVE 8 SETTLEMENT · " + label, fill=(255, 255, 255))
        s.paste(rf, (0, 28)); s.paste(ren, (rf.width+12, 28))
        d.text((6, H+30), "reference", fill=(200, 200, 200))
        d.text((rf.width+18, H+30), "render (frozen rig)", fill=(200, 200, 200))
        out = os.path.join(EVID, "settle8-" + name + ".png")
        s.save(out); print("wrote", os.path.relpath(out, ROOT))

def details():
    # detail crops from the 4K portraits (the detail camera framing is unreliable; crop instead)
    jobs = [
        ("mac-studio-front", (0.16, 0.66, 0.46, 0.82), "mac-studio-detail"),
        ("dgx-spark-q34", (0.14, 0.47, 0.42, 0.74), "dgx-spark-detail"),
    ]
    for src, box, out in jobs:
        p = os.path.join(POR, src + ".png")
        if not os.path.exists(p): print("skip detail", src); continue
        im = Image.open(p).convert("RGB"); w, h = im.size
        c = im.crop((int(box[0]*w), int(box[1]*h), int(box[2]*w), int(box[3]*h)))
        c.save(os.path.join(POR, out + ".png")); print("wrote detail", out)

if __name__ == "__main__":
    details()
    build()
