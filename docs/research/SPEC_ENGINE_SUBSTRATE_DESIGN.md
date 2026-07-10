# SpecEngine substrate — final API (Branch A, built 2026-07-09)

Status: **BUILT + TESTED.** This documents the API as it actually compiles and
passes tests in `spec-engine/` (crate `cx-spec-engine`), not a sketch. It is the
owned custom basis the consolidation plan (`docs/research/CONSOLIDATION_PLAN_2026-07-09.md`)
names as Branch A:

```
SpecUnit -> DraftProducer -> Verifier -> AcceptancePolicy -> RepairPolicy -> SpecReceipt
```

It is the compiling, monomorphized Rust re-expression of the Python spine at
`scripts/spec-lab/cx_speculative_core.py`, with a receipt schema that (a) uses the
plan-mandated field names, (b) reads the existing Python ledger rows via serde
aliases, and (c) serializes to exactly the JSON the Go control plane binds (same
snake_case / nullable conventions as `control/receipt.go`'s `ClearingReceipt` and
`control/quote.go`'s `QuoteRouting`).

## Gate result (Branch A "done")

Run from `spec-engine/`:

- `cargo build` — clean, **zero warnings** (`cargo build --all-targets` greps no
  `warning`/`error`).
- `cargo test` — **11 passed, 0 failed**:
  - lib unit tests: 5 (2 render, 3 token) — each adapter proves it implements all
    four traits and emits a sane `SpecReceipt`.
  - `tests/receipt_json.rs`: 4 — every Go-mirror key present; unearned speedup
    serializes to `null`; value-exact serde round-trip; legacy Python row reads
    via aliases.
  - `tests/render_adapter.rs`: 1, `tests/token_adapter.rs`: 1 — end-to-end via the
    public API.

Both a render adapter AND a token adapter implement the SAME four traits and
aggregate to the SAME receipt shape; the receipt round-trips losslessly to JSON
the control plane can ingest. Gate met.

## 0. Crate layout, deps, isolation

```
spec-engine/
  Cargo.toml            # standalone; empty [workspace] table => NOT a member of agent/
  src/
    lib.rs              # crate docs + re-exports + the honesty invariants
    receipt.rs          # SpecReceipt, Modality, QualityTier, Evidence, BaselineSource, Details
    unit.rs             # SpecUnit trait
    verify.rs           # Verification<U>, Acceptance
    traits.rs           # DraftProducer, Verifier, AcceptancePolicy, RepairPolicy
    engine.rs           # UnitTrace, SpecPipeline, run_unit, run_batch, aggregate
    adapters/
      mod.rs            # fnv_spin() shared deterministic mix
      synth_render.rs   # render-like adapter + 2 unit tests
      synth_token.rs    # token-like adapter + 3 unit tests
  examples/
    emit_receipt.rs     # end-to-end caller that prints the on-wire JSON
  tests/
    render_adapter.rs   token_adapter.rs   receipt_json.rs
```

Dependencies (the ONLY two the plan allows): `serde` (derive) and `serde_json`.
`serde_json` is built with the **`float_roundtrip`** feature — its default float
parser is faster but may drift the last ULP, and a receipt/ledger schema must
survive a to-JSON/from-JSON cycle bit-for-bit (the round-trip tests assert
value-exact equality). Both are NORMAL (not dev) deps so the integration tests in
`tests/` can name `serde_json` directly. The empty `[workspace]` table pins the
crate as its own workspace root so `cargo build` never walks up the repo tree.

## 1. The unified receipt schema (`receipt.rs`)

The plan spine (`draft_cost_s, verify_cost_s, accepted_fraction, repair_cost_s,
total_product_time_s, quality_tier, speedup_vs_baseline` + modality tag + details
map) plus a small, justified honesty/compat set. Every added field maps to a
Python ledger column (via `#[serde(alias)]`) or an explicit plan honesty rule.

```rust
pub type Details = BTreeMap<String, serde_json::Value>;   // deterministic key order

#[derive(..., Serialize, Deserialize)]
#[serde(transparent)]
pub struct Modality(pub String);   // render() / token() / combined(); serializes as a bare string

#[derive(..., Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum QualityTier { Fail, #[default] Preview, Delivery }   // gate_score() 0/0.5/1; min() = worst-wins

#[derive(..., Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum Evidence { Measured, Modeled, Synthetic, #[default] Imported }

#[derive(..., Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum BaselineSource { Measured, #[default] Modeled, Absent }

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SpecReceipt {
    pub branch_id: String,
    pub modality: Modality,

    #[serde(alias = "draft_s")]        pub draft_cost_s: f64,
    #[serde(alias = "verify_s")]       pub verify_cost_s: f64,
    #[serde(alias = "repair_s")]       pub repair_cost_s: f64,
    #[serde(alias = "speculative_s")]  pub total_product_time_s: f64,

    #[serde(alias = "baseline_s")]     pub baseline_total_time_s: f64,
    #[serde(default)]                  pub baseline_source: BaselineSource,

    pub units: u32,
    pub accepted_fraction: f64,        // Σaccepted / Σdrafted (true ratio, in [0,1])
    pub repaired_fraction: f64,
    pub exact: bool,
    #[serde(default)]                  pub quality_tier: QualityTier,

    #[serde(alias = "speedup_x")]      pub speedup_vs_baseline: Option<f64>,  // null == unearned

    #[serde(default)]                  pub evidence: Evidence,
    #[serde(default, alias = "meta")]  pub details: Details,
}
```

**Reconciliation with the Python ledgers.** The Rust crate WRITES the canonical
`*_cost_s` names and READS legacy rows via the aliases above, so one ledger schema
converges without orphaning history. A genuinely legacy row (no
`baseline_source`/`quality_tier`/`evidence` labels, a `meta` bag instead of
`details`, a bare `speedup_x`) deserializes: the labels take honest defaults —
`evidence: Imported` (semantically exact — a legacy row read into the new schema
IS an imported prior row), `baseline_source: Modeled` (conservative — the row had
a real `baseline_s` but we do not upgrade it to `Measured`), `quality_tier:
Preview` (the neutral middle, flagged for re-grading; the legacy boolean
`quality_gate` is ignored). This is proven by
`tests/receipt_json.rs::legacy_python_ledger_row_reads_via_aliases`.

**`quality_tier` vs `exact` — two orthogonal axes (important).** `quality_tier`
is the per-modality DELIVERED/COVERAGE tier; `exact` is the SEPARATE
lossless-vs-baseline flag:
- render: `exact=false` always (never bit-exact vs a full reference); `quality_tier`
  = worst delivered tile's visual grade.
- token: `exact=true` always (repair splices the target's true tokens, so the
  delivered run is lossless); `quality_tier` = speculation COVERAGE (whole window
  accepted -> Delivery, partial -> Preview, poor -> Fail).

Carrying both means a partial-coverage token receipt (`quality_tier: fail`) is
still honestly marked `exact: true`, and a cheap render win (`quality_tier:
delivery`) is honestly marked `exact: false`.

## 2. `SpecUnit` (`unit.rs`)

```rust
pub trait SpecUnit {
    type Draft;    // low-spp tile; k proposed tokens
    type Output;   // final tile pixels; accepted-prefix-plus-correction run
    type Score;    // SSIM triple; matched-prefix length
    fn unit_id(&self) -> String;
    fn modality(&self) -> Modality;
}
```

The unit is deliberately the NATURAL verify-batch: one tile (render), one k-token
window (token). This preserves token spec-decode's "one target forward pass
verifies k tokens at once" (the engine never splits a verify call) and render's
per-tile gate. Everything downstream is generic over `U` — no `dyn`, no boxing.

## 3. The four role traits (`traits.rs`) + verify carriers (`verify.rs`)

```rust
pub struct Verification<U: SpecUnit> { pub score: U::Score, pub truth: Option<U::Output> }
pub struct Acceptance { pub drafted: f64, pub accepted: f64, pub tier: QualityTier, pub exact: bool }
impl Acceptance { pub fn is_full(&self) -> bool { self.accepted >= self.drafted } pub fn fraction(&self) -> f64 { ... } }

pub trait DraftProducer<U: SpecUnit>   { fn draft(&self, unit: &U) -> U::Draft; }
pub trait Verifier<U: SpecUnit>        { fn verify(&self, unit: &U, draft: &U::Draft) -> Verification<U>; }
pub trait AcceptancePolicy<U: SpecUnit>{ fn decide(&self, unit: &U, v: &Verification<U>) -> Acceptance; }
pub trait RepairPolicy<U: SpecUnit> {
    fn accept(&self, unit: &U, draft: U::Draft) -> U::Output;                                   // NOT timed as repair
    fn repair(&self, unit: &U, draft: U::Draft, v: Verification<U>, acc: &Acceptance) -> U::Output; // timed as repair
}
```

Four SEPARATE traits (not one bundled adapter) because the plan's experiment loop
swaps them independently (cheaper `DraftProducer`, same `Verifier`).
`Verification.truth` is an `Option` so the render verifier returns `None` and
allocates no reference it does not have, while the token verifier returns `Some`
because its target pass produces the truth anyway. `RepairPolicy::{accept,repair}`
take `draft` by value, so the engine MOVES and never clones. `Acceptance` carries
the accepted *quantity* (not just a fraction) so the fold computes an honest
`Σaccepted/Σdrafted`, not a mis-weighted mean-of-ratios.

## 4. The engine (`engine.rs`)

`SpecPipeline<U,D,V,A,R>` holds the four roles + identity (`branch_id`,
`modality`) and a `PhantomData<U>`. Constructors: `new(branch_id, modality, ...)`,
plus `render(...)` / `token(...)` convenience wrappers.

- **`run_unit(&U) -> (U::Output, UnitTrace)`** — THE accept/repair loop: draft ->
  verify -> `decide` -> `if acc.is_full() { accept } else { repair }`. Draft moved
  into exactly one branch; acceptance is the pure, untimed branch condition; only
  draft/verify/repair phases are `Instant`-timed.
- **`run_batch(&[U], baseline_total_time_s, baseline_source, evidence, details)
  -> (Vec<U::Output>, SpecReceipt)`** — the modality-general DRIVER the gate names.
  `baseline_total_time_s` is a MEASURED (or explicitly MODELED) INPUT; the engine
  never fabricates it.
- **`aggregate(...) -> SpecReceipt`** — the honest fold: `accepted_fraction =
  Σaccepted/Σdrafted`; `quality_tier` = worst-wins; `exact` = non-empty AND all
  units exact; `speedup_vs_baseline = Some(baseline/spec)` ONLY when
  `baseline_source != Absent` and spec time > 0, else `None`.

## 5. The two demo adapters (`adapters/`)

Both deterministic, dependency-free, hard-wired `Evidence::Synthetic` +
`BaselineSource::Modeled`. A shared `fnv_spin(seed, iters)` derives stable hashes
AND does bounded real work so the phase timers register a positive, ordered cost
(`draft < verify < repair`) without nondeterminism.

**`synth_render` — whole-unit accept, `truth=None`, `exact=false`.** A tile's
`residual ∈ [0,1]` (draft-vs-converged distance) drives an `Ssim { global, worst,
p5 }`. A genuinely three-way per-tile decision that yields a real tier mix:
| draft judged | action | delivered tier | draft reused? |
|---|---|---|---|
| clears delivery gate (`global≥0.98 && worst≥0.95`) | ship draft as-is | Delivery | yes (accepted=1) |
| clears only preview gate (`global≥0.90 && worst≥0.85`) | ship draft as a preview | Preview | yes (accepted=1) |
| fails preview | re-render at reference spp | Delivery | no (accepted=0, repaired) |

So `quality_tier` (worst-wins over DELIVERED tiers) drops to Preview exactly when a
tile was preview-shipped; a repaired tile is reference quality and honestly records
Delivery. `accepted_fraction` (draft reuse) and `repaired_fraction` are separate,
orthogonal signals.

**`synth_token` — PARTIAL accept, `truth=Some`, `exact=true`.** An n-gram
proposer emits `k` tokens; the target-pass verifier returns the matched-prefix
length `m` and carries the true continuation as `truth`. Acceptance is `m` of `k`
(so `accepted_fraction` lands strictly between 0 and 1 — the corner render never
reaches). Coverage tier: `m==k` -> Delivery, `m≥k/2` -> Preview, else Fail. Repair
splices `draft[..m] ++ truth[m..]`, so the delivered run always equals the target
continuation -> lossless (`exact=true`). `window_with_match(id, base, k, m)` is a
public helper that builds a window matching on exactly its first `m` of `k`
tokens.

The two adapters stress genuinely different corners of the same traits (whole-unit
vs partial acceptance; `truth: None` vs `Some`; `exact: false` vs `true`), which
is the real test that the core hosts both without special-casing.

## 6. End-to-end caller + REAL emitted JSON

`examples/emit_receipt.rs` (run: `cargo run --example emit_receipt`) prints the
on-wire shape. Actual output (2026-07-09; timings are machine-dependent, the
`3401x`/`95x` speedups are SYNTHETIC demo numbers correctly labeled
`evidence: "synthetic"` so they can never be read as measured wins):

```json
// render lane receipt
{
  "branch_id": "cx_synth_render_demo",
  "modality": "render",
  "draft_cost_s": 0.000042126,
  "verify_cost_s": 0.000084084,
  "repair_cost_s": 0.000167791,
  "total_product_time_s": 0.000294001,
  "baseline_total_time_s": 1.0,
  "baseline_source": "modeled",
  "units": 3,
  "accepted_fraction": 0.6666666666666666,
  "repaired_fraction": 0.3333333333333333,
  "exact": false,
  "quality_tier": "preview",
  "speedup_vs_baseline": 3401.3489750034864,
  "evidence": "synthetic",
  "details": { "draft_spp": 4, "scene": "cube_volume" }
}

// token lane receipt
{
  "branch_id": "cx_synth_token_demo",
  "modality": "token",
  "draft_cost_s": 0.000034542,
  "verify_cost_s": 0.000131791,
  "repair_cost_s": 0.000042833,
  "total_product_time_s": 0.000209166,
  "baseline_total_time_s": 0.02,
  "baseline_source": "modeled",
  "units": 3,
  "accepted_fraction": 0.6666666666666666,
  "repaired_fraction": 0.6666666666666666,
  "exact": true,
  "quality_tier": "fail",
  "speedup_vs_baseline": 95.61783463851678,
  "evidence": "synthetic",
  "details": { "prompt_class": "repeat" }
}

// token lane, no baseline supplied
speedup_vs_baseline = null
```

## 7. Why the core does not tax either lane (the plan's Kill-rule defense)

- **Monomorphized generics, no `dyn`, no boxing** — each
  `SpecPipeline<U,D,V,A,R>` compiles to straight-line calls; zero-cost vs a
  hand-written per-lane loop.
- **Draft moved, never cloned** — `RepairPolicy::{accept,repair}` take `U::Draft`
  by value; the accept path is a move.
- **`Verification.truth: Option<U::Output>`** — the render verifier returns `None`
  and allocates no reference; the token verifier returns `Some` because its target
  pass produces truth anyway. Neither lane pays for the other's shape.
- **Unit = the natural verify-batch** — token's "one pass verifies k tokens" and
  render's "one gate per tile" both survive; the engine never splits a verify call.
- **Partial acceptance is first-class** (`Acceptance.accepted/drafted`) so token
  loses nothing to render's whole-unit model, and render pays nothing for token's
  partiality (it just sets `accepted ∈ {0,1}`).
- **Acceptance is not separately timed** — it is the pure branch condition, so no
  phantom cost appears between verify and repair.

If a future real adapter still shows the core costing more than its standalone
code, the narrowing knob is the trait set, not the receipt: e.g. add a
`verify_batch(&[U])` default-provided method so a lane that verifies many units in
one call (batched vLLM) can override it while the per-unit path stays the default.

## 8. Go ingestion mirror (the thin schema the control plane reads)

Matches `control/receipt.go` / `control/quote.go` conventions exactly — snake_case
tags, a POINTER for the nullable speedup, `map[string]any` for the free bag. This
is the compat target the crate's `tests/receipt_json.rs` golden test pins (it
asserts every key below is present). NOT wired into the control plane this wave —
advisory/ingest-only until a real MEASURED receipt exists (sequenced follow-up for
Branches B/C).

```go
// control/spec_receipt.go (sketch) — wire mirror of cx-spec-engine::SpecReceipt.
type SpecReceipt struct {
    BranchID           string         `json:"branch_id"`
    Modality           string         `json:"modality"`
    DraftCostS         float64        `json:"draft_cost_s"`
    VerifyCostS        float64        `json:"verify_cost_s"`
    RepairCostS        float64        `json:"repair_cost_s"`
    TotalProductTimeS  float64        `json:"total_product_time_s"`
    BaselineTotalTimeS float64        `json:"baseline_total_time_s"`
    BaselineSource     string         `json:"baseline_source"`
    Units              int            `json:"units"`
    AcceptedFraction   float64        `json:"accepted_fraction"`
    RepairedFraction   float64        `json:"repaired_fraction"`
    Exact              bool           `json:"exact"`
    QualityTier        string         `json:"quality_tier"`
    SpeedupVsBaseline  *float64       `json:"speedup_vs_baseline"` // null == unearned, never fabricated
    Evidence           string         `json:"evidence"`
    Details            map[string]any `json:"details"`
}
```

## 9. Honesty invariants the design ENFORCES (not just documents)

1. **No invented baselines.** `speedup_vs_baseline` is `None` (JSON `null`) unless
   the caller passes a baseline and labels it `Measured`/`Modeled`. Proven by
   `absent_baseline_yields_null_speedup` + `unearned_speedup_serializes_to_json_null`.
2. **No multiplied multipliers.** The receipt only ever holds one workload's
   `baseline/spec` ratio; there is NO field or method that composes two lanes'
   speedups. A combined number can exist only by running a genuinely nested
   unit-set through one pipeline (a real end-to-end measurement), which produces
   one ordinary `SpecReceipt` (`Modality::combined()`).
3. **Every number carries a label.** `evidence ∈ {measured, modeled, synthetic,
   imported}` and `baseline_source` travel on the receipt; the two demo adapters
   are hard-wired `synthetic`, so a stub can never masquerade as a measured win.

## 10. What this unlocks (sequenced, out of scope this wave)

- **Branch B** expresses the Cycles low-spp/tile/SSIM lane as a real `SpecUnit`
  render adapter against this exact trait set; its first cloud run yields a real
  `baseline/spec` render receipt (`evidence: measured`).
- **Branch C** forks spec-decode into a CX-owned token adapter matching this
  receipt shape by contract; its first fleet run yields a real >1x lossless token
  receipt.
- Only after both land does a genuinely nested combined workload (a render job
  whose control/LLM steps ALSO run through the token lane) produce the ONE
  legitimate end-to-end combined number — one `SpecReceipt`, not a product of two
  ledgers.

**Files (all under the assigned write paths):** the crate `spec-engine/` and this
doc `docs/research/SPEC_ENGINE_SUBSTRATE_DESIGN.md`. No cloud, no spend, no writes
elsewhere.
