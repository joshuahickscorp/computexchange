# oracles.py · the two oracles for the public site: Mac Studio + NVIDIA DGX Spark,
# rendered procedurally (Path A, license-clean, no imported meshes, no trademarks)
# inside the SAME Cycles rig as the product's metal assets (scripts/cx_knob.py family:
# near-black world · Key + thin bright Strip + Rim area lights · AgX High Contrast at
# negative exposure · transparent film · Fog Glow bloom · OpenImageDenoise).
#
# Run headless from the repo root:
#   /Applications/Blender.app/Contents/MacOS/Blender -b -P render/site/oracles.py -- \
#       --out web/assets/site/ --samples 2048
# Preview iteration (128 samples at 25% resolution, into render/site/previews/):
#   ... -- --preview --iter 1 [--only pair|studio|spark]
#
# Continuity contract (docs/TEARDOWN-2026-07-01.md, T0 rig audit): lights, world,
# color management, film, compositor are the knob rig VERBATIM. The one deliberate
# departure is the pair camera: 85mm perspective pitched down ~13 degrees per the
# site handoff (every rig camera is ortho; a two-object product scene needs the
# perspective conversation). Devices are modeled at true relative scale in mm and
# uniformly scaled into rig space so the pair occupies the knob's light envelope.

import bpy
import bmesh
import math
import sys
import time

# ---- args ----------------------------------------------------------------------------
argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def arg(name, default=None):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            return argv[i + 1]
        return True
    return default


PREVIEW = bool(arg("--preview", False))
SAMPLES = int(arg("--samples", 128 if PREVIEW else 2048))
OUT = str(arg("--out", "render/site/previews/" if PREVIEW else "web/assets/site/"))
ITER = str(arg("--iter", "0"))
ONLY = str(arg("--only", "all"))
if not OUT.endswith("/"):
    OUT += "/"

# ---- scale ---------------------------------------------------------------------------
# True dimensions (mm): Mac Studio 197 x 197 x 95 (Apple: 7.7 x 7.7 x 3.7 in).
# DGX Spark 150 x 150 x 50.5, verified against NVIDIA's hardware guide 2026-07-01.
# The knob rig lights a subject spanning ~2.2 units, so the pair (197 + 120 gap + 150
# = 467 mm) is scaled uniformly into that envelope. Relative scale is preserved: the
# size difference is part of the story.
PAIR_SPAN_MM = 197.0 + 120.0 + 150.0
S = 2.2 / (PAIR_SPAN_MM / 1000.0)  # ~4.71 units per meter


def mm(v):
    return v * S / 1000.0


# ---- scene reset ---------------------------------------------------------------------
def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.samples = SAMPLES
    sc.cycles.use_adaptive_sampling = True
    sc.cycles.adaptive_threshold = 0.005
    sc.cycles.use_denoising = True
    try:
        sc.cycles.denoiser = "OPENIMAGEDENOISE"
        sc.cycles.denoising_input_passes = "RGB_ALBEDO_NORMAL"
    except Exception as e:
        print("denoiser setup fallback:", e)
    # Color management: knob rig verbatim (AgX High Contrast, negative exposure).
    try:
        sc.view_settings.view_transform = "AgX"
        sc.view_settings.look = "AgX - High Contrast"
    except Exception:
        sc.view_settings.view_transform = "Filmic"
    sc.view_settings.exposure = -0.6
    sc.render.film_transparent = True
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGBA"
    sc.render.image_settings.color_depth = "16"
    sc.render.resolution_percentage = 25 if PREVIEW else 100
    # World: near-black, knob value.
    w = bpy.data.worlds.new("void")
    w.use_nodes = True
    w.node_tree.nodes["Background"].inputs[0].default_value = (0.008, 0.008, 0.01, 1)
    sc.world = w
    return sc


def enable_gpu(sc):
    # cx_button.py form: get_devices_for_type is REQUIRED headless in 4.x, otherwise
    # cp.devices is empty and Cycles silently falls back to CPU.
    chosen = "CPU"
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        for dt in ("METAL", "OPTIX", "CUDA"):
            try:
                prefs.compute_device_type = dt
            except TypeError:
                continue
            prefs.get_devices_for_type(dt)
            got = False
            for d in prefs.devices:
                if d.type == dt:
                    d.use = True
                    got = True
            if got:
                sc.cycles.device = "GPU"
                chosen = dt
                break
    except Exception as e:
        print("GPU setup failed, using CPU:", e)
    print("render device:", chosen)
    return chosen


# ---- geometry helpers ----------------------------------------------------------------
def rounded_box(name, w, d, h, r_corner, r_top, r_bottom, seg_corner=24, seg_fillet=6):
    """A racetrack-profile body: box (w x d x h), vertical corner radius r_corner,
    top edge fillet r_top, bottom edge fillet r_bottom. Origin at bottom center."""
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    for v in bm.verts:
        v.co.x *= w
        v.co.y *= d
        v.co.z *= h
        v.co.z += h / 2.0
    eps = h * 1e-4

    def vertical_edges():
        return [e for e in bm.edges
                if abs(e.verts[0].co.x - e.verts[1].co.x) < eps
                and abs(e.verts[0].co.y - e.verts[1].co.y) < eps]

    bmesh.ops.bevel(bm, geom=vertical_edges(), offset=r_corner,
                    segments=seg_corner, profile=0.5, affect="EDGES")

    def ring_at(z):
        return [e for e in bm.edges
                if abs(e.verts[0].co.z - z) < eps and abs(e.verts[1].co.z - z) < eps]

    if r_top > 0:
        bmesh.ops.bevel(bm, geom=ring_at(h), offset=r_top,
                        segments=seg_fillet, profile=0.5, affect="EDGES")
    if r_bottom > 0:
        bmesh.ops.bevel(bm, geom=ring_at(0.0), offset=r_bottom,
                        segments=max(3, seg_fillet - 2), profile=0.5, affect="EDGES")
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(ob)
    return ob


def smooth(ob, angle_deg=40.0):
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    bpy.ops.object.shade_auto_smooth(angle=math.radians(angle_deg))
    ob.select_set(False)


def apply_boolean(body, cutters):
    """Boolean-subtract each cutter, then return the cutters' bounding boxes in body
    space so interior faces can be re-materialed. Cutters are removed."""
    boxes = []
    bpy.context.view_layer.objects.active = body
    for cut in cutters:
        mn = [min((cut.matrix_world @ v.co)[i] for v in cut.data.vertices) for i in range(3)]
        mx = [max((cut.matrix_world @ v.co)[i] for v in cut.data.vertices) for i in range(3)]
        boxes.append((mn, mx))
        mod = body.modifiers.new("cut", "BOOLEAN")
        mod.operation = "DIFFERENCE"
        mod.solver = "EXACT"
        mod.object = cut
        bpy.ops.object.modifier_apply(modifier=mod.name)
        bpy.data.objects.remove(cut, do_unlink=True)
    return boxes


def assign_interior(body, boxes, mat_index, grow=0.0002, ymin=None):
    for poly in body.data.polygons:
        c = body.matrix_world @ poly.center
        if ymin is not None and c.y < ymin:
            continue
        for mn, mx in boxes:
            if (mn[0] - grow <= c.x <= mx[0] + grow
                    and mn[1] - grow <= c.y <= mx[1] + grow
                    and mn[2] - grow <= c.z <= mx[2] + grow):
                poly.material_index = mat_index
                break


def cutter_box(w, d, h, r, loc, seg=8):
    ob = rounded_box("cutter", w, d, h, r, 0, 0, seg_corner=seg)
    ob.location = (loc[0], loc[1], loc[2] - h / 2.0)  # rounded_box origin: bottom center
    bpy.context.view_layer.update()
    return ob


# ---- materials -----------------------------------------------------------------------
def principled(name, base, rough, metallic=1.0, coat=0.0, coat_rough=0.1):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*base, 1)
    b.inputs["Metallic"].default_value = metallic
    b.inputs["Roughness"].default_value = rough
    try:
        b.inputs["Coat Weight"].default_value = coat
        b.inputs["Coat Roughness"].default_value = coat_rough
    except KeyError:
        try:
            b.inputs["Clearcoat"].default_value = coat
        except KeyError:
            pass
    return m


def blasted_aluminum():
    """Bead-blasted (NOT brushed) light aluminum: fine isotropic roughness variation
    plus a whisper of bump for the micro-sparkle."""
    m = principled("mac-blasted-alu", (0.58, 0.60, 0.63), 0.36, coat=0.0)
    nt = m.node_tree
    b = nt.nodes["Principled BSDF"]
    tc = nt.nodes.new("ShaderNodeTexCoord")
    n = nt.nodes.new("ShaderNodeTexNoise")
    # Feature size tuned to read as blasting at hero resolution (object space).
    n.inputs["Scale"].default_value = 2500.0 / S
    n.inputs["Detail"].default_value = 3.0
    nt.links.new(tc.outputs["Object"], n.inputs["Vector"])
    mapr = nt.nodes.new("ShaderNodeMapRange")
    mapr.inputs["To Min"].default_value = 0.34
    mapr.inputs["To Max"].default_value = 0.42
    nt.links.new(n.outputs["Fac"], mapr.inputs["Value"])
    nt.links.new(mapr.outputs["Result"], b.inputs["Roughness"])
    bump = nt.nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.02
    nt.links.new(n.outputs["Fac"], bump.inputs["Height"])
    nt.links.new(bump.outputs["Normal"], b.inputs["Normal"])
    return m


def port_plastic():
    return principled("port-plastic", (0.03, 0.03, 0.033), 0.7, metallic=0.0)


def led_glass():
    # A darker glass dot, emission OFF: nothing glows on the oracles.
    return principled("led-glass", (0.02, 0.022, 0.025), 0.08, metallic=0.0, coat=1.0, coat_rough=0.05)


FOAM_CELL = 2.2  # pore pitch in true mm, shared by displacement and shading


def foam_field(nt):
    """The F2-F1 Voronoi the foam uses for BOTH displacement and shading: 0 on
    the ridge web, rising toward 1 at pore centers. Local/object coords so the
    displaced geometry and the color mask stay in register."""
    tc = nt.nodes.new("ShaderNodeTexCoord")
    v1 = nt.nodes.new("ShaderNodeTexVoronoi")
    v2 = nt.nodes.new("ShaderNodeTexVoronoi")
    for v in (v1, v2):
        v.voronoi_dimensions = "3D"
        v.inputs["Scale"].default_value = 1.0 / mm(FOAM_CELL)
        nt.links.new(tc.outputs["Object"], v.inputs["Vector"])
    v1.feature = "F1"
    v2.feature = "F2"
    sub = nt.nodes.new("ShaderNodeMath")
    sub.operation = "SUBTRACT"
    nt.links.new(v2.outputs["Distance"], sub.inputs[0])
    nt.links.new(v1.outputs["Distance"], sub.inputs[1])
    mul = nt.nodes.new("ShaderNodeMath")
    mul.operation = "MULTIPLY"
    mul.inputs[1].default_value = 1.0 / mm(FOAM_CELL)  # normalize to ~0..1
    nt.links.new(sub.outputs["Value"], mul.inputs[0])
    return mul.outputs["Value"]


def perforated_band():
    """The Mac Studio base band: near-black metal speckled with the perforation
    holes that show in the 8 mm float gap (Voronoi F1 dots as darker pits)."""
    m = principled("mac-base-band", (0.16, 0.165, 0.175), 0.45)
    nt = m.node_tree
    b = nt.nodes["Principled BSDF"]
    tc = nt.nodes.new("ShaderNodeTexCoord")
    v = nt.nodes.new("ShaderNodeTexVoronoi")
    v.voronoi_dimensions = "3D"
    v.feature = "F1"
    v.inputs["Scale"].default_value = 1.0 / mm(2.4)
    nt.links.new(tc.outputs["Object"], v.inputs["Vector"])
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].position = 0.28
    ramp.color_ramp.elements[0].color = (0.015, 0.015, 0.018, 1)
    ramp.color_ramp.elements[1].position = 0.42
    ramp.color_ramp.elements[1].color = (0.16, 0.165, 0.175, 1)
    nt.links.new(v.outputs["Distance"], ramp.inputs["Fac"])
    nt.links.new(ramp.outputs["Color"], b.inputs["Base Color"])
    return m


def champagne_gold(rough=0.28, pore_darken=False):
    m = principled("spark-gold" + ("-foam" if pore_darken else ""),
                   (0.58, 0.45, 0.27), rough)
    if pore_darken:
        nt = m.node_tree
        b = nt.nodes["Principled BSDF"]
        field = foam_field(nt)
        ramp = nt.nodes.new("ShaderNodeValToRGB")
        # Ridge web stays champagne; pore floors fall 3+ stops dark.
        ramp.color_ramp.elements[0].position = 0.13
        ramp.color_ramp.elements[0].color = (0.66, 0.52, 0.30, 1)
        ramp.color_ramp.elements[1].position = 0.38
        ramp.color_ramp.elements[1].color = (0.035, 0.026, 0.017, 1)
        nt.links.new(field, ramp.inputs["Fac"])
        nt.links.new(ramp.outputs["Color"], b.inputs["Base Color"])
        rramp = nt.nodes.new("ShaderNodeMapRange")
        rramp.inputs["To Min"].default_value = 0.30
        rramp.inputs["To Max"].default_value = 0.6
        nt.links.new(field, rramp.inputs["Value"])
        nt.links.new(rramp.outputs["Result"], b.inputs["Roughness"])
    return m


# ---- the devices ---------------------------------------------------------------------
def build_mac_studio(loc_x=0.0, yaw_deg=0.0):
    """197 x 197 x 95 mm, matched to Apple's front product photo: racetrack body
    (corner radius ~36 mm) floating 8 mm on the inset base whose visible band is
    the speckled perforation ring; front, left to right: two USB-C at -68/-52 mm,
    SD slot left of center at -24 mm, power LED far right at +68 mm, all in the
    lower third. Rear perforation field SKIPPED: not visible at the hero camera,
    and the doctrine is skip rather than fake."""
    body_h = mm(87.0)
    body = rounded_box("mac-studio", mm(197), mm(197), body_h,
                       mm(36), mm(3), mm(2), seg_corner=32, seg_fillet=7)
    body.location = (0, 0, mm(8.0))
    bpy.context.view_layer.update()

    alu = blasted_aluminum()
    plastic = port_plastic()
    body.data.materials.append(alu)
    body.data.materials.append(plastic)

    front_y = -mm(197) / 2.0
    zc = mm(8.0 + 16.0)
    cutters = [
        cutter_box(mm(9), mm(7), mm(3.5), mm(1.6), (-mm(68), front_y + mm(1.5), zc)),
        cutter_box(mm(9), mm(7), mm(3.5), mm(1.6), (-mm(52), front_y + mm(1.5), zc)),
        cutter_box(mm(26), mm(7), mm(2.8), mm(1.2), (-mm(24), front_y + mm(1.5), zc)),
    ]
    boxes = apply_boolean(body, cutters)
    assign_interior(body, boxes, 1, ymin=front_y + mm(0.4))
    smooth(body, 40)

    # Base: the floating ring. Its side band reads as the dark perforated mesh in
    # the 8 mm shadow gap, exactly what Apple's front photo shows.
    bpy.ops.mesh.primitive_cylinder_add(radius=mm(72), depth=mm(9.5),
                                        location=(0, 0, mm(4.75)), vertices=128)
    base = bpy.context.active_object
    base.name = "mac-studio-base"
    base.data.materials.append(perforated_band())
    smooth(base, 40)

    # Power LED: a 2 mm darker glass dot, no emission.
    bpy.ops.mesh.primitive_cylinder_add(radius=mm(1.0), depth=mm(0.6), vertices=24,
                                        rotation=(math.radians(90), 0, 0),
                                        location=(mm(68), front_y + mm(0.05), zc))
    led = bpy.context.active_object
    led.name = "mac-studio-led"
    led.data.materials.append(led_glass())
    smooth(led, 60)

    group = [body, base, led]
    for ob in group:
        ob.rotation_euler.z = math.radians(yaw_deg)
        x, y = ob.location.x, ob.location.y
        c, s = math.cos(math.radians(yaw_deg)), math.sin(math.radians(yaw_deg))
        ob.location.x, ob.location.y = x * c - y * s, x * s + y * c
        ob.location.x += loc_x
    return group


def stadium(name, w, h, d, r, loc):
    """A vertical pill/stadium prism in the X-Z plane (rounded in X-Z), depth d
    along Y, centered at loc."""
    ob = rounded_box(name, w, h, d, r, 0, 0, seg_corner=16)
    ob.rotation_euler = (math.radians(90), 0, 0)
    ob.location = (loc[0], loc[1] + d / 2.0, loc[2] - h / 2.0 + h / 2.0)
    # rounded_box origin is bottom-center of its w x h footprint; after the X
    # rotation the footprint stands upright and "bottom" points toward +Y.
    ob.location = (loc[0], loc[1] + d / 2.0, loc[2])
    bpy.context.view_layer.update()
    return ob


def build_dgx_spark(loc_x=0.0, yaw_deg=0.0):
    """150 x 150 x 50.5 mm, matched to the StorageReview front photos: the porous
    metal foam IS the whole front face, flush between two ~7 mm champagne side
    rails and ~2.5 mm top/bottom lips, with two vertical stadium cutouts (about
    20 x 32 mm, centers near +/-47 mm) whose smooth gold tubs sit recessed in the
    foam. The real device's left tub carries the NVIDIA plate; ours stays BLANK
    (trademark gate). Rear foam skipped: never visible at the shipped cameras.
    Satin anodize on the body, no jewelry polish."""
    body = rounded_box("dgx-spark", mm(150), mm(150), mm(50.5),
                       mm(10), mm(4), mm(4), seg_corner=20, seg_fillet=7)
    bpy.context.view_layer.update()

    gold = champagne_gold(0.38)
    body.data.materials.append(gold)

    # Full-face pocket between the rails: 136 wide x 44.5 tall, 4 mm deep.
    front_y = -mm(150) / 2.0
    fw, fh = mm(136), mm(44.5)
    zc = mm(50.5 / 2.0)
    pocket = cutter_box(fw, mm(24), fh, mm(6), (0, front_y + mm(4.0), zc), seg=12)
    boxes = apply_boolean(body, [pocket])
    backing = principled("spark-pocket", (0.10, 0.085, 0.06), 0.6, metallic=0.4)
    body.data.materials.append(backing)
    assign_interior(body, boxes, 1)
    smooth(body, 40)

    # The foam sheet: dense grid, two stadium holes punched through BEFORE
    # displacement, then carved by the Voronoi F2-F1 field (true geometry).
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=720, y_subdivisions=240,
                                    size=1.0, location=(0, front_y + mm(1.2), zc),
                                    rotation=(math.radians(90), 0, 0))
    foam = bpy.context.active_object
    foam.name = "dgx-spark-foam"
    foam.scale = (fw + mm(3), fh + mm(3), 1.0)
    bpy.ops.object.transform_apply(scale=True)
    holes = [
        stadium("hole-l", mm(16.5), mm(30.5), mm(30), mm(8.0), (-mm(47), front_y - mm(10), zc)),
        stadium("hole-r", mm(16.5), mm(30.5), mm(30), mm(8.0), (mm(47), front_y - mm(10), zc)),
    ]
    bpy.context.view_layer.objects.active = foam
    for h in holes:
        mod = foam.modifiers.new("hole", "BOOLEAN")
        mod.operation = "DIFFERENCE"
        mod.solver = "EXACT"
        mod.object = h
        bpy.ops.object.modifier_apply(modifier=mod.name)
        bpy.data.objects.remove(h, do_unlink=True)
    tex = bpy.data.textures.new("foam-voronoi", "VORONOI")
    tex.distance_metric = "DISTANCE"
    tex.weight_1 = -1.0
    tex.weight_2 = 1.0
    tex.noise_scale = mm(FOAM_CELL)
    tex.noise_intensity = 1.0
    disp = foam.modifiers.new("pores", "DISPLACE")
    disp.texture = tex
    disp.texture_coords = "LOCAL"
    disp.direction = "Y"
    disp.mid_level = 0.42
    disp.strength = mm(3.2)
    bpy.ops.object.modifier_apply(modifier=disp.name)
    foam.data.materials.append(champagne_gold(pore_darken=True))
    smooth(foam, 70)

    # The two smooth tubs, recessed 1.5 mm behind the foam face, blank.
    tubs = []
    for sx in (-1, 1):
        tub = stadium("tub", mm(19), mm(33), mm(3), mm(9.2),
                      (sx * mm(47), front_y + mm(2.2), zc))
        tub.data.materials.append(principled("tub-gold", (0.36, 0.28, 0.16), 0.5))
        smooth(tub, 50)
        tubs.append(tub)

    group = [body, foam] + tubs
    for ob in group:
        ob.rotation_euler.z = math.radians(yaw_deg)
        x, y = ob.location.x, ob.location.y
        c, s = math.cos(math.radians(yaw_deg)), math.sin(math.radians(yaw_deg))
        ob.location.x, ob.location.y = x * c - y * s, x * s + y * c
        ob.location.x += loc_x
    return group


# ---- rig: lights, camera, ground, compositor (knob rig verbatim) ----------------------
def add_area(name, loc, size, energy, color=(1, 1, 1), sx=None, aim=None):
    ld = bpy.data.lights.new(name, "AREA")
    ld.energy = energy
    ld.color = color
    if sx is not None:
        ld.shape = "RECTANGLE"
        ld.size = sx
        ld.size_y = size
    else:
        ld.size = size
    ob = bpy.data.objects.new(name, ld)
    ob.location = loc
    bpy.context.collection.objects.link(ob)
    if aim is not None:
        con = ob.constraints.new("TRACK_TO")
        con.target = aim
        con.track_axis = "TRACK_NEGATIVE_Z"
        con.up_axis = "UP_Y"
    return ob


def rig_lights_and_ground(aim_z=0.1):
    aim = bpy.data.objects.new("Aim", None)
    aim.location = (0, 0, aim_z)
    bpy.context.collection.objects.link(aim)
    # Knob rig verbatim: Key 170 · Fill 42 · Strip 240 (0.10 x 2.6) · Rim 75.
    add_area("Key", (-2.2, -2.2, 3.4), 2.6, 170, (0.96, 0.98, 1.0), aim=aim)
    add_area("Fill", (2.6, -1.0, 1.6), 2.0, 42, aim=aim)
    add_area("Strip", (-1.6, -2.2, 2.6), 2.6, 380, sx=0.05, aim=aim)
    add_area("Rim", (0.5, 2.2, 2.2), 1.4, 48, aim=aim)
    # Shadow catcher ground: contact shadows ride inside the PNG alpha exactly as
    # the knob assets do.
    bpy.ops.mesh.primitive_plane_add(size=30, location=(0, 0, 0))
    ground = bpy.context.active_object
    ground.name = "ground"
    ground.is_shadow_catcher = True
    return aim


def camera_85(aim, subject_w, subject_h, margin=1.16, pitch_deg=13.0, res=(3600, 2250)):
    sc = bpy.context.scene
    cd = bpy.data.cameras.new("cam")
    cd.lens = 85.0
    cd.sensor_width = 36.0
    cam = bpy.data.objects.new("cam", cd)
    bpy.context.collection.objects.link(cam)
    sc.camera = cam
    sc.render.resolution_x, sc.render.resolution_y = res
    # Fit: frame the subject width (and check height) with ~8% margin per side.
    aspect = res[0] / res[1]
    h_fov = 2.0 * math.atan(36.0 / (2.0 * 85.0))
    v_fov = 2.0 * math.atan((36.0 / aspect) / (2.0 * 85.0))
    d_w = (subject_w * margin) / (2.0 * math.tan(h_fov / 2.0))
    d_h = (subject_h * margin * 1.35) / (2.0 * math.tan(v_fov / 2.0))
    d = max(d_w, d_h)
    p = math.radians(pitch_deg)
    cam.location = (0, aim.location.y - d * math.cos(p), aim.location.z + d * math.sin(p))
    con = cam.constraints.new("TRACK_TO")
    con.target = aim
    con.track_axis = "TRACK_NEGATIVE_Z"
    con.up_axis = "UP_Y"
    return cam


def compositor_bloom():
    # Fog Glow 0.6 / 7 (knob and button values) plus the cx_logo.py alpha-from-bloom
    # rig so any glow that escapes the geometry survives transparent-film compositing.
    sc = bpy.context.scene
    sc.use_nodes = True
    nt = sc.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    rl = nt.nodes.new("CompositorNodeRLayers")
    glare = nt.nodes.new("CompositorNodeGlare")
    glare.glare_type = "FOG_GLOW"
    glare.threshold = 0.6
    glare.size = 7
    comp = nt.nodes.new("CompositorNodeComposite")
    bw = nt.nodes.new("CompositorNodeRGBToBW")
    mx = nt.nodes.new("CompositorNodeMath")
    mx.operation = "MAXIMUM"
    mx.use_clamp = True
    sa = nt.nodes.new("CompositorNodeSetAlpha")
    sa.mode = "REPLACE_ALPHA"
    nt.links.new(rl.outputs["Image"], glare.inputs["Image"])
    nt.links.new(glare.outputs["Image"], bw.inputs["Image"])
    nt.links.new(bw.outputs["Val"], mx.inputs[0])
    nt.links.new(rl.outputs["Alpha"], mx.inputs[1])
    nt.links.new(glare.outputs["Image"], sa.inputs["Image"])
    nt.links.new(mx.outputs["Value"], sa.inputs["Alpha"])
    nt.links.new(sa.outputs["Image"], comp.inputs["Image"])


def render_to(path):
    sc = bpy.context.scene
    sc.render.filepath = path
    t0 = time.time()
    bpy.ops.render.render(write_still=True)
    print(f"rendered {path} in {time.time() - t0:.1f}s "
          f"({sc.cycles.samples} samples, {sc.render.resolution_percentage}%)")


# ---- scenes --------------------------------------------------------------------------
STUDIO_W, STUDIO_H = mm(197), mm(95)
SPARK_W, SPARK_H = mm(150), mm(50.5)


def scene_pair():
    sc = reset_scene()
    enable_gpu(sc)
    # Composition: Studio left, Spark right, 120 mm gap, fronts toward camera,
    # each yawed ~8 degrees toward the other so the pair converses.
    studio_cx = -(PAIR_SPAN_MM / 2.0 - 197.0 / 2.0)
    spark_cx = PAIR_SPAN_MM / 2.0 - 150.0 / 2.0
    build_mac_studio(mm(studio_cx), yaw_deg=-8.0)
    build_dgx_spark(mm(spark_cx), yaw_deg=8.0)
    aim = rig_lights_and_ground(aim_z=STUDIO_H * 0.42)
    camera_85(aim, subject_w=mm(PAIR_SPAN_MM), subject_h=STUDIO_H,
              res=(3600, 2250))
    compositor_bloom()
    suffix = f"-iter{ITER}" if PREVIEW else ""
    render_to(OUT + f"oracles-pair{suffix}@3x.png")


def scene_solo(which):
    sc = reset_scene()
    enable_gpu(sc)
    if which == "studio":
        build_mac_studio(0.0, yaw_deg=-8.0)
        w, h = STUDIO_W * 1.45, STUDIO_H  # 1.45: corner-to-corner at yaw + breathing room
    else:
        build_dgx_spark(0.0, yaw_deg=8.0)
        w, h = SPARK_W * 1.45, SPARK_H
    aim = rig_lights_and_ground(aim_z=STUDIO_H * 0.42)
    camera_85(aim, subject_w=w, subject_h=h, res=(2048, 2048))
    compositor_bloom()
    name = "mac-studio" if which == "studio" else "dgx-spark"
    suffix = f"-iter{ITER}" if PREVIEW else ""
    render_to(OUT + f"{name}{suffix}@3x.png")


if ONLY in ("all", "pair"):
    scene_pair()
if ONLY in ("all", "studio"):
    scene_solo("studio")
if ONLY in ("all", "spark"):
    scene_solo("spark")
print("oracles done.")
