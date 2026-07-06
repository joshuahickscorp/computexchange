#!/usr/bin/env python3
# _grain.py · add subtle luminance-scaled film grain to a rendered PNG (in place). The panel named
# "no grain" as a render signature · real sensors add grain, heavier in the shadows. A finishing
# step, applied AFTER the numeric gate (the gate reads the raw frame).
#   python3 render/_grain.py <png> [strength=2.6] [seed=7]
import sys
import numpy as np
from PIL import Image

path = sys.argv[1]
strength = float(sys.argv[2]) if len(sys.argv) > 2 else 2.6   # on the 0-255 scale (subtle)
seed = int(sys.argv[3]) if len(sys.argv) > 3 else 7
rng = np.random.default_rng(seed)

im = np.asarray(Image.open(path).convert("RGB"), float)
lum = im.mean(2, keepdims=True) / 255.0
# monochrome grain (luminance noise · like real film/sensor) · heavier in shadows, light in highlights
mono = rng.standard_normal(im.shape[:2] + (1,))
scale = strength * (0.55 + 0.9 * (1.0 - lum))
out = np.clip(im + mono * scale, 0, 255).astype(np.uint8)
Image.fromarray(out).save(path)
print(f"grain applied · {path} · strength {strength} seed {seed}")
