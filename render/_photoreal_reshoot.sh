#!/bin/zsh
cd /Users/scammermike/Downloads/computexchange/.claude/worktrees/model-refinement
B=/Applications/Blender.app/Contents/MacOS/Blender
S=700
mkdir -p render/portraits-raw render/portraits
r() { echo "== $1 $2 =="; $B -b -P render/build_scene.py -- --portrait $1 --shot $2 --pw 3840 --psamples $S --pdir render/portraits-raw/ 2>&1 | grep -iE "rendered|error" | tail -1; }
for shot in front q34 side; do r studio $shot; done
for shot in front q34 side top; do r spark $shot; done
echo "== pair =="; $B -b -P render/build_scene.py -- --only pair --samples $S --out render/portraits-raw/ 2>&1 | grep -iE "rendered|error" | tail -1
python3 - << 'PY'
import os, glob, sys; sys.path.insert(0,'render'); import post_chain
from PIL import Image
raw='render/portraits-raw'; fin='render/portraits'
# detail crops from the raw 4K (studio ports from front, spark bezel from q34)
def crop(src,box,out):
    im=Image.open(src).convert('RGB'); w,h=im.size
    im.crop((int(box[0]*w),int(box[1]*h),int(box[2]*w),int(box[3]*h))).save(out)
crop(f'{raw}/mac-studio-front.png',(0.14,0.62,0.52,0.86),f'{raw}/mac-studio-detail.png')
crop(f'{raw}/dgx-spark-q34.png',(0.02,0.42,0.42,0.82),f'{raw}/dgx-spark-detail.png')
for f in sorted(glob.glob(f'{raw}/*.png')):
    post_chain.post(f, f'{fin}/'+os.path.basename(f))
    print('post', os.path.basename(f))
PY
echo "== photoreal reshoot+post done =="
