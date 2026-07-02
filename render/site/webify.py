# webify.py · turn the 16-bit render masters into the served 8-bit set: the @3x
# in place plus @2x and @1x downsizes (LANCZOS, never hand-resized). Run from the
# repo root after render/site/oracles.py; part of `make render-oracles`.
import os
from PIL import Image

DIR = "web/assets/site"
for stem in ("oracles-pair", "mac-studio", "dgx-spark"):
    src = f"{DIR}/{stem}@3x.png"
    if not os.path.exists(src):
        raise SystemExit(f"missing {src} (run the render first)")
    im = Image.open(src).convert("RGBA")  # 8-bit from here on
    im.save(src, optimize=True)
    for n in (2, 1):
        w = im.width * n // 3
        h = im.height * n // 3
        im.resize((w, h), Image.LANCZOS).save(f"{DIR}/{stem}@{n}x.png", optimize=True)
    sizes = {n: os.path.getsize(f"{DIR}/{stem}@{n}x.png") // 1024 for n in (1, 2, 3)}
    print(f"{stem}: @1x {sizes[1]}KB · @2x {sizes[2]}KB · @3x {sizes[3]}KB")
