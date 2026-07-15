#!/usr/bin/env python3
from __future__ import annotations
import argparse
from pathlib import Path
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps

ap=argparse.ArgumentParser(description='Create a display-only comparison board without geometric realignment.')
ap.add_argument('--reference',required=True)
ap.add_argument('--baseline')
ap.add_argument('--candidate',required=True)
ap.add_argument('--out',required=True)
ap.add_argument('--display-width',type=int,default=720)
a=ap.parse_args()
ref_native=Image.open(a.reference).convert('RGB')
cand_native=Image.open(a.candidate).convert('RGB')
if ref_native.size!=cand_native.size:
    raise SystemExit(f'Reference and candidate pixel dimensions differ {ref_native.size} != {cand_native.size}. Solve/render the camera correctly; do not resize for overlay.')
base_native=None
if a.baseline:
    base_native=Image.open(a.baseline).convert('RGB')
    if base_native.size!=ref_native.size:
        raise SystemExit(f'Baseline dimensions differ {base_native.size} != {ref_native.size}.')
overlay_native=Image.blend(ref_native,cand_native,0.5)
diff_native=ImageOps.autocontrast(ImageChops.difference(ref_native,cand_native))
items=[('REFERENCE',ref_native)]
if base_native is not None: items.append(('ACCEPTED BASELINE',base_native))
items += [('CANDIDATE',cand_native),('50% OVERLAY — DISPLAY ONLY',overlay_native),('ABSOLUTE PIXEL DIFFERENCE',diff_native)]
font=ImageFont.load_default(); cards=[]
for label,img in items:
    scale=min(1.0,a.display_width/img.width)
    shown=img.resize((max(1,round(img.width*scale)),max(1,round(img.height*scale))),Image.Resampling.LANCZOS) if scale!=1 else img.copy()
    card=Image.new('RGB',(shown.width,shown.height+36),'white'); card.paste(shown,(0,36)); ImageDraw.Draw(card).text((10,11),label,fill='black',font=font); cards.append(card)
height=max(c.height for c in cards); cards=[ImageOps.pad(c,(c.width,height),color='white',centering=(0.5,0)) for c in cards]
out=Image.new('RGB',(sum(c.width for c in cards),height),'white'); x=0
for card in cards: out.paste(card,(x,0)); x+=card.width
p=Path(a.out); p.parent.mkdir(parents=True,exist_ok=True); out.save(p); print(p)
