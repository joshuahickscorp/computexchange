use cx_renderer::decoupled::run_microbench;

#[test]
fn cached_reshade_matches_independent_trace_for_albedo_variants() {
    let r = run_microbench(96, 96, 8, 3);
    println!(
        "\n===== DECOUPLED SHADING MICROBENCH =====\n\
variants        : {}\n\
pixels          : {}\n\
independent_ms  : {:.3}\n\
cached_ms       : {:.3}\n\
speedup_x       : {:.3}\n\
max_abs_diff    : {:.3e}\n\
primary_tests   : {} -> {} ({:.2}x reduction)\n\
shadow_tests    : {} -> {} ({:.2}x reduction)\n\
shade_evals     : {} -> {}\n\
========================================\n",
        r.variants,
        r.pixels,
        r.independent_ms,
        r.cached_ms,
        r.speedup_x,
        r.max_abs_diff,
        r.independent.primary_sphere_tests,
        r.cached.primary_sphere_tests,
        r.primary_test_reduction_x,
        r.independent.shadow_sphere_tests,
        r.cached.shadow_sphere_tests,
        r.shadow_test_reduction_x,
        r.independent.shade_evals,
        r.cached.shade_evals,
    );

    assert!(
        r.max_abs_diff <= 1e-7,
        "cached re-shade must be exact in the albedo-only regime; diff={}",
        r.max_abs_diff
    );
    assert!(
        r.primary_test_reduction_x >= 7.9,
        "8 variants should trace primary geometry once, not per variant; reduction={}",
        r.primary_test_reduction_x
    );
    assert!(
        r.shadow_test_reduction_x >= 7.9,
        "8 variants should trace shadow visibility once, not per variant; reduction={}",
        r.shadow_test_reduction_x
    );
}
