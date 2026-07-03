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
TURN = arg("--turnaround", None) # "studio" | "spark" · Gate 1 wireframe+shaded turntable
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


# ---- measured constants · every value traces to a render/MEASUREMENTS.md row ----------
# Spec-anchor doctrine: spec supplies absolute dimensions; the reference images supply the
# radii, feature sizes and positions. INFERRED values are flagged; nothing is guessed.
STUDIO = {
    "width":        197.0,   # front_width_anchor (Apple spec)
    "depth":        197.0,   # spec (square footprint)
    "height":        95.0,   # front_height_spec
    "corner_R":      31.4,   # plan_corner_R · footprint / vertical-edge radius
    "top_fillet_R":   8.27,  # top_edge_fillet_R (MEASURED target for the rendered corner)
    "top_fillet_build": 8.9, # builder knob, tuned so the RENDERED front corner = 8.3 above
    "intake_band":    8.55,  # intake_band_height · perforated hex mesh on the bottom fillet
    "reveal_gap":     2.5,   # base_reveal_gap · INFERRED design parameter (not measured)
    "usbc_w":         2.62,  # usbc_short_axis_horiz
    "usbc_h":         8.47,  # usbc_long_axis_vert · VERTICAL (settled, two photographers)
    "usbc_left_x":  -66.16,  # usbc_left_x_from_center
    "usbc_right_x": -51.36,  # usbc_right_x_from_center
    "sd_w":          26.85,  # sd_slot_width · horizontal
    "sd_h":           2.50,  # sd_slot_height
    "sd_x":         -24.41,  # sd_center_x_from_center
    "led_x":         87.70,  # led_x_from_center
    "led_d":          2.94,  # led_diameter (approx, glow-inclusive)
    "port_row_z":    24.36,  # port_row_center_from_base (above the device bottom)
    "led_z":         27.50,  # led_from_base
}

SPARK = {
    "width":       150.0,   # front_long_anchor · the 150mm long (x) axis of the front strip
    "depth":       150.0,   # top_depth_spec (y)
    "height":       50.5,   # front_short_edge_spec · the 50.5mm short (z) axis · a 3:1 STRIP
    "edge_R":        6.09,  # front_edge_R_mean · crisp front-face edge fillet
    "pill_long":    31.41,  # pill along the 50.5 short/depth (z) axis
    "pill_short":   12.96,  # pill along the 150 long (x) axis
    "pill_pitch":  112.90,  # center-to-center along the 150 x-axis (pills at +/- pitch/2)
    "foam_field_long":  148.02,  # foam extent along x
    "foam_field_short":  46.34,  # foam extent along z
    "foam_lip":      2.53,  # champagne lip on the long (top/bottom) edges
    "foam_cell_cm": 13.75,  # foam_cells_per_cm mean (13.0 / 14.5) -> ~0.73mm cell pitch
    "top_panel_w": 114.15,  # recessed top vent panel
    "top_panel_h": 105.06,
    "top_panel_inset": 17.92,
    "champ_Lab":  (72.52, 7.78, 42.78),   # champagne shell target
    "foam_Lab":   (38.09, 1.19, 20.50),   # foam mean target (darker gold)
}

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
    m = principled("mac-blasted-alu", (0.86, 0.87, 0.89), 0.38, coat=0.0)
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


# TWO overlapping scales (the reference is coarse open pores ~7/cm + finer sub-structure
# ~14/cm; the phase-0 13-14.5/cm was the FINE scale, its coarse partner is ~1.45mm). The
# clean-bg sth_front-1 remeasure gives ~6.5/cm coarse. Displacement must stay UNDER the cell
# pitch or adjacent pores overlap into gravel.
FOAM_CELL = 20.0 / SPARK["foam_cell_cm"]   # coarse pore pitch ~= 1.45 mm (~7/cm)
FOAM_FINE = 10.0 / SPARK["foam_cell_cm"]   # fine sub-structure ~= 0.73 mm (~14/cm)


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
    fine_scaled.inputs[1].default_value = 0.22   # coarse ~1.5mm cells dominate the read
    nt.links.new(fine, fine_scaled.inputs[0])
    add = nt.nodes.new("ShaderNodeMath")
    add.operation = "ADD"
    add.use_clamp = True
    nt.links.new(coarse, add.inputs[0])
    nt.links.new(fine_scaled.outputs["Value"], add.inputs[1])
    return add.outputs["Value"]


def perforated_band():
    """The Mac Studio base intake: a REGULAR hex-packed array of round holes (the reference is
    a machined perforation, not a random cell field · rider 1). Object x-z grid, horizontal
    pitch ph, row pitch ph*sqrt(3)/2, alternate rows offset half a pitch; the pattern
    foreshortens naturally as the bottom fillet curves under. The web is bright bead-blast
    aluminium, the pits dark; tone stays L*~52 to match the reference."""
    m = principled("mac-base-band", (0.6, 0.61, 0.63), 0.5)
    nt = m.node_tree
    b = nt.nodes["Principled BSDF"]
    tc = nt.nodes.new("ShaderNodeTexCoord")
    sep = nt.nodes.new("ShaderNodeSeparateXYZ")
    nt.links.new(tc.outputs["Object"], sep.inputs["Vector"])

    def mk(op, a, bb=None, clamp=False):
        n = nt.nodes.new("ShaderNodeMath"); n.operation = op; n.use_clamp = clamp
        def put(i, val):
            if hasattr(val, "is_linked"):
                nt.links.new(val, n.inputs[i])
            else:
                n.inputs[i].default_value = val
        put(0, a)
        if bb is not None: put(1, bb)
        return n.outputs[0]

    ph = mm(1.70); pv = ph * 0.866
    u = mk("MULTIPLY", sep.outputs["X"], 1.0 / ph)
    v = mk("MULTIPLY", sep.outputs["Z"], 1.0 / pv)
    rowmod = mk("MODULO", mk("FLOOR", v), 2.0)          # 0 or 1 per row
    ushift = mk("ADD", u, mk("MULTIPLY", rowmod, 0.5))
    cu = mk("SUBTRACT", mk("FRACT", ushift), 0.5)
    cv = mk("SUBTRACT", mk("FRACT", v), 0.5)
    dist = mk("SQRT", mk("ADD", mk("MULTIPLY", cu, cu), mk("MULTIPLY", cv, cv)))
    # hole mask: 1 inside a round hole, 0 on the web, soft edge (r_norm sets the diameter)
    hole = nt.nodes.new("ShaderNodeMapRange")
    hole.inputs["From Min"].default_value = 0.45       # web
    hole.inputs["From Max"].default_value = 0.39       # hole
    hole.inputs["To Min"].default_value = 0.0
    hole.inputs["To Max"].default_value = 1.0
    hole.clamp = True
    nt.links.new(dist, hole.inputs["Value"])
    mix = nt.nodes.new("ShaderNodeMixRGB")
    mix.inputs["Color1"].default_value = (0.74, 0.75, 0.77, 1)   # bright bead-blast web
    mix.inputs["Color2"].default_value = (0.16, 0.16, 0.17, 1)   # pit floor: dark grey, lit, not soot
    nt.links.new(hole.outputs["Result"], mix.inputs["Fac"])
    nt.links.new(mix.outputs["Color"], b.inputs["Base Color"])
    bump = nt.nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.28
    bump.inputs["Distance"].default_value = mm(0.4)
    nt.links.new(mk("SUBTRACT", 1.0, hole.outputs["Result"]), bump.inputs["Height"])
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
    tint.inputs["Color1"].default_value = (0.72, 0.56, 0.30, 1)
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
    # Anodized champagne is a COLOURED oxide, not a pure mirror · metallic ~0.5 so the gold
    # diffuse shows (b*~40), otherwise a neutral key reflects off pure metal and reads cream.
    m = principled("spark-gold" + ("-foam" if pore_darken else ""),
                   (0.68, 0.55, 0.29), rough, metallic=0.28)  # muted warm titanium, not jewelry (doctrine)
    if pore_darken:
        nt = m.node_tree
        b = nt.nodes["Principled BSDF"]
        tc = nt.nodes.new("ShaderNodeTexCoord")
        coarse = _voronoi_ridge(nt, tc, FOAM_CELL)   # 0 on the strut web, ~0.5 at pore centers
        fine = _voronoi_ridge(nt, tc, FOAM_FINE)

        def mr(val, fmn, fmx, tmn, tmx, clamp=True):
            n = nt.nodes.new("ShaderNodeMapRange"); n.clamp = clamp
            n.inputs["From Min"].default_value = fmn; n.inputs["From Max"].default_value = fmx
            n.inputs["To Min"].default_value = tmn; n.inputs["To Max"].default_value = tmx
            nt.links.new(val, n.inputs["Value"]); return n.outputs["Result"]

        # web mask driven by real mesh CURVATURE (Pointiness) of the displaced foam: the convex
        # strut tops read bright gold, the concave pore floors dark · this keys colour to the
        # actual geometry (the Voronoi field alone did not produce the open-cell contrast). A
        # touch of the coarse Voronoi web keeps the struts continuous where curvature is flat.
        geo = nt.nodes.new("ShaderNodeNewGeometry")
        wcurv = mr(geo.outputs["Pointiness"], 0.50, 0.555, 0.0, 1.0)   # narrow bright web (pore-dominant)
        wcoarse = mr(coarse, 0.16, 0.05, 0.0, 0.22)                    # light secondary web
        web = nt.nodes.new("ShaderNodeMath"); web.operation = "MAXIMUM"
        nt.links.new(wcurv, web.inputs[0]); nt.links.new(wcoarse, web.inputs[1])
        webmask = web.outputs[0]

        mixc = nt.nodes.new("ShaderNodeMixRGB")
        # pinned to sth_front-1 clean foam (mean L47): the web/pore split showed pores +11 L too
        # bright (ref 16, ren 27) and web +6 (ref 74, ren 79) · darken both, pores hardest.
        mixc.inputs["Color1"].default_value = (0.012, 0.008, 0.004, 1)  # near-black open pore
        mixc.inputs["Color2"].default_value = (0.585, 0.44, 0.155, 1)   # gold strut, darkened to pull the mean to target
        nt.links.new(webmask, mixc.inputs["Fac"])
        # AO carries the pore depth the shallow displacement cannot: deep cavity self-shadow
        ao = nt.nodes.new("ShaderNodeAmbientOcclusion"); ao.inputs["Distance"].default_value = mm(1.3)
        aomix = nt.nodes.new("ShaderNodeMixRGB"); aomix.blend_type = "MULTIPLY"
        aomix.inputs["Fac"].default_value = 0.85
        nt.links.new(mixc.outputs["Color"], aomix.inputs["Color1"])
        nt.links.new(ao.outputs["Color"], aomix.inputs["Color2"])
        nt.links.new(aomix.outputs["Color"], b.inputs["Base Color"])
        # struts glossier (catch the gold), pores matte
        nt.links.new(mr(webmask, 0.0, 1.0, 0.50, 0.22), b.inputs["Roughness"])
    return m


# ---- the devices ---------------------------------------------------------------------
def build_mac_studio(loc_x=0.0, yaw_deg=0.0):
    """197 x 197 x 95 mm · every dimension from STUDIO (traces to MEASUREMENTS.md).
    Racetrack body: footprint corner 31.4 mm, a TIGHT 8.27 mm top-edge fillet (top dead-flat,
    sides near-vertical), an 8.55 mm perforated intake band on the bottom fillet, floating on a
    2.5 mm reveal gap over a circular foot. Front row from the measured x/z: two VERTICAL USB-C
    pockets (2.62 x 8.47) with dark interiors and a centered tongue blade, a horizontal beveled
    SD slot (26.85 x 2.50), and a dark power LED dot (no emission). Rear field skipped."""
    # apple_front is a floating product shot: bottom = the intake mesh curving under, no
    # foot. The 2.5 mm reveal + foot is a tabletop feature (added in the phase-4 portrait),
    # so the default body floats at z=0 with the intake band on its bottom fillet.
    W = mm(STUDIO["width"]); D = mm(STUDIO["depth"]); Htot = mm(STUDIO["height"])
    intake = mm(STUDIO["intake_band"])
    body = rounded_box("mac-studio", W, D, Htot,
                       mm(STUDIO["corner_R"]), mm(STUDIO["top_fillet_build"]), intake,
                       seg_corner=48, seg_fillet=12)
    body.location = (0, 0, 0)
    bpy.context.view_layer.update()

    alu = blasted_aluminum()
    cavity = port_plastic()
    body.data.materials.append(alu)                 # 0
    body.data.materials.append(cavity)              # 1
    body.data.materials.append(perforated_band())   # 2

    front_y = -D / 2.0
    POCKET = mm(4.2); DCUT = mm(10.0)
    yc = front_y + POCKET - DCUT / 2.0
    pz = mm(STUDIO["port_row_z"])                    # port row height above the ground
    usbc = [("usbc-l", STUDIO["usbc_left_x"]), ("usbc-r", STUDIO["usbc_right_x"])]
    cutters = []
    for _, x in usbc:                                # VERTICAL slots: w x h = 2.62 x 8.47
        cutters.append(cutter_box(mm(STUDIO["usbc_w"]), DCUT, mm(STUDIO["usbc_h"]), mm(1.1),
                                  (mm(x), yc, pz), seg=10))
    cutters.append(cutter_box(mm(STUDIO["sd_w"]), DCUT, mm(STUDIO["sd_h"]), mm(0.9),
                              (mm(STUDIO["sd_x"]), yc, pz), seg=8))   # horizontal beveled slot
    boxes = apply_boolean(body, cutters)
    assign_interior(body, boxes, 1, ymin=front_y + mm(0.3))
    # intake band = the lower `intake` mm (bottom fillet) carries the perforated mesh
    for poly in body.data.polygons:
        if (body.matrix_world @ poly.center).z < intake + mm(0.3):
            poly.material_index = 2
    smooth(body, 40)

    # USB-C tongue blades: a slim VERTICAL blade centered + recessed in each pocket
    tongues = []
    for _, x in usbc:
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=(mm(x), front_y + mm(3.0), pz))
        t = bpy.context.active_object; t.name = "usbc-tongue"
        t.scale = (mm(1.0) / 2, mm(2.2) / 2, mm(5.6) / 2)
        bpy.ops.object.transform_apply(scale=True)
        t.data.materials.append(cavity)
        tongues.append(t)

    # power LED: a small darker glass dot, emission OFF (doctrine: nothing glows)
    bpy.ops.mesh.primitive_cylinder_add(radius=mm(STUDIO["led_d"] / 2.0), depth=mm(0.5), vertices=24,
                                        rotation=(math.radians(90), 0, 0),
                                        location=(mm(STUDIO["led_x"]), front_y + mm(0.05), mm(STUDIO["led_z"])))
    led = bpy.context.active_object; led.name = "mac-studio-led"
    led.data.materials.append(led_glass()); smooth(led, 60)

    group = [body, led] + tongues
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
    """150 x 150 x 50.5 mm, sitting flat · every value from SPARK (traces to MEASUREMENTS.md).
    The FRONT is the 150 x 50.5 face, a ~3:1 STRIP (not a square): a two-scale open-cell metal
    foam field (148 x 46) framed by thin ~2.5 mm champagne lips, with two recessed champagne
    pill hand-holds (12.96 wide x 31.4 tall, arrayed along the 150 axis) that have real
    inner-wall depth. Champagne anodized shell; smooth sides; the top (150 x 150) carries a
    recessed vent panel. Rear foam/ports skipped. Trademark gate: pills stay blank."""
    W = mm(SPARK["width"]); D = mm(SPARK["depth"]); H = mm(SPARK["height"])
    r = mm(SPARK["edge_R"])
    body = rounded_box("dgx-spark", W, D, H, r, r, r, seg_corner=24, seg_fillet=8)
    bpy.context.view_layer.update()
    body.data.materials.append(champagne_gold(0.50))                       # 0 shell
    body.data.materials.append(principled("spark-pill-wall", (0.40, 0.27, 0.085), 0.72, metallic=0.1))  # 1 inner wall

    front_y = -D / 2.0
    zc = H / 2.0
    px = mm(SPARK["pill_pitch"]) / 2.0        # pills symmetric at +/- pitch/2 along x
    pw = mm(SPARK["pill_short"]); phz = mm(SPARK["pill_long"])

    # recessed pill pockets with real inner-wall depth (stadium prisms cut into the front)
    # stadium() places its front edge at loc.y+DCUT/2 and extends back in -y, so to cut a
    # POCK-deep pocket into the body (front face at front_y) the front edge sits at front_y+POCK.
    POCK = mm(4.2); DCUT = mm(10.0)
    cutters = []
    for sx in (-1, 1):
        cutters.append(stadium("pillcut", pw, phz, DCUT, pw / 2.0,
                               (sx * px, front_y + POCK - DCUT / 2.0, zc)))
    boxes = apply_boolean(body, cutters)
    assign_interior(body, boxes, 1, ymin=front_y + mm(0.3))
    smooth(body, 40)

    # top recessed vent panel (seen in 3/4 + top): a shallow rounded-rect pocket in the top face
    tp = cutter_box(mm(SPARK["top_panel_w"]), mm(SPARK["top_panel_h"]), mm(3.0), mm(12),
                    (0, 0, H + mm(1.5) - mm(3.0) / 2.0), seg=16)
    # (cut from the top; cutter_box origin logic handles the z placement)
    tbox = apply_boolean(body, [tp])
    assign_interior(body, tbox, 1)

    # champagne tub floors sitting recessed at the back of each pocket, blank
    tubs = []
    for sx in (-1, 1):
        tub = stadium("tub", pw - mm(1.4), phz - mm(1.4), mm(1.4), (pw - mm(1.4)) / 2.0,
                      (sx * px, front_y + POCK - mm(1.6), zc))
        tub.data.materials.append(principled("spark-tub", (0.46, 0.32, 0.11), 0.6, metallic=0.2))
        smooth(tub, 50); tubs.append(tub)

    # the open-cell foam field (148 x 46), two-scale Voronoi displacement, pill holes.
    ffx = mm(SPARK["foam_field_long"]); ffz = mm(SPARK["foam_field_short"])
    if EXPORT:
        bpy.ops.mesh.primitive_grid_add(x_subdivisions=2, y_subdivisions=2, size=1.0,
                                        location=(0, front_y - mm(0.4), zc),
                                        rotation=(math.radians(90), 0, 0))
        foam = bpy.context.active_object; foam.name = "dgx-spark-foam"
        foam.scale = (ffx, ffz, 1.0); bpy.ops.object.transform_apply(scale=True)
        foam.data.materials.append(foam_flat_material())
    else:
        bpy.ops.mesh.primitive_grid_add(x_subdivisions=820, y_subdivisions=280, size=1.0,
                                        location=(0, front_y - mm(0.4), zc),
                                        rotation=(math.radians(90), 0, 0))
        foam = bpy.context.active_object; foam.name = "dgx-spark-foam"
        foam.scale = (ffx, ffz, 1.0); bpy.ops.object.transform_apply(scale=True)
        bpy.context.view_layer.objects.active = foam
        for sx in (-1, 1):                       # holes where the pills are
            h = stadium("fhole", pw + mm(1.0), phz + mm(1.0), mm(20), (pw + mm(1.0)) / 2.0,
                        (sx * px, front_y - mm(9), zc))
            md = foam.modifiers.new("hole", "BOOLEAN"); md.operation = "DIFFERENCE"
            md.solver = "EXACT"; md.object = h
            bpy.ops.object.modifier_apply(modifier=md.name)
            bpy.data.objects.remove(h, do_unlink=True)
        # coarse displacement only · the ~1.5mm pores must read distinct, not sandpaper. The
        # fine sub-structure lives in the shader (color/roughness), not the geometry.
        for nm, cell, strength, mid in (("pores", FOAM_CELL, mm(1.4), 0.42),):
            tex = bpy.data.textures.new("foam-voronoi-" + nm, "VORONOI")
            tex.distance_metric = "DISTANCE"; tex.weight_1 = -1.0; tex.weight_2 = 1.0
            tex.noise_scale = mm(cell); tex.noise_intensity = 1.0
            disp = foam.modifiers.new(nm, "DISPLACE")
            disp.texture = tex; disp.texture_coords = "LOCAL"; disp.direction = "Y"
            disp.mid_level = mid; disp.strength = strength
            bpy.ops.object.modifier_apply(modifier=disp.name)
        foam.data.materials.append(champagne_gold(pore_darken=True))
        smooth(foam, 70)

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
    # Key: large soft box, high, camera-left-and-front. Energy in watts (metric). Softened
    # + trimmed so the bright bead-blast aluminium top does not blow a specular highlight
    # (clipcheck: device pixels >=0.98 must stay under 1%).
    add_area("Key", (-0.9, -0.7, 1.15), 1.9, 30, (1.0, 0.99, 0.97), aim=aim)
    # Rim: a bright narrow strip behind and above, drawing the top edges.
    add_area("Rim", (0.55, 1.1, 0.95), 0.5, 22, (0.93, 0.96, 1.0), sx=0.06, aim=aim)
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


# ---- Gate 1 · flat-lit wireframe-plus-shaded turnaround (proves the geometry is real) ----
def flat_turn_rig():
    sc = bpy.context.scene
    sc.render.film_transparent = True
    w = bpy.data.worlds.new("flat"); w.use_nodes = True
    w.node_tree.nodes["Background"].inputs[0].default_value = (0.32, 0.32, 0.34, 1)
    sc.world = w
    sc.view_settings.look = "None"
    sc.view_settings.exposure = 0.0
    aim = bpy.data.objects.new("Aim", None); aim.location = (0, 0, mm(47.5))
    bpy.context.collection.objects.link(aim)
    add_area("t-key", (-0.7, -0.9, 0.9), 1.8, 24, (1, 1, 1), aim=aim)
    add_area("t-fill", (0.9, -0.7, 0.5), 1.8, 18, (1, 1, 1), aim=aim)
    add_area("t-top", (0.0, 0.3, 1.3), 1.8, 16, (1, 1, 1), aim=aim)
    return aim

def wireframe_overlay(objs, thickness=0.28):
    """Duplicate the shaded meshes and turn their edges into thin dark tubes (Wireframe
    modifier) so the render shows the real tessellation over the shaded surface."""
    wm = bpy.data.materials.new("wire"); wm.use_nodes = True
    bsdf = wm.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (0.02, 0.02, 0.025, 1)
    try: bsdf.inputs["Emission Color"].default_value = (0.02, 0.02, 0.025, 1)
    except KeyError: pass
    for ob in objs:
        if ob.type != "MESH": continue
        dup = ob.copy(); dup.data = ob.data.copy(); dup.name = ob.name + "-wire"
        bpy.context.collection.objects.link(dup)
        m = dup.modifiers.new("wf", "WIREFRAME"); m.thickness = mm(thickness); m.use_replace = True
        dup.data.materials.clear(); dup.data.materials.append(wm)

def orbit_camera(aim, size, yaw_deg, elev_deg, res):
    sc = bpy.context.scene
    cd = bpy.data.cameras.new("tcam"); cd.type = "ORTHO"; cd.ortho_scale = size * 1.22
    cam = bpy.data.objects.new("tcam", cd); bpy.context.collection.objects.link(cam); sc.camera = cam
    ya = math.radians(yaw_deg); el = math.radians(elev_deg); R = 1.2
    ax, ay, az = aim.location
    cam.location = (ax + R * math.cos(el) * math.sin(ya),
                    ay - R * math.cos(el) * math.cos(ya),
                    az + R * math.sin(el))
    con = cam.constraints.new("TRACK_TO"); con.target = aim
    con.track_axis = "TRACK_NEGATIVE_Z"; con.up_axis = "UP_Y"

def turnaround_device(which):
    sc = reset_scene(); enable_gpu(sc); sc.cycles.samples = 256
    if which == "studio":
        objs = build_mac_studio(0.0, yaw_deg=0.0); size = mm(210)
    else:
        objs = build_dgx_spark(0.0, yaw_deg=0.0); size = mm(165)
    wireframe_overlay(objs)
    aim = flat_turn_rig()
    name = "mac-studio" if which == "studio" else "dgx-spark"
    for tag, yaw in (("front", 0), ("q34", 40), ("side", 90), ("rear34", 140)):
        orbit_camera(aim, size, yaw, elev_deg=16, res=(1100, 1100))
        render_to(f"render/verify/turn-{name}-{tag}.png")

# ---- verify mode · orthographic reference-match renders (overlay against press imagery) ----
def verify_rig_front(subject_w, subject_h, res, bright=False):
    """Neutral even front-elevation light: a flat product-catalogue look so silhouette
    and feature positions read against the reference, not a dramatic hero light."""
    sc = bpy.context.scene
    sc.render.film_transparent = True
    w = bpy.data.worlds.new("vfworld")
    w.use_nodes = True
    # bright high-key white surround (Apple product studio) for aluminium so it reads
    # as bright silver · dim directional (StorageReview desk) for the Spark champagne
    w.node_tree.nodes["Background"].inputs[0].default_value = (0.34, 0.34, 0.36, 1) if bright else (0.15, 0.14, 0.14, 1)
    sc.world = w
    # bright verify exposure trimmed so the rendered aluminium mid-face reads L*~84 (Apple's
    # studio tone). The Spark (non-bright) is a brighter directional gold studio (sth_front-1):
    # the key rakes the foam so struts catch gold + pores self-shadow; champagne reads ~L72.
    sc.view_settings.exposure = -1.5 if bright else -0.35
    aim = bpy.data.objects.new("Aim", None)
    aim.location = (0, 0, subject_h * 0.5)
    bpy.context.collection.objects.link(aim)
    if bright:
        add_area("vf-key", (-0.4, -1.3, subject_h * 0.5 + 0.6), 2.0, 30, (1, 1, 1), aim=aim)
        add_area("vf-fill", (0.7, -1.3, subject_h * 0.5), 2.0, 22, (1, 1, 1), aim=aim)
    else:
        add_area("vf-key", (-0.45, -1.15, subject_h * 0.5 + 0.7), 1.5, 78, (1.0, 0.98, 0.93), aim=aim)
        add_area("vf-fill", (0.7, -1.3, subject_h * 0.5), 1.9, 22, (1.0, 0.99, 0.97), aim=aim)
    # orthographic front elevation: look along +Y at the -Y face
    cd = bpy.data.cameras.new("vcam")
    cd.type = "ORTHO"
    cd.ortho_scale = max(subject_w, subject_h) * 1.12
    cam = bpy.data.objects.new("vcam", cd)
    # apple_front is a LEVEL front elevation (its +1.3% height is the intake band, not tilt).
    # On a 197 mm-deep body even 1.5 deg projects ~5 mm of false height, so the verify cam is
    # exactly level · the top fillet reads as an edge, no top face.
    tilt = math.radians(0.0)
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
    verify_rig_front(sw, sh, res, bright=(which == "studio"))
    name = "mac-studio" if which == "studio" else "dgx-spark"
    render_to("render/verify/" + name + "-front.png")


if TURN:
    turnaround_device("studio" if TURN in ("all", "studio") else "spark")
    if TURN == "all":
        turnaround_device("spark")
    print("turnaround renders done.")
elif VERIFY:
    if VERIFY in ("all", "studio"):
        verify_device("studio")
    if VERIFY in ("all", "spark"):
        verify_device("spark")
    print("verify renders done.")
elif EXPORT:
    scene_pair()  # export path returns early after building + exporting
elif ONLY in ("all", "pair"):
    scene_pair()
if not TURN and not VERIFY and not EXPORT and ONLY in ("all", "studio"):
    scene_solo("studio")
if not TURN and not VERIFY and not EXPORT and ONLY in ("all", "spark"):
    scene_solo("spark")
print("build_scene done.")
