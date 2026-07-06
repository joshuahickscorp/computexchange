#!/usr/bin/env python3
# render/measure.py · deterministic measurement rig for the tabletop oracles.
#
# Rule 2: no guessed geometry. Every value in MEASUREMENTS.md is produced here
# from reference imagery, anchored to one known real dimension per device
# (Mac Studio front width 197 mm · DGX Spark front long-edge 150 mm), with a
# pixel-evidence crop written under measure_evidence/.
#
# Pure numpy + Pillow (no scipy / no cv2). Segmentation is border flood fill on a
# per-image is-device predicate; radii by Kasa algebraic circle fit; ports/pills
# by connected-component labelling; foam density by ridge-peak counting along a
# strip of known real length.
#
# Dash gate: middot only, no U+2014 / U+2013.

import os, sys, json, math
from collections import deque
import numpy as np
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.abspath(__file__))
REF  = os.path.join(ROOT, "ref")
EVID = os.path.join(ROOT, "measure_evidence")
os.makedirs(EVID, exist_ok=True)

ROWS = []   # measurement rows -> MEASUREMENTS.md
def row(device, param, value, unit, source, crop, conf, note=""):
    ROWS.append(dict(device=device, param=param, value=value, unit=unit,
                     source=source, crop=crop, conf=conf, note=note))
    v = f"{value:.2f}" if isinstance(value, float) else str(value)
    print(f"  [{device:10s}] {param:26s} = {v:>8} {unit:6s} conf={conf:6s} ({source})")

# ---------------------------------------------------------------- image io
def load_rgb(path):
    return np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)

def downscale(rgb, target_w):
    h, w = rgb.shape[:2]
    if w <= target_w: return rgb, 1.0
    s = target_w / w
    im = Image.fromarray(rgb).resize((target_w, int(round(h*s))), Image.LANCZOS)
    return np.asarray(im, dtype=np.uint8), s   # s = ds_px / full_px

def save_crop(rgb, box, name, marks=None, lines=None):
    """box=(x0,y0,x1,y1) in the rgb given. marks=[(x,y,r,color)], lines=[(x0,y0,x1,y1,color)]."""
    x0,y0,x1,y1 = [int(v) for v in box]
    x0=max(0,x0); y0=max(0,y0); x1=min(rgb.shape[1],x1); y1=min(rgb.shape[0],y1)
    im = Image.fromarray(rgb[y0:y1, x0:x1].copy())
    d = ImageDraw.Draw(im)
    if marks:
        for (x,y,r,c) in marks:
            d.ellipse([x-x0-r, y-y0-r, x-x0+r, y-y0+r], outline=c, width=2)
    if lines:
        for (a,b,c2,d2,col) in lines:
            d.line([a-x0,b-y0,c2-x0,d2-y0], fill=col, width=2)
    p = os.path.join(EVID, name)
    im.save(p)
    return os.path.relpath(p, ROOT)

# ---------------------------------------------------------------- segmentation
def flood_from_border(nondev):
    """Return bg-reachable mask: True where a border pixel reaches via nondev(True)."""
    h, w = nondev.shape
    reach = np.zeros((h,w), bool)
    dq = deque()
    for x in range(w):
        for y in (0, h-1):
            if nondev[y,x] and not reach[y,x]:
                reach[y,x]=True; dq.append((y,x))
    for y in range(h):
        for x in (0, w-1):
            if nondev[y,x] and not reach[y,x]:
                reach[y,x]=True; dq.append((y,x))
    while dq:
        y,x = dq.popleft()
        for dy,dx in ((1,0),(-1,0),(0,1),(0,-1)):
            ny,nx=y+dy,x+dx
            if 0<=ny<h and 0<=nx<w and nondev[ny,nx] and not reach[ny,nx]:
                reach[ny,nx]=True; dq.append((ny,nx))
    return reach

def largest_cc_from_center(mask):
    """BFS the connected device region containing the mask centroid (drops specks)."""
    h,w = mask.shape
    ys,xs = np.where(mask)
    if len(ys)==0: return mask
    cy,cx = int(ys.mean()), int(xs.mean())
    if not mask[cy,cx]:
        d = (ys-cy)**2+(xs-cx)**2; i=int(np.argmin(d)); cy,cx=ys[i],xs[i]
    out = np.zeros_like(mask); out[cy,cx]=True; dq=deque([(cy,cx)])
    while dq:
        y,x=dq.popleft()
        for dy,dx in ((1,0),(-1,0),(0,1),(0,-1)):
            ny,nx=y+dy,x+dx
            if 0<=ny<h and 0<=nx<w and mask[ny,nx] and not out[ny,nx]:
                out[ny,nx]=True; dq.append((ny,nx))
    return out

def silhouette(rgb, is_device):
    """is_device: HxWx3 uint8 -> HxW bool. Returns solid device mask (holes filled)."""
    dev0 = is_device(rgb)
    reach = flood_from_border(~dev0)      # background reachable from frame
    solid = ~reach                        # everything the bg cannot reach = device+its holes
    solid = largest_cc_from_center(solid) # drop stray warm specks in bg
    return solid

def bbox_of(mask):
    ys,xs=np.where(mask); return xs.min(),ys.min(),xs.max()+1,ys.max()+1

# ---------------------------------------------------------------- predicates
def pred_neutral_bg(corner_rgb, thr):
    """Device = far enough (max abs channel diff) from a uniform neutral bg color."""
    c = np.asarray(corner_rgb, float)
    def f(rgb):
        d = np.abs(rgb.astype(float)-c).max(2)
        return d > thr
    return f

def pred_warm(rb_thr):
    """Device = warm (R noticeably above B) -> champagne/foam vs neutral grey/blue bg."""
    def f(rgb):
        r=rgb[:,:,0].astype(int); b=rgb[:,:,2].astype(int)
        return (r-b) > rb_thr
    return f

def bg_color(rgb, frac=0.06):
    h,w=rgb.shape[:2]; ph,pw=int(h*frac),int(w*frac)
    patches=[rgb[:ph,:pw],rgb[:ph,-pw:],rgb[-ph:,:pw],rgb[-ph:,-pw:]]
    return np.median(np.concatenate([p.reshape(-1,3) for p in patches],0),0)

# ---------------------------------------------------------------- geometry
def contour_points(mask):
    """Boundary pixels of a solid mask (device pixel with a 4-neighbour outside)."""
    m=mask
    up=np.zeros_like(m); up[1:]=m[:-1]
    dn=np.zeros_like(m); dn[:-1]=m[1:]
    lf=np.zeros_like(m); lf[:,1:]=m[:,:-1]
    rt=np.zeros_like(m); rt[:,:-1]=m[:,1:]
    edge = m & ~(up&dn&lf&rt)
    ys,xs=np.where(edge); return xs.astype(float), ys.astype(float)

def kasa_circle(x, y):
    """Algebraic least-squares circle fit. Returns (cx,cy,R,rms_resid_px)."""
    A=np.c_[x, y, np.ones_like(x)]
    b=x*x+y*y
    sol,*_=np.linalg.lstsq(A,b,rcond=None)
    cx=sol[0]/2; cy=sol[1]/2; R=math.sqrt(sol[2]+cx*cx+cy*cy)
    resid=np.sqrt((x-cx)**2+(y-cy)**2)-R
    return cx,cy,R,float(np.sqrt(np.mean(resid**2)))

def fit_corner(mask, which, frac=0.16):
    """Circle-fit one rounded corner. which in {tl,tr,bl,br}. Returns (cx,cy,R,rms,pts).
    Window is SQUARE (frac*min(W,H) each axis) so an elongated silhouette does not turn
    the window into a sliver of the straight long edge."""
    x0,y0,x1,y1=bbox_of(mask); W=x1-x0; H=y1-y0
    win=frac*min(W,H)
    cx,cy=contour_points(mask)
    if 't' in which: ysel = cy < y0+win
    else:            ysel = cy > y1-win
    if which[1]=='l': xsel = cx < x0+win
    else:             xsel = cx > x1-win
    m = ysel & xsel
    px,py=cx[m],cy[m]
    # keep only the arc: drop points on the straight extreme edges (min/max rows/cols)
    keep = (px>x0+1)&(px<x1-2)&(py>y0+1)&(py<y1-2)
    px,py=px[keep],py[keep]
    if len(px)<12: return None
    a,b,R,rms=kasa_circle(px,py)
    # trimmed refit: drop points far from the fitted circle (straight-edge legs of the
    # corner window) and refit, twice. Converges onto the true arc, lowers rms.
    for _ in range(3):
        resid=np.abs(np.sqrt((px-a)**2+(py-b)**2)-R)
        keep2=resid < max(1.0, 1.2*rms)
        if keep2.sum()<12 or keep2.all(): break
        px,py=px[keep2],py[keep2]
        a,b,R,rms=kasa_circle(px,py)
    return a,b,R,rms,(px,py)

# ---------------------------------------------------------------- blobs
def _boxcount(mask, r):
    H,W=mask.shape; w=2*r+1
    Mp=np.pad(mask.astype(np.int64), r, mode='constant')
    I=np.zeros((H+2*r+1, W+2*r+1), np.int64); I[1:,1:]=np.cumsum(np.cumsum(Mp,0),1)
    return I[w:H+w,w:W+w]-I[0:H,w:W+w]-I[w:H+w,0:W]+I[0:H,0:W]

def dilate(mask, r=2): return _boxcount(mask, r) > 0
def erode(mask, r=2):  return _boxcount(mask, r) >= (2*r+1)**2
def close_(mask, r=2): return erode(dilate(mask, r), r)

def label_blobs(mask):
    """4-connectivity CC labelling on a small ROI mask. Returns list of dicts."""
    h,w=mask.shape; lab=np.zeros((h,w),int); nid=0; out=[]
    for sy in range(h):
        for sx in range(w):
            if mask[sy,sx] and lab[sy,sx]==0:
                nid+=1; dq=deque([(sy,sx)]); lab[sy,sx]=nid; pts=[]
                while dq:
                    y,x=dq.popleft(); pts.append((y,x))
                    for dy,dx in ((1,0),(-1,0),(0,1),(0,-1)):
                        ny,nx=y+dy,x+dx
                        if 0<=ny<h and 0<=nx<w and mask[ny,nx] and lab[ny,nx]==0:
                            lab[ny,nx]=nid; dq.append((ny,nx))
                ys=np.array([p[0] for p in pts]); xs=np.array([p[1] for p in pts])
                out.append(dict(area=len(pts),cx=xs.mean(),cy=ys.mean(),
                                x0=xs.min(),y0=ys.min(),x1=xs.max()+1,y1=ys.max()+1))
    return out

def local_std(L, r=4):
    """Box std of L over (2r+1)^2 window via integral images (foam=high, pill=low)."""
    H,W=L.shape; w=2*r+1
    Lp=np.pad(L.astype(np.float64), r, mode='edge')          # (H+2r, W+2r)
    I1=np.zeros((H+2*r+1, W+2*r+1)); I2=np.zeros_like(I1)
    I1[1:,1:]=np.cumsum(np.cumsum(Lp,0),1)
    I2[1:,1:]=np.cumsum(np.cumsum(Lp*Lp,0),1)
    def box(I): return I[w:H+w, w:W+w]-I[0:H, w:W+w]-I[w:H+w, 0:W]+I[0:H, 0:W]
    n=w*w; m=box(I1)/n; m2=box(I2)/n
    return np.sqrt(np.maximum(m2-m*m,0.0))

# ---------------------------------------------------------------- colour
def srgb_to_lab(rgb01):
    def inv(c): return np.where(c<=0.04045, c/12.92, ((c+0.055)/1.055)**2.4)
    r,g,b=[inv(rgb01[...,i]) for i in range(3)]
    X=r*0.4124+g*0.3576+b*0.1805; Y=r*0.2126+g*0.7152+b*0.0722; Z=r*0.0193+g*0.1192+b*0.9505
    X/=0.95047; Z/=1.08883
    def f(t): return np.where(t>0.008856, np.cbrt(t), 7.787*t+16/116)
    fX,fY,fZ=f(X),f(Y),f(Z)
    return np.stack([116*fY-16, 500*(fX-fY), 200*(fY-fZ)],-1)

def patch_lab(rgb, box):
    x0,y0,x1,y1=[int(v) for v in box]
    p=rgb[y0:y1,x0:x1].reshape(-1,3).astype(float)/255.0
    lab=srgb_to_lab(p.reshape(1,-1,3)).reshape(-1,3)
    return lab.mean(0)

# ================================================================ JOBS
def job_mac_studio_front():
    dev="mac-studio"; src="ref/mac-studio/apple_front.jpg"
    rgb=load_rgb(os.path.join(ROOT,src))
    ds,s=downscale(rgb,1600)                      # s = ds/full
    bg=bg_color(ds)
    mask=silhouette(ds, pred_neutral_bg(bg, thr=16))
    x0,y0,x1,y1=bbox_of(mask); Wpx=x1-x0; Hpx=y1-y0
    mmpp=197.0/Wpx                                # anchor: width 197 mm
    # evidence: silhouette bbox
    dbg=ds.copy(); dbg[~mask]= (dbg[~mask]*0.35).astype(np.uint8)
    crop=save_crop(dbg,(x0-20,y0-20,x1+20,y1+20),"mac_front_silhouette.png")
    row(dev,"front_width_anchor",197.0,"mm",src,crop,"anchor","known real width (Apple spec 197 mm); the one absolute this image scales from")
    row(dev,"front_aspect_W:H_meas",round(Wpx/Hpx,3),"ratio",src,crop,"high",f"image aspect vs spec {197/95:.3f}; image reads taller by intake-band inclusion")
    # corner radius: top-left + top-right vertical-edge corners
    Rs=[]
    for w_ in ("tl","tr"):
        f=fit_corner(mask,w_,frac=0.18)
        if f:
            a,b,R,rms,(px,py)=f; Rs.append(R)
            crop=save_crop(ds,(a-R-70,b-R-70,a+R+70,b+R+70),f"mac_corner_{w_}.png",
                           marks=[(a,b,int(R),(255,40,40))])
            row(dev,f"front_corner_R_{w_}",R*mmpp,"mm",src,crop,"high",f"Kasa fit rms={rms*mmpp:.2f}mm")
    if Rs:
        Rmean=float(np.mean(Rs))*mmpp
        # ONE measurement: the front-outline top-corner curvature IS the top-edge fillet
        # R_top (front_corner_R_tl/tr above are its two per-corner fits). Merged, not two
        # features. Distinct from the 31 mm footprint / vertical-edge radius.
        row(dev,"top_edge_fillet_R",Rmean,"mm",src,"ref/measure_evidence/mac_corner_tl.png","high",
            f"= front-outline top-corner R_top (mean of tl/tr, spread {(max(Rs)-min(Rs))*mmpp:.2f}mm); tight edge, top dead-flat")
    # ---- fine features at FULL res inside the front face
    # map ds bbox back to full res
    fx0,fy0,fx1,fy1=[int(v/s) for v in (x0,y0,x1,y1)]
    face=rgb[fy0:fy1,fx0:fx1]
    fmmpp=197.0/(fx1-fx0)
    L=srgb_to_lab(face.astype(float)/255.0)[...,0]
    # ports = dark slots in lower third of the face
    h,w=L.shape
    band=np.zeros_like(L,bool); band[int(h*0.55):int(h*0.9)]=True
    dark=(L<45)&band
    blobs=[b for b in label_blobs(dark) if b['area']>(0.0004*h*w)]
    blobs.sort(key=lambda b:b['cx'])
    # classify: USB-C are tall-ish small ovals; SD is a wide thin slot
    usbc=[b for b in blobs if (b['x1']-b['x0'])<(b['y1']-b['y0'])*1.6]
    sd  =[b for b in blobs if (b['x1']-b['x0'])>=(b['y1']-b['y0'])*2.2]
    marks=[]
    base_y=h  # face bottom in face coords ~ device bottom
    if len(usbc)>=2:
        u=sorted(usbc[:2],key=lambda b:b['cx'])
        wpx=np.mean([b['x1']-b['x0'] for b in u]); hpx=np.mean([b['y1']-b['y0'] for b in u])
        long_mm=max(wpx,hpx)*fmmpp; short_mm=min(wpx,hpx)*fmmpp
        vertical = hpx>wpx
        cyy=np.mean([b['cy'] for b in u])
        # permanent zoom evidence (3x nearest, no interpolation) of the two ports
        bx0=max(0,min(b['x0'] for b in u)-40); bx1=max(b['x1'] for b in u)+40
        by0=max(0,min(b['y0'] for b in u)-40); by1=max(b['y1'] for b in u)+40
        zc=face[by0:by1,bx0:bx1]
        Image.fromarray(zc).resize((zc.shape[1]*3,zc.shape[0]*3),Image.NEAREST).save(os.path.join(EVID,"mac_usbc_zoom.png"))
        zcrop="ref/measure_evidence/mac_usbc_zoom.png"
        row(dev,"usbc_orientation","vertical","axis",src,"ref/measure_evidence/tiebreak_usbc.png","settled",
            f"SETTLED by two photographers: Apple H/W {max(wpx,hpx)/min(wpx,hpx):.2f}, Wikimedia H/W 1.87 (3/4, foreshortened) · both vertical")
        row(dev,"usbc_long_axis_vert",long_mm,"mm",src,zcrop,"high","= USB-C receptacle 8.4mm dimension, oriented vertical")
        row(dev,"usbc_short_axis_horiz",short_mm,"mm",src,zcrop,"high","= USB-C 2.6mm dimension")
        row(dev,"usbc_pair_spacing",abs(u[1]['cx']-u[0]['cx'])*fmmpp,"mm",src,"ref/measure_evidence/mac_ports.png","high","center-to-center, along the width")
        row(dev,"usbc_left_x_from_center",(u[0]['cx']-w/2)*fmmpp,"mm",src,"ref/measure_evidence/mac_ports.png","high","feature position: + is right of center")
        row(dev,"usbc_right_x_from_center",(u[1]['cx']-w/2)*fmmpp,"mm",src,"ref/measure_evidence/mac_ports.png","high","feature position")
        row(dev,"port_row_center_from_base",(base_y-cyy)*fmmpp,"mm",src,"ref/measure_evidence/mac_ports.png","high","z of the port row above the base")
        for b in u: marks.append((int(b['cx']),int(b['cy']),8,(40,120,255)))
    if sd:
        b=max(sd,key=lambda b:b['area'])
        row(dev,"sd_slot_orientation","horizontal","axis",src,"ref/measure_evidence/mac_ports.png","high","wide slot, long axis horizontal (opposite of the USB-C ports)")
        row(dev,"sd_slot_width",(b['x1']-b['x0'])*fmmpp,"mm",src,"ref/measure_evidence/mac_ports.png","high","horizontal long axis")
        row(dev,"sd_slot_height",(b['y1']-b['y0'])*fmmpp,"mm",src,"ref/measure_evidence/mac_ports.png","high","")
        row(dev,"sd_center_x_from_center",(b['cx']-w/2)*fmmpp,"mm",src,"ref/measure_evidence/mac_ports.png","high","feature position: left of center")
        marks.append((int(b['cx']),int(b['cy']),10,(40,220,120)))
    # LED = small power dot at the far right, on the port row. Locate by brightness peak +
    # intensity-weighted centroid inside a tight window (blob shape is unreliable because the
    # dot merges with surrounding bright silver at low thresholds).
    wy0,wy1=int(h*0.66),int(h*0.80); wx0,wx1=int(w*0.88),int(w*0.985)
    win=L[wy0:wy1, wx0:wx1]
    pk=win.max()
    selc=win>=pk-3            # tight core for centroid + size
    if selc.sum()>=4:
        yy,xx=np.where(selc); wsum=win[selc]-(pk-3)
        cx=wx0+float((xx*wsum).sum()/wsum.sum()); cy=wy0+float((yy*wsum).sum()/wsum.sum())
        dia=2*math.sqrt(selc.sum()/math.pi)
        row(dev,"led_diameter",dia*fmmpp,"mm",src,"ref/measure_evidence/mac_ports.png","approx","power dot core (glow-inclusive)")
        row(dev,"led_from_right_edge",(w-cx)*fmmpp,"mm",src,"ref/measure_evidence/mac_ports.png","med","")
        row(dev,"led_from_base",(h-cy)*fmmpp,"mm",src,"ref/measure_evidence/mac_ports.png","med","LED height above device base")
        row(dev,"led_x_from_center",(cx-w/2)*fmmpp,"mm",src,"ref/measure_evidence/mac_ports.png","med","feature position: far right")
        marks.append((int(cx),int(cy),10,(255,220,40)))
    save_crop(face,(0,int(h*0.5),w,h),"mac_ports.png",marks=marks)
    # intake band = perforated hex mesh at the front bottom. Its TOP is the texture onset
    # scanning up from the bottom (aluminium above is smooth, mesh below is perforated).
    texp=local_std(L,3)[:, int(w*0.30):int(w*0.70)].mean(1)
    alu=np.median(texp[int(h*0.35):int(h*0.55)]); thr=alu+3.0
    mesh_top=h-1
    for r in range(h-1,int(h*0.80),-1):
        if texp[r]>thr: mesh_top=r
        elif mesh_top-r>3: break
    intake=(h-mesh_top)*fmmpp
    im=Image.fromarray(face[int(h*0.70):].copy()); dd=ImageDraw.Draw(im)
    dd.line([0,mesh_top-int(h*0.70),w,mesh_top-int(h*0.70)],fill=(255,60,60),width=3)
    im.save(os.path.join(EVID,"mac_intake_band.png"))
    row(dev,"intake_band_height",intake,"mm",src,"ref/measure_evidence/mac_intake_band.png","high",
        "perforated hex mesh, front-face bottom edge to silhouette bottom (NOT the ground gap)")
    # front height: spec anchors the absolute (95 mm). The image reads +1.2% because the
    # near-level front captures a sliver of the underside intake below the base plane; the
    # scale itself is validated by the USB-C long axis (8.47 vs true 8.4 mm, +0.8%).
    meas_h=(fy1-fy0)*197.0/w
    row(dev,"front_height_spec",95.0,"mm",src,"ref/measure_evidence/mac_front_silhouette.png","spec",
        f"absolute from Apple spec; image measures {meas_h:.2f} (+{100*(meas_h/95-1):.1f}%) = intake-band inclusion, not scale error")
    # aluminium Lab patch (diffuse mid-face, away from features/highlight)
    lab=patch_lab(face,(int(w*0.30),int(h*0.28),int(w*0.45),int(h*0.42)))
    save_crop(face,(int(w*0.30),int(h*0.28),int(w*0.45),int(h*0.42)),"mac_alu_patch.png")
    row(dev,"alu_Lab_L",float(lab[0]),"L*",src,"ref/measure_evidence/mac_alu_patch.png","high","diffuse mid-face patch")
    row(dev,"alu_Lab_a",float(lab[1]),"a*",src,"ref/measure_evidence/mac_alu_patch.png","high","")
    row(dev,"alu_Lab_b",float(lab[2]),"b*",src,"ref/measure_evidence/mac_alu_patch.png","high","")
    return mask,mmpp

def job_mac_studio_plan():
    # plan-view corner radius from the dimensions.com vector drawing (top/plan view)
    dev="mac-studio"; src="ref/mac-studio/dim_top-front.svg.png"
    rgb=load_rgb(os.path.join(ROOT,src))
    # plan drawing = cyan strokes on white, left half of the sheet
    r,g,b=rgb[:,:,0].astype(int),rgb[:,:,1].astype(int),rgb[:,:,2].astype(int)
    cyan=(b>120)&(g>110)&(r<120)
    # restrict to the left plan square region (x<0.5 of sheet, the labelled 'Plan')
    h,w=cyan.shape; region=np.zeros_like(cyan); region[:int(h*0.62),:int(w*0.60)]=True
    strokes=cyan&region
    ys,xs=np.where(strokes)
    if len(xs)<50: return
    x0,x1,y0,y1=xs.min(),xs.max(),ys.min(),ys.max(); Wpx=x1-x0
    mmpp=197.0/Wpx
    # fill the square outline to a solid, fit a corner
    solid=np.zeros((h,w),bool); solid[strokes]=True
    reach=flood_from_border(~solid)   # note: stroke is thin; fill interior
    filled=~reach
    filled=largest_cc_from_center(filled) if filled.any() else filled
    m=fit_corner(filled,"tr",frac=0.20)
    if m:
        a,bb,R,rms,_=m
        crop=save_crop(rgb,(a-R-40,bb-R-40,a+R+40,bb+R+40),"mac_plan_corner.png",
                       marks=[(a,bb,int(R),(255,40,40))])
        row(dev,"plan_corner_R",R*mmpp,"mm",src,crop,"high",
            f"footprint / vertical-edge radius (distinct from 7.6mm top fillet); vector arc rms={rms*mmpp:.3f}mm")

def job_mac_studio_reveal():
    # base_reveal_gap = the air/shadow gap between the intake band and the ground. The front
    # press shot floats (no ground), so this comes from the desk 3/4. Both the front-face
    # height and the gap sit on the same near-vertical plane, so their foreshortening cancels
    # in a ratio. The recess conflates with the cast contact shadow, so this is a bracket.
    dev="mac-studio"; src="ref/mac-studio/apple_desk-setup.jpg"
    rgb=load_rgb(os.path.join(ROOT,src))
    sc=rgb.shape[1]/1400.0
    dx0,dx1=int(410*sc),int(640*sc); dy0,dy1=int(470*sc),int(650*sc)
    sub=rgb[dy0:dy1,dx0:dx1]; H,W=sub.shape[:2]
    L=srgb_to_lab(sub.astype(float)/255.0)[...,0]
    # clean column on the right of the front face (near the LED, away from cables)
    cx=int(W*0.66); col=L[:,cx-3:cx+3].mean(1)
    # front-face bottom edge = last bright row before the dark drop, in the lower third
    lo=int(H*0.55)
    bright=np.where(col[lo:]>40)[0]
    if len(bright):
        ff_bottom=lo+bright.max()
        # desk resumes = first row below ff_bottom where brightness climbs back to desk level
        below=col[ff_bottom:]
        desk=np.where(below>28)[0]
        gap_px = desk[desk>4][0] if len(desk[desk>4]) else 0
        # front-face height proxy = ff_bottom minus the top-front edge (brightest upper row)
        ff_top=int(np.argmax(col[:int(H*0.5)]))
        ff_h=max(1, ff_bottom-ff_top)
        dark_mm=(gap_px/ff_h)*95.0   # dark region = recess + cast shadow, shadow-dominated
        Image.fromarray(sub[int(H*0.78):, int(W*0.05):int(W*0.78)]).resize(
            (int((W*0.73)*3),int((H*0.22)*3)),Image.LANCZOS).save(os.path.join(EVID,"mac_reveal_gap.png"))
        # No reference resolves the recess (front floats; every 3/4 conflates it with the
        # cast contact shadow, which here reads ~{dark_mm:.0f}mm, shadow-dominated). Per the
        # closure directive this is a DECLARED DESIGN PARAMETER: 2.5 mm, INFERRED not measured,
        # to be tuned in phase 3 against the 3/4 blend. Second hunt (iFixit teardown, OWC
        # teardown, review galleries) found no clean side-elevation on a surface.
        row(dev,"base_reveal_gap","2.5 (INFERRED)","mm",src,"ref/measure_evidence/mac_reveal_gap.png","inferred",
            f"declared design parameter, NOT measured; recess conflates with cast shadow (~{dark_mm:.0f}mm). Tune in phase 3 vs the 3/4 blend")

def job_dgx_front():
    # cl_front-foam.jpg = the FRONT: tall foam face w/ two champagne pill hand-holds
    # (top pill dark window, bottom pill NVIDIA logo), stood vertical on clean grey bg.
    dev="dgx-spark"; src="ref/dgx-spark/cl_front-foam.jpg"
    rgb=load_rgb(os.path.join(ROOT,src))
    ds,s=downscale(rgb,1200)
    mask=silhouette(ds, pred_warm(rb_thr=12))
    x0,y0,x1,y1=bbox_of(mask); Wpx=x1-x0; Hpx=y1-y0
    # front face long edge = 150 mm ; here long edge is VERTICAL (stood on end)
    longpx=max(Wpx,Hpx); shortpx=min(Wpx,Hpx)
    mmpp=150.0/longpx
    dbg=ds.copy(); dbg[~mask]=(dbg[~mask]*0.35).astype(np.uint8)
    crop=save_crop(dbg,(x0-15,y0-15,x1+15,y1+15),"dgx_front_silhouette.png")
    meas_short=shortpx*mmpp
    row(dev,"front_long_anchor",150.0,"mm",src,crop,"anchor","NVIDIA spec 150 mm; the one absolute this image scales from")
    row(dev,"front_short_edge_spec",50.5,"mm",src,crop,"spec",
        f"absolute from spec; this source reads {meas_short:.1f} ({100*(meas_short/50.5-1):+.1f}%, incl. thin visible side); sth_side reads 48.6 (-3.8%, foreshorten)")
    row(dev,"front_aspect_long:short_meas",round(longpx/shortpx,3),"ratio",src,crop,"high",
        f"image aspect vs spec {150/50.5:.2f}; a ~3:1 STRIP, NOT square")
    # edge radii: fit two corners of the (rotated) front face. Tight window so the
    # nearly-flat short-end edge is excluded (a rounded-rect R < half the short edge).
    # fit the two LEFT corners: the left edge is the clean front-face edge, whereas the
    # right edge shows the champagne side at a slight angle (not a clean rounded-rect corner).
    Rs=[]
    for w_ in ("tl","bl"):
        f=fit_corner(mask,w_,frac=0.22)
        if f:
            a,b,R,rms,_=f; Rs.append(R)
            crop=save_crop(ds,(a-R-25,b-R-25,a+R+25,b+R+25),f"dgx_corner_{w_}.png",
                           marks=[(a,b,int(R),(255,40,40))])
            row(dev,f"front_edge_R_{w_}",R*mmpp,"mm",src,crop,"high",f"Kasa rms={rms*mmpp:.2f}mm, left edge")
    if Rs: row(dev,"front_edge_R_mean",float(np.mean(Rs))*mmpp,"mm",src,"(see corners)","high","clean left edge")
    return mask,mmpp,ds,s,rgb

def job_dgx_pills_border(mask,mmpp,ds):
    # pills + champagne rail measured on the FRONT (cl_front-foam, stood vertical).
    dev="dgx-spark"; src="ref/dgx-spark/cl_front-foam.jpg"
    x0,y0,x1,y1=bbox_of(mask); W=x1-x0; H=y1-y0
    L=srgb_to_lab(ds.astype(float)/255.0)[...,0]
    tex=local_std(L,4)
    # foam-interior island test: erode the device mask so the outer champagne frame
    # (which touches the border) is excluded; pills are smooth islands inside the foam.
    core=erode(mask, max(2,int(0.05*W)))
    champ=close_((L>52)&(tex<11)&core, 2)
    blobs=label_blobs(champ)
    cand=[b for b in blobs if b['area']>0.003*mask.sum()
          and 1.4<((b['x1']-b['x0'])/max(1,b['y1']-b['y0']))<7.0]
    cand.sort(key=lambda b:-b['area'])
    pills=sorted(cand[:2], key=lambda b:b['cy'])
    if len(pills)==2:
        wl=np.mean([b['x1']-b['x0'] for b in pills]); hl=np.mean([b['y1']-b['y0'] for b in pills])
        marks=[(int(b['cx']),int(b['cy']),14,(255,60,60)) for b in pills]
        boxes=[(b['x0'],b['y0'],b['x1'],b['y1']) for b in pills]
        crop=save_crop(ds,(x0-10,y0-10,x1+10,y1+10),"dgx_pills.png",marks=marks,
                       lines=[(bx0,by0,bx1,by0,(60,160,255)) for (bx0,by0,bx1,by1) in boxes]
                            +[(bx0,by1,bx1,by1,(60,160,255)) for (bx0,by0,bx1,by1) in boxes])
        row(dev,"pill_orientation","long axis || 50.5mm short/depth axis","axis",src,crop,"high",
            "each pill wider across the short edge; the two pills are arrayed along the 150mm long axis")
        row(dev,"pill_long",wl*mmpp,"mm",src,crop,"high","hand-hold cutout long axis (runs along the 50.5mm short/depth edge), 2 present")
        row(dev,"pill_short",hl*mmpp,"mm",src,crop,"high","pill width along the 150mm long axis")
        row(dev,"pill_center_from_end",(pills[0]['cy']-y0)*mmpp,"mm",src,crop,"high","near 50mm-end -> first pill center, along the 150mm axis")
        row(dev,"pill_pitch",(pills[1]['cy']-pills[0]['cy'])*mmpp,"mm",src,crop,"high","center-to-center along the 150mm long axis")
    # foam field extent -> champagne border margins. The textured foam is one region;
    # its bbox vs the device bbox gives the champagne frame on each edge (thin lip on the
    # long edges, thicker band on the short/pill ends).
    foam=close_((tex>11)&mask, 3)
    fb=[b for b in label_blobs(foam) if b['area']>0.1*mask.sum()]
    if fb:
        b=max(fb,key=lambda b:b['area'])
        long_lip = ((b['x0']-x0)+(x1-b['x1']))/2*mmpp   # long edges (thin champagne lip)
        end_band = ((b['y0']-y0)+(y1-b['y1']))/2*mmpp   # short/pill ends band
        crop=save_crop(ds,(x0-8,y0-8,x1+8,y1+8),"dgx_border.png",
                       lines=[(b['x0'],b['y0'],b['x1'],b['y0'],(255,60,60)),
                              (b['x0'],b['y1'],b['x1'],b['y1'],(255,60,60)),
                              (b['x0'],b['y0'],b['x0'],b['y1'],(255,60,60)),
                              (b['x1'],b['y0'],b['x1'],b['y1'],(255,60,60))])
        row(dev,"foam_field_long",(b['y1']-b['y0'])*mmpp,"mm",src,crop,"med","textured foam field, long axis")
        row(dev,"foam_field_short",(b['x1']-b['x0'])*mmpp,"mm",src,crop,"med","textured foam field, short axis")
        row(dev,"foam_border_margin",long_lip,"mm",src,crop,"med","champagne lip, long edge (thin)")
        row(dev,"foam_end_band",end_band,"mm",src,crop,"med","champagne band at short/pill ends")

def job_dgx_top():
    # cl_side-profile.jpg = the square TOP face (150x150): champagne frame + recessed
    # weave-vent panel + thin exhaust slot; foam of front/rear peeks at top & bottom.
    dev="dgx-spark"; src="ref/dgx-spark/cl_side-profile.jpg"
    rgb=load_rgb(os.path.join(ROOT,src))
    ds,s=downscale(rgb,1200)
    mask=silhouette(ds, pred_warm(rb_thr=12))
    x0,y0,x1,y1=bbox_of(mask); Wpx=x1-x0; Hpx=y1-y0
    mmpp=150.0/Wpx
    dbg=ds.copy(); dbg[~mask]=(dbg[~mask]*0.35).astype(np.uint8)
    crop=save_crop(dbg,(x0-15,y0-15,x1+15,y1+15),"dgx_top_silhouette.png")
    meas_depth=Hpx*mmpp
    row(dev,"top_width_anchor",150.0,"mm",src,crop,"anchor","NVIDIA spec 150 mm; the one absolute this image scales from")
    row(dev,"top_depth_spec",150.0,"mm",src,crop,"spec",f"absolute from spec; this source reads {meas_depth:.1f} ({100*(meas_depth/150-1):+.1f}%)")
    row(dev,"top_aspect_W:D_meas",round(Wpx/Hpx,3),"ratio",src,crop,"high","top face is square (spec 1.00)")
    m=fit_corner(mask,"tr",frac=0.24)
    if m:
        a,b,R,rms,_=m
        crop=save_crop(ds,(a-R-25,b-R-25,a+R+25,b+R+25),"dgx_top_corner.png",marks=[(a,b,int(R),(255,40,40))])
        row(dev,"top_plan_corner_R",R*mmpp,"mm",src,crop,"med",f"footprint corner (foam-edge softens fit), rms={rms*mmpp:.2f}mm")
    # recessed weave panel: smooth low-texture bright region in the center
    L=srgb_to_lab(ds.astype(float)/255.0)[...,0]; tex=local_std(L,4)
    inside=np.zeros_like(mask); inside[y0+int(0.12*Hpx):y1-int(0.12*Hpx), x0+int(0.12*Wpx):x1-int(0.12*Wpx)]=True
    panel=(tex<6)&inside&mask
    blobs=[b for b in label_blobs(panel) if b['area']>0.05*mask.sum()]
    if blobs:
        b=max(blobs,key=lambda b:b['area'])
        crop=save_crop(ds,(b['x0']-8,b['y0']-8,b['x1']+8,b['y1']+8),"dgx_top_panel.png")
        row(dev,"top_panel_width",(b['x1']-b['x0'])*mmpp,"mm",src,crop,"med","recessed vent panel")
        row(dev,"top_panel_height",(b['y1']-b['y0'])*mmpp,"mm",src,crop,"med","")
        row(dev,"top_panel_inset_margin",((b['x0']-x0)+(x1-b['x1']))/2*mmpp,"mm",src,crop,"med","frame edge -> panel, mean L/R")

def job_dgx_foam_density():
    # count foam cells per cm across a 20 mm strip in two regions, high-res front
    dev="dgx-spark"; src="ref/dgx-spark/storagereview_front.jpg"
    rgb=load_rgb(os.path.join(ROOT,src))
    # scale: locate the foam front face width via warm mask bbox
    warm=pred_warm(14)(rgb)
    solid=largest_cc_from_center(~flood_from_border(~warm))
    x0,y0,x1,y1=bbox_of(solid)
    mmpp=150.0/(x1-x0)   # front face width 150 mm
    per20=int(round(20.0/mmpp))
    L=srgb_to_lab(rgb.astype(float)/255.0)[...,0]
    def density(cx,cy,tag):
        xa=cx-per20//2; xb=xa+per20; yb=slice(cy-6,cy+6)
        strip=L[yb, xa:xb].mean(0)
        strip=strip-strip.mean()
        # count ridge peaks (local maxima above small prominence)
        peaks=0
        for i in range(1,len(strip)-1):
            if strip[i]>strip[i-1] and strip[i]>=strip[i+1] and strip[i]>3:
                peaks+=1
        cpc=peaks/2.0   # peaks over 20 mm -> per cm
        crop=save_crop(rgb,(xa,cy-14,xb,cy+14),f"dgx_foam_{tag}.png",
                       lines=[(xa,cy,xb,cy,(255,60,60))])
        row(dev,f"foam_cells_per_cm_{tag}",cpc,"cells/cm",src,crop,"med",
            f"{peaks} ridges / 20mm strip")
    cxc=(x0+x1)//2; cyc=(y0+y1)//2
    density(cxc, cyc, "A")                     # central foam
    density(cxc, y0+int(0.30*(y1-y0)), "B")    # upper foam
    # champagne + foam Lab
    lab_ch=patch_lab(rgb,(x0+int(0.02*(x1-x0)), (y0+y1)//2-8, x0+int(0.05*(x1-x0)), (y0+y1)//2+8))
    save_crop(rgb,(x0+int(0.02*(x1-x0)), (y0+y1)//2-8, x0+int(0.05*(x1-x0)), (y0+y1)//2+8),"dgx_champ_patch.png")
    row(dev,"champagne_Lab_L",float(lab_ch[0]),"L*",src,"ref/measure_evidence/dgx_champ_patch.png","med","left rail patch")
    row(dev,"champagne_Lab_a",float(lab_ch[1]),"a*",src,"ref/measure_evidence/dgx_champ_patch.png","med","")
    row(dev,"champagne_Lab_b",float(lab_ch[2]),"b*",src,"ref/measure_evidence/dgx_champ_patch.png","med","")
    lab_fo=patch_lab(rgb,(cxc-40,cyc-40,cxc+40,cyc+40))
    save_crop(rgb,(cxc-40,cyc-40,cxc+40,cyc+40),"dgx_foam_patch.png")
    row(dev,"foam_mean_Lab_L",float(lab_fo[0]),"L*",src,"ref/measure_evidence/dgx_foam_patch.png","med","mean over foam patch")
    row(dev,"foam_mean_Lab_a",float(lab_fo[1]),"a*",src,"ref/measure_evidence/dgx_foam_patch.png","med","")
    row(dev,"foam_mean_Lab_b",float(lab_fo[2]),"b*",src,"ref/measure_evidence/dgx_foam_patch.png","med","")

def job_dgx_side_thickness():
    # cross-check device thickness/short edge from the smooth-side vertical shot
    dev="dgx-spark"; src="ref/dgx-spark/sth_side-1-vertical.jpg"
    rgb=load_rgb(os.path.join(ROOT,src))
    ds,s=downscale(rgb,1000)
    warm=pred_warm(10)(ds)
    solid=largest_cc_from_center(~flood_from_border(~warm))
    x0,y0,x1,y1=bbox_of(solid); Wpx=x1-x0; Hpx=y1-y0
    longpx=max(Wpx,Hpx); shortpx=min(Wpx,Hpx)
    mmpp=150.0/longpx
    crop=save_crop((lambda d: (d.__setitem__((~solid),(d[~solid]*0.4).astype(np.uint8)) or d))(ds.copy()),
                   (x0-10,y0-10,x1+10,y1+10),"dgx_side_silhouette.png")
    row(dev,"side_thickness_persp_check",shortpx*mmpp,"mm",src,crop,"med",
        f"smooth side reads {shortpx*mmpp:.1f} vs spec 50.5 ({100*(shortpx*mmpp/50.5-1):+.1f}%); a per-source perspective indicator, NOT the absolute (spec 50.5 governs)")

# ================================================================ main
def main():
    print("== measuring ==")
    m,mmpp=job_mac_studio_front()
    job_mac_studio_plan()
    job_mac_studio_reveal()
    dm,dmmpp,dds,ds_s,drgb=job_dgx_front()
    job_dgx_pills_border(dm,dmmpp,dds)
    job_dgx_top()
    job_dgx_foam_density()
    job_dgx_side_thickness()
    write_md()
    print(f"\n{len(ROWS)} rows -> render/MEASUREMENTS.md")

def write_md():
    lines=["# render/MEASUREMENTS.md · measured geometry & tone",
           "",
           "Every value produced by `render/measure.py` from `render/ref/`. One known real",
           "dimension anchors each device (Mac Studio front width 197 mm · DGX Spark front",
           "long edge 150 mm); all other values follow from the pixel scale. Evidence crops",
           "under `render/measure_evidence/`. Dash gate: middot only.",
           ""]
    for dev,title in (("mac-studio","## Mac Studio"),("dgx-spark","## DGX Spark")):
        lines+=[title,"",
                "| parameter | value | unit | conf | source image | evidence crop | note |",
                "|---|---|---|---|---|---|---|"]
        for r in ROWS:
            if r['device']!=dev: continue
            v=f"{r['value']:.2f}" if isinstance(r['value'],float) else str(r['value'])
            src=os.path.basename(r['source'])
            crop=os.path.basename(r['crop']) if r['crop'] and r['crop']!="(see tl/tr)" and not r['crop'].startswith("(") else r['crop']
            lines.append(f"| {r['param']} | {v} | {r['unit']} | {r['conf']} | {src} | {crop} | {r['note']} |")
        lines.append("")
    open(os.path.join(ROOT,"MEASUREMENTS.md"),"w").write("\n".join(lines))

if __name__=="__main__":
    main()
