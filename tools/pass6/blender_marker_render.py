#!/usr/bin/env python3
"""Create an actual Blender-rendered marker scene and extract rasterized Object Index centroids.

Run:
  blender -b -P .claude/pass6/camera_roundtrip_blender.py -- --out-dir review/pass6/evidence/camera
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
import bpy
from mathutils import Vector

argv=sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else []
ap=argparse.ArgumentParser(); ap.add_argument('--out-dir',required=True); ns=ap.parse_args(argv)
out=Path(ns.out_dir); out.mkdir(parents=True,exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True)
scene=bpy.context.scene
scene.render.engine='BLENDER_EEVEE_NEXT'
scene.render.resolution_x=1280; scene.render.resolution_y=960; scene.render.resolution_percentage=100
scene.render.pixel_aspect_x=1.0; scene.render.pixel_aspect_y=1.0
scene.render.image_settings.file_format='PNG'; scene.render.filepath=str(out/'marker_render.png')
scene.view_layers[0].use_pass_object_index=True
scene.world.color=(0.005,0.005,0.005)
try:
    scene.view_settings.view_transform='Standard'; scene.view_settings.look='None'; scene.view_settings.exposure=0; scene.view_settings.gamma=1
except Exception: pass

cam_data=bpy.data.cameras.new('PASS6_IntegrationCamera')
cam_data.type='PERSP'; cam_data.lens=52.0; cam_data.sensor_width=36.0; cam_data.sensor_fit='HORIZONTAL'; cam_data.shift_x=0.0; cam_data.shift_y=0.0
cam=bpy.data.objects.new('PASS6_IntegrationCamera',cam_data); bpy.context.collection.objects.link(cam); scene.camera=cam
cam.location=(0.42,-0.72,0.34)
target=Vector((0.0,0.0,0.25)); cam.rotation_euler=(target-cam.location).to_track_quat('-Z','Y').to_euler()
points=[(-.16,-.04,-.06),(-.08,.02,-.04),(0,.06,-.02),(.08,.02,0),(.16,-.04,.02),(-.15,.03,.08),(-.07,-.03,.10),(.01,.04,.12),(.09,-.02,.14),(.16,.03,.16),(-.10,.00,.18),(0.02,-.04,.21)]
for i,p in enumerate(points,1):
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=2,radius=.0065,location=p)
    ob=bpy.context.object; ob.name=f'PASS6_MARKER_{i:02d}'; ob.pass_index=i
    mat=bpy.data.materials.new(ob.name+'_MAT'); mat.diffuse_color=(1,1,1,1); mat.use_nodes=True
    bs=mat.node_tree.nodes.get('Principled BSDF')
    if bs:
        bs.inputs['Base Color'].default_value=(1,1,1,1)
        if 'Emission Color' in bs.inputs: bs.inputs['Emission Color'].default_value=(1,1,1,1)
        if 'Emission Strength' in bs.inputs: bs.inputs['Emission Strength'].default_value=2
    ob.data.materials.append(mat)

bpy.ops.render.render(write_still=True)
rr=bpy.data.images.get('Render Result')
if rr is None: raise RuntimeError('Render Result missing')
pass_data=None
for getter in (
    lambda: rr.view_layers[0].passes['IndexOB'].rect,
    lambda: rr.layers[0].passes['IndexOB'].rect,
):
    try: pass_data=getter(); break
    except Exception: pass
if pass_data is None: raise RuntimeError('Could not access rasterized IndexOB pass')
w,h=scene.render.resolution_x,scene.render.resolution_y
vals=list(pass_data); channels=4 if len(vals)==w*h*4 else 1
centroids={}; counts={}
for idx in range(1,len(points)+1):
    sx=sy=n=0.0
    for y in range(h):
        row=y*w
        for x in range(w):
            value=vals[(row+x)*channels]
            if abs(value-idx)<0.25:
                sx+=x+0.5; sy+=(h-1-y)+0.5; n+=1
    if n==0: raise RuntimeError(f'No raster pixels found for marker {idx}')
    centroids[str(idx)]=[sx/n,sy/n]; counts[str(idx)]=int(n)
meta={
  'schema_version':2,
  'pass':True,
  'image':'marker_render.png',
  'resolution_px':[w,h],
  'render_percentage':scene.render.resolution_percentage,
  'pixel_aspect':[scene.render.pixel_aspect_x,scene.render.pixel_aspect_y],
  'camera':{
    'matrix_world':[list(row) for row in cam.matrix_world],
    'lens_mm':cam_data.lens,'sensor_width_mm':cam_data.sensor_width,'sensor_fit':cam_data.sensor_fit,
    'shift_x':cam_data.shift_x,'shift_y':cam_data.shift_y,'type':cam_data.type
  },
  'markers_world':{str(i+1):list(p) for i,p in enumerate(points)},
  'actual_pixel_centroids_top_left':centroids,
  'raster_pixel_counts':counts
}
(out/'marker_scene.json').write_text(json.dumps(meta,indent=2)+'\n',encoding='utf-8')
print(out/'marker_scene.json')
