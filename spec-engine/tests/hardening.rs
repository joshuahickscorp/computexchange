//! Adversarial contract tests for the production-facing checked APIs.

use cx_spec_engine::{
    try_aggregate, Acceptance, AcceptancePolicy, BaselineSource, Details, DraftProducer,
    EngineError, Evidence, Modality, QualityTier, ReceiptParseError, RepairPolicy, SpecPipeline,
    SpecReceipt, SpecUnit, UnitTrace, Verification, Verifier, MAX_SPEC_BATCH_UNITS,
    MAX_SPEC_RECEIPT_DETAILS_JSON_BYTES, MAX_SPEC_RECEIPT_JSON_BYTES, MAX_SPEC_RECEIPT_JSON_DEPTH,
};
use std::sync::{
    atomic::{AtomicUsize, Ordering},
    Arc,
};
use std::time::Duration;

#[derive(Clone)]
struct Unit {
    id: &'static str,
    modality: &'static str,
}

impl SpecUnit for Unit {
    type Draft = u32;
    type Output = u32;
    type Score = ();

    fn unit_id(&self) -> String {
        if self.id == "delayed-preflight" {
            std::thread::sleep(Duration::from_millis(10));
        }
        self.id.into()
    }

    fn modality(&self) -> Modality {
        Modality(self.modality.into())
    }
}

#[test]
fn batch_receipt_charges_identity_preflight_and_driver_bookkeeping() {
    let unit = Unit {
        id: "delayed-preflight",
        modality: "token",
    };
    let (_, receipt) = pipeline(1.0, 1.0)
        .try_run_batch(
            &[unit],
            1.0,
            BaselineSource::Measured,
            Evidence::Measured,
            Details::new(),
        )
        .unwrap();
    assert!(receipt.overhead_cost_s >= 0.005, "{receipt:?}");
    assert!(receipt.total_product_time_s >= 0.005, "{receipt:?}");
    assert_eq!(
        receipt.total_product_time_s,
        receipt.draft_cost_s
            + receipt.verify_cost_s
            + receipt.repair_cost_s
            + receipt.overhead_cost_s
    );
}

struct Draft;
impl DraftProducer<Unit> for Draft {
    fn draft(&self, _unit: &Unit) -> u32 {
        7
    }
}

struct Verify;
impl Verifier<Unit> for Verify {
    fn verify(&self, _unit: &Unit, draft: &u32) -> Verification<Unit> {
        Verification {
            score: (),
            truth: Some(*draft),
        }
    }
}

struct Policy {
    drafted: f64,
    accepted: f64,
}
impl AcceptancePolicy<Unit> for Policy {
    fn decide(&self, _unit: &Unit, _verification: &Verification<Unit>) -> Acceptance {
        Acceptance {
            drafted: self.drafted,
            accepted: self.accepted,
            tier: QualityTier::Delivery,
            exact: true,
        }
    }
}

struct Repair;
impl RepairPolicy<Unit> for Repair {
    fn accept(&self, _unit: &Unit, draft: u32) -> u32 {
        draft
    }

    fn repair(
        &self,
        _unit: &Unit,
        _draft: u32,
        verification: Verification<Unit>,
        _acceptance: &Acceptance,
    ) -> u32 {
        verification.truth.unwrap_or_default()
    }
}

fn pipeline(drafted: f64, accepted: f64) -> SpecPipeline<Unit, Draft, Verify, Policy, Repair> {
    SpecPipeline::token(
        "hardening",
        Draft,
        Verify,
        Policy { drafted, accepted },
        Repair,
    )
}

#[test]
fn malformed_acceptance_never_steers_the_branch() {
    let unit = Unit {
        id: "u",
        modality: "token",
    };
    for (drafted, accepted) in [
        (f64::NAN, 0.0),
        (0.0, 0.0),
        (-1.0, 0.0),
        (1.0, f64::INFINITY),
        (1.0, -0.1),
        (1.0, 1.1),
    ] {
        let err = pipeline(drafted, accepted).try_run_unit(&unit).unwrap_err();
        assert!(matches!(err, EngineError::InvalidAcceptance { .. }));
    }
}

#[test]
fn modality_and_baseline_contracts_fail_before_batch_delivery() {
    let unnamed = Unit {
        id: "",
        modality: "token",
    };
    assert!(matches!(
        pipeline(1.0, 1.0).try_run_unit(&unnamed),
        Err(EngineError::InvalidUnitIdentity(_))
    ));

    let wrong = Unit {
        id: "u",
        modality: "render",
    };
    assert!(matches!(
        pipeline(1.0, 1.0).try_run_unit(&wrong),
        Err(EngineError::ModalityMismatch { .. })
    ));

    let good = Unit {
        id: "u",
        modality: "token",
    };
    assert!(matches!(
        pipeline(1.0, 1.0).try_run_batch(
            &[],
            1.0,
            BaselineSource::Measured,
            Evidence::Measured,
            Details::new(),
        ),
        Err(EngineError::EmptyBatch)
    ));
    assert!(matches!(
        pipeline(1.0, 1.0).try_run_batch(
            &[good],
            f64::NAN,
            BaselineSource::Measured,
            Evidence::Measured,
            Details::new(),
        ),
        Err(EngineError::InvalidBaseline(_))
    ));
}

#[test]
fn batch_work_is_bounded_before_output_and_trace_allocation() {
    let units = vec![
        Unit {
            id: "u",
            modality: "token",
        };
        MAX_SPEC_BATCH_UNITS + 1
    ];
    assert!(matches!(
        pipeline(1.0, 1.0).try_run_batch(
            &units,
            1.0,
            BaselineSource::Measured,
            Evidence::Measured,
            Details::new(),
        ),
        Err(EngineError::TooManyUnits { .. })
    ));
}

struct CountingDraft(Arc<AtomicUsize>);
impl DraftProducer<Unit> for CountingDraft {
    fn draft(&self, _unit: &Unit) -> u32 {
        self.0.fetch_add(1, Ordering::SeqCst);
        7
    }
}

#[test]
fn duplicate_batch_ids_fail_before_any_product_callback() {
    let calls = Arc::new(AtomicUsize::new(0));
    let pipe = SpecPipeline::token(
        "unique-ledger",
        CountingDraft(calls.clone()),
        Verify,
        Policy {
            drafted: 1.0,
            accepted: 1.0,
        },
        Repair,
    );
    let units = [
        Unit {
            id: "same",
            modality: "token",
        },
        Unit {
            id: "same",
            modality: "token",
        },
    ];
    assert!(matches!(
        pipe.try_run_batch(
            &units,
            1.0,
            BaselineSource::Measured,
            Evidence::Measured,
            Details::new(),
        ),
        Err(EngineError::DuplicateUnitIdentity(_))
    ));
    assert_eq!(calls.load(Ordering::SeqCst), 0);
}

#[test]
fn oversized_details_fail_before_any_product_callback() {
    let calls = Arc::new(AtomicUsize::new(0));
    let pipe = SpecPipeline::token(
        "bounded-details",
        CountingDraft(calls.clone()),
        Verify,
        Policy {
            drafted: 1.0,
            accepted: 1.0,
        },
        Repair,
    );
    let mut details = Details::new();
    details.insert(
        "blob".into(),
        serde_json::json!("x".repeat(MAX_SPEC_RECEIPT_DETAILS_JSON_BYTES)),
    );
    let unit = Unit {
        id: "u",
        modality: "token",
    };
    assert!(matches!(
        pipe.try_run_batch(
            &[unit],
            1.0,
            BaselineSource::Measured,
            Evidence::Measured,
            details,
        ),
        Err(EngineError::InvalidReceipt(_))
    ));
    assert_eq!(calls.load(Ordering::SeqCst), 0);
}

struct PanicDraft;
impl DraftProducer<Unit> for PanicDraft {
    fn draft(&self, _unit: &Unit) -> u32 {
        panic!("hostile adapter panic payload")
    }
}

#[test]
fn checked_api_contains_adapter_panics_without_output() {
    let pipe = SpecPipeline::token(
        "panic-boundary",
        PanicDraft,
        Verify,
        Policy {
            drafted: 1.0,
            accepted: 1.0,
        },
        Repair,
    );
    let unit = Unit {
        id: "u",
        modality: "token",
    };
    assert!(matches!(
        pipe.try_run_unit(&unit),
        Err(EngineError::CallbackPanic { phase: "draft", .. })
    ));
}

#[test]
fn aggregate_rejects_incoherent_repair_outcomes() {
    let trace = UnitTrace {
        unit_id: "u".into(),
        draft_s: 0.1,
        verify_s: 0.1,
        repair_s: 0.0,
        overhead_s: 0.0,
        drafted: 1.0,
        accepted: 1.0,
        repaired: false,
        tier: QualityTier::Delivery,
        exact: true,
    };
    let aggregate = |trace: UnitTrace| {
        try_aggregate(
            "hardening",
            Modality("token".into()),
            &[trace],
            1.0,
            BaselineSource::Measured,
            Evidence::Measured,
            Details::new(),
        )
    };

    let mut partial_without_repair = trace.clone();
    partial_without_repair.accepted = 0.5;
    assert!(matches!(
        aggregate(partial_without_repair),
        Err(EngineError::InvalidTrace { .. })
    ));

    let mut full_with_repair = trace.clone();
    full_with_repair.repaired = true;
    assert!(matches!(
        aggregate(full_with_repair),
        Err(EngineError::InvalidTrace { .. })
    ));

    let mut accepted_with_repair_time = trace;
    accepted_with_repair_time.repair_s = 0.01;
    assert!(matches!(
        aggregate(accepted_with_repair_time),
        Err(EngineError::InvalidTrace { .. })
    ));
}

#[test]
fn aggregate_rejects_duplicate_trace_identities() {
    let trace = UnitTrace {
        unit_id: "same".into(),
        draft_s: 0.1,
        verify_s: 0.1,
        repair_s: 0.0,
        overhead_s: 0.0,
        drafted: 1.0,
        accepted: 1.0,
        repaired: false,
        tier: QualityTier::Delivery,
        exact: true,
    };
    assert!(matches!(
        try_aggregate(
            "hardening",
            Modality::token(),
            &[trace.clone(), trace],
            1.0,
            BaselineSource::Measured,
            Evidence::Measured,
            Details::new(),
        ),
        Err(EngineError::DuplicateUnitIdentity(_))
    ));
}

fn canonical_json(evidence: &str) -> String {
    serde_json::json!({
        "schema_version": 1,
        "branch_id": "receipt-test",
        "modality": "render",
        "draft_cost_s": 1.0,
        "verify_cost_s": 0.2,
        "repair_cost_s": 0.3,
        "overhead_cost_s": 0.1,
        "total_product_time_s": 1.6,
        "baseline_total_time_s": 3.2,
        "baseline_source": "measured",
        "units": 1,
        "accepted_fraction": 0.0,
        "repaired_fraction": 1.0,
        "exact": false,
        "artifact_verified": evidence == "measured",
        "quality_tier": "delivery",
        "speedup_vs_baseline": 2.0,
        "evidence": evidence,
        "details": {}
    })
    .to_string()
}

#[test]
fn strict_parser_rejects_size_conflicts_and_arithmetic_lies() {
    let huge = " ".repeat(MAX_SPEC_RECEIPT_JSON_BYTES + 1);
    assert!(matches!(
        SpecReceipt::from_json(&huge),
        Err(ReceiptParseError::TooLarge { .. })
    ));

    let conflict = canonical_json("measured").replace(
        "\"draft_cost_s\":1.0",
        "\"draft_cost_s\":1.0,\"draft_s\":9.0",
    );
    assert!(matches!(
        SpecReceipt::from_json(&conflict),
        Err(ReceiptParseError::Json(_))
    ));

    let lie = canonical_json("measured").replace(
        "\"total_product_time_s\":1.6",
        "\"total_product_time_s\":1.0",
    );
    assert!(matches!(
        SpecReceipt::from_json(&lie),
        Err(ReceiptParseError::Invalid(_))
    ));

    let long_hidden_cost = canonical_json("measured")
        .replace("\"draft_cost_s\":1.0", "\"draft_cost_s\":3593.0")
        .replace(
            "\"total_product_time_s\":1.6",
            "\"total_product_time_s\":3600.0",
        )
        .replace(
            "\"baseline_total_time_s\":3.2",
            "\"baseline_total_time_s\":7200.0",
        );
    assert!(matches!(
        SpecReceipt::from_json(&long_hidden_cost),
        Err(ReceiptParseError::Invalid(_))
    ));

    let zero_baseline = canonical_json("measured")
        .replace(
            "\"baseline_total_time_s\":3.2",
            "\"baseline_total_time_s\":0.0",
        )
        .replace(
            "\"speedup_vs_baseline\":2.0",
            "\"speedup_vs_baseline\":null",
        );
    assert!(matches!(
        SpecReceipt::from_json(&zero_baseline),
        Err(ReceiptParseError::Invalid(_))
    ));

    let too_many_units = canonical_json("measured").replace(
        "\"units\":1",
        &format!("\"units\":{}", cx_spec_engine::MAX_SPEC_RECEIPT_UNITS + 1),
    );
    assert!(matches!(
        SpecReceipt::from_json(&too_many_units),
        Err(ReceiptParseError::Invalid(_))
    ));

    let duplicate_detail =
        canonical_json("measured").replace("\"details\":{}", "\"details\":{\"x\":1,\"x\":2}");
    assert!(matches!(
        SpecReceipt::from_json(&duplicate_detail),
        Err(ReceiptParseError::Json(_))
    ));

    let mut nested = "null".to_string();
    for _ in 0..=MAX_SPEC_RECEIPT_JSON_DEPTH {
        nested = format!("[{nested}]");
    }
    let too_deep = canonical_json("measured").replace(
        "\"details\":{}",
        &format!("\"details\":{{\"nested\":{nested}}}"),
    );
    assert!(matches!(
        SpecReceipt::from_json(&too_deep),
        Err(ReceiptParseError::Json(_))
    ));
}

#[test]
fn legacy_failed_gate_migrates_to_fail_and_eligibility_is_derived() {
    let legacy = r#"{
        "branch_id":"old", "modality":"render", "units":1,
        "draft_s":1.0, "verify_s":0.0, "repair_s":0.0,
        "speculative_s":1.0, "baseline_s":2.0, "speedup_x":2.0,
        "accepted_fraction":1.0, "repaired_fraction":0.0,
        "exact":false, "quality_gate":false, "meta":{}
    }"#;
    let migrated = SpecReceipt::from_json(legacy).unwrap();
    assert_eq!(migrated.quality_tier, QualityTier::Fail);
    assert!(!migrated.delivery_eligible());

    let synthetic = SpecReceipt::from_json(&canonical_json("synthetic")).unwrap();
    assert!(!synthetic.delivery_eligible());
    let measured = SpecReceipt::from_json(&canonical_json("measured")).unwrap();
    assert!(measured.delivery_eligible());

    let forged = canonical_json("synthetic")
        .replace("\"artifact_verified\":false", "\"artifact_verified\":true");
    assert!(matches!(
        SpecReceipt::from_json(&forged),
        Err(ReceiptParseError::Invalid(_))
    ));

    let legacy_measured_delivery = canonical_json("measured")
        .replace("\"schema_version\":1,", "")
        .replace("\"artifact_verified\":true,", "\"delivery_eligible\":true,");
    let parked = SpecReceipt::from_json(&legacy_measured_delivery).unwrap();
    assert!(!parked.artifact_verified);
    assert!(!parked.delivery_eligible());

    let raw: SpecReceipt = serde_json::from_str(&legacy_measured_delivery).unwrap();
    assert_eq!(raw.schema_version, 0);
    assert!(
        !raw.delivery_eligible(),
        "direct serde must not bypass strict migration"
    );
}

#[test]
fn strict_legacy_quality_gate_is_boolean_and_agrees_with_explicit_tier() {
    fn with_gate(mut value: serde_json::Value, gate: serde_json::Value) -> String {
        value
            .as_object_mut()
            .unwrap()
            .insert("quality_gate".into(), gate);
        value.to_string()
    }

    let delivery: serde_json::Value = serde_json::from_str(&canonical_json("measured")).unwrap();
    let agreed = with_gate(delivery.clone(), serde_json::json!(true));
    assert!(SpecReceipt::from_json(&agreed).is_ok());

    for (name, input) in [
        (
            "string gate",
            with_gate(delivery.clone(), serde_json::json!("true")),
        ),
        (
            "null gate",
            with_gate(delivery.clone(), serde_json::Value::Null),
        ),
        (
            "false contradicts delivery",
            with_gate(delivery.clone(), serde_json::json!(false)),
        ),
        (
            "true contradicts fail",
            with_gate(
                {
                    let mut fail: serde_json::Value =
                        serde_json::from_str(&canonical_json("synthetic")).unwrap();
                    fail["quality_tier"] = serde_json::json!("fail");
                    fail
                },
                serde_json::json!(true),
            ),
        ),
    ] {
        assert!(
            matches!(
                SpecReceipt::from_json(&input),
                Err(ReceiptParseError::Invalid(_))
            ),
            "{name} must fail strict ingress"
        );
    }

    let mut fail: serde_json::Value = serde_json::from_str(&canonical_json("synthetic")).unwrap();
    fail["quality_tier"] = serde_json::json!("fail");
    let agreed_fail = with_gate(fail, serde_json::json!(false));
    assert!(SpecReceipt::from_json(&agreed_fail).is_ok());
}

#[test]
fn empty_receipt_cannot_carry_work_or_a_counterfactual_baseline() {
    let mut empty: serde_json::Value = serde_json::from_str(&canonical_json("measured")).unwrap();
    empty["units"] = serde_json::json!(0);
    empty["accepted_fraction"] = serde_json::json!(0.0);
    empty["repaired_fraction"] = serde_json::json!(0.0);
    empty["exact"] = serde_json::json!(false);
    empty["artifact_verified"] = serde_json::json!(false);
    empty["quality_tier"] = serde_json::json!("fail");
    assert!(matches!(
        SpecReceipt::from_json(&empty.to_string()),
        Err(ReceiptParseError::Invalid(_))
    ));
}
