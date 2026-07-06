#!/usr/bin/env python3
# render/_panel_save.py · extract the panel workflow result JSON from a task-output file and write
# render/panel/loopN/_verdicts.json (tolerant to truncation of the last lens block).
#   python3 render/_panel_save.py <task_output_file> <loop_number>
import sys, json, re

raw = open(sys.argv[1]).read()
loop = int(sys.argv[2])

def balanced_from(s, start):
    d = 0
    for i in range(start, len(s)):
        c = s[i]
        if c == '{': d += 1
        elif c == '}':
            d -= 1
            if d == 0: return s[start:i+1]
    return None

data = None
try:
    top = json.loads(raw)
    res = top.get("result", top)
    data = {"loop": res.get("loop", loop), "panel": res["panel"]}
    print("clean parse")
except Exception as e:
    print("tolerant parse:", e)
    data = {"loop": loop, "panel": []}
    for m in re.finditer(r'\{\s*"lens"\s*:\s*"(\w+)"', raw):
        block = balanced_from(raw, m.start())
        if block:
            try:
                data["panel"].append(json.loads(block)); continue
            except Exception:
                pass
        lens = m.group(1); verds = []
        tail = raw[m.start():]
        for vm in re.finditer(r'\{\s*"image"\s*:\s*"(img_\d+\.png)"', tail):
            vb = balanced_from(tail, vm.start())
            if vb:
                try: verds.append(json.loads(vb))
                except Exception: pass
            if vm.group(1) == "img_13.png": break
        data["panel"].append({"lens": lens, "verdicts": verds})

out = f"render/panel/loop{loop}/_verdicts.json"
json.dump(data, open(out, "w"), indent=1)
print("saved", out, ":", sum(len(p["verdicts"]) for p in data["panel"]), "verdicts across",
      len(data["panel"]), "lenses")
