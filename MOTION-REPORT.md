# Motion Direction Report

## Diagnosis

The prior version was technically smooth but not convincing because it behaved like a continuous 3D demo. The devices were always available to the camera, the scroll runway was short, and the motion changed too many spatial axes at once. That reads as a model viewer, not a product film.

The fix is not more easing. The fix is editorial control: held frames, heavier scroll pressure, fewer axes changing per beat, and compositions that can stand still as finished product photographs.

## References

- Apple Vision Pro: static product/person hero, editorial copy hold, then full-bleed video. The premium feel comes from pacing and restraint, not constant movement. https://www.apple.com/apple-vision-pro/
- Apple AirPods Pro 3: oversized crops, high key product imagery, localized motion, and long quiet sections around the cinematic pieces. https://www.apple.com/airpods-pro/
- Apple Mac Studio: product scale and crop carry the page more than animation. The strongest frames are almost stills with giant type. https://www.apple.com/mac-studio/
- Apple HIG Motion: motion should support understanding and remain intentional. https://developer.apple.com/design/human-interface-guidelines/motion
- Apple WWDC springs session: subtle differences matter, and the character of the interface should govern the motion. https://developer.apple.com/videos/play/wwdc2023/10158/
- Flow analysis: Apple product promos lean on authored frame sequences and specialized compression, which explains why pure live 3D needs stronger direction to avoid a cheap model-viewer feel. https://graydonpleasants.com/posts/flow-apples-secret-weapon/

## Direction

1. Treat the scroll as a film strip, not a turntable.
2. Hold each composition long enough to read.
3. Use scroll pressure before travel begins.
4. Change one primary thing at a time: camera crop, lighting, or text, not all three equally.
5. Let type and product scale do more work than orbital movement.
6. If this still misses, escalate to pre-rendered image sequence or video scrub, because that is the reference-category technique Apple uses for the most polished product moments.

## Changes Applied

- Replaced the curved camera rail with held linear shot interpolation so the camera no longer arcs around the devices.
- Increased scroll inertia and release settling.
- Reduced drag/yaw range so interaction feels like inspection, not orbiting.
- Added stronger dwell pressure around beat rests.
- Increased desktop beat height so the sequence needs more scroll distance.
- Added a subtle transition veil that darkens during travel and relaxes at rest.
- Enlarged the price monument to make that beat read as an Apple-style type/product composition instead of another pass around the models.

## Next Escalation

If the live Three version still feels cheap after this pass, the next move should be a storyboarded render sequence:

1. Render 5 to 7 hero-grade still frames from Blender/Cycles.
2. Use scroll to scrub a compressed image sequence or short video between those frames.
3. Keep the live Three scene only for optional drag inspection after the hero, or remove it.

That would trade real-time flexibility for authored pixels, which is probably the correct trade if the visual bar is Apple product-page polish.
