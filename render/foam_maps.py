#!/usr/bin/env python3
# foam_maps.py · generate the DGX Spark open-cell-foam PBR maps for the real-time
# hero (the Cycles still uses true Voronoi displacement; the glb uses these baked
# maps on a flat plane so the glb stays tiny). Deterministic, numpy only, no Blender.
# Same F2-F1 Voronoi field the Cycles shader uses (build_scene.py foam_field).
#
#   python3 render/foam_maps.py            # writes web/assets/site/tex/foam_{normal,rough,ao}.png
#
# Output: 1024px tiling maps (1K is the real-time budget per docs; 2K if needed).
import numpy as np
from PIL import Image
import os

SIZE = 512
CELLS = 18          # coarse Voronoi cells across the tile (finer, denser foam)
CELLS_FINE = 32     # the overlapping second scale, per the reproduction checklist
SEED = 7            # fixed so the maps are reproducible
DEPTH = 1.0         # height contrast of the pores
OUT = "web/assets/site/tex"


def voronoi_f1f2(size, cells, seed):
    """Tiling Voronoi: for each pixel, distance to nearest (F1) and 2nd (F2) seed.
    F2-F1 is high on the ridge web between cells, ~0 at pore centers · the open-cell
    metal-foam signature."""
    rng = np.random.default_rng(seed)
    # seed points on a toroidal grid so the tile wraps seamlessly
    pts = (np.stack(np.meshgrid(np.arange(cells), np.arange(cells)), -1)
           .reshape(-1, 2).astype(np.float64))
    pts += rng.uniform(0.08, 0.92, pts.shape)  # jitter within each cell
    ys, xs = np.mgrid[0:size, 0:size].astype(np.float64)
    px = np.stack([xs / size * cells, ys / size * cells], -1)  # (size,size,2)
    f1 = np.full((size, size), 1e9)
    f2 = np.full((size, size), 1e9)
    for oy in (-cells, 0, cells):          # wrap the 8 neighbours for seamless tiling
        for ox in (-cells, 0, cells):
            for p in pts:
                d = np.hypot(px[..., 0] - (p[0] + ox), px[..., 1] - (p[1] + oy))
                closer = d < f1
                f2 = np.where(closer, f1, np.minimum(f2, d))
                f1 = np.where(closer, d, f1)
    return f1, f2


def main():
    os.makedirs(OUT, exist_ok=True)
    f1, f2 = voronoi_f1f2(SIZE, CELLS, SEED)
    web = f2 - f1                              # coarse ridge network
    web = (web - web.min()) / (web.max() - web.min() + 1e-9)
    g1, g2 = voronoi_f1f2(SIZE, CELLS_FINE, SEED + 1)   # fine sub-structure
    fine = g2 - g1
    fine = (fine - fine.min()) / (fine.max() - fine.min() + 1e-9)
    web = np.clip(web + 0.4 * fine, 0, 1)      # two overlapping scales
    # height: ridges high (bright web), pore floors low (dark cavities)
    height = np.clip(web ** 0.8, 0, 1)

    # AO: pore floors occluded · a soft power of height
    ao = np.clip(0.25 + 0.75 * height ** 1.4, 0, 1)

    # roughness: floors rougher than the polished web ridges
    rough = np.clip(0.42 + 0.30 * (1.0 - height), 0, 1)

    # normal from height gradient (tangent-space, +Z out)
    gy, gx = np.gradient(height * DEPTH * 6.0)
    nz = np.ones_like(height)
    nl = np.sqrt(gx * gx + gy * gy + nz * nz)
    nx, ny, nz = -gx / nl, -gy / nl, nz / nl
    normal = np.stack([(nx * 0.5 + 0.5), (ny * 0.5 + 0.5), (nz * 0.5 + 0.5)], -1)

    Image.fromarray((normal * 255).astype(np.uint8), "RGB").save(f"{OUT}/foam-normal.png")
    Image.fromarray((rough * 255).astype(np.uint8), "L").save(f"{OUT}/foam-rough.png")
    Image.fromarray((ao * 255).astype(np.uint8), "L").save(f"{OUT}/foam-ao.png")
    print(f"foam maps written to {OUT}/ (normal, rough, ao · {SIZE}px, {CELLS} cells)")


if __name__ == "__main__":
    main()
