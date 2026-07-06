# Site scroll choreography · the scale trio (design notes)

The scale trio (6x RTX 5090 rack as the base, Mac Studio + DGX Spark on top) is the site's
frame-of-reference hero. It answers "what is computexchange?" in one image: run AI on anything from a
desktop box to a 6-GPU rack, and the relative sizes make the scale instantly legible. These are notes
for how it reveals on scroll · it plugs into the existing scroll-scrub narrative engine
(web/assets/site/hero.js · the 5-beat choreography already in the repo).

## The 5 beats (scroll-scrubbed, top -> bottom)
1. **ESTABLISH** · the full trio, centered, the money shot (trio-q34). Headline: the range in one
   glance. The lit RTX 5090 rings breathe (a slow emissive pulse · the rig is "running / earning").
2. **THE DESKTOP** · camera pushes in on the Mac Studio (top-left). Copy: local inference on the most
   popular Apple platform. The Studio's front ports + SD slot resolve as it fills frame.
3. **THE APPLIANCE** · pan to the DGX Spark (top-right). Copy: the compact NVIDIA AI box. The champagne
   foam-top + pill bezels resolve.
4. **THE RACK** · pull down + in to the 6x RTX 5090 wall (gpurig-q34 / gpu-macro for the ring+X detail).
   Copy: the headline compute · 6 flagship GPUs · the numbers (VRAM / TFLOPS / tok-s). Rings glow hot.
5. **THE OFFER** · pull back to the full trio + CTA. Copy: rent your compute / earn from your idle GPUs.
   The scale story reinforced · the desktops read as "yours," the rack as "the network."

## How to build it (fits the existing engine)
- The trio is PRE-RENDERED (not live WebGL · the lit rig + desktops are heavy). So the scroll scrubs a
  **rendered camera-move sequence**, exactly like the existing hero scroll-scrub: render a dolly path
  (wide trio -> Studio push -> Spark push -> rack push -> wide) as N frames, and map scroll progress ->
  frame (the engine already does this for the current hero). ~120-180 frames at the hero width is plenty.
- Alternatively (cheaper): cross-fade the 5 key stills (trio-q34, a Studio crop, a Spark crop,
  gpu-macro, trio-front) with a subtle Ken-Burns push on each · scroll drives the crossfade + zoom.
- The emissive-ring "breathing" can be a CSS/JS opacity pulse on a masked glow layer over the pre-render,
  so it animates without re-rendering.
- LCP: ship beat-1 (trio-q34) as the poster/first frame so the hero paints instantly; lazy-load the rest.

## Assets ready for this
- render/rack_previews/trio-q34.png, trio-front.png (full-quality + bloom)
- render/rack_previews/gpurig-q34.png, gpu-macro.png (rack + card detail)
- desktop crops can come from the trio frames or dedicated Studio/Spark portraits.

## Open (owner calls)
- Exact copy per beat · the real numbers (VRAM / TFLOPS / tok-s) to headline the rack.
- Whether to render the full dolly sequence (richer) or crossfade the stills (cheaper) · recommend the
  dolly sequence for the hero, stills as the fallback.
