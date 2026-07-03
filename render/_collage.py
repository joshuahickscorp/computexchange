#!/usr/bin/env python3
# render/_collage.py · the four evidence collages for the shift report. Pure PIL, black ground,
# thin labels. Missing inputs are skipped (never crash the report build).
import os
from PIL import Image, ImageDraw, ImageFont

OUT = "render/collages"; os.makedirs(OUT, exist_ok=True)
BG = (8, 8, 9); FG = (200, 200, 205)

def font(sz):
    for p in ["/System/Library/Fonts/SFNSMono.ttf", "/System/Library/Fonts/Menlo.ttc",
              "/Library/Fonts/Arial.ttf"]:
        if os.path.exists(p):
            try: return ImageFont.truetype(p, sz)
            except Exception: pass
    return ImageFont.load_default()

def load(path, w):
    if not os.path.exists(path): return None
    im = Image.open(path).convert("RGB")
    return im.resize((w, round(im.size[1]*w/im.size[0])), Image.LANCZOS)

def grid(tiles, cols, cellw, title, out, pad=18, lab=16):
    tiles = [(load(p, cellw), cap) for p, cap in tiles]
    tiles = [(im, cap) for im, cap in tiles if im]
    if not tiles: print("skip", out); return
    rows = (len(tiles)+cols-1)//cols
    rh = [0]*rows
    for i, (im, _) in enumerate(tiles): rh[i//cols] = max(rh[i//cols], im.size[1])
    W = pad + cols*(cellw+pad)
    H = 54 + sum(rh) + rows*(lab+pad) + pad
    cv = Image.new("RGB", (W, H), BG); d = ImageDraw.Draw(cv)
    d.text((pad, 18), title, FG, font=font(24))
    y = 54
    for r in range(rows):
        x = pad
        for c in range(cols):
            i = r*cols+c
            if i >= len(tiles): break
            im, cap = tiles[i]
            cv.paste(im, (x, y))
            d.text((x, y+im.size[1]+4), cap, (150,150,155), font=font(lab))
            x += cellw+pad
        y += rh[r]+lab+pad
    cv.save(out); print("->", out, cv.size)

P = "render/portraits"; RAW = "render/portraits-raw"; REF = "render/ref"; EV = "render/measure_evidence"

# 1 · gate frames (the four the panel judges)
grid([(f"{P}/mac-studio-front.png","Studio front · final (post)"),
      (f"{P}/dgx-spark-front.png","Spark front · final (post)"),
      (f"{P}/oracles-pair@3x.png","Pair · final (post)"),
      (f"{P}/dgx-spark-detail.png","Spark detail · final (post)")],
     2, 760, "CX ORACLES · GATE FRAMES (photoreal frontier)", f"{OUT}/1_gate_frames.png")

# 2 · settlement pairs · my final beside the real reference
grid([(f"{P}/mac-studio-front.png","Studio · render"),
      (f"{REF}/mac-studio-front-ref.jpg","Studio · real photo"),
      (f"{P}/dgx-spark-front.png","Spark · render"),
      (f"{REF}/dgx-spark-front-ref.jpg","Spark · real photo")],
     2, 720, "SETTLEMENT · render beside the real reference", f"{OUT}/2_settlement_pairs.png")

# 3 · detail + raking evidence (foam geometry, bevel edge, pill relief)
grid([(f"{RAW}/dgx-spark-detail.png","Spark bezel+foam · detail crop"),
      ("render/calib/spark-raking.png","Raking light · concave pill relief"),
      ("render/foamgeo-dethread.png","Foam · de-threaded cells"),
      ("render/material-bevel-edge.png","Bevel edge · T7 hairline")],
     2, 720, "MICROREALISM EVIDENCE · foam · relief · bevel", f"{OUT}/3_detail_raking.png")

# 4 · loop-history strip (spark front across the waves, if present)
hist = [(f"{EV}/wave3-spark-front.png","early wave"),
        (f"{EV}/spark_front_compare.png","mid settle"),
        (f"{P}/dgx-spark-front.png","final (post)")]
grid([(p,c) for p,c in hist], 3, 500, "LOOP HISTORY · Spark front across the waves", f"{OUT}/4_loop_history.png")
print("collages done")
