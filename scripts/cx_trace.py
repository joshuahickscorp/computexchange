# Trace the Cx mark into two contour polygons (C body, x glyph) for the Blender metal
# render. No potrace/inkscape here, so: key the white mark off the black ground, flood-fill
# the C (the largest connected shape), derive the x as the remainder, Moore-trace each
# boundary, decimate, and VERIFY by re-rasterising (IoU) before trusting the result.
# Output: /tmp/cx_paths.json with normalized, Y-up coords centered on origin for Blender.
import json
from collections import deque
import numpy as np
from PIL import Image, ImageDraw

SRC = "logo/cx_logo.png"
N = 600  # working resolution for tracing


def load_mask():
    im = Image.open(SRC).convert("RGB").resize((N, N), Image.LANCZOS)
    a = np.asarray(im).astype(np.float32)
    lum = 0.2126 * a[:, :, 0] + 0.7152 * a[:, :, 1] + 0.0722 * a[:, :, 2]
    return lum > 128


def flood(mask, seed):
    H, W = mask.shape
    out = np.zeros_like(mask)
    q = deque([seed]); out[seed] = True
    while q:
        y, x = q.popleft()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < H and 0 <= nx < W and mask[ny, nx] and not out[ny, nx]:
                out[ny, nx] = True; q.append((ny, nx))
    return out


def moore_trace(mask):
    P = np.zeros((mask.shape[0] + 2, mask.shape[1] + 2), bool)
    P[1:-1, 1:-1] = mask
    ys, xs = np.where(P)
    start = (int(ys[0]), int(xs[0]))           # topmost then leftmost boundary pixel
    dirs = [(0, -1), (-1, -1), (-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1)]  # W,NW,N,NE,E,SE,S,SW

    def nb(c, k):
        return (c[0] + dirs[k][0], c[1] + dirs[k][1])

    boundary = [start]
    cur = start
    back = 0                                    # entered from West (start is leftmost in its row)
    second = None
    for _ in range(8 * mask.sum() + 16):
        found = None
        for k in range(8):
            idx = (back + 1 + k) % 8
            ny, nx = nb(cur, idx)
            if P[ny, nx]:
                found = idx; nxt = (ny, nx); break
        if found is None:
            break
        back = dirs.index((cur[0] - nxt[0], cur[1] - nxt[1]))
        cur = nxt
        if second is None:
            second = cur
        if cur == start and len(boundary) > 2 and boundary[1] == second:
            break
        boundary.append(cur)
    return [(x - 1, y - 1) for (y, x) in boundary]


def rdp(pts, eps):
    if len(pts) < 3:
        return pts
    a = np.array(pts, float)

    def _rec(s, e):
        if e <= s + 1:
            return [s]
        p0, p1 = a[s], a[e]
        d = p1 - p0; L = np.hypot(*d) or 1.0
        dist = np.abs((a[s + 1:e, 0] - p0[0]) * d[1] - (a[s + 1:e, 1] - p0[1]) * d[0]) / L
        i = int(np.argmax(dist)) + s + 1
        if dist.max() > eps:
            return _rec(s, i) + _rec(i, e)
        return [s]
    keep = _rec(0, len(a) - 1) + [len(a) - 1]
    return [pts[i] for i in sorted(set(keep))]


def iou(poly, ref):
    img = Image.new("L", (N, N), 0)
    ImageDraw.Draw(img).polygon([(x, y) for x, y in poly], fill=255)
    p = np.asarray(img) > 128
    return (p & ref).sum() / float((p | ref).sum())


mask = load_mask()
ys, xs = np.where(mask)
left_seed = (int(ys[np.argmin(xs)]), int(xs.min()))   # a pixel on the C's left edge
C = flood(mask, left_seed)
X = mask & ~C
print("C px:", int(C.sum()), " x px:", int(X.sum()))

out = {}
checkimg = Image.new("RGBA", (N, N), (0, 0, 0, 255))
draw = ImageDraw.Draw(checkimg)
for name, m, col in (("C", C, (255, 255, 255, 255)), ("x", X, (90, 200, 255, 255))):
    raw = moore_trace(m)
    dec = rdp(raw, 1.2)
    score = iou(dec, m)
    print(f"{name}: raw {len(raw)} -> {len(dec)} pts, IoU {score:.3f}")
    draw.polygon([(x, y) for x, y in dec], fill=col)
    # normalize: center on origin, Y-up, fit in a 2.0 box keyed to the WHOLE mark
    out[name] = dec
checkimg.save("/tmp/cx_trace_check.png")

# shared normalization (both shapes in the mark's common frame)
allpts = np.array(out["C"] + out["x"], float)
mn = allpts.min(0); mx = allpts.max(0)
ctr = (mn + mx) / 2.0
scale = 2.0 / max(mx - mn)
norm = {k: [[(px - ctr[0]) * scale, -(py - ctr[1]) * scale] for px, py in v] for k, v in out.items()}
json.dump({"C": norm["C"], "x": norm["x"]}, open("/tmp/cx_paths.json", "w"))
print("wrote /tmp/cx_paths.json ; check /tmp/cx_trace_check.png")
