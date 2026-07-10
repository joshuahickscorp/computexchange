# Speculative Render Ladder - 2026-07-09

## Summary

- Source ledger: `docs/speed-lane-reports/spec-lab/cycles_quality_ladder_ledger.jsonl`
- Tile refinement ledger: `docs/speed-lane-reports/spec-lab/tile_refinement_ledger.jsonl`
- Gate rows: `187`
- Actual tile-refinement rows: `4`
- Delivery rows: `141`
- Preview-or-better rows: `161`
- Failed rows: `26`
- Output ledger: `docs/speed-lane-reports/spec-lab/speculative_render_ladder_ledger.jsonl`

## Policy

- Delivery: global SSIM >= `0.98` and worst-tile SSIM >= `0.95`
- Preview: global SSIM >= `0.9` and worst-tile SSIM >= `0.85`
- Global SSIM alone cannot pass.

## Best Delivery Gate

```json
{
  "accepted_tile_fraction_model": 1.0,
  "action": "ship_delivery",
  "denoise_time_s": 0.0,
  "device": "CUDA",
  "draft_samples": 2,
  "draft_time_s": 0.86,
  "escalation": "none",
  "evidence_type": "imported_measured",
  "global_ssim": 1.0,
  "net_speedup_if_shipped_x": 14.3372,
  "p5_tile_ssim": 1.0,
  "png_mae": 0.0,
  "png_maxe": 0.0,
  "protocol": "draft->verify->gate->refine/escalate",
  "ref_samples": 4096,
  "ref_time_s": 12.33,
  "scene": "scene_world_volume.xml",
  "source_ledger": "/Users/scammermike/Downloads/computexchange/docs/speed-lane-reports/spec-lab/cycles_quality_ladder_ledger.jsonl",
  "source_line": 75,
  "tier": "delivery",
  "tile_count": 64,
  "total_time_s": 0.86,
  "variant": "raw",
  "whole_frame_escalated_speedup_x": 0.9348,
  "worst_tile_ssim": 1.0
}
```

## Best Preview Gate

```json
{
  "accepted_tile_fraction_model": 1.0,
  "action": "ship_delivery",
  "denoise_time_s": 0.0,
  "device": "CUDA",
  "draft_samples": 2,
  "draft_time_s": 0.86,
  "escalation": "none",
  "evidence_type": "imported_measured",
  "global_ssim": 1.0,
  "net_speedup_if_shipped_x": 14.3372,
  "p5_tile_ssim": 1.0,
  "png_mae": 0.0,
  "png_maxe": 0.0,
  "protocol": "draft->verify->gate->refine/escalate",
  "ref_samples": 4096,
  "ref_time_s": 12.33,
  "scene": "scene_world_volume.xml",
  "source_ledger": "/Users/scammermike/Downloads/computexchange/docs/speed-lane-reports/spec-lab/cycles_quality_ladder_ledger.jsonl",
  "source_line": 75,
  "tier": "delivery",
  "tile_count": 64,
  "total_time_s": 0.86,
  "variant": "raw",
  "whole_frame_escalated_speedup_x": 0.9348,
  "worst_tile_ssim": 1.0
}
```

## Ladder Rows

| Scene | Variant | Draft spp | Tier | Global | Worst tile | Speedup if shipped | Action | Evidence |
|---|---|---:|---|---:|---:|---:|---|---|
| cx_many_glass.xml | oidn | 1 | preview | 0.991011233 | 0.92108417 | 3.7074 | ship_preview_or_escalate_for_delivery | imported_measured |
| cx_many_glass.xml | oidn | 1 | preview | 0.991011266 | 0.921084166 | 3.7302 | ship_preview_or_escalate_for_delivery | imported_measured |
| cx_many_glass.xml | oidn | 2 | preview | 0.994772796 | 0.946685087 | 3.6771 | ship_preview_or_escalate_for_delivery | imported_measured |
| cx_many_glass.xml | oidn | 2 | preview | 0.994772782 | 0.946684771 | 3.592 | ship_preview_or_escalate_for_delivery | imported_measured |
| cx_many_glass.xml | oidn | 4 | delivery | 0.997568241 | 0.974706065 | 1.6358 | ship_delivery | imported_measured |
| cx_many_glass.xml | oidn | 4 | delivery | 0.997568037 | 0.974685209 | 3.6092 | ship_delivery | imported_measured |
| cx_many_glass.xml | oidn | 4 | delivery | 0.997568036 | 0.974685209 | 3.6101 | ship_delivery | imported_measured |
| cx_many_glass.xml | oidn | 8 | delivery | 0.998942888 | 0.990480306 | 1.6049 | ship_delivery | imported_measured |
| cx_many_glass.xml | oidn | 8 | delivery | 0.998944134 | 0.990452736 | 3.6419 | ship_delivery | imported_measured |
| cx_many_glass.xml | oidn | 8 | delivery | 0.998944117 | 0.990452736 | 3.6837 | ship_delivery | imported_measured |
| cx_many_glass.xml | oidn | 16 | delivery | 0.999504879 | 0.99563778 | 1.5798 | ship_delivery | imported_measured |
| cx_many_glass.xml | oidn | 16 | delivery | 0.999507633 | 0.995686364 | 3.6071 | ship_delivery | imported_measured |
| cx_many_glass.xml | oidn | 16 | delivery | 0.999507627 | 0.995686364 | 3.6846 | ship_delivery | imported_measured |
| cx_many_glass.xml | oidn | 32 | delivery | 0.99972641 | 0.997174816 | 1.6194 | ship_delivery | imported_measured |
| cx_many_glass.xml | oidn | 32 | delivery | 0.999729703 | 0.997208784 | 3.5843 | ship_delivery | imported_measured |
| cx_many_glass.xml | oidn | 32 | delivery | 0.9997297 | 0.997208794 | 3.6472 | ship_delivery | imported_measured |
| cx_many_glass.xml | oidn | 64 | delivery | 0.999821221 | 0.997939992 | 3.6095 | ship_delivery | imported_measured |
| cx_many_glass.xml | raw | 1 | fail | 0.975205365 | 0.698278498 | 3.8969 | reject_or_refine | imported_measured |
| cx_many_glass.xml | raw | 1 | fail | 0.975205357 | 0.698278498 | 3.929 | reject_or_refine | imported_measured |
| cx_many_glass.xml | raw | 2 | fail | 0.986169414 | 0.843107388 | 3.8376 | reject_or_refine | imported_measured |
| cx_many_glass.xml | raw | 2 | fail | 0.986169369 | 0.843107388 | 3.7644 | reject_or_refine | imported_measured |
| cx_many_glass.xml | raw | 4 | preview | 0.994483589 | 0.929527014 | 1.7173 | ship_preview_or_escalate_for_delivery | imported_measured |
| cx_many_glass.xml | raw | 4 | preview | 0.994480197 | 0.929763962 | 3.7612 | ship_preview_or_escalate_for_delivery | imported_measured |
| cx_many_glass.xml | raw | 4 | preview | 0.994480193 | 0.929763962 | 3.7842 | ship_preview_or_escalate_for_delivery | imported_measured |
| cx_many_glass.xml | raw | 8 | delivery | 0.997938038 | 0.96762912 | 1.6735 | ship_delivery | imported_measured |
| cx_many_glass.xml | raw | 8 | delivery | 0.997937327 | 0.967733036 | 3.799 | ship_delivery | imported_measured |
| cx_many_glass.xml | raw | 8 | delivery | 0.997937312 | 0.967733036 | 3.8656 | ship_delivery | imported_measured |
| cx_many_glass.xml | raw | 16 | delivery | 0.999215336 | 0.985140527 | 1.6482 | ship_delivery | imported_measured |
| cx_many_glass.xml | raw | 16 | delivery | 0.999216977 | 0.985191632 | 3.7612 | ship_delivery | imported_measured |
| cx_many_glass.xml | raw | 16 | delivery | 0.99921697 | 0.985191632 | 3.8656 | ship_delivery | imported_measured |
| cx_many_glass.xml | raw | 32 | delivery | 0.999681129 | 0.993216772 | 1.6907 | ship_delivery | imported_measured |
| cx_many_glass.xml | raw | 32 | delivery | 0.999682718 | 0.993277893 | 3.7426 | ship_delivery | imported_measured |
| cx_many_glass.xml | raw | 32 | delivery | 0.999682712 | 0.993277893 | 3.8245 | ship_delivery | imported_measured |
| cx_many_glass.xml | raw | 64 | delivery | 0.999854288 | 0.996755943 | 3.7216 | ship_delivery | imported_measured |
| cx_many_glass.xml | raw | 64 | delivery | 0.999854288 | 0.996755943 | 3.7612 | ship_delivery | imported_measured |
| cx_many_glass.xml | raw | 128 | delivery | 0.999925463 | 0.998284948 | 3.61 | ship_delivery | imported_measured |
| cx_many_glass.xml | raw | 256 | delivery | 0.999958066 | 0.999011308 | 3.3119 | ship_delivery | imported_measured |
| cx_many_glass.xml | raw | 512 | delivery | 0.999974529 | 0.999387316 | 2.8651 | ship_delivery | imported_measured |
| cx_many_glass.xml | raw | 1024 | delivery | 0.999983223 | 0.999582903 | 2.2776 | ship_delivery | imported_measured |
| scene_caustics.xml | oidn | 1 | delivery | 1.0 | 1.0 | 3.6632 | ship_delivery | imported_measured |
| scene_caustics.xml | oidn | 2 | delivery | 1.0 | 1.0 | 4.5243 | ship_delivery | imported_measured |
| scene_caustics.xml | oidn | 4 | delivery | 1.0 | 1.0 | 3.9293 | ship_delivery | imported_measured |
| scene_caustics.xml | oidn | 8 | delivery | 1.0 | 1.0 | 3.5097 | ship_delivery | imported_measured |
| scene_caustics.xml | oidn | 16 | delivery | 1.0 | 1.0 | 4.2992 | ship_delivery | imported_measured |
| scene_caustics.xml | oidn | 32 | delivery | 1.0 | 1.0 | 3.7726 | ship_delivery | imported_measured |
| scene_caustics.xml | oidn | 64 | delivery | 1.0 | 1.0 | 3.8299 | ship_delivery | imported_measured |
| scene_caustics.xml | raw | 1 | delivery | 1.0 | 1.0 | 4.0354 | ship_delivery | imported_measured |
| scene_caustics.xml | raw | 2 | delivery | 1.0 | 1.0 | 5.011 | ship_delivery | imported_measured |
| scene_caustics.xml | raw | 4 | delivery | 1.0 | 1.0 | 5.4658 | ship_delivery | imported_measured |
| scene_caustics.xml | raw | 4 | delivery | 1.0 | 1.0 | 4.3019 | ship_delivery | imported_measured |
| scene_caustics.xml | raw | 8 | delivery | 1.0 | 1.0 | 5.1154 | ship_delivery | imported_measured |
| scene_caustics.xml | raw | 8 | delivery | 1.0 | 1.0 | 3.8 | ship_delivery | imported_measured |
| scene_caustics.xml | raw | 16 | delivery | 1.0 | 1.0 | 5.1818 | ship_delivery | imported_measured |
| scene_caustics.xml | raw | 16 | delivery | 1.0 | 1.0 | 4.75 | ship_delivery | imported_measured |
| scene_caustics.xml | raw | 32 | delivery | 1.0 | 1.0 | 5.32 | ship_delivery | imported_measured |
| scene_caustics.xml | raw | 32 | delivery | 1.0 | 1.0 | 4.1081 | ship_delivery | imported_measured |
| scene_caustics.xml | raw | 64 | delivery | 1.0 | 1.0 | 5.1818 | ship_delivery | imported_measured |
| scene_caustics.xml | raw | 64 | delivery | 1.0 | 1.0 | 4.1835 | ship_delivery | imported_measured |
| scene_cube_surface.xml | raw | 4 | preview | 0.988839826 | 0.948606026 | 3.1739 | ship_preview_or_escalate_for_delivery | imported_measured |
| scene_cube_surface.xml | raw | 8 | delivery | 0.995968115 | 0.979001121 | 2.9595 | ship_delivery | imported_measured |
| scene_cube_surface.xml | raw | 16 | delivery | 0.998492206 | 0.992427866 | 2.8816 | ship_delivery | imported_measured |
| scene_cube_surface.xml | raw | 32 | delivery | 0.999444002 | 0.997370094 | 2.92 | ship_delivery | imported_measured |
| scene_cube_surface.xml | raw | 64 | delivery | 0.999789934 | 0.998928998 | 2.8816 | ship_delivery | imported_measured |
| scene_cube_volume.xml | oidn | 1 | delivery | 0.999190394 | 0.979741077 | 5.6061 | ship_delivery | imported_measured |
| scene_cube_volume.xml | oidn | 1 | delivery | 0.999190325 | 0.979622568 | 7.6808 | ship_delivery | imported_measured |
| scene_cube_volume.xml | oidn | 2 | delivery | 0.99947653 | 0.98792556 | 5.3079 | ship_delivery | imported_measured |
| scene_cube_volume.xml | oidn | 2 | delivery | 0.999476578 | 0.987860653 | 7.5158 | ship_delivery | imported_measured |
| scene_cube_volume.xml | oidn | 4 | delivery | 0.999683115 | 0.988645519 | 5.0929 | ship_delivery | imported_measured |
| scene_cube_volume.xml | oidn | 4 | delivery | 0.999683172 | 0.988580375 | 7.5989 | ship_delivery | imported_measured |
| scene_cube_volume.xml | oidn | 8 | delivery | 0.999717924 | 0.989543232 | 5.0189 | ship_delivery | imported_measured |
| scene_cube_volume.xml | oidn | 8 | delivery | 0.999717883 | 0.989543232 | 7.6007 | ship_delivery | imported_measured |
| scene_cube_volume.xml | oidn | 16 | delivery | 0.999546566 | 0.978834995 | 5.3462 | ship_delivery | imported_measured |
| scene_cube_volume.xml | oidn | 16 | delivery | 0.999546762 | 0.978834995 | 7.43 | ship_delivery | imported_measured |
| scene_cube_volume.xml | oidn | 32 | delivery | 0.999734679 | 0.987230828 | 5.5056 | ship_delivery | imported_measured |
| scene_cube_volume.xml | oidn | 32 | delivery | 0.999734777 | 0.987230828 | 7.1164 | ship_delivery | imported_measured |
| scene_cube_volume.xml | oidn | 64 | delivery | 0.999871443 | 0.994354384 | 5.0928 | ship_delivery | imported_measured |
| scene_cube_volume.xml | raw | 1 | fail | 0.83820959 | 0.371408167 | 6.0992 | reject_or_refine | imported_measured |
| scene_cube_volume.xml | raw | 1 | fail | 0.838208271 | 0.371408167 | 8.6795 | reject_or_refine | imported_measured |
| scene_cube_volume.xml | raw | 2 | fail | 0.926569042 | 0.591646972 | 5.6769 | reject_or_refine | imported_measured |
| scene_cube_volume.xml | raw | 2 | fail | 0.926569438 | 0.591664255 | 8.358 | reject_or_refine | imported_measured |
| scene_cube_volume.xml | raw | 4 | fail | 0.965934861 | 0.76551179 | 5.4265 | reject_or_refine | imported_measured |
| scene_cube_volume.xml | raw | 4 | fail | 0.965934902 | 0.76551179 | 8.4625 | reject_or_refine | imported_measured |
| scene_cube_volume.xml | raw | 8 | preview | 0.98337316 | 0.867669714 | 5.3478 | ship_preview_or_escalate_for_delivery | imported_measured |
| scene_cube_volume.xml | raw | 8 | preview | 0.983373675 | 0.867683468 | 8.4625 | ship_preview_or_escalate_for_delivery | imported_measured |
| scene_cube_volume.xml | raw | 16 | preview | 0.991824361 | 0.931548839 | 5.7209 | ship_preview_or_escalate_for_delivery | imported_measured |
| scene_cube_volume.xml | raw | 16 | preview | 0.991824777 | 0.931545948 | 8.2561 | ship_preview_or_escalate_for_delivery | imported_measured |
| scene_cube_volume.xml | raw | 32 | delivery | 0.995965093 | 0.965073782 | 5.904 | ship_delivery | imported_measured |
| scene_cube_volume.xml | raw | 32 | delivery | 0.99596516 | 0.965073782 | 7.8721 | ship_delivery | imported_measured |
| scene_cube_volume.xml | raw | 64 | delivery | 0.997972352 | 0.982018952 | 7.3333 | ship_delivery | imported_measured |
| scene_cube_volume.xml | raw | 64 | delivery | 0.997972425 | 0.982018952 | 5.4667 | ship_delivery | imported_measured |
| scene_cube_volume.xml | raw | 128 | delivery | 0.998970082 | 0.990779048 | 6.4952 | ship_delivery | imported_measured |
| scene_cube_volume.xml | raw | 256 | delivery | 0.999470018 | 0.995301193 | 5.6364 | ship_delivery | imported_measured |
| scene_cube_volume.xml | raw | 512 | delivery | 0.999723485 | 0.997517213 | 4.2893 | ship_delivery | imported_measured |
| scene_cube_volume.xml | raw | 1024 | delivery | 0.99984766 | 0.998651041 | 2.927 | ship_delivery | imported_measured |
| scene_cube_volume.xml | tile_refine | 16 | fail | 0.716872525 | 0.113994193 | 1.9554 | reject_or_fallback | measured_tile_refinement |
| scene_cube_volume.xml | tile_refine | 16 | fail | 0.716872373 | 0.113992474 | 1.9367 | reject_or_fallback | measured_tile_refinement |
| scene_cube_volume.xml | tile_refine | 16 | preview | 0.99388874 | 0.946571643 | 1.617 | refine_more_or_fallback_for_delivery | measured_tile_refinement |
| scene_cube_volume.xml | tile_refine | 16 | preview | 0.99388874 | 0.946571643 | 3.4897 | refine_more_or_fallback_for_delivery | measured_tile_refinement |
| scene_monkey.xml | oidn | 1 | delivery | 0.998109142 | 0.978757605 | 5.3347 | ship_delivery | imported_measured |
| scene_monkey.xml | oidn | 1 | delivery | 0.998109142 | 0.978757593 | 6.278 | ship_delivery | imported_measured |
| scene_monkey.xml | oidn | 2 | delivery | 0.998984822 | 0.987727855 | 5.0282 | ship_delivery | imported_measured |
| scene_monkey.xml | oidn | 2 | delivery | 0.99898482 | 0.987727924 | 6.2817 | ship_delivery | imported_measured |
| scene_monkey.xml | oidn | 4 | delivery | 0.999513701 | 0.991403939 | 2.4251 | ship_delivery | imported_measured |
| scene_monkey.xml | oidn | 4 | delivery | 0.999519756 | 0.991837139 | 5.2668 | ship_delivery | imported_measured |
| scene_monkey.xml | oidn | 4 | delivery | 0.99951975 | 0.991837139 | 6.2831 | ship_delivery | imported_measured |
| scene_monkey.xml | oidn | 8 | delivery | 0.999765015 | 0.993630568 | 2.4599 | ship_delivery | imported_measured |
| scene_monkey.xml | oidn | 8 | delivery | 0.999770702 | 0.994004097 | 4.6601 | ship_delivery | imported_measured |
| scene_monkey.xml | oidn | 8 | delivery | 0.999770701 | 0.994003745 | 5.79 | ship_delivery | imported_measured |
| scene_monkey.xml | oidn | 16 | delivery | 0.999864563 | 0.995853736 | 2.393 | ship_delivery | imported_measured |
| scene_monkey.xml | oidn | 16 | delivery | 0.999870398 | 0.996190494 | 5.2703 | ship_delivery | imported_measured |
| scene_monkey.xml | oidn | 16 | delivery | 0.9998704 | 0.996190494 | 5.9241 | ship_delivery | imported_measured |
| scene_monkey.xml | oidn | 32 | delivery | 0.999917992 | 0.997429671 | 2.3276 | ship_delivery | imported_measured |
| scene_monkey.xml | oidn | 32 | delivery | 0.999923745 | 0.997757098 | 4.9487 | ship_delivery | imported_measured |
| scene_monkey.xml | oidn | 32 | delivery | 0.999923748 | 0.997757002 | 6.1338 | ship_delivery | imported_measured |
| scene_monkey.xml | oidn | 64 | delivery | 0.999946493 | 0.997786858 | 4.2477 | ship_delivery | imported_measured |
| scene_monkey.xml | raw | 1 | fail | 0.973619595 | 0.328145653 | 5.9167 | reject_or_refine | imported_measured |
| scene_monkey.xml | raw | 1 | fail | 0.973619563 | 0.328145653 | 7.1667 | reject_or_refine | imported_measured |
| scene_monkey.xml | raw | 2 | fail | 0.987411338 | 0.52578367 | 5.4615 | reject_or_refine | imported_measured |
| scene_monkey.xml | raw | 2 | fail | 0.987411333 | 0.52578367 | 7.0685 | reject_or_refine | imported_measured |
| scene_monkey.xml | raw | 4 | fail | 0.994308944 | 0.702331872 | 6.88 | reject_or_refine | imported_measured |
| scene_monkey.xml | raw | 4 | fail | 0.994304903 | 0.702008074 | 2.7937 | reject_or_refine | imported_measured |
| scene_monkey.xml | raw | 4 | fail | 0.994308945 | 0.702334334 | 5.7374 | reject_or_refine | imported_measured |
| scene_monkey.xml | raw | 4 | fail | 0.994308944 | 0.702331872 | 7.0685 | reject_or_refine | imported_measured |
| scene_monkey.xml | raw | 8 | fail | 0.997478673 | 0.839460065 | 6.2169 | reject_or_refine | imported_measured |
| scene_monkey.xml | raw | 8 | fail | 0.99747539 | 0.839624001 | 2.7937 | reject_or_refine | imported_measured |
| scene_monkey.xml | raw | 8 | fail | 0.997478672 | 0.839460065 | 5.0265 | reject_or_refine | imported_measured |
| scene_monkey.xml | raw | 8 | fail | 0.997478673 | 0.839460065 | 6.45 | reject_or_refine | imported_measured |
| scene_monkey.xml | raw | 16 | preview | 0.998963606 | 0.927667245 | 6.2169 | ship_preview_or_escalate_for_delivery | imported_measured |
| scene_monkey.xml | raw | 16 | preview | 0.9989621 | 0.927770238 | 2.7077 | ship_preview_or_escalate_for_delivery | imported_measured |
| scene_monkey.xml | raw | 16 | preview | 0.998963606 | 0.927667245 | 5.7959 | ship_preview_or_escalate_for_delivery | imported_measured |
| scene_monkey.xml | raw | 16 | preview | 0.998963605 | 0.927667245 | 6.6154 | ship_preview_or_escalate_for_delivery | imported_measured |
| scene_monkey.xml | raw | 32 | delivery | 0.999617997 | 0.975919427 | 6.6154 | ship_delivery | imported_measured |
| scene_monkey.xml | raw | 32 | delivery | 0.999616785 | 0.976002532 | 2.6269 | ship_delivery | imported_measured |
| scene_monkey.xml | raw | 32 | delivery | 0.999617996 | 0.975919427 | 5.3585 | ship_delivery | imported_measured |
| scene_monkey.xml | raw | 32 | delivery | 0.999617996 | 0.975919427 | 6.88 | ship_delivery | imported_measured |
| scene_monkey.xml | raw | 64 | delivery | 0.999822485 | 0.988598461 | 6.3704 | ship_delivery | imported_measured |
| scene_monkey.xml | raw | 64 | delivery | 0.999822484 | 0.988598461 | 4.544 | ship_delivery | imported_measured |
| scene_sphere_bump.xml | oidn | 1 | delivery | 0.997314035 | 0.982955732 | 3.3918 | ship_delivery | imported_measured |
| scene_sphere_bump.xml | oidn | 1 | delivery | 0.99731402 | 0.982954204 | 3.3232 | ship_delivery | imported_measured |
| scene_sphere_bump.xml | oidn | 2 | delivery | 0.998726306 | 0.992546913 | 3.5458 | ship_delivery | imported_measured |
| scene_sphere_bump.xml | oidn | 2 | delivery | 0.998726307 | 0.992546549 | 3.3944 | ship_delivery | imported_measured |
| scene_sphere_bump.xml | oidn | 4 | delivery | 0.999246407 | 0.995563854 | 3.5955 | ship_delivery | imported_measured |
| scene_sphere_bump.xml | oidn | 4 | delivery | 0.999246445 | 0.995563854 | 3.3501 | ship_delivery | imported_measured |
| scene_sphere_bump.xml | oidn | 8 | delivery | 0.99958449 | 0.997588622 | 3.591 | ship_delivery | imported_measured |
| scene_sphere_bump.xml | oidn | 8 | delivery | 0.999584489 | 0.997588592 | 3.0764 | ship_delivery | imported_measured |
| scene_sphere_bump.xml | oidn | 16 | delivery | 0.999761654 | 0.998580176 | 4.017 | ship_delivery | imported_measured |
| scene_sphere_bump.xml | oidn | 16 | delivery | 0.99976165 | 0.998580198 | 3.0747 | ship_delivery | imported_measured |
| scene_sphere_bump.xml | oidn | 32 | delivery | 0.999883032 | 0.999356 | 3.5507 | ship_delivery | imported_measured |
| scene_sphere_bump.xml | oidn | 32 | delivery | 0.999883029 | 0.999356 | 2.9697 | ship_delivery | imported_measured |
| scene_sphere_bump.xml | oidn | 64 | delivery | 0.999919802 | 0.999535787 | 3.4575 | ship_delivery | imported_measured |
| scene_sphere_bump.xml | raw | 1 | fail | 0.951877975 | 0.739435215 | 3.8714 | reject_or_refine | imported_measured |
| scene_sphere_bump.xml | raw | 1 | fail | 0.951877979 | 0.739435215 | 3.7971 | reject_or_refine | imported_measured |
| scene_sphere_bump.xml | raw | 2 | preview | 0.987003275 | 0.910832129 | 3.9853 | ship_preview_or_escalate_for_delivery | imported_measured |
| scene_sphere_bump.xml | raw | 2 | preview | 0.987003278 | 0.910832129 | 3.8529 | ship_preview_or_escalate_for_delivery | imported_measured |
| scene_sphere_bump.xml | raw | 4 | delivery | 0.994660223 | 0.959727762 | 3.6575 | ship_delivery | imported_measured |
| scene_sphere_bump.xml | raw | 4 | delivery | 0.994660227 | 0.959727762 | 4.0448 | ship_delivery | imported_measured |
| scene_sphere_bump.xml | raw | 4 | delivery | 0.994660223 | 0.959727762 | 3.7971 | ship_delivery | imported_measured |
| scene_sphere_bump.xml | raw | 8 | delivery | 0.99818179 | 0.986848258 | 3.3797 | ship_delivery | imported_measured |
| scene_sphere_bump.xml | raw | 8 | delivery | 0.998181805 | 0.986848258 | 4.0448 | ship_delivery | imported_measured |
| scene_sphere_bump.xml | raw | 8 | delivery | 0.998181773 | 0.986847898 | 3.4474 | ship_delivery | imported_measured |
| scene_sphere_bump.xml | raw | 16 | delivery | 0.999282562 | 0.994502029 | 3.3375 | ship_delivery | imported_measured |
| scene_sphere_bump.xml | raw | 16 | delivery | 0.999282615 | 0.994502681 | 4.5932 | ship_delivery | imported_measured |
| scene_sphere_bump.xml | raw | 16 | delivery | 0.999282586 | 0.994501637 | 3.4474 | ship_delivery | imported_measured |
| scene_sphere_bump.xml | raw | 32 | delivery | 0.999709934 | 0.99788074 | 3.3375 | ship_delivery | imported_measured |
| scene_sphere_bump.xml | raw | 32 | delivery | 0.99970993 | 0.997880596 | 3.9853 | ship_delivery | imported_measured |
| scene_sphere_bump.xml | raw | 32 | delivery | 0.999709946 | 0.997880811 | 3.3165 | ship_delivery | imported_measured |
| scene_sphere_bump.xml | raw | 64 | delivery | 0.999874359 | 0.999089405 | 3.2963 | ship_delivery | imported_measured |
| scene_sphere_bump.xml | raw | 64 | delivery | 0.999874359 | 0.999089405 | 3.8714 | ship_delivery | imported_measured |
| scene_world_volume.xml | oidn | 1 | delivery | 1.0 | 1.0 | 10.9733 | ship_delivery | imported_measured |
| scene_world_volume.xml | oidn | 2 | delivery | 1.0 | 1.0 | 12.9695 | ship_delivery | imported_measured |
| scene_world_volume.xml | oidn | 4 | delivery | 1.0 | 1.0 | 12.3309 | ship_delivery | imported_measured |
| scene_world_volume.xml | oidn | 8 | delivery | 1.0 | 1.0 | 10.1845 | ship_delivery | imported_measured |
| scene_world_volume.xml | oidn | 16 | delivery | 1.0 | 1.0 | 11.304 | ship_delivery | imported_measured |
| scene_world_volume.xml | oidn | 32 | delivery | 1.0 | 1.0 | 11.6293 | ship_delivery | imported_measured |
| scene_world_volume.xml | oidn | 64 | delivery | 1.0 | 1.0 | 10.3532 | ship_delivery | imported_measured |
| scene_world_volume.xml | raw | 1 | delivery | 1.0 | 1.0 | 12.0882 | ship_delivery | imported_measured |
| scene_world_volume.xml | raw | 2 | delivery | 1.0 | 1.0 | 14.3372 | ship_delivery | imported_measured |
| scene_world_volume.xml | raw | 4 | delivery | 1.0 | 1.0 | 13.5495 | ship_delivery | imported_measured |
| scene_world_volume.xml | raw | 8 | delivery | 1.0 | 1.0 | 11.0089 | ship_delivery | imported_measured |
| scene_world_volume.xml | raw | 16 | delivery | 1.0 | 1.0 | 12.33 | ship_delivery | imported_measured |
| scene_world_volume.xml | raw | 32 | delivery | 1.0 | 1.0 | 12.7113 | ship_delivery | imported_measured |
| scene_world_volume.xml | raw | 64 | delivery | 1.0 | 1.0 | 7.4286 | ship_delivery | imported_measured |
| scene_world_volume.xml | raw | 64 | delivery | 1.0 | 1.0 | 11.2091 | ship_delivery | imported_measured |
| scene_world_volume.xml | raw | 128 | delivery | 1.0 | 1.0 | 6.7294 | ship_delivery | imported_measured |
| scene_world_volume.xml | raw | 256 | delivery | 1.0 | 1.0 | 5.7778 | ship_delivery | imported_measured |
| scene_world_volume.xml | raw | 512 | delivery | 1.0 | 1.0 | 4.3664 | ship_delivery | imported_measured |
| scene_world_volume.xml | raw | 1024 | delivery | 1.0 | 1.0 | 2.9792 | ship_delivery | imported_measured |

## Interpretation

- This is a speculative render protocol scaffold using imported measured Cycles quality rows and actual tile-refinement receipts when present.
- Delivery rows are plausible immediate CX product levers: low-spp drafts verified against high-spp references.
- Actual tile-refinement rows are scored as measured `tile_refine` variants, not modeled multipliers.
- Failed rows identify the tile/refinement work that must exist before broader claims.
- OIDN rows are validated in the imported quality ledger and include denoise time.
