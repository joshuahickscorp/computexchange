#!/usr/bin/env python3
# render/_panel_build.py · assemble one FORENSIC PANEL set. Copies my post gate frames MIXED with
# real-photo controls (the actual hardware) into render/panel/loopN/ under NEUTRAL shuffled names,
# so a cold vision agent cannot infer photo-vs-render from the filename. Writes a private KEY.
#   python3 render/_panel_build.py <loop_number>
import sys, os, shutil
from PIL import Image

LOOP = int(sys.argv[1]) if len(sys.argv) > 1 else 1
OUT = f"render/panel/loop{LOOP}"
os.makedirs(OUT, exist_ok=True)
for f in os.listdir(OUT):
    os.remove(os.path.join(OUT, f))

# ('label', path, is_mine) · MINE = my post frames (the ones under test); REAL = actual photographs.
MINE = "render/portraits"
items = [
    ("mine:studio-front", f"{MINE}/mac-studio-front.png", True),
    ("mine:spark-front",  f"{MINE}/dgx-spark-front.png",  True),
    ("mine:pair",         f"{MINE}/oracles-pair@3x.png",          True),
    ("mine:spark-detail", f"{MINE}/dgx-spark-detail.png", True),
    ("mine:studio-q34",   f"{MINE}/mac-studio-q34.png",   True),
    ("mine:spark-q34",    f"{MINE}/dgx-spark-q34.png",    True),
    # real controls · Spark
    ("real:spark-sth2",   "render/ref/dgx-spark/sth_front-2.jpg",      False),
    ("real:spark-foam",   "render/ref/dgx-spark/cl_front-foam.jpg",    False),
    ("real:spark-srv",    "render/ref/dgx-spark/storagereview_front.jpg", False),
    ("real:spark-side",   "render/ref/dgx-spark/cl_side-matte.jpg",    False),
    # real controls · Studio
    ("real:studio-apple", "render/ref/mac-studio/apple_front.jpg",     False),
    ("real:studio-wiki",  "render/ref/mac-studio/wikimedia_front.jpg", False),
    ("real:studio-3q",    "render/ref/mac-studio/apple_lifestyle_3q.jpg", False),
]
items = [(l, p, m) for (l, p, m) in items if os.path.exists(p)]

# two fixed permutations (no RNG in this env) · loop1 and loop2 shuffle differently
PERM = {1: [7, 0, 11, 3, 9, 1, 5, 12, 2, 8, 4, 10, 6],
        2: [2, 9, 4, 12, 0, 7, 10, 3, 11, 1, 8, 5, 6]}
order = PERM.get(LOOP, list(range(len(items))))
order = [i for i in order if i < len(items)]

key = []
for slot, idx in enumerate(order, 1):
    label, path, mine = items[idx]
    name = f"img_{slot:02d}.png"
    im = Image.open(path).convert("RGB")
    if max(im.size) > 1600:                       # normalize scale so size is not a tell
        s = 1600 / max(im.size)
        im = im.resize((round(im.size[0]*s), round(im.size[1]*s)), Image.LANCZOS)
    im.save(os.path.join(OUT, name))
    key.append((name, label, "MINE" if mine else "REAL"))

with open(f"{OUT}/_KEY.txt", "w") as fh:
    fh.write(f"# PANEL loop{LOOP} · private key (agents never see this)\n")
    for name, label, kind in key:
        fh.write(f"{name}\t{kind}\t{label}\n")
print(f"loop{LOOP}: {len(key)} images ->", OUT)
for name, label, kind in key:
    print(f"  {name}  {kind:4}  {label}")
