#!/usr/bin/env python3
# render/settle_sheet.py · the two mandatory phase-4 settlement gates.
#
#   python3 render/settle_sheet.py spark    # pore-depth: does the displaced-plane foam hold
#                                            # web->pore contrast at grazing light? geometry-or-nothing
#   python3 render/settle_sheet.py studio   # soap-bar: does the tight top fillet survive the 3/4?
#
# Spark PASS = the render's grazing-foam web/pore L spread stays substantial (pores do not wash
# to the web tone). Studio PASS = the top edge reads as a tight fillet, not a soap-bar round.

import os, sys
import numpy as np
from PIL import Image, ImageDraw
import measure as M

ROOT = os.path.dirname(os.path.abspath(__file__))

def foam_contrast(patch):
    """web-quartile L, pore-quartile L, spread · on a foam patch."""
    L = M.srgb_to_lab(patch.astype(float) / 255.0)[..., 0].reshape(-1)
    web = float(np.mean(L[L >= np.percentile(L, 70)]))
    pore = float(np.mean(L[L <= np.percentile(L, 30)]))
    return web, pore, web - pore

def load_render(path):
    a = np.asarray(Image.open(os.path.join(ROOT, path)).convert("RGBA"))
    return a[..., :3], a[..., 3] > 128

def settle_spark():
    rgb, al = load_render("portraits/dgx-spark-q34.png")
    x0, y0, x1, y1 = M.bbox_of(al)
    # the foam front face is the lower-left band in the 3/4; sample a patch on it
    W = x1 - x0; H = y1 - y0
    fx0, fx1 = x0 + int(0.10 * W), x0 + int(0.34 * W)
    fy0, fy1 = y0 + int(0.62 * H), y0 + int(0.86 * H)
    rpatch = rgb[fy0:fy1, fx0:fx1]
    rw, rp, rspread = foam_contrast(rpatch)

    # reference 3/4 foam: nv_hero_3q (small) foam strip
    ref = M.load_rgb("ref/dgx-spark/nv_hero_3q.png")
    warm = M.pred_warm(12)(ref); solid = M.largest_cc_from_center(~M.flood_from_border(~warm))
    a0, b0, a1, b1 = M.bbox_of(solid)
    # foam front band lower-center of the device
    rp2 = ref[b0 + int(0.55 * (b1 - b0)):b0 + int(0.8 * (b1 - b0)),
              a0 + int(0.3 * (a1 - a0)):a0 + int(0.7 * (a1 - a0))]
    ew, ep, espread = foam_contrast(rp2)

    verdict = "PASS · pores hold depth" if rspread >= 14 else "FAIL · foam washing flat -> reopen geometry"
    lines = [
        ("SPARK 3/4 SETTLEMENT · pore-depth under directional grazing light", (255, 255, 255)),
        (f"  render foam:    web L {rw:.0f}  pore L {rp:.0f}  spread {rspread:.0f}", (200, 210, 220)),
        (f"  reference (nv_hero_3q): web L {ew:.0f}  pore L {ep:.0f}  spread {espread:.0f}", (180, 190, 200)),
        (f"  {verdict}", (120, 220, 130) if rspread >= 14 else (240, 90, 90)),
        ("  gate: if the grazing spread collapses toward 0 the displaced plane reads flat; geometry-or-nothing", (170, 180, 190)),
    ]
    _sheet("dgx-spark", "portraits/dgx-spark-q34.png", ref, lines, "settle-spark-q34.png",
           rpatch, rp2)
    print(f"SPARK spread render {rspread:.0f} vs ref {espread:.0f} -> {verdict}")
    return rspread

def settle_studio():
    rgb, al = load_render("portraits/mac-studio-q34.png")
    # soap-bar: fit the top-front edge radius on the 3/4 silhouette
    x0, y0, x1, y1 = M.bbox_of(al); W = x1 - x0
    f = M.fit_corner(al, "tl", frac=0.16)
    rmm = f[2] * (197.0 / W) if f else None   # rough (3/4 foreshortens; indicative)
    ref = M.load_rgb("ref/mac-studio/apple_lifestyle_3q.jpg")
    lines = [
        ("STUDIO 3/4 SETTLEMENT · soap-bar-radius check vs apple_lifestyle_3q", (255, 255, 255)),
        (f"  render top-front silhouette corner ~{rmm:.1f} mm (3/4, indicative)" if rmm else "  corner fit n/a", (200, 210, 220)),
        ("  target: a TIGHT top-edge fillet (~8mm), top dead-flat, sides near-vertical · NOT a soap-bar round", (170, 180, 190)),
        ("  visual gate: the top must read as a crisp rounded edge, not a pillow", (170, 180, 190)),
    ]
    _sheet("mac-studio", "portraits/mac-studio-q34.png", ref, lines, "settle-studio-q34.png")
    print(f"STUDIO 3/4 top-front corner ~{rmm} mm")

def _sheet(name, renpath, refimg, lines, out, rpatch=None, refpatch=None):
    ren = Image.open(os.path.join(ROOT, renpath)).convert("RGB")
    ren.thumbnail((1200, 1200))
    ref = Image.fromarray(refimg); ref.thumbnail((1200, 1200))
    W = max(ren.width, ref.width)
    th = 22 * len(lines) + 20
    H = 30 + max(ren.height, ref.height) + th + 20
    sheet = Image.new("RGB", (ren.width + ref.width + 12, max(ren.height, ref.height) + th + 40), (22, 22, 26))
    d = ImageDraw.Draw(sheet)
    d.text((6, 6), "reference 3/4", fill=(230, 230, 230)); sheet.paste(ref, (0, 26))
    d.text((ref.width + 16, 6), "render 3/4 (portrait light)", fill=(230, 230, 230)); sheet.paste(ren, (ref.width + 12, 26))
    y = max(ren.height, ref.height) + 34
    for t, c in lines:
        d.text((8, y), t, fill=c); y += 20
    if rpatch is not None:
        pr = Image.fromarray(rpatch).resize((160, 160), Image.NEAREST); sheet.paste(pr, (sheet.width - 340, y - 170))
    if refpatch is not None:
        pf = Image.fromarray(refpatch).resize((160, 160), Image.NEAREST); sheet.paste(pf, (sheet.width - 170, y - 170))
    sheet.save(os.path.join(ROOT, "measure_evidence", out))
    print("wrote measure_evidence/" + out)

if __name__ == "__main__":
    dev = sys.argv[1] if len(sys.argv) > 1 else "spark"
    (settle_spark if dev == "spark" else settle_studio)()
