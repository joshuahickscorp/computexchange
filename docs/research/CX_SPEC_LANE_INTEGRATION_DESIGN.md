# CX Spec Lane Integration — the SpecReceipt as a product surface (2026-07-10)

Status: DESIGN + one additive type landed. This wave ships `control/spec_receipt.go`
(the Go mirror of `spec-engine/src/receipt.rs`) + `control/spec_receipt_test.go`
(real-emitter ingestion proof) and THIS document. **No hot file is touched** — no
change to `quote.go`, `api.go`, `store.go`, `receipt.go`, `types.go`, or
`db/schema.sql`. Every wiring step below is a LATER, owner-approved climb, sequenced
at the end. Companion docs: `docs/research/CONSOLIDATION_PLAN_2026-07-09.md` (the
staged-multiplier table this design must never violate),
`docs/research/RENDER_REPAIR_LOOP_DESIGN.md` (the repair lever and its measured
RUN 4 negative), `docs/research/SPEC_ENGINE_SUBSTRATE_DESIGN.md` (the engine).

## 1. What now exists (MEASURED where stated)

- **The canonical wire schema** is `spec-engine/src/receipt.rs`: the plan spine
  (`draft_cost_s / verify_cost_s / repair_cost_s / total_product_time_s /
  baseline_total_time_s`), `units`, `accepted_fraction`, `repaired_fraction`, a
  required `exact` bool, closed enums `quality_tier` (fail/preview/delivery),
  `evidence` (measured/modeled/synthetic/imported), `baseline_source`
  (measured/modeled/absent), a NULLABLE `speedup_vs_baseline`, and a free-form
  `details` map. Serde aliases ingest the two legacy families (`*_s`, `*_cost`).
- **The Go mirror now exists**: `control/spec_receipt.go` — pure additive type +
  `UnmarshalJSON` (canonical keys + the same aliases) + `Validate()` (fractions in
  [0,1], times >= 0, speedup null-or-positive, the three closed vocabularies, and
  the `baseline_source=absent => null speedup` honesty rule). This closes the
  consolidation plan's named Branch-A gap ("Go ingest is proven structurally, not by
  a real unmarshal"). Proof is real: `spec_receipt_test.go` ingests the three
  VERBATIM emitter blobs from `spec-engine/tests/ingest_lanes.rs` plus the REAL
  RUN 3 and RUN 4 integrated ledger receipts mapped through the REAL
  `scripts/spec-lab/cx_render_spec_adapter.py`.
- **The measured record the product can stand on** (the staged table, no products
  taken): render integrated 4K kf=1 = **5.561x @ worst-tile 0.9095, MEASURED,
  self-pruned at the strict delivery gate** (RUN 3); the repair pass = **a decisive
  MEASURED negative** (RUN 4: worst-tile unchanged to 4 decimals, `selector_recall
  = 0.0` — the two-draft selector finds variance, not OIDN bias); the 0.95-tier
  (g>=0.95, wt>=0.90) IS cleared by RUN 3 and independently reproduces the banked
  5.84x within 5%. Token lane: lossless ~1x floor on real prompts. Combined:
  NONE-YET.

The product consequence of that record is load-bearing and appears throughout:
**the only render tier the ledger has actually delivered at >1x is the 0.95/preview
band — so that is the only tier the product may sell today.** Strict delivery
(wt>=0.95) stays quotable as a target, never as a promise, until a receipt clears it.

## 2. The precedent this design copies: the routing block (entries 93–94)

The substrate-routing block is the proven pattern for "attach an honest,
measured-basis decision to quote → submit → receipt without touching dispatch"
(`docs/internal/CREED_AND_PATH_TO_TEN.md` entries 93–94):

1. **Pure decision file** (`control/routing.go`): deterministic, no DB/clock/rand,
   unit-tested in isolation; the measured curve transcribed into code with every
   modeled figure labeled `[MODELED]` and biased AGAINST our own case.
2. **Honesty boundary at the call site**: the block attaches ONLY where the
   measurement applies (`generativeJobType && records > 0`); every other shape gets
   NO block rather than an unmeasured guess.
3. **One wire struct** (`QuoteRouting`) reused verbatim by quote response, submit
   response (`JobSubmitResponse.Routing`), persisted `jobs.routing_*` columns, a
   best-effort `routed` timeline event, and the receipt projection.
4. **Pure receipt projection** (`control/receipt.go` `receiptRouting(inv)`): rebuilds
   the block from persisted columns, returns nil when the job carried none, NEVER
   re-decides.
5. **Advisory first**: nothing in dispatch acts on the decision until the lane is
   real (`litGPUWorkers` was a live-wired parameter from day one, 0 until verified
   vLLM supply registers).

The spec lane maps onto this pattern one-for-one. Where it differs (a receipt is an
OUTCOME, not a decision), the difference is stated.

## 3. The product surface: a `render_speculative` job type

### 3.1 Job type (later climb — touches `types.go` validJobTypes + the agent)

A new closed-set tag `render_speculative`: "render this shot through the
accept/verify/repair lane; deliver at the quality tier you clear, priced by what
you cleared." Proposed `JobType` fields (wire-compatible with the existing tagged
enum; all `omitempty`): `scene_ref` (asset object key), `frames`, `resolution`,
`ref_spp`, `draft_spp`, `requested_tier` ("preview" | "delivery"),
`keyframe_every`, `repair_top_k`. The runner contract is exactly
`scripts/spec-lab/pod/exp_render_stack.py`'s config JSON, which already exists and
is measured — the job type is a thin, validated projection of it.

Deliberately NOT in v1: `combined` jobs. The plan's invariant holds: a combined
number exists only as one delivered job measured end-to-end (sequenced step 3);
until such a receipt exists there is nothing honest to sell under that name.

### 3.2 Quote path (mirrors `buildQuote`'s routing attach)

Behind the honesty boundary `jobType == "render_speculative"` (the render analog of
`generativeJobType && records > 0`), attach a `speculation` block to the quote:

```
"speculation": {
  "modality":            "render",
  "quoted_tier":         "preview",            // the ONLY sellable tier today (§1)
  "speedup_band":        [2.7, 5.6],           // [MODELED] from the RUN 2/RUN 3 measured band
  "baseline_secs":       <modeled T_ref for this shot>,   // [MODELED]
  "spec_secs":           <modeled T_stack for this shot>, // [MODELED]
  "basis":               specQuoteBasis        // one const, names the ledger + labels
}
```

`specQuoteBasis` is the `quoteRoutingBasis` analog, e.g.: `"speedup_band [MODELED]
from the measured 2026-07-09 integrated classroom receipts (RUN 2 H100 2.722x /
RUN 3 A100 5.561x): docs/speed-lane-reports/spec-lab/
integrated_spec_render_token_ledger.jsonl — a band of two same-scene measurements,
not a measurement of this job; excludes provisioning"`. Rules copied from routing:
the quoted numbers are ALWAYS labeled, always a band from real ledger rows (never
extrapolated past what was measured — the `gpuBatchCeiling` discipline), and the
conservatism points against us. A shot outside the measured envelope (different
scene class, resolution above 4K, frames >> 4) gets NO speedup band — only the
tier contract — exactly as an embed job gets no routing block.

### 3.3 Submit + persistence (mirrors entry 94)

On submit: persist the QUOTED contract (tier + band + basis) so the receipt can
later show promised-vs-delivered. Storage follows the entry-94 idempotent pattern
(`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS ...`) but as ONE `spec_receipt_json`
JSONB-style text column rather than routing's four scalars: the SpecReceipt carries
sixteen fields plus an open `details` map, and the parse/validate path for it is
precisely the new `ParseSpecReceipt` — one column, one codec, no scalar-column
drift. A second nullable column `spec_quote_json` holds the quoted block. Timeline
events: `speculation_quoted` at submit, `speculation_receipt` when the receipt
lands (best-effort, like `routed`).

### 3.4 Where the SpecReceipt attaches: the ClearingReceipt projection

`ClearingReceipt` gains one optional facet, exactly like `Routing`:

```go
// control/receipt.go (later climb)
Speculation *SpecReceipt `json:"speculation,omitempty"`
```

filled by a pure `receiptSpeculation(inv)` twin of `receiptRouting(inv)`: parse
`spec_receipt_json` through `ParseSpecReceipt` (so a corrupt row fails loudly, never
silently projects), nil when the job carried no spec lane, NEVER recomputed. The
buyer-facing meaning of the fields, stated on the receipt:

- `quality_tier` = what was DELIVERED (worst-wins across tiles). A `fail` tier on a
  finished job means the lane self-pruned and the buyer got the reference-path
  render instead (see §3.5) — the receipt shows the honest attempt, RUN-3-style.
- `speedup_vs_baseline` = the ONE ratio `baseline_total_time_s /
  total_product_time_s` for THIS job; null when `baseline_source=absent` (a job for
  which no reference was rendered has no honest denominator — we never invent one).
- `evidence` = measured on the buyer's own job (the one place `measured` is the
  norm, not the exception); `details` carries the SSIM triple, scene, device, and —
  when the repair pass ran — `selector_recall` and per-tile pre/post scores.

Difference from routing, named: routing projects a submit-time DECISION;
speculation projects a completion-time OUTCOME. So the routing columns are written
in `createJob` while `spec_receipt_json` is written by the collect/settle path when
results land — the first write into `collect.go`'s neighborhood, which is why it is
its own sequenced step and not bundled with the quote surface.

### 3.5 Pricing and the self-prune contract (honesty made commercial)

The tier contract, priced the way the gate already behaves in the harness:

- **Delivered `preview`+** (clears the quoted tier): billed as quoted — the spec
  lane's cheaper seconds are what the buyer bought.
- **Self-pruned (`fail`)**: the buyer is NEVER charged spec-lane seconds for quality
  the lane did not deliver. The job falls back to the reference path and bills as a
  plain render; the failed attempt's cost is the platform's risk, and the receipt
  still shows the pruned SpecReceipt (the buyer sees exactly what was tried and why
  it was rejected). This is RUN 1–4's `decision.action="prune"` turned into a
  billing rule: the gate never loosens to protect margin.
- A receipt whose `evidence` is `modeled` (e.g. a `keyframe_every>1` job whose crop
  step is modeled) PARKS: it can inform, it cannot bill a speedup —
  `delivery_eligible=false` in the adapter is the precedent.

## 4. MEASURED vs MODELED, per surface

| number | label | why |
|---|---|---|
| quote `speedup_band`, `baseline_secs`, `spec_secs` | MODELED, always | a promise from the ledger band, not a measurement of this job; cites `specQuoteBasis` |
| receipt `total_product_time_s`, `baseline_total_time_s`, `speedup_vs_baseline` | MEASURED (when `baseline_source=measured`) | real wall-clock of THIS job's spec path and reference path on the same silicon |
| receipt with `baseline_source=modeled` | speedup allowed but labeled | e.g. baseline taken from a cached same-scene reference rather than re-rendered; the label rides the receipt |
| receipt with `baseline_source=absent` | speedup NULL | a buyer who declines to pay for a reference render gets tier + costs, no ratio — enforced by `Validate()` |
| `keyframe_every>1` crop step | MODELED ⇒ receipt parks | the adapter's existing `modeled` rule, unchanged |
| any cross-lane product (render_x * token_x) | FORBIDDEN | no field or code path exists for it, in Rust or in the Go mirror — invariant #1 is structural |

## 5. Fleet (Metal) vs GPU lane for render jobs

What the split means here, honestly bounded:

- **GPU lane is the measured lane.** Every real integrated receipt (RUN 1–4) is
  A100/H100 SECURE under the provisioning ladder A100 → H100 → H200 (Blender 4.2
  has no Blackwell kernels; fail-loud CPU guard mandatory). RUN 3 vs RUN 4 also
  measured that the RATIO is hardware-relative (an H100 renders the 4096-spp
  reference proportionally faster, compressing the same stack to ~2.7x), so a
  quoted band must name its silicon or quote the band across both.
- **The fleet (Metal) is NOT a measured render substrate today.** Blender's Metal
  backend runs on the fleet's M-class nodes (the local asset-render path proves
  basic capability), but zero fleet render receipts exist — no draft-rate, no
  SSIM-under-Metal parity check, no tile-throughput curve. Under the routing
  precedent's rule ("the sweep measured generative decode only, so every other
  shape gets NO routing block"), **`render_speculative` jobs get NO fleet-vs-GPU
  routing block until a fleet render sweep exists.** The job runs on the GPU lane,
  and the quote says so plainly.
- **The honest future split** (a measured experiment, not a claim): the lane's
  economics want cheap drafts and expensive repairs/references separated —
  low-spp draft tiles + SSIM verify on fleet Metal nodes (verify is device-agnostic
  numpy), anchors/repairs/references on the GPU lane. That is a warm-pool +
  transfer-cost question the spec-render memory already flags; it becomes real only
  via a fleet draft-rate sweep (the render analog of the A100 capability sweep)
  producing its own `MEASURED` curve first.
- **The token rider**: the integrated receipts' token lane (json-template job-event
  stream, `exact=true`, MEASURED) is fleet-native already — it is CPU-cheap and
  lossless, and rides inside the same receipt without a lane split of its own.

## 6. Honest sequencing (design only — every code step below is owner-gated)

0. **DONE (this wave, additive only):** `control/spec_receipt.go` +
   `control/spec_receipt_test.go` + this doc. Build/vet/tests green; zero existing
   files modified.
1. **Schema + projection** (touches `db/schema.sql`, `store.go`, `types.go`,
   `receipt.go`): `spec_receipt_json` + `spec_quote_json` columns (idempotent ALTER
   pattern), `receiptSpeculation` projection + unit tests. No producer yet — columns
   stay NULL in production. Gate: schema applies twice cleanly; receipt untouched
   for every existing job.
2. **Ingest producer** (touches `collect.go` or a new additive endpoint): the
   spec-lab driver posts its integrated receipt to the control plane for a job it
   ran as an external worker; `ParseSpecReceipt` validates at the door. Gate: RUN-3
   and RUN-4 shaped payloads round-trip to a receipt projection on a real PG stack.
3. **Quote surface** (touches `quote.go`, `types.go`): `render_speculative` job
   type + the `speculation` block behind its honesty boundary, preview tier only
   (§1). Gate: quote/submit/receipt integration tests in the entry-93/94 style; a
   non-spec job's quote is byte-identical.
4. **Delivery tier unlock**: ONLY after a strict-delivery receipt exists — the
   next selector candidate is cross-denoiser disagreement (OIDN vs OptiX on the
   same render, ~$0.75 to validate, per RUN 4's finding); a second decisive
   negative parks the delivery tier rather than loosening the gate.
5. **Combined jobs**: only after the plan's sequenced step 3 (a genuinely nested
   workload) produces the one legitimate end-to-end receipt.

Kill clauses: if step 2's real ingestion shows the schema needs a breaking change,
fix `receipt.rs` FIRST and re-mirror (the Rust file stays canonical); if the fleet
render sweep (§5) cannot beat GPU-lane economics for drafts, the split stays
GPU-only and this doc records the measured reason.

## 7. What this wave explicitly did NOT do

No job type registered; no schema column added; no endpoint, store method, quote
field, or dispatch change; no RunPod call, no pod, no cloud spend (the two ledger
blobs were mapped locally from the existing jsonl through the existing adapter).
The control plane compiles and its full test suite passes with the two new files
being pure additions.
