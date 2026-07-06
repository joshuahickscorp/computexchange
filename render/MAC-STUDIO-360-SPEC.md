# Mac Studio · 360 spec (researched 2026-07 · 2025 M4 Max / M3 Ultra, the current Studio)

The Studio model (build_mac_studio in build_scene.py) has an accurate FRONT/tone. For 360 it needs
the REAR (ports + the exhaust vent), sides, top verified. Body 197 x 197 x 95 mm, aluminium.
Dash gate: middot only.

## REAR FACE (197 wide x 95 tall) · CORRECTED 2026-07-06 from Apple press photos (GRADING-REPORT)
FLIP-FLOP AUTOPSY: the earlier "large CIRCULAR exhaust vent" + "power inlet at far left" were BOTH
WRONG (a memory guess). Apple's own 2025 back press image shows a RECTANGULAR perforation field and
a different port order. Corrected below · verified against the pixel-measured mm map in the report.
- The exhaust is a **RECTANGULAR hex-packed perforation field ~173 mm wide x ~53 mm tall**, ~12 mm
  side margins, starting ~5 mm below the top-edge roll, ending ~37 mm above the desk (entirely ABOVE
  the port row). Hole dia ~1.3-2 mm at ~2 mm pitch, ~25-30 staggered rows. NOT a circle.
- **Port row** (facing the rear, LEFT -> RIGHT, centers ~23 mm above the desk plane · mm from left edge):
  1. **4x Thunderbolt 5** (USB-C, VERTICAL pills) at ~30/40/50/60 mm.
  2. **1x 10 GbE RJ-45** at ~74 mm (square + tab notch).
  3. **AC power inlet** at ~98.5 mm = **DEAD CENTER** of the row (black 3-lobe cloverleaf recess). NOT far left.
  4. **2x USB-A** (VERTICAL) at ~120/132 mm.
  5. **1x HDMI 2.1** at ~149 mm (horizontal).
  6. **1x 3.5 mm** headphone jack at ~166 mm.
  7. **Power button** at ~180 mm (flush ~11 mm circle, FAR RIGHT · the machine's left-rear corner from the front).
- FRONT (already modelled · CORRECT · do NOT "fix"): 2x USB-C (M4 Max) or 2x TB5 (M3 Ultra) mounted
  VERTICALLY + an SDXC slot to their right + a white status LED far right. Vertical ports are RIGHT.

Model each port as a recessed dark cavity of the right shape/orientation in the aluminium rear; the
exhaust as a recessed RECTANGULAR perforated field. Blank, no text (trademark gate). Keep rear aluminium.

## Sources (rear corrections)
- Apple 2025 back press image (the mm map): https://www.apple.com/newsroom/images/2025/03/apple-unveils-new-mac-studio-the-most-powerful-mac-ever/article/Apple-Mac-Studio-back-250305_big.jpg.large.jpg
- AppleInsider ("power in the middle and a power button to one corner"): https://appleinsider.com/articles/25/04/01/2025-mac-studio-review-one-clear-purchase-choice-for-most-buyers
- Apple Newsroom ("over 4,000 perforations on the back and bottom"): https://www.apple.com/newsroom/2022/03/apple-unveils-all-new-mac-studio-and-studio-display/

## Sources
- Apple Mac Studio tech specs: https://www.apple.com/mac-studio/specs/
- Apple Support · Mac Studio (2025) tech specs: https://support.apple.com/en-us/122211
- MacRumors launch (M4 Max / M3 Ultra, TB5): https://www.macrumors.com/2025/03/05/new-mac-studio-with-m4-max-and-m3-ultra/
- Tom's Hardware review: https://www.tomshardware.com/desktops/mini-pcs/apple-mac-studio-early-2025-review

## TODO for 360
- [ ] REAR IS WRONG (unticked 2026-07-06): the built rear has a CIRCULAR vent + wrong port order. The
      real rear is a RECTANGULAR ~173x53mm perforation field + the port order corrected above (M1/M2).
      The old "[x] reads as the real Mac Studio back" was a false completion · rebuild to the mm map.
- [x] verify sides + top · audited (side yaw 90): clean brushed-aluminium sides with the intake
      band + foot at the base (correct), top aperture/vent already built. Studio is 360-complete.
- [ ] confirm body dimensions (197/197/95) against Apple's spec.
