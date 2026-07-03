#!/usr/bin/env python3
# render/_checkpoint_export.py · assemble the grader-upload checkpoint folder: ONE report md + a rich
# set of per-angle hero frames and collages covering every angle, capped under 20 attachments.
# JPG q90 at 2000px long edge keeps each attachment upload-friendly.
import os, glob
from PIL import Image, ImageDraw, ImageFont

OUT = os.path.expanduser("~/Downloads/cx-oracles-checkpoint-2026-07-03")
os.makedirs(OUT, exist_ok=True)
P = "render/portraits"; REF = "render/ref"; EV = "render/measure_evidence"; CAL = "render/calib"
BG = (8, 8, 9); FG = (205, 205, 210); SUB = (150, 150, 156)

def font(sz):
    for p in ["/System/Library/Fonts/SFNSMono.ttf", "/System/Library/Fonts/Menlo.ttc", "/Library/Fonts/Arial.ttf"]:
        if os.path.exists(p):
            try: return ImageFont.truetype(p, sz)
            except Exception: pass
    return ImageFont.load_default()

def load(path, w=None):
    if not os.path.exists(path): return None
    im = Image.open(path).convert("RGB")
    if w: im = im.resize((w, round(im.size[1]*w/im.size[0])), Image.LANCZOS)
    return im

def save_jpg(im, name, q=90):
    im.save(os.path.join(OUT, name), "JPEG", quality=q)
    print("->", name, im.size)

def hero(src, name, longedge=2000):
    im = load(src)
    if not im: print("MISS", src); return
    s = longedge / max(im.size)
    if s < 1.0: im = im.resize((round(im.size[0]*s), round(im.size[1]*s)), Image.LANCZOS)
    save_jpg(im, name)

def grid(tiles, cols, cellw, title, name, pad=18, lab=17, q=90):
    tiles = [(load(p, cellw), cap) for p, cap in tiles]
    tiles = [(im, cap) for im, cap in tiles if im]
    if not tiles: print("skip", name); return
    rows = (len(tiles)+cols-1)//cols
    rh = [0]*rows
    for i, (im, _) in enumerate(tiles): rh[i//cols] = max(rh[i//cols], im.size[1])
    W = pad + cols*(cellw+pad); H = 58 + sum(rh) + rows*(lab+pad) + pad
    cv = Image.new("RGB", (W, H), BG); d = ImageDraw.Draw(cv)
    d.text((pad, 20), title, FG, font=font(26))
    y = 58
    for r in range(rows):
        x = pad
        for c in range(cols):
            i = r*cols+c
            if i >= len(tiles): break
            im, cap = tiles[i]; cv.paste(im, (x, y))
            d.text((x, y+im.size[1]+4), cap, SUB, font=font(lab)); x += cellw+pad
        y += rh[r]+lab+pad
    save_jpg(cv, name, q)

# ---- report ----
import shutil
shutil.copy("render/PHOTOREAL-SHIFT-REPORT.md", os.path.join(OUT, "00_REPORT.md"))
print("-> 00_REPORT.md")

# ---- 10 per-angle hero frames (all angles) ----
ANGLES = [
    ("mac-studio-front", "01_studio-front"), ("mac-studio-q34", "02_studio-q34"),
    ("mac-studio-side", "03_studio-side"), ("mac-studio-detail", "04_studio-detail"),
    ("dgx-spark-front", "05_spark-front"), ("dgx-spark-q34", "06_spark-q34"),
    ("dgx-spark-side", "07_spark-side"), ("dgx-spark-top", "08_spark-top"),
    ("dgx-spark-detail", "09_spark-detail"), ("oracles-pair@3x", "10_pair"),
]
for src, nm in ANGLES:
    hero(f"{P}/{src}.png", f"{nm}.jpg")

# ---- 11 · all-angles contact sheet ----
grid([(f"{P}/mac-studio-front.png", "Studio front"), (f"{P}/mac-studio-q34.png", "Studio 3/4"),
      (f"{P}/mac-studio-side.png", "Studio side"), (f"{P}/dgx-spark-front.png", "Spark front"),
      (f"{P}/dgx-spark-q34.png", "Spark 3/4"), (f"{P}/dgx-spark-side.png", "Spark side"),
      (f"{P}/dgx-spark-top.png", "Spark top"), (f"{P}/oracles-pair@3x.png", "Pair"),
      (f"{P}/mac-studio-detail.png", "Studio detail"), (f"{P}/dgx-spark-detail.png", "Spark detail")],
     5, 560, "CX ORACLES · ALL ANGLES (final · post)", "11_all-angles.jpg")

# ---- 12 · settlement · render beside the real reference ----
grid([(f"{P}/mac-studio-front.png", "Studio · render"), (f"{REF}/mac-studio-front-ref.jpg", "Studio · real photo"),
      (f"{P}/dgx-spark-front.png", "Spark · render"), (f"{REF}/dgx-spark-front-ref.jpg", "Spark · real photo")],
     2, 720, "SETTLEMENT · render beside the real reference", "12_settlement-vs-real.jpg")

# ---- 13 · microrealism evidence ----
grid([(f"{P}/dgx-spark-detail.png", "Foam macro · torn/merged open cells"),
      (f"{EV}/commit1-raking.png", "Raking light · concave pill relief"),
      (f"{EV}/foamgeo-dethread.png", "Foam · de-threaded cells"),
      (f"{EV}/material-bevel-edge.png", "Bevel edge · hairline (T7)")],
     2, 720, "MICROREALISM · foam · relief · bevel", "13_microrealism.jpg")

# ---- 14 · reflection · readable-edge softbox (grader T5) ----
grid([(f"{P}/mac-studio-q34.png", "Studio top · softbox reflection (readable edge)"),
      (f"{P}/mac-studio-front.png", "Studio front · reflection gradient"),
      (f"{P}/dgx-spark-q34.png", "Spark · top vent + softbox"),
      (f"{P}/oracles-pair@3x.png", "Pair · consistent room in both metals")],
     2, 720, "T5 REFLECTIONS · readable-edge softbox (Apple dark-hero)", "14_reflection-edge.jpg")

# ---- 15 · loop history · Spark front across the run ----
grid([(f"{EV}/wave3-spark-front.png", "early wave"),
      (f"{EV}/spark_front_compare.png", "mid settle"),
      (f"{P}/dgx-spark-front.png", "final (post · L8)")],
     3, 520, "LOOP HISTORY · Spark front (foam de-tile + depth)", "15_loop-history.jpg")

print("\n== checkpoint export complete ==")
print("folder:", OUT)
for f in sorted(os.listdir(OUT)):
    sz = os.path.getsize(os.path.join(OUT, f)) / 1e6
    print(f"  {f}  ({sz:.1f} MB)")
print("attachment count:", len(os.listdir(OUT)))
