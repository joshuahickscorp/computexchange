#!/usr/bin/env python3
# Deliverables contact sheet · composite the hero renders into one labeled overview.
import os
from PIL import Image, ImageDraw, ImageFont

RO = "/Users/scammermike/Downloads/computexchange/.claude/worktrees/rack-oracle/render/rack_previews"
MR = "/Users/scammermike/Downloads/computexchange/.claude/worktrees/model-refinement/render/previews"

# rows of (label, path)
ROWS = [
    [("SCALE TRIO · q34", f"{RO}/trio-q34.png"), ("SCALE TRIO · front", f"{RO}/trio-front.png")],
    [("6x RTX 5090 · q34", f"{RO}/gpurig-q34.png"), ("6x RTX 5090 · front", f"{RO}/gpurig-front.png")],
    [("5090 FE · front", f"{RO}/gpu-front.png"), ("5090 FE · q34", f"{RO}/gpu-q34.png"),
     ("5090 FE · rear", f"{RO}/gpu-rearq34.png"), ("5090 FE · macro", f"{RO}/gpu-macro.png")],
    [("5090 FE · side", f"{RO}/gpu-side.png"), ("5090 FE · top", f"{RO}/gpu-top.png"),
     ("DGX Spark · rear", f"{MR}/audit-spark-rearq34.png"), ("Mac Studio · rear", f"{MR}/audit-studio-rearq34.png")],
]

CELL_W, CELL_H, PAD, LBL_H = 520, 380, 14, 26
BG = (10, 10, 12)
maxcols = max(len(r) for r in ROWS)
W = maxcols * CELL_W + (maxcols + 1) * PAD
H = len(ROWS) * (CELL_H + LBL_H) + (len(ROWS) + 1) * PAD
sheet = Image.new("RGB", (W, H), BG)
draw = ImageDraw.Draw(sheet)
try:
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 18)
except Exception:
    font = ImageFont.load_default()

for ri, row in enumerate(ROWS):
    ncols = len(row)
    cw = (W - (ncols + 1) * PAD) // ncols   # spread wide rows across the full width
    y = PAD + ri * (CELL_H + LBL_H + PAD)
    for ci, (label, path) in enumerate(row):
        x = PAD + ci * (cw + PAD)
        draw.rectangle([x, y, x + cw, y + CELL_H + LBL_H], fill=(20, 20, 24))
        if os.path.exists(path):
            im = Image.open(path).convert("RGB")
            im.thumbnail((cw - 8, CELL_H - 8))
            ox = x + (cw - im.width) // 2
            oy = y + (CELL_H - im.height) // 2
            sheet.paste(im, (ox, oy))
        else:
            draw.text((x + 10, y + 10), "MISSING", fill=(200, 80, 80), font=font)
        draw.text((x + 8, y + CELL_H + 3), label, fill=(210, 210, 215), font=font)

out = f"{RO}/DELIVERABLES-CONTACT-SHEET.png"
sheet.save(out)
print("contact sheet ->", out, sheet.size)
