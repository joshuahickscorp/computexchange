//! Phase-1 microbench: trace a material-free path structure once, then re-shade
//! N albedo variants against that cached structure.
//!
//! This deliberately models the cleanest useful reuse case from
//! `DECOUPLED_SHADING_NOTES.md`: same camera, geometry, light, and visibility;
//! only material base color changes. The cache stores the geometric facts a
//! renderer pays ray traversal for (primary hit, normal, material slot, shadow
//! visibility). Variant shading is pure BSDF/color evaluation.

use std::time::Instant;

use crate::vec3::{vec3, Vec3};

#[derive(Copy, Clone)]
struct Ray {
    origin: Vec3,
    dir: Vec3,
}

#[derive(Copy, Clone)]
struct Sphere {
    center: Vec3,
    radius: f32,
    material_slot: usize,
}

#[derive(Copy, Clone)]
struct Hit {
    t: f32,
    p: Vec3,
    n: Vec3,
    material_slot: usize,
    sphere_index: usize,
}

#[derive(Copy, Clone)]
struct CachedPixel {
    hit: bool,
    n: Vec3,
    material_slot: usize,
    light_visible: bool,
}

#[derive(Copy, Clone, Default, Debug)]
pub struct Counters {
    pub primary_sphere_tests: u64,
    pub shadow_sphere_tests: u64,
    pub shade_evals: u64,
}

#[derive(Debug)]
pub struct MicrobenchResult {
    pub variants: usize,
    pub pixels: usize,
    pub independent_ms: f64,
    pub cached_ms: f64,
    pub speedup_x: f64,
    pub max_abs_diff: f32,
    pub independent: Counters,
    pub cached: Counters,
    pub primary_test_reduction_x: f64,
    pub shadow_test_reduction_x: f64,
}

struct Scene {
    spheres: [Sphere; 2],
    light_dir: Vec3,
    env: Vec3,
}

fn scene() -> Scene {
    Scene {
        spheres: [
            Sphere { center: vec3(-0.35, 0.0, 0.0), radius: 0.9, material_slot: 0 },
            Sphere { center: vec3(0.95, 0.35, -0.2), radius: 0.45, material_slot: 1 },
        ],
        light_dir: vec3(-0.45, -0.65, 1.0).normalize(),
        env: vec3(0.015, 0.018, 0.022),
    }
}

fn variants(n: usize) -> Vec<[Vec3; 2]> {
    const COLORS: [Vec3; 8] = [
        vec3(0.86, 0.10, 0.08),
        vec3(0.08, 0.38, 0.92),
        vec3(0.12, 0.72, 0.24),
        vec3(0.92, 0.72, 0.10),
        vec3(0.62, 0.18, 0.92),
        vec3(0.95, 0.48, 0.16),
        vec3(0.18, 0.78, 0.78),
        vec3(0.82, 0.82, 0.82),
    ];
    (0..n)
        .map(|i| {
            [
                COLORS[i % COLORS.len()],
                vec3(0.62, 0.60, 0.56), // secondary object unchanged across variants
            ]
        })
        .collect()
}

fn camera_ray(x: usize, y: usize, width: usize, height: usize) -> Ray {
    let eye = vec3(0.0, -4.2, 0.45);
    let target = vec3(0.0, 0.0, 0.0);
    let forward = (target - eye).normalize();
    let right = forward.cross(vec3(0.0, 0.0, 1.0)).normalize();
    let up = right.cross(forward).normalize();
    let aspect = width as f32 / height as f32;
    let tan_half = (38.0_f32.to_radians() * 0.5).tan();
    let ndc_x = (((x as f32 + 0.5) / width as f32) * 2.0 - 1.0) * aspect;
    let ndc_y = 1.0 - ((y as f32 + 0.5) / height as f32) * 2.0;
    let dir = (forward + right * (ndc_x * tan_half) + up * (ndc_y * tan_half)).normalize();
    Ray { origin: eye, dir }
}

fn hit_sphere(ray: &Ray, s: &Sphere, t_min: f32) -> Option<(f32, Vec3, Vec3)> {
    let oc = ray.origin - s.center;
    let a = ray.dir.dot(ray.dir);
    let half_b = oc.dot(ray.dir);
    let c = oc.dot(oc) - s.radius * s.radius;
    let disc = half_b * half_b - a * c;
    if disc < 0.0 {
        return None;
    }
    let sqrt_d = disc.sqrt();
    let mut t = (-half_b - sqrt_d) / a;
    if t <= t_min {
        t = (-half_b + sqrt_d) / a;
        if t <= t_min {
            return None;
        }
    }
    let p = ray.origin + ray.dir * t;
    let n = (p - s.center) / s.radius;
    Some((t, p, n))
}

fn trace_primary(ray: &Ray, sc: &Scene, ctr: &mut Counters) -> Option<Hit> {
    let mut best: Option<Hit> = None;
    for (i, s) in sc.spheres.iter().enumerate() {
        ctr.primary_sphere_tests += 1;
        if let Some((t, p, n)) = hit_sphere(ray, s, 1e-4) {
            if best.map(|h| t < h.t).unwrap_or(true) {
                best = Some(Hit { t, p, n, material_slot: s.material_slot, sphere_index: i });
            }
        }
    }
    best
}

fn light_visible(hit: &Hit, sc: &Scene, ctr: &mut Counters) -> bool {
    let ray = Ray { origin: hit.p + hit.n * 1e-3, dir: sc.light_dir };
    for (i, s) in sc.spheres.iter().enumerate() {
        if i == hit.sphere_index {
            continue;
        }
        ctr.shadow_sphere_tests += 1;
        if hit_sphere(&ray, s, 1e-4).is_some() {
            return false;
        }
    }
    true
}

fn shade(n: Vec3, material_slot: usize, light_visible: bool, mats: &[Vec3; 2], sc: &Scene) -> Vec3 {
    let ndotl = n.dot(sc.light_dir).max(0.0);
    let direct = if light_visible { 1.75 * ndotl } else { 0.0 };
    let ambient = 0.045;
    mats[material_slot].mul_comp(Vec3::splat(ambient + direct)) + sc.env
}

fn independent_render(width: usize, height: usize, mats: &[Vec3; 2], ctr: &mut Counters) -> Vec<Vec3> {
    let sc = scene();
    let mut out = vec![Vec3::ZERO; width * height];
    for y in 0..height {
        for x in 0..width {
            let ray = camera_ray(x, y, width, height);
            let c = match trace_primary(&ray, &sc, ctr) {
                None => sc.env,
                Some(hit) => {
                    let vis = light_visible(&hit, &sc, ctr);
                    ctr.shade_evals += 1;
                    shade(hit.n, hit.material_slot, vis, mats, &sc)
                }
            };
            out[y * width + x] = c;
        }
    }
    out
}

fn build_cache(width: usize, height: usize, ctr: &mut Counters) -> Vec<CachedPixel> {
    let sc = scene();
    let mut cache = vec![
        CachedPixel { hit: false, n: Vec3::ZERO, material_slot: 0, light_visible: false };
        width * height
    ];
    for y in 0..height {
        for x in 0..width {
            let ray = camera_ray(x, y, width, height);
            if let Some(hit) = trace_primary(&ray, &sc, ctr) {
                let vis = light_visible(&hit, &sc, ctr);
                cache[y * width + x] = CachedPixel {
                    hit: true,
                    n: hit.n,
                    material_slot: hit.material_slot,
                    light_visible: vis,
                };
            }
        }
    }
    cache
}

fn reshade_cache(cache: &[CachedPixel], mats: &[Vec3; 2], ctr: &mut Counters) -> Vec<Vec3> {
    let sc = scene();
    let mut out = vec![Vec3::ZERO; cache.len()];
    for (i, px) in cache.iter().enumerate() {
        if px.hit {
            ctr.shade_evals += 1;
            out[i] = shade(px.n, px.material_slot, px.light_visible, mats, &sc);
        } else {
            out[i] = sc.env;
        }
    }
    out
}

fn max_abs_diff(a: &[Vec3], b: &[Vec3]) -> f32 {
    let mut m = 0.0f32;
    for (x, y) in a.iter().zip(b.iter()) {
        m = m.max((x.x - y.x).abs());
        m = m.max((x.y - y.y).abs());
        m = m.max((x.z - y.z).abs());
    }
    m
}

/// Run a deterministic CPU microbench. `repeats` makes wall-clock less noisy;
/// outputs are validated on every repeat.
pub fn run_microbench(width: usize, height: usize, n_variants: usize, repeats: usize) -> MicrobenchResult {
    assert!(n_variants > 0);
    assert!(repeats > 0);
    let mats = variants(n_variants);
    let pixels = width * height;

    let mut independent = Counters::default();
    let t0 = Instant::now();
    let mut independent_images = Vec::new();
    for _ in 0..repeats {
        independent_images.clear();
        for mat in &mats {
            independent_images.push(independent_render(width, height, mat, &mut independent));
        }
    }
    let independent_ms = t0.elapsed().as_secs_f64() * 1000.0;

    let mut cached = Counters::default();
    let t1 = Instant::now();
    let mut cached_images = Vec::new();
    for _ in 0..repeats {
        cached_images.clear();
        let cache = build_cache(width, height, &mut cached);
        for mat in &mats {
            cached_images.push(reshade_cache(&cache, mat, &mut cached));
        }
    }
    let cached_ms = t1.elapsed().as_secs_f64() * 1000.0;

    let mut diff = 0.0f32;
    for (a, b) in independent_images.iter().zip(cached_images.iter()) {
        diff = diff.max(max_abs_diff(a, b));
    }

    MicrobenchResult {
        variants: n_variants,
        pixels,
        independent_ms,
        cached_ms,
        speedup_x: independent_ms / cached_ms.max(1e-9),
        max_abs_diff: diff,
        independent,
        cached,
        primary_test_reduction_x: independent.primary_sphere_tests as f64
            / (cached.primary_sphere_tests as f64).max(1.0),
        shadow_test_reduction_x: independent.shadow_sphere_tests as f64
            / (cached.shadow_sphere_tests as f64).max(1.0),
    }
}
