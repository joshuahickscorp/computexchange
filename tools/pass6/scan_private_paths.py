#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, re
from pathlib import Path

ap=argparse.ArgumentParser(); ap.add_argument('--root',default='.'); ap.add_argument('--output',default='review/pass6/evidence/package/private_path_scan.json')
a=ap.parse_args(); root=Path(a.root).resolve(); hits=[]
patterns=[re.compile(r'/Users/[^/\s]+/'),re.compile(r'/home/[^/\s]+/'),re.compile(r'[A-Za-z]:\\Users\\[^\\\s]+\\')]
binary={'.blend','.glb','.gltf','.png','.jpg','.jpeg','.exr','.zip','.pyc'}
for p in root.rglob('*'):
    if not p.is_file() or p.suffix.lower() in binary or any(x in p.parts for x in ('.git','.pass6-worktrees','pass6_install_backups','.venv','.venv-pass6','__pycache__','.pytest_cache')): continue
    text=p.read_text(encoding='utf-8',errors='ignore')
    for i,line in enumerate(text.splitlines(),1):
        if any(rx.search(line) for rx in patterns): hits.append({'path':str(p.relative_to(root)),'line':i,'excerpt':line[:300]})
out=root/a.output; out.parent.mkdir(parents=True,exist_ok=True)
data={'pass':not hits,'hit_count':len(hits),'hits':hits}; out.write_text(json.dumps(data,indent=2)+'\n')
print(json.dumps(data,indent=2)); raise SystemExit(0 if not hits else 2)
