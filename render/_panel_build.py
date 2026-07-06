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
    # real controls · OTHER hardware in DARK staging (L13 · fixes the staging leak, spec line 131 ·
    # so the pool spans bright AND dark and "dark == render" is no longer a valid shortcut)
    ("real:dark-cpu",     "render/ref/pool-dark/us_zwlwJKtaU7U.jpg",   False),
    ("real:dark-internals", "render/ref/pool-dark/us_qTRGISczzM8.jpg", False),
    ("real:dark-keyboard", "render/ref/pool-dark/us_1osIUArK5oA.jpg",  False),
    ("real:dark-hdd",     "render/ref/pool-dark/us_sIX4eDtak7k.jpg",   False),
]
items = [(l, p, m) for (l, p, m) in items if os.path.exists(p)]

# deterministic per-loop shuffle (robust to item count · random IS available in this plain script)
import random as _rng
_rng.seed(LOOP * 97 + 13)
order = list(range(len(items)))
_rng.shuffle(order)

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
