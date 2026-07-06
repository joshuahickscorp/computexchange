# render/_audit_desktop.py · 360 audit tool for the Studio / Spark · renders a desktop from ANY
# yaw by exec-loading build_scene.py's builders (--only none, no dispatch) and pre-rotating the
# object so a front camera sees the chosen face. Reuses the frozen rig. Dash gate: middot only.
#   Blender -b -P render/_audit_desktop.py -- --which spark --yaw 180 --name rear
#   (yaw 0 = front, 180 = rear, 200/205 = rear-3/4, 90 = side)
import bpy, sys, os
HERE = os.path.dirname(os.path.abspath(__file__)); SCENE = os.path.join(HERE, "build_scene.py")
_argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
def _a(n, d=None):
    if n in _argv:
        i = _argv.index(n)
        return _argv[i + 1] if i + 1 < len(_argv) and not str(_argv[i + 1]).startswith("--") else True
    return d
WHICH = str(_a("--which", "spark")); YAW = float(_a("--yaw", 200.0)); NAME = str(_a("--name", "rear"))
ns = {"__file__": SCENE, "__name__": "scene_defs"}
_sv = sys.argv; sys.argv = ["b", "--", "--only", "none", "--preview"]
exec(compile(open(SCENE).read(), SCENE, "exec"), ns); sys.argv = _sv
mm = ns["mm"]
sc = ns["reset_scene"](); ns["enable_gpu"](sc)
if WHICH == "spark":
    ns["build_dgx_spark"](0.0, yaw_deg=YAW); sw, sh = mm(150), mm(50.5)
else:
    ns["build_mac_studio"](0.0, yaw_deg=YAW); sw, sh = mm(197), mm(95)
aim = ns["portrait_rig"](sh, **ns["PORTRAIT_RIG"])
ns["portrait_camera"](aim, sw, sh, "front", (1500, 1050), margin=1.5)
ns["render_to"](f"render/previews/audit-{WHICH}-{NAME}.png")
print("AUDIT done", WHICH, NAME)
