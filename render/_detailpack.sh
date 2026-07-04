#!/bin/zsh
cd /Users/scammermike/Downloads/computexchange/.claude/worktrees/model-refinement
B=/Applications/Blender.app/Contents/MacOS/Blender
echo "== spark detail 4K =="
$B -b -P render/build_scene.py -- --portrait spark --shot detail --pw 3840 --psamples 700 --pdir render/portraits-raw/ 2>&1 | grep -iE "rendered|error" | tail -1
echo "== studio detail 4K =="
$B -b -P render/build_scene.py -- --portrait studio --shot detail --pw 3840 --psamples 700 --pdir render/portraits-raw/ 2>&1 | grep -iE "rendered|error" | tail -1
python3 - << 'PY'
import sys; sys.path.insert(0,'render'); import post_chain
for f in ['dgx-spark-detail','mac-studio-detail']:
    post_chain.post(f'render/portraits-raw/{f}.png', f'render/portraits/{f}.png'); print('post', f)
PY
python3 render/_collage.py >/dev/null 2>&1 && echo "collages ok"
python3 render/_checkpoint_export.py 2>&1 | tail -2
echo "== DETAILPACK DONE =="
