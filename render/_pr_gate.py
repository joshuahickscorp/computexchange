#!/usr/bin/env python3
# render/_pr_gate.py · THE PR-GATE (MASTER-LOOP wave 6 R6.4 · descended from FRONTIER-V2).
# Rule, fixed BEFORE any run (commit 8294af7): for EVERY MINE gate frame in a loop,
#   (a) render-call count <= mean(clean-product REAL controls' render-call counts) + 1 vote
#   (b) the frame draws no >=2-agent tell keyword that NO real control drew (>=1) in the same sitting
# Two consecutive passing loops close the photoreal criterion for the object set under test.
#   python3 render/_pr_gate.py render/panel/loopN
import sys, json
from collections import defaultdict, Counter

D = sys.argv[1]
GATE = {"mine:studio-front", "mine:spark-front", "mine:pair", "mine:spark-detail"}
CLEAN_REAL = {"real:studio-apple", "real:studio-wiki", "real:spark-sth2",
              "real:spark-foam", "real:spark-side"}   # clean-product class (studio sweeps)
# environmental class (spark-srv lab bench, studio-3q lifestyle, dark-* in-situ) informs nothing here

def tell_key(t):
    t = t.lower()
    for kw in ["foam", "pore", "reflect", "bevel", "edge", "roughness", "grain", "noise", "dof",
               "focus", "bloom", "vignette", "aberration", "uniform", "periodic", "repeat", "clean",
               "sharp", "perfect", "smudge", "dust", "shadow", "contact", "ground", "coplanar",
               "symmetry", "port", "seam", "specular", "highlight", "plastic", "matte", "texture"]:
        if kw in t: return kw
    import re
    return re.sub(r"[^a-z ]", "", t)[:24].strip()

key = {}
for line in open(f"{D}/_KEY.txt"):
    line = line.strip()
    if not line or line.startswith("#"): continue
    name, kind, label = line.split("\t")
    key[name] = (kind, label)

verd = json.load(open(f"{D}/_verdicts.json"))
panel = verd["panel"]; nag = len(panel)

byimg = defaultdict(list)
for p in panel:
    for v in p["verdicts"]:
        byimg[v["image"]].append((p["lens"], v["call"], v.get("tells", [])))

rc = {}; mine_tells = {}; real_tells = Counter()
for name, rows in byimg.items():
    kind, label = key.get(name, ("?", name))
    n_render = sum(1 for _, c, _ in rows if c == "CG_RENDER")
    rc[label] = n_render
    tc = Counter()
    for _, c, tells in rows:
        if c == "CG_RENDER":
            for t in tells: tc[tell_key(t)] += 1
    if kind == "MINE":
        mine_tells[label] = tc
    else:
        for k in tc: real_tells[k] += tc[k]   # any real control drawing the tell at all

clean_counts = [rc[l] for l in rc if l in CLEAN_REAL]
if not clean_counts:
    print("PR-GATE: no clean-product real controls in this loop · INVALID"); sys.exit(2)
thr = (sum(clean_counts) / len(clean_counts)) + 1.0

print(f"== PR-GATE {D} · {nag} agents ==")
print(f"clean-product REAL render-calls: " +
      ", ".join(f"{l.split(':')[1]}={rc[l]}" for l in sorted(rc) if l in CLEAN_REAL))
print(f"threshold = mean({[rc[l] for l in sorted(rc) if l in CLEAN_REAL]}) + 1 = {thr:.2f}\n")

ok = True
for label in sorted(GATE):
    if label not in rc:
        print(f"  {label:20} MISSING from loop"); ok = False; continue
    a = rc[label] <= thr
    uniq = [k for k, n in mine_tells.get(label, {}).items() if n >= 2 and real_tells.get(k, 0) == 0]
    b = not uniq
    verdict = "PASS" if (a and b) else "FAIL"
    if not (a and b): ok = False
    extra = "" if b else f"  unique-tells: {uniq}"
    print(f"  {label:20} render-calls {rc[label]} <= {thr:.2f}: {'yes' if a else 'NO'} · "
          f"unique >=2 tells: {'none' if b else 'YES'} -> {verdict}{extra}")

print(f"\nPR-GATE LOOP VERDICT: {'PASS' if ok else 'FAIL'}")
sys.exit(0 if ok else 1)
