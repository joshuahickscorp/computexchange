#!/usr/bin/env python3
# render/post_chain.py · photoreal POST (T4 image-formation + T9 roll). ONE chain, applied
# identically to every frame AFTER the gated raw render. Order: roll · chromatic aberration ·
# bloom · vignette · grain · trim. The gate stays pre-post; this is the glass+sensor layer.
#   python3 render/post_chain.py in.png out.png [roll_deg]
# Parameters (documented, edge-of-notice):
#   roll 0.3deg · CA R +0.18% / B -0.18% radial · bloom thr 0.88 blur 7 str 0.30 ·
#   vignette corner ~0.80 · grain sigma 0.008 luminance, deterministic per output name.
import sys
import numpy as np
from PIL import Image, ImageFilter

PARAMS = dict(roll=0.3, ca_r=1.0018, ca_b=0.9982, bloom_thr=0.88, bloom_blur=7,
              bloom_str=0.30, vig_amp=0.10, vig_floor=0.87, grain=0.011)

def _scale_channel(ch, factor):
    w, h = ch.size
    s = ch.resize((max(1, round(w*factor)), max(1, round(h*factor))), Image.BICUBIC)
    sw, sh = s.size
    if factor >= 1.0:
        left, top = (sw-w)//2, (sh-h)//2
        return s.crop((left, top, left+w, top+h))
    bg = Image.new("L", (w, h), 0); bg.paste(s, ((w-sw)//2, (h-sh)//2)); return bg

def post(inp, outp, roll=None, p=PARAMS):
    roll = p["roll"] if roll is None else roll
    im = Image.open(inp).convert("RGB")
    if roll:                                            # T9 · sub-degree camera roll
        im = im.rotate(roll, resample=Image.BICUBIC, expand=False)
    r, g, b = im.split()                                # T4 · radial chromatic aberration
    im = Image.merge("RGB", (_scale_channel(r, p["ca_r"]), g, _scale_channel(b, p["ca_b"])))
    a = np.asarray(im, np.float64)/255.0
    h, w = a.shape[:2]
    lum = a @ np.array([0.2126, 0.7152, 0.0722])        # T4 · specular bloom past geometric bounds
    mask = np.clip((lum - p["bloom_thr"])/(1.0 - p["bloom_thr"]), 0, 1)[..., None]
    bl = np.asarray(Image.fromarray((np.clip(mask*a, 0, 1)*255).astype("uint8"))
                    .filter(ImageFilter.GaussianBlur(p["bloom_blur"])), np.float64)/255.0
    a = a + bl*p["bloom_str"]
    yy, xx = np.mgrid[0:h, 0:w]; cx, cy = w/2.0, h/2.0   # T4 · gentle vignette
    rr = np.sqrt(((xx-cx)/cx)**2 + ((yy-cy)/cy)**2)
    vig = np.clip(1.0 - p["vig_amp"]*np.clip(rr-0.35, 0, None)**2, p["vig_floor"], 1.0)[..., None]
    a = a*vig
    rng = np.random.default_rng(abs(hash(outp)) % (2**31))   # T4 · fine luminance grain, per-frame constant
    a = a + rng.normal(0.0, p["grain"], (h, w, 1))
    Image.fromarray((np.clip(a, 0, 1)*255 + 0.5).astype("uint8")).save(outp)
    return outp

if __name__ == "__main__":
    inp, outp = sys.argv[1], sys.argv[2]
    roll = float(sys.argv[3]) if len(sys.argv) > 3 else None
    post(inp, outp, roll)
    print("post ->", outp)
