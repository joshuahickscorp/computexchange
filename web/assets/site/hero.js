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

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    if (!renderer.getContext()) throw new Error('no webgl context');
  } catch (e) {
    onFail(e);
    return null;
  }

  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
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
  const groups = { 'mac-studio': new THREE.Group(), 'dgx-spark': new THREE.Group() };
  scene.add(groups['mac-studio'], groups['dgx-spark']);
  const baseEmissive = new WeakMap();

  let loaded = false;
  const loader = new GLTFLoader();
  loader.load('/assets/site/oracles.glb', (gltf) => {
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
    frame();
  }, undefined, (err) => onFail(err));

  // ---- camera rig -------------------------------------------------------------------
  let yaw = REST_YAW, pitch = PITCH;
  let targetYaw = REST_YAW, targetPitch = PITCH;
  let dragging = false, lastX = 0, lastY = 0, released = 0;
  let idlePhase = 0;

  function placeCamera() {
    const cy = Math.max(PITCH - PITCH_RANGE, Math.min(PITCH + PITCH_RANGE, pitch));
    const r = DIST;
    camera.position.set(
      TARGET.x + r * Math.cos(cy) * Math.sin(yaw),
      TARGET.y + r * Math.sin(cy),
      TARGET.z + r * Math.cos(cy) * Math.cos(yaw)
    );
    camera.lookAt(TARGET);
  }

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
      targetYaw = clamp(targetYaw - dx * 0.006, REST_YAW - YAW_RANGE, REST_YAW + YAW_RANGE);
      targetPitch = clamp(targetPitch - dy * 0.003, PITCH - PITCH_RANGE, PITCH + PITCH_RANGE);
      frame();
    } else {
      hover(cx, cy);
    }
  }
  function onUp() { dragging = false; released = performance.now(); frame(); }

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
    for (const k of ['mac-studio', 'dgx-spark']) {
      if (ray.intersectObject(groups[k], true).length) { hit = k; break; }
    }
    if (hit !== hovered) {
      hovered = hit;
      for (const k of ['mac-studio', 'dgx-spark']) {
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
  let pending = false;
  function frame() { if (!pending) { pending = true; requestAnimationFrame(tick); } }

  function tick(t) {
    pending = false;
    // ease targets toward rest when released
    if (!dragging) {
      const sinceUp = (t - released) / 1000;
      if (sinceUp > 0.25) {
        targetYaw += (REST_YAW - targetYaw) * 0.04;
        targetPitch += (PITCH - targetPitch) * 0.04;
      }
    }
    // idle drift (killed by reduced-motion)
    let idle = 0;
    if (!reduceMotion && !dragging) { idlePhase += 0.0015; idle = Math.sin(idlePhase) * 0.008; }
    yaw += (targetYaw + idle - yaw) * 0.12;
    pitch += (targetPitch - pitch) * 0.12;
    placeCamera();
    if (loaded) renderer.render(scene, camera);

    // keep animating while settling or idling
    const settling = Math.abs(yaw - targetYaw) > 1e-4 || Math.abs(pitch - targetPitch) > 1e-4;
    if (settling || (!reduceMotion && !dragging)) frame();
  }

  // context loss → fallback
  canvas.addEventListener('webglcontextlost', (e) => { e.preventDefault(); onFail(new Error('context lost')); });

  resize();
  placeCamera();
  frame();

  return {
    info: () => renderer.info,
    dispose: () => { renderer.dispose(); env.dispose && env.dispose(); },
  };
}

function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }

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
