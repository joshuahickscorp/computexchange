//! THE M0 CORRECTNESS GATE.
//!
//! A 100%-albedo Lambertian sphere in a uniform-radiance environment must
//! render INVISIBLE: every pixel equals the environment radiance to within
//! Monte Carlo noise. This test renders the furnace and asserts the measured
//! deviation is inside statistically safe bounds. Run with printed numbers:
//!
//!   cargo test --release -- --nocapture
//!
//! The bounds below are NOT fitted to one lucky run — they are set from the
//! analytic noise of the estimator so the gate is meaningful, and the RNG is
//! seeded deterministically so the result is reproducible run-to-run.

use cx_renderer::furnace::{furnace_stats, render, RenderConfig, Scene};
use cx_renderer::vec3::Vec3;

/// Analytic per-path standard deviation of the uniform-hemisphere furnace
/// estimator: the single-bounce estimate is `2*cos(theta)` with mean 1 and
/// variance 1/3, so a sphere pixel's mean-of-`spp` has std = sqrt(1/3 / spp).
/// Background pixels are exact (zero variance). We size the tolerances from this.
fn per_sphere_pixel_std(spp: usize) -> f32 {
    ((1.0 / 3.0) / spp as f32).sqrt()
}

#[test]
fn white_furnace_sphere_is_invisible() {
    let env_level = 0.5_f32;
    let scene = Scene::white_furnace(env_level);
    let env = Vec3::splat(env_level);

    let cfg = RenderConfig {
        width: 64,
        height: 64,
        spp: 2048,
        ..RenderConfig::default()
    };

    let frame = render(&scene, &cfg);
    let stats = furnace_stats(&frame, env);

    let sigma = per_sphere_pixel_std(cfg.spp);
    let n_pixels = (cfg.width * cfg.height) as f32;

    // Grand mean averages noise over EVERY pixel*sample; its std is minuscule.
    // Allow a generous 0.005 absolute.
    let grand_dev = ((stats.grand_mean.x - env_level).abs()
        + (stats.grand_mean.y - env_level).abs()
        + (stats.grand_mean.z - env_level).abs())
        / 3.0;

    // RMS per-pixel dev over the whole image is at most the sphere-pixel sigma
    // (background pixels are exact, so they only pull it down). Allow 1.5x
    // headroom for the finite-pixel estimate.
    let rms_bound = 1.5 * sigma;

    // Max single-channel dev: with ~n_pixels*3 near-Gaussian samples the extreme
    // is ~sqrt(2*ln(N)) sigma. For N ~ 12k that's ~4.4 sigma; use 6 sigma so a
    // fat MC tail never flakes the gate.
    let max_bound = 6.0 * sigma;

    println!("\n===== WHITE FURNACE TEST =====");
    println!(
        "resolution      : {}x{}  ({} pixels)",
        cfg.width,
        cfg.height,
        n_pixels as u64
    );
    println!("samples/pixel   : {}", cfg.spp);
    println!("max path depth  : {}", cfg.max_depth);
    println!("env radiance L  : {}", env_level);
    println!(
        "sphere coverage : {:.1}% of primary rays hit the sphere",
        frame.sphere_hit_fraction * 100.0
    );
    println!("--- measured deviation from uniform env ---");
    println!(
        "grand mean      : ({:.6}, {:.6}, {:.6})   (target {:.6})",
        stats.grand_mean.x, stats.grand_mean.y, stats.grand_mean.z, env_level
    );
    println!("grand-mean |dev|: {:.3e}   (bound {:.3e})", grand_dev, 0.005);
    println!("RMS per-px dev  : {:.6}   (bound {:.6})", stats.rms_dev, rms_bound);
    println!("mean |dev|      : {:.6}", stats.mean_abs_dev);
    println!("max |dev|       : {:.6}   (bound {:.6})", stats.max_abs_dev, max_bound);
    println!(
        "analytic sphere-pixel sigma @ {} spp: {:.6}",
        cfg.spp, sigma
    );
    println!("================================\n");

    // The sphere must actually be in frame, or the test proves nothing.
    assert!(
        frame.sphere_hit_fraction > 0.20,
        "sphere covers only {:.1}% of rays — furnace not exercised",
        frame.sphere_hit_fraction * 100.0
    );
    assert!(
        grand_dev < 0.005,
        "grand-mean radiance {:?} drifted from env {} — energy not conserved",
        stats.grand_mean,
        env_level
    );
    assert!(
        stats.rms_dev < rms_bound,
        "RMS deviation {:.6} exceeds MC-noise bound {:.6}",
        stats.rms_dev,
        rms_bound
    );
    assert!(
        stats.max_abs_dev < max_bound,
        "max deviation {:.6} exceeds 6-sigma bound {:.6} — structured error, not noise",
        stats.max_abs_dev,
        max_bound
    );
}

/// Sanity guard on the metric itself: a WRONG integrator (albedo 0.5 instead of
/// 1.0) must FAIL the invisibility check. Proves the furnace test has teeth and
/// isn't passing vacuously.
#[test]
fn furnace_detects_energy_loss() {
    let env_level = 0.5_f32;
    let mut scene = Scene::white_furnace(env_level);
    scene.sphere.albedo = Vec3::splat(0.5); // absorbing sphere — should darken
    let env = Vec3::splat(env_level);

    let cfg = RenderConfig {
        width: 48,
        height: 48,
        spp: 512,
        ..RenderConfig::default()
    };
    let frame = render(&scene, &cfg);
    let stats = furnace_stats(&frame, env);

    // A 0.5-albedo sphere reflects half the field: sphere pixels read ~0.25, a
    // ~0.25 deviation. The grand mean must visibly drop below env.
    println!(
        "\n[energy-loss guard] albedo=0.5 grand mean = ({:.4},{:.4},{:.4}), env={}",
        stats.grand_mean.x, stats.grand_mean.y, stats.grand_mean.z, env_level
    );
    assert!(
        stats.mean_abs_dev > 0.02,
        "a 0.5-albedo sphere should darken the frame, but mean dev was only {:.6} — \
         the furnace metric is not actually measuring energy",
        stats.mean_abs_dev
    );
}
