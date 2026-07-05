# render/ref/rack/RECON-D-ARCH.md · pre-matrix reference reconnaissance (D-ARCH input)

Date: 2026-07-04. Purpose: reference-SUFFICIENCY assessment feeding the D-ARCH archetype
decision. This is NOT the coverage matrix (that is gate 3, after sign-off) · it records what
a bounded recon verified, so the full hunt starts warm. VERIFIED = fetched and read this
session. PROVISIONAL = surfaced by a worker from prior knowledge, not fetched. Dash gate:
middot only.

## VERIFIED · measurement-grade sources

| item | value / content | source | grade |
|---|---|---|---|
| DGX B200 node mechanical spec | 10U rackmount · H 444mm (17.5in) · W 482.3mm max · D 897.1mm max · 142.4kg | docs.nvidia.com/dgx/dgxb200-user-guide (Mechanical Specifications; intro page has labeled front-panel w/ + w/o bezel, rear panel, tray figures · NOT dimensioned drawings) | A (spec) |
| EIA-310 U module | 1U = 44.45mm (1.75in); hole centers 6.35 / 22.25 / 38.1mm from U boundary; hole spacing pattern 12.70 / 15.88 / 15.88mm; panel height h = 44.45n - 0.8mm | en.wikipedia.org/wiki/19-inch_rack + ddbunlimited.com racking-spec-layout (corroborating) | A (standard) |
| EIA-310 widths | panel/ear width 482.6mm (19in, universal agreement); rail opening 450.85mm (17.75in) with a 450mm-minimum camp (RackSolutions/NavePoint/IEC summary) · spread recorded | Wikipedia + micropolis.com + racksolutions.com + navepoint.com + GlobalSpec IEC 60297-1 summary | A (standard, spread noted) |
| Vertiv VR enclosure family | FULL 3-sheet dimensioned submittal drawings fetched + parsed for VR3100/VR3150/VR3350 (42U) and VR3107/VR3307/VR3357 (48U): outer H/W/D (42U: 1998mm front-view H; 48U: 2265mm), EIA rail opening 452mm, rail hole centers 466mm, frame opening widths (600mm rack: 499mm · 800mm rack: 699mm), front rail 40mm from front, rail-to-rail 740mm factory, hole pattern detail (44mm U spacing, 16mm pitch, 13x6.5mm slots, 6.2mm round), door 1.5mm 16GA MESH · 1/4in HEX on staggered centers · 77% OPEN AREA, side 0.9mm 20GA, top 1.2mm 18GA, casters/levelers layouts, PDU brackets | vertiv.com submittal-drawing PDFs (URLs in the recon transcript · vr3100/3150/3350/3357/3307/3107) + VR guide-spec PDF + selection guide | A (dimensioned CAD) |

## VERIFIED · photography (ServeTheHome, srcset-checked masters)

Archetype B · NVIDIA DGX GB200 NVL72 (GTC 2024, real show-floor photos, servethehome.com
"this-is-the-nvidia-dgx-gb200-nvl72"):
- full-rack FRONT ~929x800 (A for composition · show-floor light) · full-rack REAR (busbar,
  liquid manifolds, NVLink copper spine) ~800x626 (A) · compute-node FRONT 1199x800 (A) ·
  node bank + switches 1200x800 (A) · NVSwitch shelf front w/ gold handles (A) · scale shot
  with person 1067x800 (B) · internals (B/C).
- Partner/variant racks: Supermicro NVL72 Computex 2024 (segmented shots, B) · LITEON 48U MGX
  at OCP 2024 (B) · GB300: Supermicro rack (A/B), Dell/CoreWeave XE9712 real deployment
  (696x392, C/B).
- CEILING: STH masters top out ~800-1700px on the long edge · ~0.4-0.6 px/mm on a ~2.3m rack.
  Composition/proportion/material-family cues, NOT texture plates or tone pins. NVIDIA
  first-party NVL72 front views are believed render-heavy (PROVISIONAL · press-kit sweep is a
  gate-3 avenue).

Archetype A · air-cooled node hardware (real photos):
- ASRock Rack 8U8X-GNR2 SYN B200 (HGX B200 8-GPU) STH LAB review: ~25 clean studio shots at
  ~1200px inc. STRAIGHT-ON FRONT bezel/drive bays (A for the node face · controlled lighting,
  usable for tone pinning).
- Supermicro SYS-821GE-TNHR 8x H100 STH lab teardown (A/B · liquid-cooled variant).
- Supermicro HGX B200 racked at Lambda/Cologix (B · real install, ~800px).
- HGX B200 bare baseboard (STH/Astera, A detail).
- Populated-row context: Equinix SV11 DGX B200 SuperPOD aisle + rear-row shots (800x450, B).
- GAP (STH only · untested elsewhere): no dedicated straight-on photo of a POPULATED
  air-cooled DGX/HGX 19in enclosure. Gate-3 avenues: NVIDIA SuperPOD/Eos install imagery,
  xAI Colossus tour (STH, PROVISIONAL), Lambda/CoreWeave/Microsoft galleries, integrator
  (Supermicro rack-scale) pages, DataCenterDynamics.

## PROVISIONAL (not fetched · gate-3 leads)

- STH xAI Colossus cluster tour (Supermicro HGX racks, rows) · STH DGX H100 show coverage ·
  Dell XE9680 + Supermicro SYS-821GE product pages (photo-vs-render undetermined) · NVIDIA
  Eos/SuperPOD imagery (mixed photo/CGI, needs per-image vetting) · CoreWeave/Azure GB200
  install PR (needs CGI vetting) · GTC 2025 GB300 galleries.
- NVIDIA QM9700 1U InfiniBand switch front (32x OSFP 2x16) · APC NetShelter SX submittal
  drawings · blanking panel / cable-manager / zero-U PDU drawings (rate-limited out of this
  recon; standard vendor PDFs, low risk).

## VERIFIED · late-arriving worker results (fold into the gate-3 matrix)

- EIA hole pattern, PRIMARY source: actual EIA RS-310 (USAS C83.9-1968) scanned standard,
  OCR-read · universal spacing 0.625/0.625/0.500in per 1.75in U, holes at 6.35/22.25/38.1mm
  from the U boundary, panel h = 44.45n - 0.79mm, hole-center span 465.12mm, opening 450.85mm
  min, panel 482.60mm preferred (casa.co.nz PDF). Intel Server Rack Cabinet Compatibility
  Guide Rev 2.4 (cdrdv2-public.intel.com, cites ANSI/EIA-310-D-1992): SQUARE mounting hole
  9.5mm (0.375in), round alt Ø7.1mm, flange detail dims. Cage nut: 9.53mm cutout, M6 body
  ~13.5-13.7mm wide (RS PRO / ITA / Chatsworth). EIA-310-D and IEC 60297-3-100 numerically
  equivalent (no source names a differing value).
- Archetype A populated racks, REAL PHOTOS (verified image URLs, servethehome.com):
  NVIDIA EOS supercomputer · DGX H100 racks (4 nodes/rack): overview hero, rack rows, rear
  liquid manifolds, fiber (2024-02 uploads, ~700-800px, A/B) · xAI Colossus (Supermicro HGX
  H100, mostly liquid-cooled variants): compute-hall wide (A), rack front (A/B), node close
  (C). CONFIRMED-CGI flags for the pool: Meta datacenter imagery = renders/diagrams ·
  CoreWeave = stylized marketing art · Google gallery = real but no GPU-identifiable gear.
- Equinix SV11 SuperPOD aisle (2025-12): one worker labels the racks DGX B200 SuperPOD, the
  other 8x GB200 NVL72 · CONFLICT, resolve at gate 3 before using as either archetype's ref.

## Recon notes

Run 2026-07-04 under heavy transient API rate limiting; several worker fan-outs died and were
salvaged. Every VERIFIED row above was actually fetched/read. The full coverage matrix with
downloaded assets, per-view grades, and three-named-avenue gap declarations is gate 3 work.
