//! End-to-end caller: run each demo adapter through the engine and print the
//! emitted `SpecReceipt` JSON — the exact on-wire shape the Go control plane
//! ingests. Run with: `cargo run --example emit_receipt`.
//!
//! Every receipt here is labeled `evidence: "synthetic"` with a MODELED baseline,
//! so nothing printed can be mistaken for a measured lane win.

use cx_spec_engine::adapters::synth_render::{self, RenderTile};
use cx_spec_engine::adapters::synth_token::{self, window_with_match};
use cx_spec_engine::{BaselineSource, Details, Evidence};

fn main() {
    // --- render lane: whole-unit accept, truth=None, exact=false ---
    let render = synth_render::pipeline("cx_synth_render_demo");
    let tiles = vec![
        RenderTile {
            id: 0,
            residual: 0.00,
        }, // Delivery (ship draft)
        RenderTile {
            id: 1,
            residual: 0.05,
        }, // Preview  (ship draft as preview)
        RenderTile {
            id: 2,
            residual: 0.40,
        }, // Fail     (re-render at reference)
    ];
    let mut rdetails = Details::new();
    rdetails.insert("scene".into(), serde_json::json!("cube_volume"));
    rdetails.insert(
        "draft_spp".into(),
        serde_json::json!(synth_render::DRAFT_SPP),
    );
    let (_r_out, r_receipt) = render.run_batch(
        &tiles,
        1.0,
        BaselineSource::Modeled,
        Evidence::Synthetic,
        rdetails,
    );
    println!("// render lane receipt");
    println!("{}", serde_json::to_string_pretty(&r_receipt).unwrap());

    // --- token lane: partial accept, truth=Some, exact=true ---
    let token = synth_token::pipeline("cx_synth_token_demo");
    let windows = vec![
        window_with_match(0, 3, 8, 8),  // Delivery (all 8 matched)
        window_with_match(1, 10, 8, 6), // Preview  (6 of 8)
        window_with_match(2, 20, 8, 2), // Fail     (2 of 8)
    ];
    let mut tdetails = Details::new();
    tdetails.insert("prompt_class".into(), serde_json::json!("repeat"));
    let (_t_out, t_receipt) = token.run_batch(
        &windows,
        0.02,
        BaselineSource::Modeled,
        Evidence::Synthetic,
        tdetails,
    );
    println!("\n// token lane receipt");
    println!("{}", serde_json::to_string_pretty(&t_receipt).unwrap());

    // --- honesty: no baseline => speedup is null, never fabricated ---
    let (_t_out2, t_absent) = token.run_batch(
        &windows,
        0.0,
        BaselineSource::Absent,
        Evidence::Synthetic,
        Details::new(),
    );
    println!("\n// token lane, no baseline supplied (speedup_vs_baseline is null)");
    println!(
        "speedup_vs_baseline = {}",
        serde_json::to_value(&t_absent).unwrap()["speedup_vs_baseline"]
    );
}
