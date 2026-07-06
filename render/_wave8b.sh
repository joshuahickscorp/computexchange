#!/bin/zsh
cd /Users/scammermike/Downloads/computexchange/.claude/worktrees/model-refinement
B=/Applications/Blender.app/Contents/MacOS/Blender
for shot in top q34 side; do
  echo "== spark $shot =="
  $B -b -P render/build_scene.py -- --portrait spark --shot $shot --pw 3840 --psamples 640 --pdir render/portraits/ 2>&1 | grep -iE "rendered|error" | tail -1
done
echo "== pair =="
$B -b -P render/build_scene.py -- --only pair --samples 640 --out render/portraits/ 2>&1 | grep -iE "rendered|error" | tail -1
echo "== wave8b done =="
