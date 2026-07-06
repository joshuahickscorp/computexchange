# DGX Spark · 360 spec (researched 2026-07 for the rear/side/top build)

The Spark model (build_dgx_spark in build_scene.py) has an accurate FRONT (champagne pill bezel +
foam top). For 360 completion it needs the REAR I/O, sides, and top verified. Body 150 x 150 x
50.5 mm (front strip is 150 x 50.5). Finish: champagne/gold metal with the perforated foam-texture
top. Dash gate: middot only.

## REAR PANEL I/O (150mm-wide x 50.5mm-tall rear face) · left -> right, high confidence
1. **Power button** (small, round/recessed) at the far left.
2. **4x USB-C (Type-C)** in a row. The **leftmost is the 240W USB-C Power Delivery input** (marked
   with a DC symbol); the other **3 are 20 Gbps with DisplayPort alt-mode**.
3. **1x HDMI 2.1a** display out.
4. **1x RJ-45** 10 GbE ethernet.
5. **2x QSFP56** cages (200 GbE total, driven by an integrated NVIDIA ConnectX-7 SmartNIC) — the
   two largest connectors, at the far right. Wider/taller cage openings than the RJ-45.

Model each as a recessed dark cavity of the right shape in the champagne rear face (blank, no text
per the trademark gate). USB-C = small rounded slots; HDMI = the familiar trapezoid; RJ-45 = square
with the tab notch; QSFP = larger rectangular cages. Keep the rear face champagne to match the body.

## Sources
- NVIDIA DGX Spark User Guide · Hardware Overview: https://docs.nvidia.com/dgx/dgx-spark/hardware.html
- StorageReview DGX Spark review: https://www.storagereview.com/review/nvidia-dgx-spark-review-the-ai-appliance-bringing-datacenter-capabilities-to-desktops
- Chargerlab teardown: https://www.chargerlab.com/teardown-of-nvidia-dgx-spark-4tb/
- NVIDIA DGX Spark Quick Start Guide (PDF): https://www.nvidia.com/content/dam/en-zz/Solutions/dgx-spark/DGX-Spark-Quick-Start-Guide.pdf

## TODO for 360
- [x] build the rear I/O row into build_dgx_spark (11 recessed cavities: power/4x USB-C/2x USB-A/
      HDMI/RJ-45/2x QSFP) · verified via _audit_desktop.py (rear-q34): reads as the real port bank.
      Refine later: differentiate port sizes more (USB-C vs QSFP) for a dedicated rear hero.
- [x] verify sides + top · audited (side yaw 90): clean champagne sides (correct · plain metal),
      foam-texture top + vent panel already built. Spark is 360-complete (front/rear/sides/top).
- [ ] confirm every body dimension against a source (150/150/50.5).

## FRONT/REAR MESH · "cheese-grater" (research 2026-07 · fixes the panel's 'procedural glitter' tell)
The gold front is NOT a random speckle/foam · it is a STRUCTURED organic lattice of airflow holes —
the miniaturised DGX Station / Mac-Pro "cheese-grater". Reviews: "gold-speckled metallic finish",
"cheese-grater gold front", "organic mesh for the front and rear, modeled after the DGX-1". For
photoreal the front must read as a PRECISE REPEATING 3D hole array (hemispherical/tapered dimples in
a hex or organic pattern, each catching a rim of key light on its lip), gold ANODISED ALUMINIUM
(warm metallic), NOT procedural glitter. Also present: miniature hand-hold cutouts (a nod to the big
DGX handles) + an NVIDIA green logo badge (blank per the trademark gate). Both FRONT and REAR are mesh.
- Sources: StorageReview (cheese-grater, gold-speckled), TheRegister first-look, Exxact/SabrePC (all-gold
  chassis + organic mesh front+rear, DGX-1 lineage). · NEXT SPARK PASS: replace the speckle look with a
  real dimple-lattice mesh material/geometry · touch the gated Spark only with care (measure before/after).
