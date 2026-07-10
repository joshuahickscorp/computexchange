//! M0 — a pure-CPU unidirectional path tracer whose only job is to pass the
//! **white furnace test**.
//!
//! The furnace test is the cheapest known ground-truth correctness gate for a
//! path tracer, and it needs zero assets:
//!
//!   Put a Lambertian sphere of albedo 1.0 inside an environment that emits a
//!   *uniform* radiance L in every direction. A perfectly-diffuse, perfectly-
//!   reflective surface in a uniform field must re-emit exactly L (energy in ==
//!   energy out, cosine-weighted hemisphere integral of rho/pi is exactly rho).
//!   So the sphere is INVISIBLE: every pixel — sphere or background — must read
//!   L, to within Monte Carlo noise.
//!
//! Any of the classic integrator bugs breaks this to a value the eye (and this
//! test) can see:
//!   * dropping the cosine term, or the /pi in the BRDF, or mismatching the
//!     sampling pdf  -> sphere darkens or brightens (throughput != albedo);
//!   * a bad self-intersection epsilon                    -> dark speckle ring;
//!   * sampling directions below the surface              -> energy loss at the
//!                                                           terminator.
//!
//! We deliberately use **uniform hemisphere sampling** (not cosine-importance
//! sampling) so the estimator actually exercises the full `brdf * cos / pdf`
//! weighting. With cosine sampling every term cancels to a constant and the
//! image has literally zero variance — a weaker test. With uniform sampling the
//! per-path estimate is `2 * cos(theta)` (mean 1, variance 1/3), so passing the
//! gate proves the weighting converges to the analytic answer under real MC
//! noise.

use crate::rng::Pcg32;
use crate::vec3::{vec3, Vec3};

const PI: f32 = std::f32::consts::PI;

#[derive(Copy, Clone)]
pub struct Ray {
    pub origin: Vec3,
    pub dir: Vec3,
}

#[derive(Copy, Clone)]
pub struct Sphere {
    pub center: Vec3,
    pub radius: f32,
    /// Lambertian albedo, per channel. Furnace gate demands (1,1,1).
    pub albedo: Vec3,
}

impl Sphere {
    /// Nearest ray-sphere hit with `t > t_min`, or `None`. Returns (t, normal).
    #[inline]
    fn hit(&self, r: &Ray, t_min: f32) -> Option<(f32, Vec3)> {
        let oc = r.origin - self.center;
        let a = r.dir.dot(r.dir);
        let half_b = oc.dot(r.dir);
        let c = oc.dot(oc) - self.radius * self.radius;
        let disc = half_b * half_b - a * c;
        if disc < 0.0 {
            return None;
        }
        let sqrt_d = disc.sqrt();
        // near root first
        let mut t = (-half_b - sqrt_d) / a;
        if t <= t_min {
            t = (-half_b + sqrt_d) / a;
            if t <= t_min {
                return None;
            }
        }
        let p = r.origin + r.dir * t;
        let n = (p - self.center) / self.radius; // outward unit normal
        Some((t, n))
    }
}

pub struct Scene {
    pub sphere: Sphere,
    /// Uniform environment radiance seen when a ray escapes to infinity.
    pub env_radiance: Vec3,
}

impl Scene {
    /// The canonical white furnace: 100%-albedo sphere, uniform env radiance L.
    pub fn white_furnace(env: f32) -> Scene {
        Scene {
            sphere: Sphere {
                center: Vec3::ZERO,
                radius: 1.0,
                albedo: Vec3::ONE,
            },
            env_radiance: Vec3::splat(env),
        }
    }
}

/// Sample a direction uniformly over the hemisphere about unit `n`.
/// Returns (direction, cos_theta) where cos_theta = dot(dir, n).
#[inline]
fn sample_hemisphere_uniform(n: Vec3, rng: &mut Pcg32) -> (Vec3, f32) {
    let u1 = rng.next_f32(); // == cos(theta), uniform in [0,1) -> uniform solid angle
    let u2 = rng.next_f32();
    let cos_theta = u1;
    let sin_theta = (1.0 - u1 * u1).max(0.0).sqrt();
    let phi = 2.0 * PI * u2;
    let (t, b) = Vec3::build_onb(n);
    let dir = t * (sin_theta * phi.cos()) + b * (sin_theta * phi.sin()) + n * cos_theta;
    (dir, cos_theta)
}

/// Estimate incoming radiance along `ray`. Plain unidirectional path tracing:
/// the ONLY emitter is the environment (reached on a miss). No NEE/MIS is
/// needed because there are no localized lights — the furnace's whole point is a
/// uniform field. `max_depth` guards against pathological non-termination; for a
/// single convex sphere in an open field every path escapes in exactly one
/// bounce, so it is never actually hit here.
pub fn radiance(mut ray: Ray, scene: &Scene, rng: &mut Pcg32, max_depth: u32) -> Vec3 {
    const T_MIN: f32 = 1e-4;
    let mut throughput = Vec3::ONE;

    for _ in 0..max_depth {
        match scene.sphere.hit(&ray, T_MIN) {
            None => {
                // Escaped to the uniform environment.
                return throughput.mul_comp(scene.env_radiance);
            }
            Some((t, n)) => {
                let hit_p = ray.origin + ray.dir * t;
                let (new_dir, cos_theta) = sample_hemisphere_uniform(n, rng);

                // Lambertian BRDF f = albedo / pi.
                // Uniform-hemisphere pdf p = 1 / (2*pi).
                // MC weight = f * cos / p = (albedo/pi) * cos * (2*pi)
                //           = albedo * 2 * cos.
                let weight = scene.sphere.albedo * (2.0 * cos_theta);
                throughput = throughput.mul_comp(weight);

                ray = Ray {
                    origin: hit_p + n * T_MIN,
                    dir: new_dir,
                };
            }
        }
    }
    // Depth-limited without escaping: return black (energy loss). Never happens
    // for the convex-sphere furnace; kept so a future concave scene stays bias-
    // honest rather than looping forever.
    Vec3::ZERO
}

pub struct RenderConfig {
    pub width: usize,
    pub height: usize,
    pub spp: usize,
    pub max_depth: u32,
    /// Vertical field of view, radians.
    pub fov_y: f32,
    /// Base seed; per-pixel streams are derived from it deterministically.
    pub seed: u64,
}

impl Default for RenderConfig {
    fn default() -> Self {
        RenderConfig {
            width: 64,
            height: 64,
            spp: 2048,
            max_depth: 16,
            fov_y: 40.0_f32.to_radians(),
            seed: 0x5eed_1234_abcd_0001,
        }
    }
}

/// A rendered frame: linear-radiance RGB, row-major, `width * height` pixels.
pub struct Frame {
    pub width: usize,
    pub height: usize,
    pub pixels: Vec<Vec3>,
    /// Fraction of pixels whose primary ray hit the sphere (diagnostic — the
    /// furnace is only interesting if the sphere actually covers pixels).
    pub sphere_hit_fraction: f32,
}

/// Pinhole camera at (0,0,`cam_z`) looking down -Z at the origin sphere.
pub fn render(scene: &Scene, cfg: &RenderConfig) -> Frame {
    let cam_z = 4.0_f32;
    let eye = vec3(0.0, 0.0, cam_z);
    let forward = vec3(0.0, 0.0, -1.0);
    let right = vec3(1.0, 0.0, 0.0);
    let up = vec3(0.0, 1.0, 0.0);
    let aspect = cfg.width as f32 / cfg.height as f32;
    let tan_half = (cfg.fov_y * 0.5).tan();

    let mut pixels = vec![Vec3::ZERO; cfg.width * cfg.height];
    let mut sphere_hits: u64 = 0;
    let inv_spp = 1.0 / cfg.spp as f32;

    for y in 0..cfg.height {
        for x in 0..cfg.width {
            // Deterministic, decorrelated per-pixel stream.
            let pix_idx = (y * cfg.width + x) as u64;
            let mut rng = Pcg32::new(
                cfg.seed ^ pix_idx.wrapping_mul(0x9E3779B97F4A7C15),
                pix_idx.wrapping_mul(0xD1B54A32D192ED03) | 1,
            );

            let mut acc = Vec3::ZERO;
            for _ in 0..cfg.spp {
                let jx = rng.next_f32();
                let jy = rng.next_f32();
                let ndc_x = ((x as f32 + jx) / cfg.width as f32) * 2.0 - 1.0;
                let ndc_y = 1.0 - ((y as f32 + jy) / cfg.height as f32) * 2.0;
                let dir = (forward
                    + right * (ndc_x * aspect * tan_half)
                    + up * (ndc_y * tan_half))
                    .normalize();
                let ray = Ray { origin: eye, dir };

                if scene.sphere.hit(&ray, 1e-4).is_some() {
                    sphere_hits += 1;
                }
                acc = acc + radiance(ray, scene, &mut rng, cfg.max_depth);
            }
            pixels[y * cfg.width + x] = acc * inv_spp;
        }
    }

    let total_samples = (cfg.width * cfg.height * cfg.spp) as f32;
    Frame {
        width: cfg.width,
        height: cfg.height,
        pixels,
        sphere_hit_fraction: sphere_hits as f32 / total_samples,
    }
}

/// Deviation of a rendered frame from a uniform reference radiance — the actual
/// furnace metric. All values are per-channel-averaged magnitudes of
/// (pixel - env).
pub struct FurnaceStats {
    /// Mean over the whole image of |pixel - env|, averaged over channels.
    pub mean_abs_dev: f32,
    /// sqrt(mean over image of ||pixel - env||^2 / 3) — RMS per-channel dev.
    pub rms_dev: f32,
    /// Largest single per-channel |pixel - env| anywhere in the image.
    pub max_abs_dev: f32,
    /// Grand mean radiance over the whole image (should equal env to << noise).
    pub grand_mean: Vec3,
}

pub fn furnace_stats(frame: &Frame, env: Vec3) -> FurnaceStats {
    let n = frame.pixels.len() as f32;
    let mut sum_abs = 0.0f64;
    let mut sum_sq = 0.0f64;
    let mut max_abs = 0.0f32;
    let mut mean = Vec3::ZERO;

    for p in &frame.pixels {
        let d = *p - env;
        for &c in &[d.x, d.y, d.z] {
            let a = c.abs();
            sum_abs += a as f64;
            sum_sq += (c as f64) * (c as f64);
            if a > max_abs {
                max_abs = a;
            }
        }
        mean = mean + *p;
    }

    FurnaceStats {
        mean_abs_dev: (sum_abs / (n as f64 * 3.0)) as f32,
        rms_dev: (sum_sq / (n as f64 * 3.0)).sqrt() as f32,
        max_abs_dev: max_abs,
        grand_mean: mean / n,
    }
}
