//! Cross-lane ingest proof for the consolidation wave.
//!
//! The speculation lanes each emit a receipt from a DIFFERENT codebase:
//!   * Lane 1 — `scripts/spec-lab/cx_speculative_core.py` (`SpecReceipt.to_dict()`)
//!   * Lane 2 — `scripts/spec-lab/cx_render_spec_adapter.py` (`RenderSpecReceipt.to_dict()`)
//!   * Lane 3 — `token-spec-poc` (`token_spec_poc::SpecReceipt` serialized by `main.rs`)
//!   * Lane 4 — `scripts/spec-lab/cx_transcode_spec_adapter.py`
//!     (`TranscodeSpecReceipt.to_dict()`) — the generalization wave's SECOND real media
//!     modality; emits the canonical `receipt.rs` names NATIVELY (no alias crutch)
//!
//! This test PROVES they compose on the wire: each JSON blob below was produced by
//! ACTUALLY INVOKING its emitter (Python `to_dict()` / `cargo run` on the POC), then
//! pasted here verbatim — none is hand-authored. Every one must
//! `serde_json::from_str::<SpecReceipt>` into the canonical Branch-A schema with the
//! costs / `exact` / `quality_tier` / `speedup` the emitter meant.
//!
//! Regeneration (money-safe, local, no cloud/Blender):
//!   Lane 1: python3 -c 'see cx_speculative_core.SpeculativeEngine(...).run(...).to_dict()'
//!   Lane 2: python3 -c 'cx_render_spec_adapter.RenderSpecAdapter().receipt_from_measurements(...).to_dict()'
//!   Lane 3: (cd token-spec-poc && cargo run --quiet | head -1)
//!   Lane 4: python3 scripts/spec-lab/cx_transcode_spec_adapter.py --codec x264 --gate 0.99
//!           (needs local ffmpeg; the blob below is a REAL MEASURED local run, ledgered in
//!           docs/speed-lane-reports/spec-lab/transcode_spec_ledger.jsonl)

use cx_spec_engine::{BaselineSource, Evidence, QualityTier, SpecReceipt};

/// Absolute cost tolerance. The blobs are frozen snapshots, so equality is exact
/// in practice; the epsilon just documents that costs are floats, not that they drift.
fn close(a: f64, b: f64) {
    assert!((a - b).abs() < 1e-9, "cost mismatch: {a} vs {b}");
}

// ---------------------------------------------------------------------------
// Lane 1 — cx_speculative_core.py SpecReceipt.to_dict()
// Emitted by SpeculativeEngine("cx_core_demo","token", ...).run(8 units).to_dict().
// Legacy shape: costs under *_s, bare speedup_x, a bool quality_gate (no tier),
// modality extras under `meta`. Reads into A purely via the pre-existing aliases.
// ---------------------------------------------------------------------------
const LANE1_CORE: &str = r#"{"branch_id": "cx_core_demo", "modality": "token", "units": 8, "attempted_units": 8, "fallback_units": 0, "accepted_units": 6, "repaired_units": 2, "rejected_units": 2, "attempted_fraction": 1.0, "fallback_fraction": 0.0, "accepted_fraction": 0.75, "accepted_attempt_fraction": 0.75, "repaired_fraction": 0.25, "draft_s": 7e-06, "verify_s": 9e-06, "repair_s": 2e-06, "fallback_s": 0.0, "baseline_s": 2e-06, "speculative_s": 1.8e-05, "speedup_x": 0.106433, "exact": true, "quality_gate": true, "meta": {"prompt_class": "repeat"}}"#;

// ---------------------------------------------------------------------------
// Lane 2 — cx_render_spec_adapter.py RenderSpecReceipt.to_dict()
// 4 tiles, 3 accept + 1 repair-to-reference. Post-reconciliation shape: costs under
// the no-suffix *_cost / total_product_time / baseline_cost names (read via the
// aliases added to A), a closed-enum quality_tier ("delivery"), a lower-cased
// evidence ("synthetic"), an explicit exact=false, and the SSIM gate-spec string
// relocated into meta.quality_gate_spec (it is NOT a quality_tier).
// ---------------------------------------------------------------------------
const LANE2_RENDER: &str = r#"{"draft_cost": 4.0, "verify_cost": 0.4, "accepted_fraction": 0.75, "repair_cost": 8.0, "total_product_time": 12.4, "quality_tier": "delivery", "speedup_vs_baseline": 2.580645, "exact": false, "modality": "render", "branch_id": "render", "units": 4, "accepted_units": 3, "repaired_units": 1, "repaired_fraction": 0.25, "baseline_cost": 32.0, "quality_gate": true, "delivery_eligible": true, "evidence": "synthetic", "global_ssim": 0.97, "worst_tile_ssim": 0.8, "p5_ssim": null, "claim_scope": "Measured single delivered unit ratio only; per-tile ratios are NOT multiplied. baseline_cost is a real reference-quality render of this unit.", "meta": {"quality_gate_spec": "g>=0.98,wt>=0.95"}}"#;

// ---------------------------------------------------------------------------
// Lane 3 — token-spec-poc `cargo run` first line (the "code" stream).
// Same *_s / speedup_x / bool-quality_gate shape as Lane 1, but `meta` is a rich
// object (the ReceiptMeta struct) — it must land in A's free-form `details` map.
// ---------------------------------------------------------------------------
const LANE3_TOKEN: &str = r#"{"branch_id":"token-spec-poc","modality":"token","units":694,"attempted_units":694,"fallback_units":0,"accepted_units":133,"repaired_units":46,"rejected_units":561,"attempted_fraction":1.0,"fallback_fraction":0.0,"accepted_fraction":0.191643,"repaired_fraction":0.136499,"draft_s":0.002218,"verify_s":0.000078,"repair_s":0.0,"fallback_s":0.0,"baseline_s":0.000065,"speculative_s":0.002638,"speedup_x":0.024516,"exact":true,"quality_gate":true,"meta":{"tokens_emitted":470,"rounds":337,"target_calls":337,"target_call_reduction_x":1.394659,"mean_accept_len":0.394659,"draft_producer":"ngram_prompt_lookup","target_backend":"mock_fixed_stream","walltime_label":"MODELED","notes":"stream=code; order=3; k=16; speedup_x is MODELED (verify pass is not yet one decode step — needs forward_all_logits + KvCacheSlot::truncate per TOKEN_LANE_FORK_DESIGN.md). accepted_fraction and target_call_reduction_x are MEASURED."}}"#;

// ---------------------------------------------------------------------------
// Lane 4 — cx_transcode_spec_adapter.py TranscodeSpecReceipt.to_dict()
// A REAL MEASURED local ffmpeg run (Apple Silicon CPU, x264 CRF23 ultrafast-draft vs
// veryslow-baseline, 0.99 SSIM gate): 6 segments, 4 accepted + 2 repaired at the baseline
// recipe, one-ratio speedup 1.037785. This lane emits the canonical receipt.rs field names
// NATIVELY — draft_cost_s / total_product_time_s / baseline_total_time_s / enum
// quality_tier / lower-case evidence / explicit baseline_source — so ingest must succeed
// with ZERO serde aliases exercised. Pasted verbatim from the ledger (line 3 of
// docs/speed-lane-reports/spec-lab/transcode_spec_ledger.jsonl), never hand-authored.
// ---------------------------------------------------------------------------
const LANE4_TRANSCODE: &str = r#"{"accepted_fraction": 0.666667, "accepted_units": 4, "baseline_source": "measured", "baseline_total_time_s": 6.465343, "branch_id": "transcode", "claim_scope": "Measured single delivered-video ratio only; per-segment ratios are NOT multiplied. baseline_total_time_s is a real whole-video slow-preset encode of the SAME source. SSIM-gated delivery is NOT lossless (exact=false).", "details": {"audit_note": "baseline_ssim/delivered_ssim/repaired_ssim and the byte-checks are post-delivery audits — measurement-only, never charged to total_product_time_s.", "baseline_bytes": 5527363, "baseline_ssim_vs_source": 0.992691, "bitexact_vs_baseline": false, "codec": "x264", "content": "testsrc2+mandelbrot halves, noise=alls=0:allf=t, 1280x720@30fps, 12s (synthesized locally, lossless x264 qp0 mezzanine; synthesis is content creation, charged to NEITHER lane)", "cost_note": "draft_cost_s = segmentation + per-segment fast-preset encodes + concat mux (segment 0.074943s + drafts + concat 0.079861s); verify_cost_s is CHARGED (the source is the pipeline input, so the SSIM/MD5 gate is a real product step, unlike the render lane's measurement-only reference SSIM); total_product_time_s = draft+verify+repair covers the FULL delivered pipeline.", "delivered_bytes": 10848928, "delivered_ssim_vs_source": 0.986597, "fast_args": "-c:v libx264 -preset ultrafast -crf 23 -pix_fmt yuv420p", "ffmpeg": "ffmpeg version 8.1.1 Copyright (c) 2000-2026 the FFmpeg developers", "gate_achievable_by_baseline": true, "host": "local (Apple Silicon, ffmpeg CPU encode)", "lossless_note": "SSIM-gated delivery is NOT lossless: accepted segments are lossy fast-preset encodes that cleared the gate, exact=false by construction.", "mode": "ssim", "parallelism_note": "segments are encoded SEQUENTIALLY — the wall-clock ratio contains no parallel-segment credit; segment-parallel fan-out is a real, unexercised lever.", "per_segment": [{"accepted": true, "draft_encode_s": 0.124356, "draft_ssim": 0.998699, "repair_encode_s": 0.0, "repaired_ssim": null, "seg_id": "seg_000", "verify_s": 0.126394}, {"accepted": true, "draft_encode_s": 0.125166, "draft_ssim": 0.998455, "repair_encode_s": 0.0, "repaired_ssim": null, "seg_id": "seg_001", "verify_s": 0.125583}, {"accepted": true, "draft_encode_s": 0.123661, "draft_ssim": 0.998571, "repair_encode_s": 0.0, "repaired_ssim": null, "seg_id": "seg_002", "verify_s": 0.124821}, {"accepted": true, "draft_encode_s": 0.250751, "draft_ssim": 0.99238, "repair_encode_s": 0.0, "repaired_ssim": null, "seg_id": "seg_003", "verify_s": 0.219748}, {"accepted": false, "draft_encode_s": 0.32463, "draft_ssim": 0.989903, "repair_encode_s": 1.618596, "repaired_ssim": 0.988765, "seg_id": "seg_004", "verify_s": 0.314859}, {"accepted": false, "draft_encode_s": 0.427014, "draft_ssim": 0.988318, "repair_encode_s": 1.795275, "repaired_ssim": 0.987202, "seg_id": "seg_005", "verify_s": 0.374283}], "repair_note": "repaired segments are re-encoded with the EXACT baseline recipe (same codec, CRF, preset), so delivered quality is gate-or-baseline everywhere; per-segment encoding lacks whole-video rate-control context (near-identical under CRF + keyframe-aligned segments, noted not hidden).", "run_label": "MEASURED/local", "segment_time_s": 2.0, "slow_args": "-c:v libx264 -preset veryslow -crf 23 -pix_fmt yuv420p", "source_synthesis_s_uncharged": 1.988885, "ssim_gate": 0.99}, "draft_cost_s": 1.530382, "evidence": "measured", "exact": false, "modality": "transcode", "quality_tier": "delivery", "repair_cost_s": 3.413871, "repaired_fraction": 0.333333, "repaired_units": 2, "speedup_vs_baseline": 1.037785, "total_product_time_s": 6.229942, "units": 6, "verify_cost_s": 1.285688}"#;

#[test]
fn lane1_core_python_ledger_ingests() {
    let r: SpecReceipt =
        serde_json::from_str(LANE1_CORE).expect("cx_speculative_core row must deserialize into A");
    assert_eq!(r.branch_id, "cx_core_demo");
    assert_eq!(r.modality.as_str(), "token");
    assert_eq!(r.units, 8);
    // costs read from the legacy *_s keys via alias.
    close(r.draft_cost_s, 7e-6);
    close(r.verify_cost_s, 9e-6);
    close(r.repair_cost_s, 2e-6);
    close(r.total_product_time_s, 1.8e-5); // speculative_s
    close(r.baseline_total_time_s, 2e-6); // baseline_s
    close(r.accepted_fraction, 0.75);
    assert!(r.exact, "core reports lossless token output");
    assert_eq!(r.speedup_vs_baseline, Some(0.106433)); // speedup_x alias
                                                       // A defaults the labels a legacy row never carried; the bool quality_gate is ignored.
    assert_eq!(r.quality_tier, QualityTier::Preview);
    assert_eq!(r.evidence, Evidence::Imported);
    assert_eq!(r.baseline_source, BaselineSource::Modeled);
    // `meta` bag lands in `details` via alias.
    assert_eq!(
        r.details.get("prompt_class").unwrap(),
        &serde_json::json!("repeat")
    );
}

#[test]
fn lane2_render_adapter_ingests() {
    let r: SpecReceipt = serde_json::from_str(LANE2_RENDER)
        .expect("cx_render_spec_adapter row must deserialize into A (the reconciled lane)");
    assert_eq!(r.branch_id, "render");
    assert_eq!(r.modality.as_str(), "render");
    assert_eq!(r.units, 4);
    // costs read from the no-suffix render keys via the newly added aliases.
    close(r.draft_cost_s, 4.0); // draft_cost
    close(r.verify_cost_s, 0.4); // verify_cost
    close(r.repair_cost_s, 8.0); // repair_cost
    close(r.total_product_time_s, 12.4); // total_product_time
    close(r.baseline_total_time_s, 32.0); // baseline_cost
    close(r.accepted_fraction, 0.75);
    // the render lane is never bit-exact; its fidelity is the tier, which the gate earned.
    assert!(!r.exact, "render is never exact vs a full reference");
    assert_eq!(r.quality_tier, QualityTier::Delivery);
    assert_eq!(r.evidence, Evidence::Synthetic); // lower-cased "synthetic" ingested
    assert_eq!(r.speedup_vs_baseline, Some(2.580645)); // baseline/total, one ratio
                                                       // the SSIM gate-spec string is preserved (not lost) under details.quality_gate_spec.
    assert_eq!(
        r.details.get("quality_gate_spec").unwrap(),
        &serde_json::json!("g>=0.98,wt>=0.95"),
        "the free SSIM gate-spec must survive in details, never as quality_tier"
    );
}

#[test]
fn lane3_token_poc_ingests() {
    let r: SpecReceipt =
        serde_json::from_str(LANE3_TOKEN).expect("token-spec-poc row must deserialize into A");
    assert_eq!(r.branch_id, "token-spec-poc");
    assert_eq!(r.modality.as_str(), "token");
    assert_eq!(r.units, 694);
    close(r.draft_cost_s, 0.002218);
    close(r.verify_cost_s, 0.000078);
    close(r.repair_cost_s, 0.0);
    close(r.total_product_time_s, 0.002638); // speculative_s
    close(r.baseline_total_time_s, 0.000065); // baseline_s
    close(r.accepted_fraction, 0.191643);
    assert!(r.exact, "the POC's greedy spec-decode is provably lossless");
    assert_eq!(r.speedup_vs_baseline, Some(0.024516)); // speedup_x alias
    assert_eq!(r.quality_tier, QualityTier::Preview); // defaulted (bool quality_gate ignored)
    assert_eq!(r.evidence, Evidence::Imported); // defaulted
                                                // the rich ReceiptMeta struct lands whole in A's free-form `details` map.
    assert_eq!(
        r.details.get("walltime_label").unwrap(),
        &serde_json::json!("MODELED")
    );
    assert_eq!(
        r.details.get("target_call_reduction_x").unwrap(),
        &serde_json::json!(1.394659)
    );
}

#[test]
fn lane4_transcode_adapter_ingests_natively() {
    let r: SpecReceipt = serde_json::from_str(LANE4_TRANSCODE)
        .expect("cx_transcode_spec_adapter row must deserialize into A with zero aliases");
    assert_eq!(r.branch_id, "transcode");
    assert_eq!(r.modality.as_str(), "transcode");
    assert_eq!(r.units, 6);
    // canonical names read DIRECTLY — this lane never emitted a legacy key.
    close(r.draft_cost_s, 1.530382);
    close(r.verify_cost_s, 1.285688);
    close(r.repair_cost_s, 3.413871);
    close(r.total_product_time_s, 6.229942);
    close(r.baseline_total_time_s, 6.465343);
    close(r.accepted_fraction, 0.666667);
    close(r.repaired_fraction, 0.333333);
    // SSIM-gated transcode is NOT lossless, even at delivery tier — the tier says the
    // delivered video is gate-or-baseline quality everywhere, exact says bytes.
    assert!(!r.exact, "ssim-gated transcode must never claim exactness");
    assert_eq!(r.quality_tier, QualityTier::Delivery);
    // A REAL measured local run: labels are explicit, nothing defaults.
    assert_eq!(r.evidence, Evidence::Measured);
    assert_eq!(r.baseline_source, BaselineSource::Measured);
    assert_eq!(r.speedup_vs_baseline, Some(1.037785)); // one ratio, baseline/total
                                                       // honesty extras survive in details.
    assert_eq!(
        r.details.get("run_label").unwrap(),
        &serde_json::json!("MEASURED/local")
    );
    assert_eq!(
        r.details.get("gate_achievable_by_baseline").unwrap(),
        &serde_json::json!(true),
        "the reference-self-consistency flag must survive ingest"
    );
    assert_eq!(
        r.details
            .get("per_segment")
            .unwrap()
            .as_array()
            .unwrap()
            .len(),
        6,
        "per-segment transparency must land whole in details"
    );
}

/// The composition claim itself: all four lanes land in ONE `Vec<SpecReceipt>`,
/// i.e. they are the same on-the-wire type after ingest.
#[test]
fn all_lanes_share_one_schema() {
    let lanes = [LANE1_CORE, LANE2_RENDER, LANE3_TOKEN, LANE4_TRANSCODE];
    let receipts: Vec<SpecReceipt> = lanes
        .iter()
        .map(|j| serde_json::from_str(j).expect("every lane must ingest into the one schema"))
        .collect();
    assert_eq!(receipts.len(), 4);
    // Each re-serializes back to canonical JSON carrying the plan spine keys.
    for r in &receipts {
        let v = serde_json::to_value(r).unwrap();
        for k in [
            "draft_cost_s",
            "verify_cost_s",
            "repair_cost_s",
            "total_product_time_s",
            "baseline_total_time_s",
            "quality_tier",
            "speedup_vs_baseline",
            "exact",
        ] {
            assert!(v.get(k).is_some(), "re-serialized receipt missing `{k}`");
        }
    }
    // modality tags survive so the staged-multiplier table can group by lane.
    assert_eq!(receipts[0].modality.as_str(), "token");
    assert_eq!(receipts[1].modality.as_str(), "render");
    assert_eq!(receipts[2].modality.as_str(), "token");
    assert_eq!(receipts[3].modality.as_str(), "transcode");
}
