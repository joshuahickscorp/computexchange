#!/bin/zsh
cd /Users/scammermike/Downloads/computexchange/.claude/worktrees/model-refinement
B=/Applications/Blender.app/Contents/MacOS/Blender
S=${1:-450}
mkdir -p render/portraits-raw render/portraits
r() { echo "== $1 $2 =="; $B -b -P render/build_scene.py -- --portrait $1 --shot $2 --pw 2000 --psamples $S --pdir render/portraits-raw/ 2>&1 | grep -iE "rendered|error" | tail -1; }
r studio front; r studio q34; r spark front; r spark q34
echo "== pair =="; $B -b -P render/build_scene.py -- --only pair --samples $S --out render/portraits-raw/ 2>&1 | grep -iE "rendered|error" | tail -1
python3 - << 'PY'
import os, glob, sys; sys.path.insert(0,'render'); import post_chain
from PIL import Image
raw='render/portraits-raw'; fin='render/portraits'
def crop(src,box,out):
    im=Image.open(src).convert('RGB'); w,h=im.size
    im.crop((int(box[0]*w),int(box[1]*h),int(box[2]*w),int(box[3]*h))).save(out)
crop(f'{raw}/dgx-spark-q34.png',(0.02,0.42,0.42,0.82),f'{raw}/dgx-spark-detail.png')
for f in ['mac-studio-front','mac-studio-q34','dgx-spark-front','dgx-spark-q34','dgx-spark-detail','oracles-pair@3x']:
    p=f'{raw}/{f}.png'
    if os.path.exists(p): post_chain.post(p, f'{fin}/{f}.png'); print('post',f)
PY
echo "== panel reshoot done =="
