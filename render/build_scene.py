# build_scene.py · the tabletop hero: a Mac Studio and an NVIDIA DGX Spark sitting
# on a matte desk, seen from a standing human's eye line looking DOWN at the surface
# (~36 degrees of down-pitch, perspective). Procedural, from scratch, no imported
# meshes, no trademarks. This is the source of truth for BOTH the Cycles stills
# (web fallback + og-image) and the glTF export for the live Three.js hero, so the
# two are pixel-cousins.
#
# Run headless from the repo root:
#   /Applications/Blender.app/Contents/MacOS/Blender -b -P render/build_scene.py -- \
#       --out web/assets/site/ --samples 1024
# Preview iteration (128 samples, 50% res, into render/previews/):
#   ... -- --preview --iter 1 [--only pair|studio|spark]
# glTF export (no render):
#   ... -- --export render/gltf/
#
# Doctrine (docs/SITE-REBUILD-T0.md): real matte near-black floor (NOT a transparent
# shadow catcher), void-black world matching the site --bg, one soft key high and
# camera-left, a dim rim strip behind and above, a low fill so the shadow side does
# not crush. AgX. Metric scale, both devices at exact relative size on the desk.

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


EXPORT = arg("--export", None)
VERIFY = arg("--verify", None)   # "studio" | "spark" | "all" · orthographic reference-match renders
PREVIEW = bool(arg("--preview", False))
SAMPLES = int(arg("--samples", 128 if PREVIEW else 2048))
OUT = str(arg("--out", "render/previews/" if PREVIEW else "web/assets/site/"))
ITER = str(arg("--iter", "0"))
ONLY = str(arg("--only", "all"))
if not OUT.endswith("/"):
    OUT += "/"

# ---- scale ---------------------------------------------------------------------------
# True metric scale: 1 Blender unit = 1 metre, so every dimension is real and the
# Studio (197 x 197 x 95 mm) genuinely dwarfs the Spark (150 x 150 x 50.5 mm).
# Ground truth per docs/SITE-REBUILD-T0.md.
PAIR_SPAN_MM = 197.0 + 120.0 + 150.0
S = 1.0  # unit scale factor kept so material feature sizes stay expressed in 1/S


def mm(v):
    return v / 1000.0


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
    sc.view_settings.exposure = -0.35
    sc.render.film_transparent = False
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGBA"
    sc.render.image_settings.color_depth = "16"
    sc.render.resolution_percentage = 25 if PREVIEW else 100
    # World: void black matching the site --bg (#060606). The devices are lit, the
    # world is not · reflections read as abstract dark gradients, not a studio.
    w = bpy.data.worlds.new("void")
    w.use_nodes = True
    w.node_tree.nodes["Background"].inputs[0].default_value = (0.006, 0.006, 0.007, 1)
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


FOAM_CELL = 2.0       # coarse pore pitch in true mm (reference is a fine dense foam)
FOAM_FINE = FOAM_CELL / 3.0   # the overlapping second scale (checklist: ~1/3)


def _voronoi_ridge(nt, tc, cell):
    """One F2-F1 Voronoi ridge field at a given cell size · 0 on the web, ~1 at
    pore centers."""
    v1 = nt.nodes.new("ShaderNodeTexVoronoi")
    v2 = nt.nodes.new("ShaderNodeTexVoronoi")
    for v in (v1, v2):
        v.voronoi_dimensions = "3D"
        v.inputs["Scale"].default_value = 1.0 / mm(cell)
        nt.links.new(tc.outputs["Object"], v.inputs["Vector"])
    v1.feature = "F1"
    v2.feature = "F2"
    sub = nt.nodes.new("ShaderNodeMath")
    sub.operation = "SUBTRACT"
    nt.links.new(v2.outputs["Distance"], sub.inputs[0])
    nt.links.new(v1.outputs["Distance"], sub.inputs[1])
    mul = nt.nodes.new("ShaderNodeMath")
    mul.operation = "MULTIPLY"
    mul.inputs[1].default_value = 1.0 / mm(cell)
    nt.links.new(sub.outputs["Value"], mul.inputs[0])
    return mul.outputs["Value"]


def foam_field(nt):
    """TWO overlapping Voronoi scales (checklist): the coarse cell defines the big
    open pores, a finer field at ~1/3 scale adds the sub-structure real open-cell
    metal foam shows. Local/object coords so it stays in register with the
    displaced geometry."""
    tc = nt.nodes.new("ShaderNodeTexCoord")
    coarse = _voronoi_ridge(nt, tc, FOAM_CELL)
    fine = _voronoi_ridge(nt, tc, FOAM_FINE)
    mix = nt.nodes.new("ShaderNodeMath")
    mix.operation = "MULTIPLY_ADD"       # coarse + 0.4*fine
    fine_scaled = nt.nodes.new("ShaderNodeMath")
    fine_scaled.operation = "MULTIPLY"
    fine_scaled.inputs[1].default_value = 0.4
    nt.links.new(fine, fine_scaled.inputs[0])
    add = nt.nodes.new("ShaderNodeMath")
    add.operation = "ADD"
    add.use_clamp = True
    nt.links.new(coarse, add.inputs[0])
    nt.links.new(fine_scaled.outputs["Value"], add.inputs[1])
    return add.outputs["Value"]


def perforated_band():
    """The Mac Studio base intake: a FINE DENSE regular hole mesh (reference is ~1.3 mm
    pitch), not sparse speckle. A high-frequency Voronoi F1 gives one round pit per cell;
    a sharp ramp keeps the metal web bright and the holes small + dark, and a Bump sinks
    them so they read as recessed perforations, not painted dots (checklist: map domain,
    not geometry)."""
    m = principled("mac-base-band", (0.19, 0.195, 0.205), 0.5)
    nt = m.node_tree
    b = nt.nodes["Principled BSDF"]
    tc = nt.nodes.new("ShaderNodeTexCoord")
    v = nt.nodes.new("ShaderNodeTexVoronoi")
    v.voronoi_dimensions = "3D"
    v.feature = "F1"
    v.inputs["Scale"].default_value = 1.0 / mm(1.3)   # ~1.3 mm pitch · dense fine mesh
    nt.links.new(tc.outputs["Object"], v.inputs["Vector"])
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].position = 0.10       # small round holes
    ramp.color_ramp.elements[0].color = (0.01, 0.01, 0.012, 1)
    ramp.color_ramp.elements[1].position = 0.24
    ramp.color_ramp.elements[1].color = (0.19, 0.195, 0.205, 1)
    nt.links.new(v.outputs["Distance"], ramp.inputs["Fac"])
    nt.links.new(ramp.outputs["Color"], b.inputs["Base Color"])
    bump = nt.nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.35
    bump.inputs["Distance"].default_value = mm(0.4)
    nt.links.new(v.outputs["Distance"], bump.inputs["Height"])
    nt.links.new(bump.outputs["Normal"], b.inputs["Normal"])
    return m


def foam_flat_material():
    """The DGX Spark foam as a FLAT image-textured PBR material for the glb (the
    Cycles still uses real Voronoi displacement · these baked maps keep the glb
    tiny). Champagne base darkened by the AO map, roughness + normal from the maps
    generated by render/foam_maps.py."""
    import os
    m = bpy.data.materials.new("spark-foam-flat")
    m.use_nodes = True
    nt = m.node_tree
    b = nt.nodes["Principled BSDF"]
    b.inputs["Metallic"].default_value = 1.0
    base = os.path.abspath("web/assets/site/tex")

    def img(name):
        i = bpy.data.images.load(f"{base}/{name}", check_existing=True)
        n = nt.nodes.new("ShaderNodeTexImage")
        n.image = i
        return n

    # base color = champagne tinted by AO (bake the cavity darkening in)
    ao = img("foam-ao.png"); ao.image.colorspace_settings.name = "Non-Color"
    tint = nt.nodes.new("ShaderNodeMixRGB")
    tint.blend_type = "MULTIPLY"; tint.inputs["Fac"].default_value = 1.0
    tint.inputs["Color1"].default_value = (0.55, 0.42, 0.22, 1)
    nt.links.new(ao.outputs["Color"], tint.inputs["Color2"])
    nt.links.new(tint.outputs["Color"], b.inputs["Base Color"])
    # roughness
    rg = img("foam-rough.png"); rg.image.colorspace_settings.name = "Non-Color"
    nt.links.new(rg.outputs["Color"], b.inputs["Roughness"])
    # normal
    nm = img("foam-normal.png"); nm.image.colorspace_settings.name = "Non-Color"
    nmap = nt.nodes.new("ShaderNodeNormalMap")
    nt.links.new(nm.outputs["Color"], nmap.inputs["Color"])
    nt.links.new(nmap.outputs["Normal"], b.inputs["Normal"])
    return m


def champagne_gold(rough=0.28, pore_darken=False):
    m = principled("spark-gold" + ("-foam" if pore_darken else ""),
                   (0.60, 0.47, 0.25), rough)
    if pore_darken:
        nt = m.node_tree
        b = nt.nodes["Principled BSDF"]
        field = foam_field(nt)
        ramp = nt.nodes.new("ShaderNodeValToRGB")
        # The champagne web dominates (reference reads golden, not black); only the
        # deepest pore centers fall dark, and even they stay a dark champagne, not soot.
        ramp.color_ramp.elements[0].position = 0.08
        ramp.color_ramp.elements[0].color = (0.92, 0.74, 0.42, 1)
        ramp.color_ramp.elements[1].position = 0.62
        ramp.color_ramp.elements[1].color = (0.11, 0.08, 0.05, 1)
        nt.links.new(field, ramp.inputs["Fac"])
        nt.links.new(ramp.outputs["Color"], b.inputs["Base Color"])
        rramp = nt.nodes.new("ShaderNodeMapRange")
        rramp.inputs["To Min"].default_value = 0.22
        rramp.inputs["To Max"].default_value = 0.55
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

    gold = champagne_gold(0.52)
    body.data.materials.append(gold)

    # Full-face pocket between the rails: 136 wide x 44.5 tall, 4 mm deep.
    front_y = -mm(150) / 2.0
    fw, fh = mm(124), mm(38.0)
    zc = mm(50.5 / 2.0)
    pocket = cutter_box(fw, mm(24), fh, mm(6), (0, front_y + mm(4.0), zc), seg=12)
    boxes = apply_boolean(body, [pocket])
    backing = principled("spark-pocket", (0.10, 0.085, 0.06), 0.6, metallic=0.4)
    body.data.materials.append(backing)
    assign_interior(body, boxes, 1)
    smooth(body, 40)

    # The foam sheet. EXPORT path (glb): a FLAT low-poly plane with a UV map and the
    # baked foam maps (render/foam_maps.py) · keeps the glb tiny for the live hero.
    # RENDER path (Cycles still): a dense grid carved by real Voronoi displacement.
    if EXPORT:
        bpy.ops.mesh.primitive_grid_add(x_subdivisions=2, y_subdivisions=2,
                                        size=1.0, location=(0, front_y + mm(1.2), zc),
                                        rotation=(math.radians(90), 0, 0))
        foam = bpy.context.active_object
        foam.name = "dgx-spark-foam"
        foam.scale = (fw + mm(3), fh + mm(3), 1.0)
        bpy.ops.object.transform_apply(scale=True)
        bpy.ops.object.select_all(action="DESELECT")
        foam.select_set(True)
        bpy.context.view_layer.objects.active = foam
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.uv.smart_project(angle_limit=1.15)
        bpy.ops.object.mode_set(mode="OBJECT")
        foam.data.materials.append(foam_flat_material())
    else:
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
        # TWO displacement scales for real two-scale foam geometry (checklist): the
        # coarse pores carry the depth, a finer pass at ~1/3 scale adds sub-structure.
        for nm, cell, strength, mid in (("pores", FOAM_CELL, mm(4.2), 0.42),
                                        ("pores-fine", FOAM_FINE, mm(1.2), 0.5)):
            tex = bpy.data.textures.new("foam-voronoi-" + nm, "VORONOI")
            tex.distance_metric = "DISTANCE"
            tex.weight_1 = -1.0
            tex.weight_2 = 1.0
            tex.noise_scale = mm(cell)
            tex.noise_intensity = 1.0
            disp = foam.modifiers.new(nm, "DISPLACE")
            disp.texture = tex
            disp.texture_coords = "LOCAL"
            disp.direction = "Y"
            disp.mid_level = mid
            disp.strength = strength
            bpy.ops.object.modifier_apply(modifier=disp.name)
        foam.data.materials.append(champagne_gold(pore_darken=True))
        smooth(foam, 70)

    # The two smooth tubs, recessed 1.5 mm behind the foam face, blank.
    tubs = []
    for sx in (-1, 1):
        tub = stadium("tub", mm(18), mm(33), mm(3), mm(8.8),
                      (sx * mm(47), front_y + mm(4.0), zc))
        tub.data.materials.append(principled("tub-gold", (0.52, 0.40, 0.21), 0.34))
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


def tabletop_rig(aim_loc=(0.0, 0.0, 0.03)):
    """One soft key high and camera-left, a dim rim strip behind and above to catch
    the top edges, a low fill so the shadow side does not crush. A large matte
    near-black floor (#0a0a0a range) anchors both devices with a real contact
    shadow. All area lights TRACK_TO an aim empty between the two machines."""
    aim = bpy.data.objects.new("Aim", None)
    aim.location = aim_loc
    bpy.context.collection.objects.link(aim)
    # Key: large soft box, high, camera-left-and-front. Energy in watts (metric).
    add_area("Key", (-0.9, -0.7, 1.15), 1.35, 50, (1.0, 0.99, 0.97), aim=aim)
    # Rim: a bright narrow strip behind and above, drawing the top edges.
    add_area("Rim", (0.55, 1.1, 0.95), 0.5, 34, (0.93, 0.96, 1.0), sx=0.06, aim=aim)
    # Fill: very low, camera-right, so the shadow side keeps detail without lifting flat.
    add_area("Fill", (1.15, -0.9, 0.5), 1.1, 7, (0.96, 0.98, 1.0), aim=aim)

    # The desk: a matte near-black plane. #0a0a0a is ~0.0033 linear; a faint
    # roughness gradient keeps the surface from reading as a perfect mirror-mist.
    bpy.ops.mesh.primitive_plane_add(size=4.0, location=(0, 0, 0))
    floor = bpy.context.active_object
    floor.name = "floor"
    fm = principled("desk", (0.0022, 0.0022, 0.0026), 0.62, metallic=0.0)
    nt = fm.node_tree
    b = nt.nodes["Principled BSDF"]
    tc = nt.nodes.new("ShaderNodeTexCoord")
    n = nt.nodes.new("ShaderNodeTexNoise")
    n.inputs["Scale"].default_value = 1.6
    n.inputs["Detail"].default_value = 2.0
    nt.links.new(tc.outputs["Object"], n.inputs["Vector"])
    mr = nt.nodes.new("ShaderNodeMapRange")
    mr.inputs["To Min"].default_value = 0.58
    mr.inputs["To Max"].default_value = 0.74
    nt.links.new(n.outputs["Fac"], mr.inputs["Value"])
    nt.links.new(mr.outputs["Result"], b.inputs["Roughness"])
    floor.data.materials.append(fm)
    return aim


def tabletop_camera(aim, subject_w, lens=58.0, pitch_deg=36.0, margin=1.32,
                    res=(3200, 2000)):
    """Perspective camera at a standing eye line, pitched DOWN onto the desk. The
    down-angle is the whole feeling: you walked up to a table and these two objects
    are on it. Distance is solved from horizontal framing so both devices sit with
    ~margin of breathing room."""
    sc = bpy.context.scene
    cd = bpy.data.cameras.new("cam")
    cd.lens = lens
    cd.sensor_width = 36.0
    cam = bpy.data.objects.new("cam", cd)
    bpy.context.collection.objects.link(cam)
    sc.camera = cam
    sc.render.resolution_x, sc.render.resolution_y = res
    h_fov = 2.0 * math.atan(cd.sensor_width / (2.0 * lens))
    dist = (subject_w * margin) / (2.0 * math.tan(h_fov / 2.0))
    p = math.radians(pitch_deg)
    ax, ay, az = aim.location
    cam.location = (ax, ay - dist * math.cos(p), az + dist * math.sin(p))
    con = cam.constraints.new("TRACK_TO")
    con.target = aim
    con.track_axis = "TRACK_NEGATIVE_Z"
    con.up_axis = "UP_Y"
    return cam


def render_to(path):
    sc = bpy.context.scene
    sc.render.filepath = path
    t0 = time.time()
    bpy.ops.render.render(write_still=True)
    print(f"rendered {path} in {time.time() - t0:.1f}s "
          f"({sc.cycles.samples} samples, {sc.render.resolution_percentage}%)")


def export_gltf(outdir):
    """Draco-compressed .glb of the built scene (both devices, no lights/camera/floor)
    for the live Three.js hero. Materials export as PBR; the foam bakes happen in a
    separate bake pass (Phase 5)."""
    import os
    os.makedirs(outdir, exist_ok=True)
    # keep only the device meshes
    keep = {"mac-studio", "mac-studio-base", "mac-studio-led",
            "dgx-spark", "dgx-spark-foam", "tub", "tub.001"}
    for ob in list(bpy.data.objects):
        if ob.type == "MESH" and ob.name not in keep and not ob.name.startswith(("mac-studio", "dgx-spark", "tub")):
            bpy.data.objects.remove(ob, do_unlink=True)
    bpy.ops.object.select_all(action="SELECT")
    path = outdir.rstrip("/") + "/oracles.glb"
    bpy.ops.export_scene.gltf(
        filepath=path, export_format="GLB", use_selection=True,
        export_draco_mesh_compression_enable=False,
        export_apply=True, export_yup=True)
    print("exported", path)


# ---- scenes --------------------------------------------------------------------------
STUDIO_W, STUDIO_H = mm(197), mm(95)
SPARK_W, SPARK_H = mm(150), mm(50.5)
# On-desk placement: Studio left, Spark right, ~120 mm gap, fronts toward camera,
# each turned a few degrees toward the other so the pair converses on the bench.
STUDIO_CX = -mm(PAIR_SPAN_MM / 2.0 - 197.0 / 2.0)
SPARK_CX = mm(PAIR_SPAN_MM / 2.0 - 150.0 / 2.0)


def build_pair():
    build_mac_studio(STUDIO_CX, yaw_deg=-9.0)
    build_dgx_spark(SPARK_CX, yaw_deg=16.0)


def scene_pair():
    sc = reset_scene()
    enable_gpu(sc)
    build_pair()
    if EXPORT:
        export_gltf(EXPORT)
        return
    aim = tabletop_rig(aim_loc=(0.0, 0.0, mm(28)))
    tabletop_camera(aim, subject_w=mm(PAIR_SPAN_MM), res=(3200, 2000))
    suffix = f"-iter{ITER}" if PREVIEW else ""
    render_to(OUT + f"oracles-pair{suffix}@3x.png")


def scene_solo(which):
    sc = reset_scene()
    enable_gpu(sc)
    if which == "studio":
        build_mac_studio(0.0, yaw_deg=-9.0)
        w = STUDIO_W * 1.5
    else:
        build_dgx_spark(0.0, yaw_deg=9.0)
        w = SPARK_W * 1.5
    aim = tabletop_rig(aim_loc=(0.0, 0.0, mm(24)))
    tabletop_camera(aim, subject_w=w, res=(2048, 2048))
    name = "mac-studio" if which == "studio" else "dgx-spark"
    suffix = f"-iter{ITER}" if PREVIEW else ""
    render_to(OUT + f"{name}{suffix}@3x.png")


# ---- verify mode · orthographic reference-match renders (overlay against press imagery) ----
def verify_rig_front(subject_w, subject_h, res):
    """Neutral even front-elevation light: a flat product-catalogue look so silhouette
    and feature positions read against the reference, not a dramatic hero light."""
    sc = bpy.context.scene
    sc.render.film_transparent = True
    w = bpy.data.worlds.new("dim")
    w.use_nodes = True
    w.node_tree.nodes["Background"].inputs[0].default_value = (0.05, 0.05, 0.055, 1)
    sc.world = w
    sc.view_settings.exposure = -0.1
    aim = bpy.data.objects.new("Aim", None)
    aim.location = (0, 0, subject_h * 0.5)
    bpy.context.collection.objects.link(aim)
    # a soft key high and front (the reference's studio key) + a low fill · directional
    # so metal reads as a champagne gradient, not a flat blown-white reflection
    add_area("vf-key", (-0.4, -1.2, subject_h * 0.5 + 0.9), 1.1, 40, (1.0, 0.99, 0.96), aim=aim)
    add_area("vf-fill", (0.7, -1.3, subject_h * 0.5), 1.4, 8, (0.96, 0.98, 1.0), aim=aim)
    # orthographic front elevation: look along +Y at the -Y face
    cd = bpy.data.cameras.new("vcam")
    cd.type = "ORTHO"
    cd.ortho_scale = max(subject_w, subject_h) * 1.12
    cam = bpy.data.objects.new("vcam", cd)
    tilt = math.radians(8)  # match the reference's slight downward look (top sliver visible)
    cam.location = (0, -2.0, subject_h * 0.5 + 2.0 * math.tan(tilt))
    cam.rotation_euler = (math.radians(90) - tilt, 0, 0)
    bpy.context.collection.objects.link(cam)
    sc.camera = cam
    sc.render.resolution_x, sc.render.resolution_y = res


def verify_device(which):
    sc = reset_scene()
    enable_gpu(sc)
    sc.cycles.samples = 384
    if which == "studio":
        build_mac_studio(0.0, yaw_deg=0.0)
        sw, sh = mm(197), mm(95)
    else:
        build_dgx_spark(0.0, yaw_deg=0.0)
        sw, sh = mm(150), mm(50.5)
    # frame at the reference aspect so the overlay lines up: pad to a fixed height
    res = (900, int(900 * sh / sw)) if sw >= sh else (int(900 * sw / sh), 900)
    verify_rig_front(sw, sh, res)
    name = "mac-studio" if which == "studio" else "dgx-spark"
    render_to("render/verify/" + name + "-front.png")


if VERIFY:
    if VERIFY in ("all", "studio"):
        verify_device("studio")
    if VERIFY in ("all", "spark"):
        verify_device("spark")
    print("verify renders done.")
elif EXPORT:
    scene_pair()  # export path returns early after building + exporting
elif ONLY in ("all", "pair"):
    scene_pair()
if not VERIFY and not EXPORT and ONLY in ("all", "studio"):
    scene_solo("studio")
if not VERIFY and not EXPORT and ONLY in ("all", "spark"):
    scene_solo("spark")
print("build_scene done.")
