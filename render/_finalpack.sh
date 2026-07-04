#!/bin/zsh
cd /Users/scammermike/Downloads/computexchange/.claude/worktrees/model-refinement
B=/Applications/Blender.app/Contents/MacOS/Blender
# 1 · wait for the in-flight Spark 4K reshoot to finish
while pgrep -f "_spark_reshoot" >/dev/null 2>&1 && ! grep -q "spark reshoot done" render/_sparkshoot.log 2>/dev/null; do sleep 30; done
echo "== spark done; rendering studio =="
# 2 · render the Studio angles at 4K (foam cache means spark stays fast; studio is fresh)
r() { echo "== $1 $2 =="; $B -b -P render/build_scene.py -- --portrait $1 --shot $2 --pw 3840 --psamples 700 --pdir render/portraits-raw/ 2>&1 | grep -iE "rendered|error" | tail -1; }
for shot in front q34 side; do r studio $shot; done
# 3 · post the studio frames + crop the studio detail
python3 - << 'PY'
import os, sys; sys.path.insert(0,'render'); import post_chain
from PIL import Image
raw='render/portraits-raw'; fin='render/portraits'
def crop(src,box,out):
    im=Image.open(src).convert('RGB'); w,h=im.size
    im.crop((int(box[0]*w),int(box[1]*h),int(box[2]*w),int(box[3]*h))).save(out)
crop(f'{raw}/mac-studio-front.png',(0.14,0.62,0.52,0.86),f'{raw}/mac-studio-detail.png')
for f in ['mac-studio-front','mac-studio-q34','mac-studio-side','mac-studio-detail']:
    p=f'{raw}/{f}.png'
    if os.path.exists(p): post_chain.post(p, f'{fin}/{f}.png'); print('post',f)
PY
# 4 · gate the delivered frames (informational)
cp render/portraits-raw/mac-studio-front.png render/calib/mac-studio-front.png
cp render/portraits-raw/dgx-spark-q34.png render/calib/dgx-spark-q34.png
echo "== GATE =="; python3 render/rig_patches.py --offset -12 2>&1 | grep -E "PASS|FAIL|ALL"
# 5 · rebuild collages + checkpoint folder + copy the report
echo "== packing =="
python3 render/_collage.py 2>&1 | tail -1
python3 render/_checkpoint_export.py 2>&1 | tail -3
cp render/PHOTOREAL-SHIFT-REPORT.md ~/Downloads/cx-oracles-checkpoint-2026-07-03/00_REPORT.md
echo "checkpoint attachments: $(ls ~/Downloads/cx-oracles-checkpoint-2026-07-03/ | wc -l | tr -d ' ')"
echo "== FINALPACK DONE =="
