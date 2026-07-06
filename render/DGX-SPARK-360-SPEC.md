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

## FRONT/REAR = METAL FOAM (RESOLVED 2026-07-06 · the existing foam3d is CORRECT · do NOT rebuild)
DISPUTE RESOLVED against the Chargerlab teardown: the gold front is genuinely **METAL FOAM** — "the
front of the chassis features metal foam decoration... two metal foam front/back panels, strikingly
similar to the NVIDIA DGX A100 and H100 ... a SOLID POROUS METAL STRUCTURE rather than a perforated
mesh design." So:
- The gated `foam3d` geometry+material (open-cell metal foam) is ACCURATE · KEEP it · do NOT rework it
  into a cheese-grater hole-lattice (that would be a REGRESSION · the DGX-1/Station grater is a
  different, larger product).
- OVERRULED (2 wrong claims): (1) my own earlier "cheese-grater" note above · WRONG · conflated the
  gold-speckled foam + the DGX-Station lineage with a machined grille. (2) the forensic panel
  (w5oyz13ew) "rebuild the front as a countersunk-hole lattice" · WRONG · the real front is porous
  foam, which legitimately looks gold-speckled · the panel also mis-called the real hand-hold cutouts
  "fabricated ovals". The vision panel is unreliable on hardware-accuracy facts (also wrong on the
  5090 LEDs + the Studio proportions) · trust the sourced teardown.
- Source: Chargerlab teardown https://www.chargerlab.com/teardown-of-nvidia-dgx-spark-4tb/ ·
  corroborated StorageReview (gold-speckled metallic finish + hand-hold cutouts + green logo badge).
- Present + correct: metal-foam front (+ rear), miniature hand-hold cutouts (DGX-handle nod), green
  logo badge (blank per trademark gate), air intakes top+bottom. The ONLY open polish: the foam
  MATERIAL could read a hair less "sparkly" and more matte-porous · but it is gated/pinned (SP10/SP11)
  and accurate · touch only with a measured before/after, never a wholesale rebuild.
