#!/bin/zsh
# phase-4 batch · render the remaining portraits + the tabletop pair sequentially (one GPU).
set -e
cd /Users/scammermike/Downloads/computexchange/.claude/worktrees/model-refinement
B=/Applications/Blender.app/Contents/MacOS/Blender
echo "== spark front =="; $B -b -P render/build_scene.py -- --portrait spark --shot front  2>&1 | grep -iE "rendered|error" | head -1
echo "== spark detail =="; $B -b -P render/build_scene.py -- --portrait spark --shot detail 2>&1 | grep -iE "rendered|error" | head -1
echo "== studio all =="; $B -b -P render/build_scene.py -- --portrait studio 2>&1 | grep -iE "rendered|error" | head -3
echo "== tabletop pair =="; $B -b -P render/build_scene.py -- --only pair --samples 768 --out render/portraits/ 2>&1 | grep -iE "rendered|error" | head -1
echo "== phase4 batch done =="
