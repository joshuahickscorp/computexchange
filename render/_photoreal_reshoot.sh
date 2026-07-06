#!/bin/zsh
cd /Users/scammermike/Downloads/computexchange/.claude/worktrees/model-refinement
B=/Applications/Blender.app/Contents/MacOS/Blender
S=700
mkdir -p render/portraits-raw render/portraits
r() { echo "== $1 $2 =="; $B -b -P render/build_scene.py -- --portrait $1 --shot $2 --pw 3840 --psamples $S --pdir render/portraits-raw/ 2>&1 | grep -iE "rendered|error" | tail -1; }
for shot in front q34 side detail; do r studio $shot; done
for shot in front q34 side top detail; do r spark $shot; done
echo "== pair =="; $B -b -P render/build_scene.py -- --only pair --samples $S --out render/portraits-raw/ 2>&1 | grep -iE "rendered|error" | tail -1
python3 - << 'PY'
import os, glob, sys; sys.path.insert(0,'render'); import post_chain
from PIL import Image
raw='render/portraits-raw'; fin='render/portraits'
# detail crops from the raw 4K (studio ports from front, spark bezel from q34)
for f in sorted(glob.glob(f'{raw}/*.png')):
    post_chain.post(f, f'{fin}/'+os.path.basename(f))
    print('post', os.path.basename(f))
PY
echo "== photoreal reshoot+post done =="
