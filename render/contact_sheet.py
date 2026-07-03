#!/usr/bin/env python3
# render/contact_sheet.py · assemble the phase-4 portrait set into one contact sheet.
import os
import numpy as np
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.abspath(__file__))
TILES = [
    ("mac-studio-front.png", "Mac Studio · front"),
    ("mac-studio-q34.png", "Mac Studio · three-quarter"),
    ("mac-studio-detail.png", "Mac Studio · detail (front ports)"),
    ("dgx-spark-front.png", "DGX Spark · front"),
    ("dgx-spark-q34.png", "DGX Spark · three-quarter"),
    ("dgx-spark-detail.png", "DGX Spark · detail (foam corner)"),
    ("oracles-pair@3x.png", "Tabletop pair · standing 35 deg eye line"),
]
TW = 900

def fit(im):
    return im.resize((TW, int(im.height * TW / im.width)), Image.LANCZOS)

def build():
    ims = []
    for f, lab in TILES:
        p = os.path.join(ROOT, "portraits", f)
        if not os.path.exists(p):
            print("skip", f); continue
        im = fit(Image.open(p).convert("RGB"))
        d = ImageDraw.Draw(im); d.rectangle([0, 0, im.width, 24], fill=(14, 14, 16))
        d.text((8, 6), lab, fill=(235, 235, 240))
        ims.append(im)
    cols = 2
    rows = (len(ims) + 1) // cols
    ch = max(i.height for i in ims)
    sheet = Image.new("RGB", (TW * cols + 8, ch * rows + 8 * rows), (20, 20, 24))
    for i, im in enumerate(ims):
        r, c = divmod(i, cols)
        sheet.paste(im, (c * (TW + 8), r * (ch + 8)))
    out = "measure_evidence/phase4-portrait-set.png"
    sheet.save(os.path.join(ROOT, out)); print("wrote", out, sheet.size)

if __name__ == "__main__":
    build()
