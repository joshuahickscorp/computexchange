//! Integration test: the on-wire receipt contract the Go control plane binds —
//! every Go-mirror key present, `speedup_vs_baseline` null when unearned, a serde
//! round-trip, and a legacy Python ledger row read via the `#[serde(alias)]`es.

use cx_spec_engine::adapters::synth_token::{self, window_with_match};
use cx_spec_engine::{BaselineSource, Details, Evidence, SpecReceipt};

/// The exact snake_case keys the Go `SpecReceipt` mirror binds (see
/// `docs/research/SPEC_ENGINE_SUBSTRATE_DESIGN.md` §Go ingestion mirror).
const GO_MIRROR_KEYS: &[&str] = &[
    "schema_version",
    "branch_id",
    "modality",
    "draft_cost_s",
    "verify_cost_s",
    "repair_cost_s",
    "overhead_cost_s",
    "total_product_time_s",
    "baseline_total_time_s",
    "baseline_source",
    "units",
    "accepted_fraction",
    "repaired_fraction",
    "exact",
    "artifact_verified",
    "quality_tier",
    "speedup_vs_baseline",
    "evidence",
    "details",
];

fn demo_receipt(baseline_source: BaselineSource) -> SpecReceipt {
    let pipe = synth_token::pipeline("cx_json_it");
    let windows = vec![
        window_with_match(0, 3, 8, 6),
        window_with_match(1, 30, 4, 4),
    ];
    let mut details = Details::new();
    details.insert("prompt_class".into(), serde_json::json!("repeat"));
    let baseline_s = if baseline_source == BaselineSource::Absent {
        0.0
    } else {
        0.02
    };
    let (_outs, receipt) = pipe.run_batch(
        &windows,
        baseline_s,
        baseline_source,
        Evidence::Synthetic,
        details,
    );
    receipt
}

#[test]
fn receipt_carries_every_go_mirror_key() {
    let receipt = demo_receipt(BaselineSource::Modeled);
    let v = serde_json::to_value(&receipt).unwrap();
    let obj = v.as_object().expect("receipt serializes to a JSON object");
    for k in GO_MIRROR_KEYS {
        assert!(
            obj.contains_key(*k),
            "receipt JSON is missing Go-mirror key `{k}`"
        );
    }
    // modality is a bare string (transparent newtype), details is an object.
    assert!(obj["modality"].is_string());
    assert!(obj["details"].is_object());
    assert_eq!(obj["details"]["prompt_class"], serde_json::json!("repeat"));
}

#[test]
fn unearned_speedup_serializes_to_json_null() {
    let receipt = demo_receipt(BaselineSource::Absent);
    assert!(receipt.speedup_vs_baseline.is_none());
    let v = serde_json::to_value(&receipt).unwrap();
    assert!(
        v.get("speedup_vs_baseline").unwrap().is_null(),
        "an unearned speedup must be JSON null, never fabricated"
    );
}

#[test]
fn receipt_round_trips_through_json() {
    let receipt = demo_receipt(BaselineSource::Modeled);
    let s = serde_json::to_string(&receipt).unwrap();
    let back: SpecReceipt = serde_json::from_str(&s).unwrap();
    assert_eq!(
        serde_json::to_value(&back).unwrap(),
        serde_json::to_value(&receipt).unwrap(),
        "round-trip must be value-identical"
    );
}

#[test]
fn legacy_python_ledger_row_reads_via_aliases() {
    // A row shaped like scripts/spec-lab/cx_speculative_core.py's SpecReceipt.to_dict:
    // legacy cost keys (*_s), a bare speedup_x, and a `meta` bag — with NO
    // baseline_source/quality_tier/evidence labels (they didn't exist yet).
    let legacy = r#"{
        "branch_id": "legacy_py_row",
        "modality": "token",
        "units": 4,
        "accepted_fraction": 0.8125,
        "repaired_fraction": 0.5,
        "draft_s": 0.0000041,
        "verify_s": 0.0000067,
        "repair_s": 0.0000012,
        "speculative_s": 0.0000120,
        "baseline_s": 0.0123,
        "speedup_x": 1025.0,
        "exact": true,
        "quality_gate": true,
        "meta": { "prompt_class": "repeat" }
    }"#;

    let r: SpecReceipt =
        serde_json::from_str(legacy).expect("legacy row must deserialize via aliases");

    // Costs mapped from the legacy *_s names.
    assert_eq!(r.draft_cost_s, 0.0000041);
    assert_eq!(r.verify_cost_s, 0.0000067);
    assert_eq!(r.repair_cost_s, 0.0000012);
    assert_eq!(r.total_product_time_s, 0.0000120); // speculative_s
    assert_eq!(r.baseline_total_time_s, 0.0123); // baseline_s
    assert_eq!(r.speedup_vs_baseline, Some(1025.0)); // speedup_x

    // Free bag mapped from the legacy `meta` key.
    assert_eq!(
        r.details.get("prompt_class").unwrap(),
        &serde_json::json!("repeat")
    );

    // Honest defaults for the labels a legacy row never carried.
    assert_eq!(r.evidence, Evidence::Imported);
    assert_eq!(r.baseline_source, BaselineSource::Modeled);

    // The unknown legacy `quality_gate` bool is ignored (no deny_unknown_fields),
    // and quality_tier defaults to the neutral Preview.
    assert_eq!(r.quality_tier, cx_spec_engine::QualityTier::Preview);
}
