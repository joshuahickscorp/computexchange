#!/bin/zsh
cd /Users/scammermike/Downloads/computexchange/.claude/worktrees/model-refinement
B=/Applications/Blender.app/Contents/MacOS/Blender
S=700
r() { echo "== $1 $2 =="; $B -b -P render/build_scene.py -- --portrait $1 --shot $2 --pw 3840 --psamples $S --pdir render/portraits-raw/ 2>&1 | grep -iE "rendered|error" | tail -1; }
for shot in front q34 side top detail; do r spark $shot; done
echo "== pair =="; $B -b -P render/build_scene.py -- --only pair --samples $S --out render/portraits-raw/ 2>&1 | grep -iE "rendered|error" | tail -1
python3 - << 'PY'
import os, glob, sys; sys.path.insert(0,'render'); import post_chain
from PIL import Image
raw='render/portraits-raw'; fin='render/portraits'
for f in ['dgx-spark-front','dgx-spark-q34','dgx-spark-side','dgx-spark-top','dgx-spark-detail','oracles-pair@3x']:
    p=f'{raw}/{f}.png'
    if os.path.exists(p): post_chain.post(p, f'{fin}/{f}.png'); print('post',f)
PY
echo "== spark reshoot done =="
