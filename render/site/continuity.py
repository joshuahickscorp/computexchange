# continuity.py · gate 6: a committed contact sheet placing the existing knob and
# button assets beside identical-size crops of the new oracle renders, so light
# direction and grade can be compared under the same eye. Run from the repo root
# after the finals exist; writes render/site/previews/continuity.png.
from PIL import Image, ImageDraw

TILE = 360
FIELD = (6, 6, 6, 255)

def tile(path, box=None):
    im = Image.open(path).convert("RGBA")
    if box:
        im = im.crop(box)
    im.thumbnail((TILE, TILE), Image.LANCZOS)
    t = Image.new("RGBA", (TILE, TILE), FIELD)
    t.alpha_composite(im, ((TILE - im.width) // 2, (TILE - im.height) // 2))
    return t

cells = [
    ("knob-on (ref)", tile("web/assets/knob-on@3x.png")),
    ("btn-launch (ref)", tile("web/assets/btn-launch-shell@3x.png")),
    ("pair (new)", tile("web/assets/site/oracles-pair@3x.png")),
    ("studio crown", tile("web/assets/site/mac-studio@3x.png", (300, 500, 1400, 1100))),
    ("spark foam", tile("web/assets/site/dgx-spark@3x.png", (300, 700, 1400, 1300))),
    ("knob crown (ref)", tile("web/assets/knob-on@3x.png", (60, 120, 660, 480))),
]
PAD, LABEL = 14, 26
cols, rows = 3, 2
W = cols * TILE + (cols + 1) * PAD
H = rows * (TILE + LABEL) + (rows + 1) * PAD
sheet = Image.new("RGBA", (W, H), FIELD)
d = ImageDraw.Draw(sheet)
for i, (name, t) in enumerate(cells):
    x = PAD + (i % cols) * (TILE + PAD)
    y = PAD + (i // cols) * (TILE + LABEL + PAD)
    sheet.alpha_composite(t, (x, y))
    d.text((x + 4, y + TILE + 6), name, fill=(131, 131, 139, 255))
sheet.convert("RGB").save("render/site/previews/continuity.png")
print("continuity.png written")
