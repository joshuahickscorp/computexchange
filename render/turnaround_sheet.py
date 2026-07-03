#!/usr/bin/env python3
# render/turnaround_sheet.py · Gate 1 · assemble the 4-angle wireframe+shaded turnaround into
# one sheet with a feature -> MEASUREMENTS.md legend. Every geometry value in the model traces
# to a row; the legend makes that mapping explicit next to the turntable.
#
#   python3 render/turnaround_sheet.py studio

import os, sys
import numpy as np
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.abspath(__file__))
BG = (26, 26, 30)

LEGEND = {
    "studio": [
        ("width x depth x height", "front_width_anchor / front_height_spec", "197 x 197 x 95 mm (spec)"),
        ("footprint / vertical-edge R", "plan_corner_R", "31.4 mm"),
        ("top-edge fillet (tight)", "top_edge_fillet_R", "8.27 mm (rendered 8.2)"),
        ("intake band (perforated)", "intake_band_height", "8.55 mm, bottom fillet"),
        ("USB-C ports x2 (VERTICAL)", "usbc_long/short_axis", "2.62 x 8.47 mm, recessed + tongue"),
        ("USB-C positions", "usbc_left/right_x_from_center", "-66.2 / -51.4 mm"),
        ("SD slot (horizontal)", "sd_slot_width/height", "26.85 x 2.50 mm at -24.4"),
        ("power LED (no emission)", "led_x_from_center / led_z", "+87.7 mm, 27.5 above base"),
        ("port row height", "port_row_center_from_base", "24.4 mm"),
        ("base reveal gap", "base_reveal_gap", "2.5 mm INFERRED (phase-4 tabletop)"),
    ],
}

def load_on_bg(path, bg):
    im = Image.open(path).convert("RGBA")
    c = Image.new("RGBA", im.size, (*bg, 255)); c.alpha_composite(im)
    return c.convert("RGB")

def build(device):
    name = "mac-studio" if device == "studio" else "dgx-spark"
    tags = [("front", "front · 0 deg"), ("q34", "three-quarter · 40 deg"),
            ("side", "side · 90 deg"), ("rear34", "rear 3/4 · 140 deg")]
    tiles = []
    for tag, label in tags:
        p = os.path.join(ROOT, f"verify/turn-{name}-{tag}.png")
        im = load_on_bg(p, BG)
        d = ImageDraw.Draw(im); d.rectangle([0, 0, im.width, 26], fill=(16, 16, 20))
        d.text((8, 7), f"{device} · wireframe+shaded · {label}", fill=(235, 235, 240))
        tiles.append(np.asarray(im))
    tw, th = tiles[0].shape[1], tiles[0].shape[0]
    grid = np.full((th*2 + 6, tw*2 + 6, 3), BG, np.uint8)
    for i, t in enumerate(tiles):
        r, c = divmod(i, 2)
        grid[r*(th+6):r*(th+6)+th, c*(tw+6):c*(tw+6)+tw] = t
    gridim = Image.fromarray(grid)

    # legend strip
    rows = LEGEND[device]
    lw = gridim.width; lh = 34 + 20 * len(rows)
    leg = Image.new("RGB", (lw, lh), (16, 16, 20)); d = ImageDraw.Draw(leg)
    d.text((10, 8), "GATE 1 · every geometry value traces to a MEASUREMENTS.md row", fill=(255, 255, 255))
    y = 30
    for feat, row, val in rows:
        d.text((14, y), feat, fill=(210, 214, 220))
        d.text((330, y), row, fill=(150, 200, 250))
        d.text((690, y), val, fill=(190, 190, 195))
        y += 20

    sheet = Image.new("RGB", (gridim.width, gridim.height + leg.height + 6), BG)
    sheet.paste(gridim, (0, 0)); sheet.paste(leg, (0, gridim.height + 6))
    outp = os.path.join(ROOT, f"verify/gate1-{name}.png")
    sheet.save(outp)
    print("wrote", os.path.relpath(outp, ROOT), sheet.size)

if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "studio")
