# render/ref/rack/SOURCES.md · reference library for the third oracle (person-owned GPU rack)

Private comparison material only. Never shipped, never a texture, never traced into a shipped
asset. Archetype: D-ARCH A-prosumer (see D-ARCH.md) · a 42U enclosure a person owns, homelab
fill: 3x 4U GPU nodes + 1U switch + 2U UPS + blanking + cable management + honest empty Us.
Anchor: the EIA-310 U-module (1U = 44.45mm exactly) plus per-part vendor mechanical specs ·
unlike the desktops, most geometry is STANDARD-DERIVED, with photos anchoring feature layout
and tone. Every file below was downloaded, integrity-checked, and (key images) eyeballed by
the driver. Recon provenance in RECON-D-ARCH.md. Dash gate: middot only.

Grade key: A = clean near-orthographic photo / dimensioned drawing or spec · B = usable,
off-axis or small or busy · C = detail/low-res only · CGI = vendor render (geometry cross-check
ONLY, never tone) · GAP = not obtained.

## Coverage matrix · canonical parts

| part | view | best file | resolution | grade | note |
|---|---|---|---|---|---|
| U-module + hole pattern | dimensioned standard | standards/eia-rs310-1968.pdf | vector scan | A | PRIMARY SOURCE (1968 EIA RS-310 scan, OCR-verified): U 44.45mm, universal spacing 0.625/0.625/0.500in, hole centers 6.35/22.25/38.1mm from U boundary, opening 450.85 min, panel 482.60, span 465.12 |
| square hole / flange detail | dimensioned figures | standards/intel-rack-compat-guide-r24.pdf | vector | A | cites ANSI/EIA-310-D-1992: 9.5mm square holes, round alt 7.1mm, flange keep-outs (16.77mm depth, 50mm height) |
| enclosure (42U) | dimensioned drawings | enclosure/apc-netshelter-sx-manual.pdf | vector | A | full install/customization manual: 42U=1991mm H, 600/750 W, 1070/1200 D, rail adjust 191-935mm (600w), factory flange for 737mm equipment, door open-area tables, casters/levelers |
| enclosure cross-checks | dimensioned drawings | enclosure/vertiv-vr31xx/33xx-submittal.pdf (6 models) · tripplite-*-drawing.pdf (4 models) | vector CAD | A | three independent vendors agree on the standard-derived geometry · over-determined |
| enclosure | 3/4 hero | enclosure-photos/ar3140_hero.jpg | 1500x1500 | A/B | reads PHOTOGRAPHIC (driver-inspected); per-image photo-vs-CGI vetting pending for the rest of the gallery |
| enclosure | front straight-on | enclosure-photos/ar3140_newegg_front.jpg | 640x1650 | B | RELABELED by driver inspection: split perforated doors = REAR elevation (SX front is single-door). Front elevation to be cross-derived from hero + drawings |
| enclosure | door open / rails | enclosure-photos/ar3140_gallery_5.jpg (+7,8,9) | 1500x1500 | CGI | driver-inspected: vendor render · rail/channel geometry cross-check only |
| enclosure | rear cable dress | enclosure-photos/ar3140_gallery_3.jpg (+4) | 1500x1500 | B | RELABELED: rear finger-duct + green patch bundles (was mis-sold as populated front) · cable-dress reference |
| enclosure | full gallery | enclosure-photos/ar3140_gallery_1-21.jpg | 1500x1500 | mixed | classify per-image at measurement time |
| node · SilverStone RM44 | front straight-on | node/rm44_front_A.jpg | 1600x1600 | A | REAL PHOTO (driver-inspected): full triangular-perforation mesh door, center lock, matte black powder-coat, mesh reads through to interior darkness · THE node anchor |
| node · RM44 | mesh macro | node/rm44_fan_detail.jpg | 1600x1600 | A | perforation pitch measurement source |
| node · RM44 | front I/O macro | node/rm44_io_macro.jpg | 1600x1600 | B | I/O cluster detail (B/W stylized) |
| node · RM44 | 3/4 · rear · bottom | node/rm44_q34.jpg · rm44_rear.jpg · rm44_bottom_feet.jpg | 640x480 | B/C | rear: 2x 80mm fans + 8-slot PCIe wall. Bottom shows FEET · rack-ear hardware NOT photographed (gap 3) |
| node dims | vendor spec | (recorded · vendor feed via Newegg markup) | text | A | 440 x 176 x 468 mm (W/H/D) verbatim |
| node alt · Sliger CX4712 | 3/4 set | node/sliger_cx4712_q34_*.webp | 1600px | B | real photos, no straight-on front · optional second model ONLY if the panel names array uniformity |
| node cross-check · Rosewill | dimensioned manual | node/rosewill_rsv-l4500_manual.pdf + rsv-l4500u_front.jpg | vector + 640px | B | dimensioned photo overlay (635x427x178) · generic-4U proportion cross-check |
| switch · MikroTik CRS354 | dimensioned drawing | switch/mikrotik-crs354-dimensions-cad.pdf | vector CAD | A | 44.3 x 443 x 297 mm (+7mm ear projection) · official vendor CAD |
| switch · CRS354 | front straight-on | switch/crs354_sth_front.jpg | 800x483 | A | REAL PHOTO w/ EXIF (driver-inspected): 48x RJ45 grouped grid + 4x SFP+ + 2x QSFP+ · WHITE chassis (deliberate material variety in the fill) |
| switch alt · CRS326 | CAD + photos | switch/mikrotik-crs326-* · crs326_sth_* | vector + 800x600 | A/B | fallback / second network unit |
| UPS · APC SMT1500RM2UC | official dims | ups/smt1500rm2uc-datasheet.pdf (+2200/3000 variants) | text spec | A | 86 x 432 x 477 mm official Schneider datasheet |
| UPS | front face | ups/smt1500rm2u_amazon_cgi.jpg | 1500x384 | CGI | geometry/layout only (LCD, button cluster, vents) |
| UPS | front face photo | ups/smt1500rm2u_coasttec_photo.jpg | 440x352 | C | only real photo found · low-conf tone patch source, flagged (gap 2) |
| blanking panel | front + 3/4 + macro | accessories/blankb1_front.jpg · blankb1_q34.jpg · blankb1_macro_cagenuts.jpg | 1500px | A | real photos: powder-coat texture, flange, CAGE NUTS + screws in macro |
| blanking dims | vendor spec | accessories/apc-ar8136blk-blanking-spec.pdf | text | A | 483 x 45 mm 1U toolless |
| cage nuts | macros | accessories/cagenuts_pile_macro.jpg · cagenut_single_macro.jpg | 1000x1000 | A | zinc texture + spring-wing geometry (9.5mm sq hole per standard row) |
| cable manager 1U | front | accessories/cableduct1u_front.jpg | 500x500 | C | finger-duct face · exact depth dims = open row (minor) |
| zero-U PDU | spec + image | accessories/apc-ap8868-pdu-spec.pdf · ap8641_pdu.png | text + 467px | A/C | 1829x56x51mm official · rear-mounted, likely invisible in the front orbit |
| rails/hardware | spec matrix | accessories/dell-rail-matrix.pdf · chatsworth-1521x-datasheet.pdf | vector | A | flange keep-outs, rail dims cross-checks |

## Coverage matrix · assembly gestalt (composition, fill density, cable dress)

| view | best file | resolution | grade | note |
|---|---|---|---|---|
| populated homelab racks (real) | homelab-gestalt/cavelab_2024_rack.jpg · cavelab_2019 · dimitrije_cabinet · linuxblog_wall_rack · xtremeownage_front/servers · shawnmix | 587-1500px | A/B | six EXIF/driver-vetted REAL photos · fill density, UPS-at-bottom, cable spaghetti truth, LED states · all 9-20U formats (gap 1: no 42U) |
| datacenter rows (context only) | context/sth_eos_racks.jpg · sth_colossus_hall.jpg | ~1200px | B | what the object is NOT (scale contrast) + panel-pool candidates |

## Declared gaps (three failed avenues each)

1. **Full-height populated 42U homelab rack photo.** Tried: (a) r/homelab · Reddit blocks
   fetch (403 + tool refusal, JSON API 403); (b) homelab YouTubers' sites (Craft Computing,
   TechnoTim, Hardware Haven, Jeff Geerling) · video-only or mini-rack, no stills; (c) vendor
   lifestyle photography (StarTech 403, NavePoint/Sysracks Amazon assets = marketing
   infographics). MITIGATION: assembly is U-arithmetic-derived; density/dress gestalt from
   the six smaller-rack photos scales by module.
2. **UPS face photo >= 800px.** Tried: (a) se.com/apc.com product galleries · 403 anti-bot;
   (b) B&H/CDW/eBay listings · 403; (c) Amazon · CGI catalog family only. MITIGATION:
   official dims + CGI layout + 440px real photo for low-conf tone; UPS face flagged
   inferred-tone in the ledger.
3. **RM44 rack-ear hardware photo.** Tried: (a) Newegg full gallery (feet shown, no ears);
   (b) silverstonetek.com · 403; (c) review search in-set · none surfaced. MITIGATION: ears
   are EIA-standard geometry (flange + thumbscrews per Intel guide fig + blankb1 macro);
   modeled generic, logged inferred.
4. (minor) 1U finger-duct exact depth · one avenue left to try (assets.tripplite.com spec
   PDF) at measurement time; face proportions readable from the 500px photo + 1U standard.

## Search avenues worked (recon + hunts, 2026-07-04)

NVIDIA press/docs (CGI-only for marketing; user guides = line art + exact specs) · STH lab +
show coverage (srcset-audited) · enclosure vendors (Vertiv/APC/TrippLite drawings fetched;
Rittal/Eaton partner-gated or unreachable) · EIA/IEC standards (1968 primary scan + Intel
EIA-310-D guide; -D text itself paywalled) · retail product photography (Newegg CDN 640-cap
discovered, MPS/AplusContent 1600px exception found for RM44) · MikroTik official CAD ·
Schneider official datasheets via distributor mirrors · homelab blogs (EXIF-verified) ·
Reddit/eBay/BH/CDW/StarTech/Supermicro/Gigabyte/ASUS-server/silverstonetek all 403 to
automated fetch (noted for manual-browser retry if ever needed).
