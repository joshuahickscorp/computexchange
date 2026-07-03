#!/bin/zsh
# wave 8 · full re-shoot on the FROZEN rig · 4K wide, 640 samples + OIDN (noise-floor route).
set -e
cd /Users/scammermike/Downloads/computexchange/.claude/worktrees/model-refinement
B=/Applications/Blender.app/Contents/MacOS/Blender
S=640
for shot in front q34 side; do
  echo "== studio $shot =="
  $B -b -P render/build_scene.py -- --portrait studio --shot $shot --pw 3840 --psamples $S --pdir render/portraits/ 2>&1 | grep -iE "rendered|error" | tail -1
done
for shot in front q34 side top; do
  echo "== spark $shot =="
  $B -b -P render/build_scene.py -- --portrait spark --shot $shot --pw 3840 --psamples $S --pdir render/portraits/ 2>&1 | grep -iE "rendered|error" | tail -1
done
echo "== pair =="
$B -b -P render/build_scene.py -- --only pair --samples $S --out render/portraits/ 2>&1 | grep -iE "rendered|error" | tail -1
echo "== reshoot done =="
