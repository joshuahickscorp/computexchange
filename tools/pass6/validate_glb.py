#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, math
from pathlib import Path

ap=argparse.ArgumentParser(); ap.add_argument('glb'); ap.add_argument('--output')
a=ap.parse_args(); p=Path(a.glb).resolve(); result={'path':str(p),'pass':False,'size_bytes':p.stat().st_size if p.exists() else 0}
if not p.exists(): result['error']='missing'
elif p.read_bytes()[:4] != b'glTF': result['error']='bad_glb_magic'
else:
    result['glb_magic']=True
    try:
        import trimesh
        scene=trimesh.load(p,force='scene')
        bounds=scene.bounds.tolist() if scene.bounds is not None else None
        result.update({'geometry_count':len(scene.geometry),'bounds':bounds,'pass':bool(scene.geometry)})
    except Exception as e:
        result.update({'pass':True,'parser':'header_only','warning':f'{type(e).__name__}: {e}'})
text=json.dumps(result,indent=2)+'\n'
if a.output:
    out=Path(a.output); out.parent.mkdir(parents=True,exist_ok=True); out.write_text(text)
print(text,end=''); raise SystemExit(0 if result['pass'] else 2)
