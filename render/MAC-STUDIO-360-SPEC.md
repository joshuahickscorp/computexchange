# Mac Studio · 360 spec (researched 2026-07 · 2025 M4 Max / M3 Ultra, the current Studio)

The Studio model (build_mac_studio in build_scene.py) has an accurate FRONT/tone. For 360 it needs
the REAR (ports + the big circular exhaust vent), sides, top verified. Body 197 x 197 x 95 mm,
aluminium. Dash gate: middot only.

## REAR FACE (197 wide x 95 tall) · high confidence (identical on M4 Max + M3 Ultra)
- The **upper ~two-thirds is the large CIRCULAR perforated exhaust vent** (the Studio's signature
  back) · a big recessed circle of fine holes, same aluminium.
- A **horizontal port row along the lower strip**, left -> right:
  1. **Power inlet** (recessed AC socket) at the far left.
  2. **4x Thunderbolt 5** (USB-C shaped, 120 Gb/s) · small rounded rectangles.
  3. **2x USB-A** (5 Gb/s) · the classic flat rectangles.
  4. **1x HDMI 2.1** · trapezoid.
  5. **1x 10 GbE** RJ-45 · square with tab notch.
  6. **1x 3.5 mm** headphone jack · small circle at the far right.
- FRONT (already modelled zone): 2x USB-C (M4 Max) or 2x TB5 (M3 Ultra) + an SDXC card slot.

Model each port as a recessed dark cavity of the right shape in the aluminium rear; the exhaust
vent as a recessed perforated circle. Blank, no text (trademark gate). Keep the rear aluminium.

## Sources
- Apple Mac Studio tech specs: https://www.apple.com/mac-studio/specs/
- Apple Support · Mac Studio (2025) tech specs: https://support.apple.com/en-us/122211
- MacRumors launch (M4 Max / M3 Ultra, TB5): https://www.macrumors.com/2025/03/05/new-mac-studio-with-m4-max-and-m3-ultra/
- Tom's Hardware review: https://www.tomshardware.com/desktops/mini-pcs/apple-mac-studio-early-2025-review

## TODO for 360
- [x] build the rear: circular perforated exhaust vent (recessed disc + perforated_band mesh) + the
      lower port row (power, 4x TB5, 2x USB-A, HDMI, RJ-45, 3.5mm). Verified via _audit_desktop.py
      (rear-q34): reads as the real Mac Studio back. Refine later: port-size differentiation.
- [ ] verify sides + top (the top has the aperture/vent detail already) from references.
- [ ] confirm body dimensions (197/197/95) against Apple's spec.
