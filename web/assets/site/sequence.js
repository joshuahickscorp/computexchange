// sequence.js · the baked-frame procession scrubber (S1 · design-direction hybrid).
//
// This is the render-DEPENDENT hero the direction calls for: instead of re-rasterizing a
// live scene (which throws away the Cycles lighting), scroll SCRUBS a pre-rendered frame
// sequence painted to a 2D canvas · Apple's actual product-page technique. It is UNWIRED
// until the frames exist (there is no manifest yet · the devices are still stand-ins). The
// live hero.js remains the procession in the meantime; this file is the drop-in seam.
//
// Wiring, once ~150 photoreal frames are baked from the cx_*.py Cycles pipeline into
// /assets/site/seq/ (content-hashed by scripts/site-build.mjs like every other asset):
//
//   import { mountSequence } from '/assets/site/sequence.js';
//   const h = mountSequence(canvas, {
//     base: '/assets/site/seq/frame-', count: 150, pad: 4, ext: 'webp',
//     onProgress: reveal,            // same page reveal, off the same glided scalar
//     onReady: () => stage.classList.add('ready'),
//   });
//
// It mirrors hero.js's engine EXACTLY (one glide filter, dwell plateaus, render-on-demand,
// dt clamp) so the baked procession inherits the same weighted-film feel and the same
// detents. Live drag-to-inspect (hero.js) is reserved for the price and release beats · the
// full hybrid runs both, cross-fading; this seam ships the baked path first.

const RESTS = [0, 1/6, 2/6, 3/6, 4/6, 5/6, 1];
const DWELL = [0.045, 0.04, 0.04, 0.04, 0.06, 0.05, 0.04];   // keep in step with hero.js (S9 · wider price/earn)
const TAU_SCROLL = 320;   // in step with hero.js (S6 · cinematic heft)

function glide(current, target, tau, dt) { return target + (current - target) * Math.exp(-dt / tau); }
function dwellRemap(p, rests, w) {
  if (p <= rests[0] + w[0]) return rests[0];
  for (let i = 0; i < rests.length - 1; i++) {
    const a = rests[i] + w[i], b = rests[i + 1] - w[i + 1];
    if (p < a) return rests[i];
    if (p < b) { const t = (p - a) / (b - a); return rests[i] + (t * t * (3 - 2 * t)) * (rests[i + 1] - rests[i]); }
  }
  return rests[rests.length - 1];
}

export function mountSequence(canvas, opts) {
  opts = opts || {};
  const onFail = opts.onFail || function () {};
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const ctx = canvas.getContext('2d');
  if (!ctx) { onFail(new Error('no 2d context')); return null; }

  const count = opts.count | 0;
  const frames = new Array(count).fill(null);
  let loaded = 0, ready = false, dpr = Math.min(window.devicePixelRatio || 1, 2);

  // preload every frame · ImageBitmap decodes off the main thread where supported
  for (let i = 0; i < count; i++) {
    const url = opts.base + String(i).padStart(opts.pad || 4, '0') + '.' + (opts.ext || 'webp');
    const img = new Image();
    img.decoding = 'async';
    img.onload = () => {
      frames[i] = img;
      if (++loaded === 1) { ready = true; frame(); if (opts.onReady) opts.onReady(); }
    };
    img.onerror = () => { if (i === 0) onFail(new Error('frame 0 missing · sequence not baked')); };
    img.src = url;
  }

  let scrollTarget = 0, scrollP = 0, pending = false, last = 0, lastDrawn = -1;
  function frame() { if (!pending) { pending = true; requestAnimationFrame(tick); } }

  function resize() {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return;
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
    lastDrawn = -1; frame();
  }

  function draw(idx) {
    const img = frames[idx] || frames[Math.max(0, Math.min(count - 1, idx))];
    if (!img || !img.width) return;
    const cw = canvas.width, ch = canvas.height;
    const s = Math.max(cw / img.width, ch / img.height);     // cover-fit, centred
    const dw = img.width * s, dh = img.height * s;
    ctx.clearRect(0, 0, cw, ch);
    ctx.drawImage(img, (cw - dw) / 2, (ch - dh) / 2, dw, dh);
  }

  function tick(now) {
    pending = false;
    const dt = Math.min(now - (last || now), 34); last = now;
    const sTarget = dwellRemap(scrollTarget, RESTS, DWELL);
    scrollP = reduceMotion ? sTarget : glide(scrollP, sTarget, TAU_SCROLL, dt);
    if (Math.abs(sTarget - scrollP) < 1e-4) scrollP = sTarget;
    const idx = Math.round(scrollP * (count - 1));
    if (ready && idx !== lastDrawn) { draw(idx); lastDrawn = idx; }
    if (opts.onProgress) opts.onProgress(scrollP);
    if (Math.abs(sTarget - scrollP) > 1e-4) frame(); else last = 0;   // render on demand
  }

  function onScroll() {
    const max = document.documentElement.scrollHeight - window.innerHeight;
    scrollTarget = max > 0 ? Math.max(0, Math.min(1, window.scrollY / max)) : 0;
    frame();
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  window.addEventListener('resize', resize);

  resize();
  if (opts.onProgress) opts.onProgress(0);
  return { info: () => ({ frames: count, loaded }), dispose: () => window.removeEventListener('scroll', onScroll) };
}
