#!/usr/bin/env python3
"""B1 step 1: MEASURE the Mac Studio reference rear hero grille from the real Apple press photo.

Detects the perforated field, its bounds (normalized to the body + in mm via the solved mm/px),
hole pitch (via autocorrelation/FFT), hole diameter, row/column density, margins and OPEN-AREA
fraction. This is the target spec the render-master grille must match (bounds 2%, density 5%,
open area 10%).

Usage: measure_ref_grille.py <reference.jpg> <mm_per_px> <out.json> <overlay.png>
"""
import sys, json
import numpy as np, cv2

ref, mmpx, out_json, out_ov = sys.argv[1], float(sys.argv[2]), sys.argv[3], sys.argv[4]
im = cv2.imread(ref); H, W = im.shape[:2]
g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)

# --- body bbox (non-white extent) ---
nb = (g < 238).astype(np.uint8)
nb = cv2.morphologyEx(nb, cv2.MORPH_OPEN, np.ones((7,7), np.uint8))
xs = np.where(nb.sum(0) > 0.06*H)[0]; ys = np.where(nb.sum(1) > 0.06*W)[0]
bx0, by0, bx1, by1 = int(xs[0]), int(ys[0]), int(xs[-1]), int(ys[-1])
bw, bh = bx1-bx0+1, by1-by0+1

# --- perforated field = high local texture variance + dark dots (the grille), NOT smooth shell ---
blur = cv2.GaussianBlur(g, (0,0), 2.0)
hf = cv2.absdiff(g, blur)                       # high-frequency energy (perforation dots)
hf_body = hf[by0:by1+1, bx0:bx1+1]
# The GRILLE is the band of DENSE periodic dots. Isolate it by the per-row/col high-frequency
# energy profile: grille rows have far higher HF than the smooth shell or the sparse port row.
rowE = (hf_body > 8).mean(axis=1)
colE = (hf_body > 8).mean(axis=0)
def band(profile, frac=0.45):
    thr = profile.max()*frac
    idx = np.where(profile > thr)[0]
    if len(idx) == 0: return 0, len(profile)-1
    # longest contiguous run above threshold (the grille band)
    splits = np.split(idx, np.where(np.diff(idx) > 3)[0]+1)
    run = max(splits, key=len)
    return int(run[0]), int(run[-1])
ry0, ry1 = band(rowE); cx0, cx1 = band(colE)
gy, gh = ry0, ry1-ry0+1
gx, gw = cx0, cx1-cx0+1
GX0, GY0, GX1, GY1 = bx0+gx, by0+gy, bx0+gx+gw-1, by0+gy+gh-1

# --- pitch via autocorrelation of the grille patch (dominant periodic spacing) ---
patch = g[GY0:GY1+1, GX0:GX1+1].astype(np.float32)
patch -= patch.mean()
F = np.fft.fft2(patch); ac = np.real(np.fft.ifft2(F*np.conj(F)))
ac = np.fft.fftshift(ac); cy, cx = ac.shape[0]//2, ac.shape[1]//2
def first_peak(profile, cmid):
    prof = profile[cmid+2:cmid+40]
    if len(prof) < 5: return None
    k = int(np.argmax(prof)) + 2
    return k
pitch_x_px = first_peak(ac[cy, :], cx)
pitch_y_px = first_peak(ac[:, cx], cy)

# --- holes: dark blobs inside the grille field -> diameter + open area ---
gp = g[GY0:GY1+1, GX0:GX1+1]
thr = int(np.percentile(gp, 45))                  # holes are the darker population
holes = (gp < thr).astype(np.uint8)
holes = cv2.morphologyEx(holes, cv2.MORPH_OPEN, np.ones((2,2), np.uint8))
hn, hlab, hstats, _ = cv2.connectedComponentsWithStats(holes, 8)
areas = hstats[1:, cv2.CC_STAT_AREA]
areas = areas[(areas > 3) & (areas < 400)]
hole_d_px = float(2*np.sqrt(np.median(areas)/np.pi)) if len(areas) else float('nan')
open_area = float(holes.mean())
n_holes_detected = int(len(areas))

res = {
  "reference": ref, "image_size_px": [W, H], "mm_per_px": mmpx,
  "body_bbox_px": [bx0, by0, bx1, by1], "body_size_px": [bw, bh],
  "body_size_mm": [round(bw*mmpx,2), round(bh*mmpx,2)],
  "grille_bbox_px": [int(GX0), int(GY0), int(GX1), int(GY1)],
  "grille_size_mm": [round(gw*mmpx,2), round(gh*mmpx,2)],
  # normalized to body (this is what the model must match within 2%)
  "grille_norm_bounds": {
    "x0": round((GX0-bx0)/bw,4), "x1": round((GX1-bx0)/bw,4),
    "y0": round((GY0-by0)/bh,4), "y1": round((GY1-by0)/bh,4),
    "w": round(gw/bw,4), "h": round(gh/bh,4)},
  "margins_mm": {"left": round((GX0-bx0)*mmpx,2), "right": round((bx1-GX1)*mmpx,2),
                 "top": round((GY0-by0)*mmpx,2), "bottom": round((by1-GY1)*mmpx,2)},
  "pitch_px": {"x": pitch_x_px, "y": pitch_y_px},
  "pitch_mm": {"x": round(pitch_x_px*mmpx,3) if pitch_x_px else None,
               "y": round(pitch_y_px*mmpx,3) if pitch_y_px else None},
  "hole_diameter_mm": round(hole_d_px*mmpx,3),
  "open_area_fraction": round(open_area,4),
  "holes_detected_in_field": n_holes_detected,
  "rows_est": int(round(gh/pitch_y_px)) if pitch_y_px else None,
  "cols_est": int(round(gw/pitch_x_px)) if pitch_x_px else None,
  "method": "texture(high-freq) field detection; pitch via 2D autocorrelation first peak; hole diameter from dark-blob median area; open area = dark fraction inside field",
}
json.dump(res, open(out_json,"w"), indent=2)
ov = im.copy()
cv2.rectangle(ov, (bx0,by0), (bx1,by1), (0,180,0), 3)
cv2.rectangle(ov, (GX0,GY0), (GX1,GY1), (0,0,255), 4)
cv2.putText(ov, "HERO GRILLE %.1fx%.1fmm pitch %.2fmm d %.2fmm open %.2f"%(
    gw*mmpx, gh*mmpx, (pitch_x_px or 0)*mmpx, hole_d_px*mmpx, open_area),
    (GX0, max(30,GY0-14)), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0,0,255), 3)
cv2.imwrite(out_ov, ov)
print("GRILLE: %.1f x %.1f mm | norm x[%.3f-%.3f] y[%.3f-%.3f] | pitch %.2f/%.2f mm | d %.2f mm | open %.3f | rows~%s cols~%s"%(
    gw*mmpx, gh*mmpx, res["grille_norm_bounds"]["x0"], res["grille_norm_bounds"]["x1"],
    res["grille_norm_bounds"]["y0"], res["grille_norm_bounds"]["y1"],
    res["pitch_mm"]["x"] or 0, res["pitch_mm"]["y"] or 0, res["hole_diameter_mm"], open_area,
    res["rows_est"], res["cols_est"]))
