use cx_renderer::decoupled::run_microbench;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let variants = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(8usize);
    let size = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(192usize);
    let repeats = args.get(3).and_then(|s| s.parse().ok()).unwrap_or(12usize);
    let r = run_microbench(size, size, variants, repeats);
    println!(
        "{{\"ok\":true,\"stage\":\"decoupled_micro\",\"variants\":{},\"pixels\":{},\
\"independent_ms\":{:.3},\"cached_ms\":{:.3},\"speedup_x\":{:.3},\
\"max_abs_diff\":{:.3e},\"primary_test_reduction_x\":{:.3},\
\"shadow_test_reduction_x\":{:.3},\"independent_primary_tests\":{},\
\"cached_primary_tests\":{},\"independent_shadow_tests\":{},\"cached_shadow_tests\":{},\
\"independent_shade_evals\":{},\"cached_shade_evals\":{}}}",
        r.variants,
        r.pixels,
        r.independent_ms,
        r.cached_ms,
        r.speedup_x,
        r.max_abs_diff,
        r.primary_test_reduction_x,
        r.shadow_test_reduction_x,
        r.independent.primary_sphere_tests,
        r.cached.primary_sphere_tests,
        r.independent.shadow_sphere_tests,
        r.cached.shadow_sphere_tests,
        r.independent.shade_evals,
        r.cached.shade_evals,
    );
}
