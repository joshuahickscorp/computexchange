#!/bin/zsh
cd /Users/scammermike/Downloads/computexchange/.claude/worktrees/model-refinement
zsh render/_photoreal_reshoot.sh
cp render/portraits-raw/mac-studio-front.png render/calib/mac-studio-front.png
cp render/portraits-raw/dgx-spark-q34.png render/calib/dgx-spark-q34.png
echo "== regate =="; python3 render/rig_patches.py --offset -12 2>&1 | grep -E "ALL|FAIL"
python3 render/_collage.py >/dev/null 2>&1 && echo "collages ok"
python3 render/_checkpoint_export.py 2>&1 | tail -2
echo "== GEOPACK DONE =="
