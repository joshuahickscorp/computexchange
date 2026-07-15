#!/usr/bin/env python3
"""Generate a genuinely-3D reticulated open-cell foam STRUT SKELETON via 3D Voronoi (SciPy).

The Voronoi edges of jittered seed points form an open reticulated strut network (like real
open-cell metal foam), NOT a 2D pattern, sphere-carved solid, or shader. Output = a verts+edges
skeleton (JSON) that build_foam_patch.py skins into tubes in Blender and clips to the patch volume.

Usage:
  gen_foam_voronoi.py <out.json> <patch_mm> <depth_mm> <pitch_mm> <jitter_frac> <seed>
"""
import sys, json
import numpy as np
from scipy.spatial import Voronoi

out, patch_mm, depth_mm, pitch_mm, jitter, seed = (
    sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4]),
    float(sys.argv[5]), int(sys.argv[6]))
rng = np.random.default_rng(seed)
# jittered grid seed points (mm), padded one pitch beyond the patch so edges near the border are real
pad = pitch_mm
nx = int((patch_mm + 2*pad)/pitch_mm) + 1
nz = int((depth_mm + 2*pad)/pitch_mm) + 1
xs = np.linspace(-pad, patch_mm+pad, nx)
zs = np.linspace(-pad, depth_mm+pad, nz)
pts = []
for x in xs:
    for y in xs:
        for z in zs:
            pts.append((x + rng.uniform(-jitter,jitter)*pitch_mm,
                        y + rng.uniform(-jitter,jitter)*pitch_mm,
                        z + rng.uniform(-jitter,jitter)*pitch_mm))
pts = np.array(pts)
vor = Voronoi(pts)
V = vor.vertices
# collect unique edges from ridge polygons; drop infinite (-1) and vertices far outside the patch
lo = np.array([-0.15*patch_mm, -0.15*patch_mm, -0.15*depth_mm])
hi = np.array([1.15*patch_mm, 1.15*patch_mm, 1.15*depth_mm])
def inside(i):
    p = V[i]; return np.all(p >= lo) and np.all(p <= hi)
edges = set()
for ridge in vor.ridge_vertices:
    r = [i for i in ridge if i != -1]
    for k in range(len(r)):
        a, b = r[k], r[(k+1) % len(r)]
        if a != b and inside(a) and inside(b):
            edges.add((min(a,b), max(a,b)))
used = sorted({i for e in edges for i in e})
remap = {old: new for new, old in enumerate(used)}
verts = [[round(float(V[i][0]),4), round(float(V[i][1]),4), round(float(V[i][2]),4)] for i in used]
elist = [[remap[a], remap[b]] for (a,b) in edges]
json.dump({"patch_mm":patch_mm,"depth_mm":depth_mm,"pitch_mm":pitch_mm,"jitter":jitter,"seed":seed,
           "verts_mm":verts,"edges":elist,"n_verts":len(verts),"n_edges":len(elist)},
          open(out,"w"))
print("FOAM SKELETON pitch=%.2f jitter=%.2f seed=%d -> %d verts %d edges" % (pitch_mm, jitter, seed, len(verts), len(elist)))
