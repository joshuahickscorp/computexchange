#!/bin/zsh
cd /Users/scammermike/Downloads/computexchange/.claude/worktrees/model-refinement
B=/Applications/Blender.app/Contents/MacOS/Blender
echo "== L19 calib (builds new 0.14 foam cache) =="
$B -b -P render/build_scene.py -- --portrait spark --shot q34 --pw 1500 --psamples 240 --pdir render/calib/ 2>&1 | grep -iE "foam3d|rendered|error" | tail -3
$B -b -P render/build_scene.py -- --portrait studio --shot front --pw 1500 --psamples 240 --pdir render/calib/ 2>&1 | grep -iE "rendered|error" | tail -1
echo "== GATE =="
G=$(python3 render/rig_patches.py --offset -12 2>&1 | grep -E "PASS|FAIL|ALL")
echo "$G"
if ! echo "$G" | grep -q "ALL PASS"; then echo "== L19 GATE FAIL · stopping =="; exit 1; fi
echo "== full 4K reshoot =="
zsh render/_photoreal_reshoot.sh 2>&1 | grep -iE "rendered|post |done" | tail -14
echo "== regate delivered =="
cp render/portraits-raw/mac-studio-front.png render/calib/mac-studio-front.png
cp render/portraits-raw/dgx-spark-q34.png render/calib/dgx-spark-q34.png
python3 render/rig_patches.py --offset -12 2>&1 | grep -E "ALL|FAIL"
python3 render/_collage.py >/dev/null 2>&1 && echo "collages ok"
python3 render/_checkpoint_export.py 2>&1 | tail -2
echo "== L19PACK DONE =="
