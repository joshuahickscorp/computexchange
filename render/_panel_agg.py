#!/usr/bin/env python3
# render/_panel_agg.py · aggregate one forensic panel loop. Maps neutral img names -> MINE/REAL via
# the private key, then reports per-frame render-call counts, named tells, and the CLEAN verdict.
# A MINE gate frame FAILS the loop if >=2 of 5 agents call it CG_RENDER, or one tell is named by
# >=2 agents. Panel CLEAN = no gate frame fails. Calibrated against the real-photo controls (if the
# reals draw the same render-calls, the agents are trigger-happy, not seeing a true tell).
#   python3 render/_panel_agg.py render/panel/loopN
import sys, os, json, re
from collections import defaultdict, Counter

D = sys.argv[1]
key = {}
for line in open(f"{D}/_KEY.txt"):
    line = line.strip()
    if not line or line.startswith("#"): continue
    name, kind, label = line.split("\t")
    key[name] = (kind, label)

verd = json.load(open(f"{D}/_verdicts.json"))
panel = verd["panel"]; nag = len(panel)

# per image: list of (lens, call, conf, tells)
byimg = defaultdict(list)
for p in panel:
    for v in p["verdicts"]:
        byimg[v["image"]].append((p["lens"], v["call"], v.get("confidence", 0), v.get("tells", [])))

GATE = {"mine:studio-front", "mine:spark-front", "mine:pair", "mine:spark-detail"}

def tell_key(t):
    t = t.lower()
    for kw in ["foam", "pore", "reflect", "bevel", "edge", "roughness", "grain", "noise", "dof",
               "focus", "bloom", "vignette", "aberration", "uniform", "periodic", "repeat", "clean",
               "sharp", "perfect", "smudge", "dust", "shadow", "contact", "ground", "coplanar",
               "symmetry", "port", "seam", "specular", "highlight", "plastic", "matte", "texture"]:
        if kw in t: return kw
    return re.sub(r"[^a-z ]", "", t)[:24].strip()

print(f"== PANEL {os.path.basename(D)} · {nag} agents ==\n")
print(f"{'image':10} {'kind':4} {'label':20} {'RENDER-calls':12} {'avg_conf':8}")
mine_rate = []; real_rate = []
gate_fail = []
for name in sorted(byimg):
    kind, label = key.get(name, ("?", name))
    rows = byimg[name]
    rcalls = sum(1 for _, c, _, _ in rows if c == "CG_RENDER")
    photc = [cf for _, c, cf, _ in rows if c == "PHOTOGRAPH"]
    rendc = [cf for _, c, cf, _ in rows if c == "CG_RENDER"]
    avgc = round(sum(cf for _, _, cf, _ in rows)/max(1, len(rows)))
    flag = ""
    (mine_rate if kind == "MINE" else real_rate).append(rcalls/nag)
    # tells named against this frame (only for CG_RENDER calls)
    tc = Counter()
    for _, c, _, tells in rows:
        if c == "CG_RENDER":
            for t in tells: tc[tell_key(t)] += 1
    hot = [(k, n) for k, n in tc.items() if n >= 2]
    if label in GATE and (rcalls >= 2 or hot):
        flag = "  <-- GATE FAIL"; gate_fail.append((label, rcalls, hot))
    print(f"{name:10} {kind:4} {label:20} {rcalls}/{nag:<10} {avgc:<8}{flag}")
    if hot:
        print(f"           tells >=2: " + "; ".join(f"{k}({n})" for k, n in hot))

mr = sum(mine_rate)/max(1, len(mine_rate)); rr = sum(real_rate)/max(1, len(real_rate))
print(f"\nMINE render-call rate: {mr:.2f}   REAL(control) render-call rate: {rr:.2f}")
clean = not gate_fail
print(f"\nGATE FRAMES: {'ALL CLEAN' if clean else 'FAILURES: ' + str(gate_fail)}")
print(f"PANEL VERDICT: {'CLEAN' if clean else 'NOT CLEAN'}")

# machine-readable summary for the loop driver
json.dump({"loop": verd.get("loop"), "clean": clean, "gate_fail": gate_fail,
           "mine_rate": mr, "real_rate": rr}, open(f"{D}/_summary.json", "w"), indent=2)
