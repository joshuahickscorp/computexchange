// hero.js · the live tabletop hero. Loads oracles.glb (the two devices built in
// render/build_scene.py, same geometry as the Cycles stills) and lights it to match:
// one soft key high and camera-left, a dim rim behind and above, a low fill. The
// camera is a locked pitch band you can drag horizontally to orbit within ~40 degrees
// and it eases back to rest. Hover lifts a device 3 percent and fades in its spec
// label. WebGL failure falls back to the Cycles still (index.html handles the swap).
//
// Self-hosted Three via the page importmap · no CDN at runtime.
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

const REST_YAW = 0;          // rest camera azimuth (radians)
const YAW_RANGE = 0.35;      // +/- ~20 degrees of horizontal orbit (about 40 deg arc)
const PITCH = 0.63;          // ~36 degrees down onto the desk (elevation of the camera)
const PITCH_RANGE = 0.05;    // a few degrees of vertical give, clamped
const DIST = 0.92;           // camera distance in metres (scene is metric)
const TARGET = new THREE.Vector3(0, 0.03, 0);

export function mountHero(canvas, opts) {
  opts = opts || {};
  const onFail = opts.onFail || function () {};
  const labelEls = opts.labels || {};       // { 'mac-studio': el, 'dgx-spark': el }
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  // dev-only instrumentation overlay · gated by opts.dev (the page reads ?perf). Absent
  // the flag it never renders, so it is effectively stripped from prod. See tick.
  const dev = !!opts.dev;
  let devEl = null;
  const ftBuf = new Array(120).fill(0);
  let ftN = 0;

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    if (!renderer.getContext()) throw new Error('no webgl context');
  } catch (e) {
    onFail(e);
    return null;
  }

  // Cap DPR at 1.5 · the full-bleed canvas is pixel-bound and this is a dark metal
  // render where 2.0 buys little. Halves fill vs 2.0 on a Retina display.
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.0;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(30, 1, 0.05, 50);

  // Abstract dark gradient environment (NOT a photographic HDRI): a tiny vertical
  // gradient so metals reflect soft tone, not a studio. Built on a canvas, run
  // through PMREM for correct roughness response.
  const env = makeGradientEnv(renderer);
  scene.environment = env;

  // Lights mirror the Blender key/rim/fill.
  const key = new THREE.DirectionalLight(0xfffdf8, 2.6);
  key.position.set(-0.9, 1.15, 0.7);
  key.castShadow = true;
  key.shadow.mapSize.set(2048, 2048);
  key.shadow.camera.near = 0.1; key.shadow.camera.far = 4;
  key.shadow.camera.left = -0.6; key.shadow.camera.right = 0.6;
  key.shadow.camera.top = 0.6; key.shadow.camera.bottom = -0.6;
  key.shadow.bias = -0.0004; key.shadow.radius = 6;
  scene.add(key);

  const rim = new THREE.DirectionalLight(0xeef4ff, 1.1);
  rim.position.set(0.55, 0.95, -1.1);
  scene.add(rim);

  const fill = new THREE.DirectionalLight(0xf5f8ff, 0.35);
  fill.position.set(1.15, 0.5, 0.9);
  scene.add(fill);

  // Shadow-catcher desk: matte, near-black, receives the contact shadow. It reads
  // as the same desk as the still but stays dark enough to sit in the site void.
  const desk = new THREE.Mesh(
    new THREE.PlaneGeometry(8, 8),
    new THREE.ShadowMaterial({ opacity: 0.5 })
  );
  desk.rotation.x = -Math.PI / 2;
  desk.receiveShadow = true;
  scene.add(desk);

  // Device groups for hover: everything whose name starts mac-studio / dgx-spark / tub.
  // gpu-rig is the six-card exposed rig · it has no geometry in oracles.glb yet, so it is a
  // procedural brushed-edge STAND-IN built here and slotted at desk plane. The bridge shift
  // (docs/BRIDGE-SHIFT-PLAN.md) bakes the photoreal rig into the glb and drops it into this
  // same group with the same beat, no choreography change.
  const groups = {
    'mac-studio': new THREE.Group(),
    'dgx-spark': new THREE.Group(),
    'gpu-rig': new THREE.Group(),
  };
  scene.add(groups['mac-studio'], groups['dgx-spark'], groups['gpu-rig']);
  const baseEmissive = new WeakMap();
  // The rig shows a procedural stand-in INSTANTLY (no network wait), then upgrades to the
  // baked photoreal rig glb the moment it loads. If the glb 404s or fails to parse, the
  // stand-in simply stays · the rig beat never renders empty.
  buildRigStandIn(groups['gpu-rig'], baseEmissive);

  let loaded = false;
  const loader = new GLTFLoader();

  // Real 6-card rig · a SEPARATE glb (built by build_rack.py, a different scene than the
  // Studio/Spark oracles.glb) attached into the same gpu-rig group with the same beat, so
  // the choreography is unchanged. On success we drop the stand-in and swap in the baked rig.
  loader.load('/assets/site/rig.glb', (gltf) => {
    const rigMeshes = [];
    gltf.scene.traverse((o) => { if (o.isMesh) rigMeshes.push(o); });
    if (!rigMeshes.length) return;                 // nothing usable · keep the stand-in
    const rig = groups['gpu-rig'];
    rig.position.set(0, 0, 0);                      // attach preserves WORLD transform · park at origin, placeCamera re-applies rgDX
    for (let i = rig.children.length - 1; i >= 0; i--) rig.remove(rig.children[i]); // drop the stand-in
    for (const o of rigMeshes) {
      o.castShadow = true;
      o.receiveShadow = true;
      rig.attach(o);
      if (o.material) {
        o.material = o.material.clone();
        baseEmissive.set(o.material, (o.material.emissive && o.material.emissive.clone()) || new THREE.Color(0, 0, 0));
      }
    }
    renderer.shadowMap.needsUpdate = true;         // rig geometry changed · re-bake the frozen shadow once
    frame();
  }, undefined, () => { /* glb failed · the stand-in remains, beat still renders */ });
  loader.load('/assets/site/oracles.glb', (gltf) => {
    // Groups must sit at the ORIGIN while meshes attach (attach preserves world
    // transforms · a pre-applied S5 offset would bake in wrong). The next tick
    // re-applies the beat offsets to the clean groups.
    groups['mac-studio'].position.set(0, 0, 0);
    groups['dgx-spark'].position.set(0, 0, 0);
    // Collect meshes FIRST · reparenting (attach) during traverse mutates the tree
    // mid-iteration and corrupts it.
    const meshes = [];
    gltf.scene.traverse((o) => { if (o.isMesh) meshes.push(o); });
    for (const o of meshes) {
      o.castShadow = true;
      o.receiveShadow = true;
      const g = o.name.startsWith('dgx-spark') || o.name.startsWith('tub') ? 'dgx-spark' : 'mac-studio';
      groups[g].attach(o);
      if (o.material) {
        o.material = o.material.clone();
        baseEmissive.set(o.material, (o.material.emissive && o.material.emissive.clone()) || new THREE.Color(0, 0, 0));
      }
    }
    loaded = true;
    // The geometry is static and the key light is directional, so its shadow map is
    // camera-independent · bake it ONCE on the next render, then freeze it instead of
    // recomputing a 2048 depth pass every frame.
    renderer.shadowMap.autoUpdate = false;
    renderer.shadowMap.needsUpdate = true;
    // E7 · intro. A single decelerating arrival into the beat 1 rest, from ~2 percent
    // along the beat 1 to 2 path (scrollP starts ahead and glides to 0). Skipped if the
    // user already scrolled or prefers reduced motion · never fight the user for the camera.
    if (!reduceMotion && scrollTarget < 0.005) scrollP = 0.02;
    frame();
  }, undefined, (err) => onFail(err));

  // ---- camera rig · scroll scrubs a 5-beat path, drag adds a temporary offset -------
  // Each beat is a camera state: target point, distance, pitch, exposure. Scroll
  // progress (0..1) lerps between adjacent beats with smoothstep. Drag adds a yaw and
  // pitch offset on top that eases back to zero on release, so the two never fight.
  // Device x positions in the yup glb (metres): Studio camera-left, Spark camera-right
  // (frozen in CONTRACT.md). target.x is the horizontal COMPOSITION control · the
  // devices shift screen-left when the camera looks to their right (higher tx) and
  // screen-right when it looks left. These keyframes were tuned and VERIFIED in a
  // headless projection harness that projects both device silhouettes through the
  // camera across every intermediate scroll position and asserts the cluster clears
  // each beat's text column, including under max drag-yaw at rest.
  const STUDIO_X = -0.135, SPARK_X = 0.159; // the frozen contract anchors (documentation)
  void STUDIO_X; void SPARK_X;
  const FADE_START = 0.90;                 // release beat: canvas opacity eases to 0 by p=1
  // E2 · dwell plateaus. Beat rests in raw p, and per-beat plateau half-widths (the three
  // device beats and the rig hold wider so the eye lands and reads; monument tighter). The
  // camera sits exactly at rest across each plateau (drag available) and travels only between.
  // S10 · owner asked for a heavier, more deliberate scroll — the plateaus are widened across
  // the board so each beat COMMITS (the shot holds longer before it releases to the next).
  const RESTS = [0, 1/7, 2/7, 3/7, 4/7, 5/7, 6/7, 1];
  // S9 · how it works grew taller (it now carries the receipts), which pushes the price and
  // earn section centres a little past their rests. Widen those two plateaus so the camera is
  // still exactly on their pose when the section is centred (the offset is absorbed, no rest
  // recompute · the earlier beats and the how rest itself stay dead-on). price holds longest.
  const DWELL = [0.050, 0.050, 0.050, 0.058, 0.048, 0.062, 0.052, 0.040];
  // S5 · the bisected procession (owner direction): models move INDEPENDENTLY in the
  // left band, the text is a vertical column on the right, always. The page opens on
  // the STUDIO alone (the Spark parked off-frame left, stDX/spDX are per-device lateral
  // offsets, 10th/11th columns) · its sections · then the Studio slides away and the
  // SPARK slides in · its sections · then SIDE BY SIDE for how · the price · earn ·
  // release with the scene faded and the download beside the footer. Per-beat camera
  // yaw (9th column). Text may pass over the models during travel (owner sanctioned);
  // every held rest is verified clean in the projection harness incl. damped drag.
  // S10 · the procession now walks Studio → Spark → the six-card RIG → all together. Each
  // device owns the frame in turn (the others park off-frame · Studio/Spark to the left,
  // the rig to the right so nothing crosses through anything else during a swap). rgDX is
  // the 12th column · the rig's lateral park offset, mirroring stDX/spDX.
  const RIG_PARK = 1.6;  // rig parked off-frame RIGHT (Studio/Spark park left at -0.9)
  const BEATS = [
    // p,    tx,     ty,    tz,  dist, pitch, exp,  ds,   yaw,   stDX, spDX, rgDX  · on screen
    [0.000, -0.030,  0.045, 0.0, 0.58, 0.45, 1.00, 0.40, -0.60,  0.0, -0.9,  1.6], // 1 arrival · the Studio alone
    [1/7,    0.100,  0.035, 0.0, 0.50, 0.40, 1.00, 0.35, -0.90,  0.0, -0.9,  1.6], // 2 studio  · closer, its specs
    [2/7,    0.300,  0.020, 0.0, 0.46, 0.42, 1.00, 0.35, -0.85, -0.9,  0.0,  1.6], // 3 spark   · the swap · Spark in
    [3/7,    0.220,  0.280, 0.0, 1.75, 0.50, 1.00, 0.42, -0.30, -0.9, -0.9,  0.0], // 4 rig     · the six-card rig alone · reframed for the real 0.56m-tall glb (verified headless)
    [4/7,    0.280,  0.050, 0.0, 0.95, 0.52, 1.00, 0.50, -0.25,  0.0,  0.0,  1.6], // 5 how     · side by side
    [5/7,    0.200,  0.240, 0.0, 1.15, 0.79, 0.32, 0.30,  0.00,  0.0,  0.0,  1.6], // 6 price   · sink + dim
    [6/7,    0.250,  0.040, 0.0, 0.72, 0.48, 1.00, 0.50, -0.35,  0.0,  0.0,  1.6], // 7 earn    · close pair, low
    [1.000,  0.150,  0.300, 0.0, 2.20, 0.95, 0.85, 0.15,  0.00,  0.0,  0.0,  1.6], // 8 release · recede + fade out
  ];
  void RIG_PARK;

  let scrollTarget = 0;                  // raw scroll fraction (set by the listener)
  let scrollP = 0;                       // smoothed/displayed fraction, glided toward the target
  let viewFrac = 0;                      // S6 · smoothed PAGE-scroll fraction (inertia, NO dwell) ·
                                         // drives the text-layer warp + reveals so the copy carries
                                         // the same momentum as the camera instead of snapping 1:1.
  // Time constants for the one glide filter, in ms (E1). Doctrine defaults · the
  // tighten-then-add-15-percent feel tuning is the owner's on-device pass (see NOTES).
  // scroll is the camera's mass (heaviest · it also HOLDS the composed shot at each dwell);
  // view is the text layer's momentum (a touch quicker than the camera so scrolling stays
  // responsive while the shot settles a beat later); drag is direct manipulation; settle is
  // the release exhale (longer than drag); veil is any alpha the engine drives.
  // S6 raised scroll 260 to 320 for more cinematic heft (owner asked for more smoothing) and
  // added view for the page content, which used to track raw scroll with no inertia at all.
  // S10 raised scroll 320 to 430 and view 260 to 320 — the owner wants the scroll to feel
  // RESISTANT and important, so the camera carries more mass (it settles into each shot a beat
  // after the wheel stops) and the copy trails with matched momentum. Drag stays direct.
  const TAU = { scroll: 430, view: 320, drag: 70, settle: 240, veil: 150 };
  let dragYaw = 0, dragPitch = 0;        // drag target offsets · set to 0 on release
  let dispYaw = 0, dispPitch = 0;        // displayed offsets, glided toward the target
  let dragging = false, lastX = 0, lastY = 0;
  // E4 · the single canvas-alpha authority. Each source writes its own channel; one
  // place composes with min and writes the opacity once. No other line touches it.
  const veil = { intro: 1, beat: 1, rm: 1 };
  let introTarget = 1;                   // E7 sets this to 0 then 1 to run the intro veil
  let appliedAlpha = -1;
  let rmIndex = -1;                      // last snapped beat index (reduced-motion crossfade)
  let firstRendered = false;             // fires opts.onReady once the first live frame paints
  const camT = new THREE.Vector3();      // reused, no per-frame allocation

  // written into a reused object · zero per-frame allocation
  const _st = { tx: 0, ty: 0, tz: 0, dist: 0, pitch: 0, exp: 1, ds: 1, yaw: 0, stDX: 0, spDX: 0, rgDX: 0 };
  function beatState(p) {
    // find the segment and interpolate LINEARLY · dwellRemap (E2) owns the easing, so a
    // second smoothstep here would double-ease. The plateaus give zero velocity at rests.
    let i = 0;
    while (i < BEATS.length - 1 && p > BEATS[i + 1][0]) i++;
    const a = BEATS[i], b = BEATS[Math.min(i + 1, BEATS.length - 1)];
    const span = (b[0] - a[0]) || 1;
    const s = Math.max(0, Math.min(1, (p - a[0]) / span));
    _st.tx = a[1] + (b[1] - a[1]) * s; _st.ty = a[2] + (b[2] - a[2]) * s; _st.tz = a[3] + (b[3] - a[3]) * s;
    // E3 · perceptual interpolation (LAW 8). Dolly distance in LOG space (zoom is
    // multiplicative), exposure in STOPS (log2 of gain), angles as angles. Positions linear.
    _st.dist = Math.exp(Math.log(a[4]) + (Math.log(b[4]) - Math.log(a[4])) * s);
    _st.pitch = a[5] + (b[5] - a[5]) * s;
    _st.exp = Math.pow(2, Math.log2(a[6]) + (Math.log2(b[6]) - Math.log2(a[6])) * s);
    _st.ds = a[7] + (b[7] - a[7]) * s;
    _st.yaw = a[8] + (b[8] - a[8]) * s;   // per-beat camera azimuth (S4) · an angle, linear
    _st.stDX = a[9] + (b[9] - a[9]) * s;  // per-device lateral offsets (S5) · the models
    _st.spDX = a[10] + (b[10] - a[10]) * s; // move independently, parking off-frame left
    _st.rgDX = a[11] + (b[11] - a[11]) * s; // the rig parks off-frame right (S10)
    return _st;
  }

  function placeCamera() {
    // reduced-motion: snap scroll to the nearest beat centre (no continuous scrub),
    // and a short opacity crossfade masks the pose jump at each boundary.
    let p = scrollP;
    if (reduceMotion) {
      let best = 0, bd = 1, bi = 0;
      for (let i = 0; i < BEATS.length; i++) {
        const d = Math.abs(scrollP - BEATS[i][0]);
        if (d < bd) { bd = d; best = BEATS[i][0]; bi = i; }
      }
      p = best;
      if (bi !== rmIndex) { rmIndex = bi; veil.rm = 0; }  // boundary · dip the veil, glide back (E4)
    }
    const st = beatState(p);
    // S5 · the models move independently: apply the beat-lerped lateral offsets to the
    // device groups. The shadow map is frozen (R1), so any frame a device actually moves
    // re-bakes it once · zero cost at rest, correct shadows in motion.
    if (Math.abs(groups['mac-studio'].position.x - st.stDX) > 1e-4
     || Math.abs(groups['dgx-spark'].position.x - st.spDX) > 1e-4
     || Math.abs(groups['gpu-rig'].position.x - st.rgDX) > 1e-4) {
      groups['mac-studio'].position.x = st.stDX;
      groups['dgx-spark'].position.x = st.spDX;
      groups['gpu-rig'].position.x = st.rgDX;
      renderer.shadowMap.needsUpdate = true;
    }
    // drag is damped per beat (ds): full orbit at arrival, restrained where the
    // devices are composed or receding, so a swing can never enter the text column.
    const cy = st.pitch + dispPitch * st.ds;
    const yaw = st.yaw + dispYaw * st.ds;   // beat azimuth + damped drag give
    camT.set(st.tx, st.ty, st.tz);
    camera.position.set(
      camT.x + st.dist * Math.cos(cy) * Math.sin(yaw),
      camT.y + st.dist * Math.sin(cy),
      camT.z + st.dist * Math.cos(cy) * Math.cos(yaw)
    );
    camera.lookAt(camT);
    renderer.toneMappingExposure = st.exp;
    // beat 5 ease-away · a channel of the single alpha authority (E4). It is a function
    // of the glided p, so it is already smooth; the min-composition happens in the tick.
    veil.beat = p >= FADE_START ? Math.max(0, 1 - (p - FADE_START) / (1 - FADE_START)) : 1;
  }

  // the ONE place canvas opacity is written · min-composed across every veil channel
  function applyCanvasAlpha() {
    const a = Math.min(veil.intro, veil.beat, veil.rm);
    if (a !== appliedAlpha) { canvas.style.opacity = String(a); appliedAlpha = a; }
  }

  // scroll drives a SINGLE scalar; all interpolation happens in the rAF tick. The beat
  // scrub is opt-in (opts.beats). onProgress lets the page reveal its beat copy off the
  // very same scalar, so there is still exactly one scroll listener for the whole page.
  function onScroll() {
    // the listener does the minimum · stash the raw target and request a frame. The
    // smoothing, camera, and reveals all happen once per frame in the tick (so scroll
    // events firing faster than frames cannot pile up work on the hot path).
    const max = document.documentElement.scrollHeight - window.innerHeight;
    scrollTarget = max > 0 ? Math.max(0, Math.min(1, window.scrollY / max)) : 0;
    frame();
  }
  if (opts.beats) window.addEventListener('scroll', onScroll, { passive: true });

  function resize() {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (w === 0 || h === 0) return;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    frame();
  }

  // ---- interaction ------------------------------------------------------------------
  function onDown(e) {
    dragging = true;
    lastX = (e.touches ? e.touches[0].clientX : e.clientX);
    lastY = (e.touches ? e.touches[0].clientY : e.clientY);
  }
  function onMove(e) {
    const cx = (e.touches ? e.touches[0].clientX : e.clientX);
    const cy = (e.touches ? e.touches[0].clientY : e.clientY);
    if (dragging) {
      const dx = cx - lastX, dy = cy - lastY;
      lastX = cx; lastY = cy;
      dragYaw = clamp(dragYaw - dx * 0.006, -YAW_RANGE, YAW_RANGE);
      dragPitch = clamp(dragPitch - dy * 0.003, -PITCH_RANGE, PITCH_RANGE);
      frame();
    } else {
      hover(cx, cy);
    }
  }
  // release · the drag target returns to rest and the display glides back with TAU.settle
  function onUp() { dragging = false; dragYaw = 0; dragPitch = 0; frame(); }

  canvas.addEventListener('mousedown', onDown);
  canvas.addEventListener('touchstart', onDown, { passive: true });
  window.addEventListener('mousemove', onMove);
  window.addEventListener('touchmove', onMove, { passive: true });
  window.addEventListener('mouseup', onUp);
  window.addEventListener('touchend', onUp);
  window.addEventListener('resize', resize);

  // ---- hover raycast ----------------------------------------------------------------
  const ray = new THREE.Raycaster();
  const ndc = new THREE.Vector2();
  let hovered = null;
  function hover(cx, cy) {
    const rect = canvas.getBoundingClientRect();
    ndc.x = ((cx - rect.left) / rect.width) * 2 - 1;
    ndc.y = -((cy - rect.top) / rect.height) * 2 + 1;
    ray.setFromCamera(ndc, camera);
    let hit = null;
    for (const k of ['mac-studio', 'dgx-spark', 'gpu-rig']) {
      if (ray.intersectObject(groups[k], true).length) { hit = k; break; }
    }
    if (hit !== hovered) {
      hovered = hit;
      for (const k of ['mac-studio', 'dgx-spark', 'gpu-rig']) {
        const on = k === hit;
        setLift(groups[k], on);
        if (labelEls[k]) labelEls[k].classList.toggle('on', on);
      }
      canvas.style.cursor = hit ? 'pointer' : 'grab';
      frame();
    }
  }
  function setLift(group, on) {
    group.traverse((o) => {
      if (o.isMesh && o.material) {
        const base = baseEmissive.get(o.material);
        if (base && o.material.emissive) {
          o.material.emissive.copy(base);
          if (on) o.material.emissive.addScalar(0.03); // restrained 3 percent lift
        }
      }
    });
  }

  // ---- render on demand -------------------------------------------------------------
  let pending = false, last = 0;
  function frame() { if (!pending) { pending = true; requestAnimationFrame(tick); } }

  function tick(now) {
    pending = false;
    const t0 = dev ? performance.now() : 0;
    // dt discipline (E1) · the loop stops and restarts, so the first frame after a rest
    // carries a huge wall-clock delta. Clamp it so a pause is never integrated.
    const dt = Math.min(now - (last || now), 34);
    last = now;

    // scroll: raw target to dwell-remapped target to glide (E2 then E1). Reduced motion
    // jumps instantly so the placeCamera snap-to-nearest-beat survives (no continuous scrub).
    const sTarget = dwellRemap(scrollTarget, RESTS, DWELL);
    scrollP = reduceMotion ? sTarget : glide(scrollP, sTarget, TAU.scroll, dt);
    // S6 · the text layer follows RAW scroll with pure inertia (no dwell · it must always
    // move when the user scrolls, never hold like the camera). The page warps its content
    // to viewFrac over the fixed stage, so the copy and the camera share one momentum.
    viewFrac = reduceMotion ? scrollTarget : glide(viewFrac, scrollTarget, TAU.view, dt);
    const dtau = dragging ? TAU.drag : TAU.settle;   // tight on grab, longer on the exhale
    dispYaw = glide(dispYaw, dragYaw, dtau, dt);
    dispPitch = glide(dispPitch, dragPitch, dtau, dt);
    // snap to target within epsilon so float residue cannot hold the loop alive
    if (Math.abs(sTarget - scrollP) < 1e-4) scrollP = sTarget;
    if (Math.abs(scrollTarget - viewFrac) < 1e-4) viewFrac = scrollTarget;
    if (Math.abs(dragYaw - dispYaw) < 1e-4) dispYaw = dragYaw;
    if (Math.abs(dragPitch - dispPitch) < 1e-4) dispPitch = dragPitch;

    placeCamera();
    // veil channels glide toward their rest (1); placeCamera set veil.beat and any rm dip
    veil.rm = glide(veil.rm, 1, TAU.veil, dt);
    veil.intro = glide(veil.intro, introTarget, TAU.veil, dt);
    if (Math.abs(1 - veil.rm) < 1e-3) veil.rm = 1;
    if (Math.abs(introTarget - veil.intro) < 1e-3) veil.intro = introTarget;
    applyCanvasAlpha();
    if (loaded) renderer.render(scene, camera);
    // first live frame is on screen · let the page crossfade the still-first LCP out
    if (loaded && !firstRendered) { firstRendered = true; if (opts.onReady) opts.onReady(); }
    // reveal the page copy off the smoothed scalars, once per frame (not per scroll event):
    // scrollP owns the camera-linked opacity timing, viewFrac owns the text-layer position.
    if (opts.onProgress) opts.onProgress(scrollP, viewFrac);

    // RENDER ON DEMAND · re-arm only while something is actually moving. At rest the loop
    // STOPS and forgets the clock, so the next wake never teleports on a stale delta.
    const moving = dragging
      || Math.abs(sTarget - scrollP) > 1e-4
      || Math.abs(scrollTarget - viewFrac) > 1e-4
      || Math.abs(dragYaw - dispYaw) > 1e-4
      || Math.abs(dragPitch - dispPitch) > 1e-4
      || Math.abs(1 - veil.rm) > 1e-3
      || Math.abs(introTarget - veil.intro) > 1e-3;
    if (dev) devStats(t0, moving);
    if (moving) frame(); else last = 0;
  }

  // dev overlay update · last frame time, p95 of the trailing 120, glided p, active beat,
  // and whether the loop is armed. Frozen at "armed no" once the loop disarms (rest test).
  function devStats(t0, moving) {
    const ms = performance.now() - t0;
    ftBuf[ftN % 120] = ms; ftN++;
    const arr = ftBuf.slice(0, Math.min(ftN, 120)).sort((a, b) => a - b);
    const p95 = arr[Math.min(arr.length - 1, Math.floor(arr.length * 0.95))] || 0;
    let bi = 0, bd = 2;
    for (let i = 0; i < RESTS.length; i++) { const d = Math.abs(scrollP - RESTS[i]); if (d < bd) { bd = d; bi = i; } }
    devEl.textContent = 'frame ' + ms.toFixed(1) + 'ms   p95 ' + p95.toFixed(1) + 'ms\n'
      + 'p ' + scrollP.toFixed(3) + '   beat ' + (bi + 1) + '\narmed ' + (moving ? 'yes' : 'no');
  }

  // context loss → fallback
  canvas.addEventListener('webglcontextlost', (e) => { e.preventDefault(); onFail(new Error('context lost')); });

  if (dev) {
    devEl = document.createElement('div');
    devEl.style.cssText = 'position:fixed;top:8px;right:8px;z-index:99999;font:11px/1.45 ui-monospace,monospace;'
      + 'color:#7dd3a0;background:rgba(0,0,0,.62);padding:6px 9px;border-radius:6px;white-space:pre;pointer-events:none';
    document.body.appendChild(devEl);
  }

  // S6 · seed the smoothed scroll from the live position so a reload mid-page settles in
  // place instead of gliding down from the top (the intro nudge only fires at the very top).
  if (opts.beats) {
    const max0 = document.documentElement.scrollHeight - window.innerHeight;
    scrollTarget = max0 > 0 ? Math.max(0, Math.min(1, window.scrollY / max0)) : 0;
  }
  viewFrac = scrollTarget;
  resize();
  placeCamera();
  frame();
  if (opts.onProgress) opts.onProgress(scrollP, viewFrac); // seed the page's initial reveal + warp

  return {
    info: () => renderer.info,
    // dev-only scene introspection (harmless handle · used by the QA evals)
    debug: () => ({ scene, camera, groups, renderer }),
    dispose: () => { renderer.dispose(); env.dispose && env.dispose(); },
  };
}

function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }

// The ONE smoothing filter (E1 · motion doctrine). Frame-rate independent
// exponential approach: a first-order critically damped response with no overshoot.
// tau in ms (smaller is tighter), dt in ms. Every animated scalar passes through this.
function glide(current, target, tau, dt) { return target + (current - target) * Math.exp(-dt / tau); }

// E2 · dwell plateaus. Map raw scroll p into a curve that HOLDS at each beat rest
// (a plateau of half-width w[i]) and travels between plateaus with zero slope at both
// edges (smoothstep). Zero slope at the joints makes the whole path C1: the camera
// velocity is zero at each rest by design (a dwell), never an accidental stall.
function dwellRemap(p, rests, w) {
  if (p <= rests[0] + w[0]) return rests[0];
  for (let i = 0; i < rests.length - 1; i++) {
    const a = rests[i] + w[i], b = rests[i + 1] - w[i + 1];
    if (p < a) return rests[i];               // inside the plateau of rest i
    if (p < b) {                              // travelling from rest i to rest i+1
      const t = (p - a) / (b - a);
      return rests[i] + (t * t * (3 - 2 * t)) * (rests[i + 1] - rests[i]);
    }
  }
  return rests[rests.length - 1];
}

// The six-card exposed rig · a PROCEDURAL STAND-IN (oracles.glb has no rig geometry yet).
// It reads as the money shot the owner tuned in render/build_rack.py: six brushed-metal card
// edges (the 1:1 side) standing proud of a dark anodized chassis, on the desk plane. Metric,
// metres. The bridge-shift bake (docs/BRIDGE-SHIFT-PLAN.md) replaces this with the photoreal
// rig in the same group · nothing in the choreography changes. Materials pick up
// scene.environment automatically, so the brushed edges catch the same soft tone as the glb.
function buildRigStandIn(group, baseEmissive) {
  const W = 0.38, D = 0.30;                 // footprint (owner-measured rig frame)
  const brushed = new THREE.MeshStandardMaterial({ color: 0xb9bdc4, metalness: 0.94, roughness: 0.33 });
  const chassis = new THREE.MeshStandardMaterial({ color: 0x161718, metalness: 0.55, roughness: 0.52 });
  const rail = new THREE.MeshStandardMaterial({ color: 0x202225, metalness: 0.70, roughness: 0.42 });
  const add = (geo, mat, x, y, z, name) => {
    const m = new THREE.Mesh(geo, mat);
    m.position.set(x, y, z);
    m.castShadow = true; m.receiveShadow = true;
    m.name = name;
    // register a zero base-emissive so the shared hover-lift path (setLift) works on the rig too
    baseEmissive.set(mat, (mat.emissive && mat.emissive.clone()) || new THREE.Color(0, 0, 0));
    group.add(m);
    return m;
  };
  // dark base tray + back panel + two end rails read as an open chassis (front left open so
  // the card edges are exposed · exactly the exposed-side look the owner asked to display).
  add(new THREE.BoxGeometry(W, 0.055, D), chassis, 0, 0.028, 0, 'gpu-rig-base');
  add(new THREE.BoxGeometry(W, 0.40, 0.020), chassis, 0, 0.20, -D / 2 + 0.010, 'gpu-rig-back');
  add(new THREE.BoxGeometry(0.020, 0.40, D), rail, -W / 2 + 0.010, 0.20, 0, 'gpu-rig-rail-l');
  add(new THREE.BoxGeometry(0.020, 0.40, D), rail, W / 2 - 0.010, 0.20, 0, 'gpu-rig-rail-r');
  add(new THREE.BoxGeometry(W, 0.028, D), rail, 0, 0.40, 0, 'gpu-rig-top');
  // six brushed card edges standing proud of the tray · the exposed 1:1 side facing the camera.
  const n = 6, span = W - 0.09, pitch = span / (n - 1), x0 = -span / 2;
  for (let i = 0; i < n; i++) {
    add(new THREE.BoxGeometry(0.013, 0.335, D - 0.055), brushed, x0 + i * pitch, 0.055 + 0.335 / 2, 0.008, 'gpu-rig-card-' + i);
  }
}

// A dark abstract gradient environment (metals reflect tone, not a studio).
function makeGradientEnv(renderer) {
  const c = document.createElement('canvas');
  c.width = 16; c.height = 256;
  const g = c.getContext('2d');
  const grad = g.createLinearGradient(0, 0, 0, 256);
  grad.addColorStop(0.0, '#1b1c20');   // faint sky
  grad.addColorStop(0.5, '#0d0d10');
  grad.addColorStop(1.0, '#050506');   // floor-dark
  g.fillStyle = grad; g.fillRect(0, 0, 16, 256);
  const tex = new THREE.CanvasTexture(c);
  tex.mapping = THREE.EquirectangularReflectionMapping;
  tex.colorSpace = THREE.SRGBColorSpace;
  const pmrem = new THREE.PMREMGenerator(renderer);
  const rt = pmrem.fromEquirectangular(tex);
  tex.dispose(); pmrem.dispose();
  return rt.texture;
}
