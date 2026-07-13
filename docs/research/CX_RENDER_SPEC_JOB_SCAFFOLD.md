# CX render_speculative job scaffold — the additive quote + receipt surface (2026-07-10)

Status: **DONE (additive only)** — `control/render_spec_job.go` +
`control/render_spec_job_test.go` + this doc. **No hot file touched** (no change
to `quote.go`, `api.go`, `store.go`, `receipt.go`, `types.go`, `db/schema.sql`,
or any other existing `control/*.go`). Stdlib only. Build / vet / full unit
suite green; the integration-tagged build compiles clean. Every wiring step
below is a LATER, owner-gated climb.

**2026-07-10 hardening update:** strict delivery is no longer “never achieved.” A bound 4K
classroom repair recipe cleared at **2.450894x**, and the untuned 1080p sweep cleared 1/3 scenes
(pavilion **1.658801x**). The shape-only quote still never promises strict delivery because it
does not bind scene content or repair-policy/build digests; it now emits **no per-job speedup
band** rather than projecting standalone scene results. See
`SPEC_ENGINE_HARDENING_2026-07-10.md`.

Companion docs: `docs/research/CX_SPEC_LANE_INTEGRATION_DESIGN.md` (the prior
wave's design — this scaffold is its §3 turned into code),
`docs/research/CONSOLIDATION_PLAN_2026-07-09.md` (the staged-multiplier table
and the banked receipts), `docs/internal/CREED_AND_PATH_TO_TEN.md` entries 93–94
(the routing block, the pattern this mirrors 1:1).

## 1. What now exists (this wave, additive)

Two pure, deterministic, stdlib-only surfaces in `control/render_spec_job.go`,
each the render analog of a proven routing-block piece:

| this wave (`render_spec_job.go`) | mirrors (routing block) | role |
|---|---|---|
| `RenderSpecParams` + `Validate()` | (shot-shape inputs) | range/enum contract on the shot params |
| `RenderSpecQuote` + `QuoteRenderSpec(p)` | `QuoteRouting` + `DecideSubstrate(...)` | advisory quote: the [MODELED] speedup band behind an honesty boundary |
| `RenderSpecReceipt` + `receiptRenderSpec(r)` | `receiptRouting(inv)` | checked buyer-facing projection; `nil, nil` for absent/non-render input and an error for malformed render rows |
| `renderSpecQuoteBasis` (const) | `quoteRoutingBasis` (const) | the one string that names the ledger + welds `[MODELED]` to every number |

- **`QuoteRenderSpec(RenderSpecParams) (RenderSpecQuote, error)`** reads a shot's
  shape (`resolution / frames / ref_spp / draft_spp / keyframe_every /
  requested_tier`) and returns the advisory block. It is PURE and
  DETERMINISTIC: no clock, no rand, no DB; identical inputs yield an identical
  quote. It errors only on params that fail `Validate`; a valid-but-out-of-
  envelope shot is not an error — it is a quote with the tier contract and NO
  band.
- **`receiptRenderSpec(*SpecReceipt) (*RenderSpecReceipt, error)`** projects a landed
  `SpecReceipt` (parsed via the already-landed `ParseSpecReceipt`) onto a
  buyer-facing block. It returns **`nil, nil` at the modality boundary** (a nil
  receipt or a non-render/token receipt), but rejects a malformed render row
  instead of disguising corruption as absence. A naked worker receipt is always
  parked until the control plane verifies job/input/artifact/policy binding; the
  projection never treats worker-local verification as server attestation.

### The two structural honesty invariants, made into code

1. **No cross-lane / cross-tile product.** There is deliberately no field,
   const, or code path that multiplies two multipliers. The quote carries a
   BAND of real same-discipline measurements (`renderSpecBandLowX` /
   `…HighX`) plus a single measured 4K anchor (`renderSpec4KAnchorX`) — never a
   synthesized figure. This is invariant #1 of the consolidation plan, enforced
   the same way the Go `SpecReceipt` enforces it: by the *absence* of any
   multiply.
2. **Unbound strict delivery is never sold.** `QuoteRenderSpec` sets
   `QuotedTier = "preview"` unconditionally and `StrictDeliveryPromised = false`
   as a compile-time constant. Even when the buyer passes
   `requested_tier="delivery"`, the quote echoes the ask, quotes preview, and
   the reason states that strict has succeeded on exact recipes but is not a
   promise for an unbound shot (the untuned scene sweep cleared 1/3).
   `TestRenderSpecQuoteNeverStrict` proves this over
   a 4×4×4×3×3 matrix of shapes and tier requests.

## 2. The MEASURED basis every number is drawn from

All figures are transcribed from the banked ledger, not recomputed
(`docs/speed-lane-reports/spec-lab/integrated_spec_render_token_ledger.jsonl` +
the `CONSOLIDATION_PLAN` staged table):

| constant | value | source / label |
|---|---|---|
| `renderSpecBandLowX` / `renderSpecBandHighX` | **3.87x – 7.87x** | representative-scene band, standalone delivery-tier renders, device-correct (many_glass 3.87 / sphere_bump 4.59 / monkey 6.88 / cube_volume 7.87). The 14.34x forgiving-scene outlier is DELIBERATELY EXCLUDED. `[MODELED]` when projected onto a shot. |
| `renderSpec4KPreviewAnchorX` | **5.561327x** | REAL RUN 3 (A100, classroom 3840×2160, ref 4096 / draft 512, kf=1): preview/no-repair result, global 0.9854 / worst-tile 0.9095. MEASURED integrated product context; not projected onto a shot. |
| strict 4K bound anchor | **2.450894x** | H100 classroom, exact `aov_edge` + match-reference repair recipe: global 0.9902 / worst-tile 0.9501. MEASURED integrated product ratio. Historical context only in the shape quote. |

The old 3.87x–7.87x values are standalone-render context, not an integrated product band. The
hardened quote keeps its legacy nullable fields for wire compatibility but leaves them nil until
scene and policy/build evidence can be bound.

The MEASURED envelope (outside it: tier contract, no band — the routing
precedent's "unmeasured shape gets no block"):

- `keyframe_every == 1` (all-anchor, zero reprojection). **kf>1 is refused a
  band**: its only integrated measurement (RUN 1, kf=4) *quality-failed*
  (worst-tile 0.164) — quoting a speedup for a config we measured to fail would
  be dishonest.
- resolution `≤ 3840×2160` (the RUN 3 4K ceiling).
- `frames ≤ 4` (the measured integrated count, RUNs 1–4; "frames >> 4" is out of
  envelope — for kf=1 this is a linear scale, so a later climb can lift it with
  one longer-sequence measurement).
- ref/draft spp ratio in `[8x, 512x]` (RUN 3 = 8x; representative many_glass at
  8 spp = 512x).

## 3. MEASURED vs MODELED, per surface

| number | label | why |
|---|---|---|
| quote `speedup_band_low/high_x`, `anchor_4k_speedup_x` | **MODELED**, always | a projection of banked receipts onto this shot, cited by `renderSpecQuoteBasis`; not a measurement of THIS job; excludes provisioning |
| receipt `speedup_vs_baseline` when `baseline_source=measured` | **MEASURED** | real wall-clock of THIS job's spec vs reference render — passed through verbatim, not rounded away |
| receipt when `baseline_source=modeled` | speedup allowed, **labeled** | `renderSpecReceiptBasis` writes the modeled-baseline caveat into the buyer sentence |
| receipt when `baseline_source=absent` | speedup **NULL** | no baseline rendered ⇒ no ratio; the projection carries `nil` and the basis says "no speedup is claimed" (enforced upstream by `SpecReceipt.Validate`) |
| `quality_tier=fail` on a finished job | **self-prune**, shown honestly | `SelfPruned=true`; the buyer got the reference render (billed as a plain render), and the attempt is still surfaced (RUN-3 style) |
| any render_x × token_x product | **FORBIDDEN** | no field or code path exists for it — invariant #1 is structural |

## 4. Fleet (Metal) vs GPU lane for render jobs

Honestly bounded, unchanged from the integration design §5 and restated here so
the scaffold and the split travel together:

- **The GPU lane is the measured lane.** Every integrated render receipt
  (RUN 1–4) is A100/H100 SECURE under the ladder A100 → H100 → H200 (Blender 4.2
  has no Blackwell kernels; fail-loud CPU guard mandatory). The ratio is
  hardware-relative (RUN 3 A100 5.56x vs RUN 4 H100 repair-free ≈4.6x), which is
  why the band is carried as a *band across silicon*, never a single-GPU
  promise.
- **The fleet (Metal) now has a measured three-scene local preview matrix, not a
  production fleet receipt.** On 2026-07-12 an M3 Ultra resident Cycles path
  measured 56.714589x for classroom and 55.926238x for Pavilion at 24+24 spp
  versus 4096 spp. BMW27 required 32+32 spp and measured 34.757412x. Every
  selected draft passed the same local-unattested RGB agreement contract and an
  independent 4096-spp audit. See
  `APPLE_METAL_SPEC_RENDER_STAGE_2026-07-12.md`. This is not a repeated
  draft-rate curve, animated-scene result, strict-delivery result, or schedulable
  fleet job.
  Under the routing
  precedent's rule, **`render_speculative` jobs get NO fleet-vs-GPU routing
  block until a fleet render sweep exists.** So this scaffold's quote surface is
  a *tier + speedup-band* surface (does this shot clear preview, and how fast),
  NOT a substrate-routing surface — the two are deliberately separate, and the
  render job runs on the GPU lane, said plainly.
- **The honest future split** (a measured experiment, not a claim): cheap draft
  tiles + device-agnostic SSIM verify on fleet Metal nodes; anchors / repairs /
  references on the GPU lane. It becomes real only via a fleet draft-rate sweep
  (the render analog of the A100 capability sweep) producing its own MEASURED
  curve first — the same discipline `gpuCompetitionCurve` demanded of the token
  lane.
- **The token rider** inside a future combined receipt is fleet-native already
  (CPU-cheap, lossless, `exact=true`) and needs no lane split of its own.

## 5. Sequencing — what is built vs what is owner-gated

**Built (this wave, additive, zero hot files):**

- `control/render_spec_job.go` — the quote + projection + Validate above.
- `control/render_spec_job_test.go` — determinism, never-strict, envelope,
  Validate rejection, and a real-`SpecReceipt` round-trip (reusing the verbatim
  RUN 3 / lane-2 / lane-3 blobs already in `spec_receipt_test.go`).
- this doc.

**Owner-gated hot-file wiring (design only — NOT done here), sequenced:**

0. **DONE (prior wave):** `control/spec_receipt.go` + `spec_receipt_test.go`
   (the Go `SpecReceipt` mirror + `ParseSpecReceipt`).
1. **Schema + projection** (`db/schema.sql`, `store.go`, `types.go`,
   `receipt.go`): a `spec_receipt_json` + `spec_quote_json` column pair
   (idempotent `ALTER … ADD COLUMN IF NOT EXISTS`, the entry-94 pattern) and a
   `ClearingReceipt.Speculation *RenderSpecReceipt` facet filled only after
   separately checking both `ParseSpecReceipt(col)` and `receiptRenderSpec(r)`
   errors. No producer yet — columns stay NULL.
2. **Quote surface** (`quote.go`, `types.go`): register the
   `render_speculative` job type (a validated projection of
   `exp_render_stack.py`'s config JSON) and attach `RenderSpecQuote` behind the
   `jobType == "render_speculative"` honesty boundary — preview tier only.
3. **Ingest producer** (`collect.go` or an additive endpoint): the spec-lab
   driver POSTs its integrated receipt for a job it ran as an external worker;
   `ParseSpecReceipt` validates at the door; the receipt projection lights up.
4. **Bound delivery-tier unlock**: strict receipts now exist. Product unlock still requires a
   content digest, exact repair/verifier policy and build digest, delivered artifact digest, and
   authoritative attestation/server-side re-verification. A shape match alone never unlocks it.
5. **Combined jobs**: only after the plan's sequenced nested-workload receipt.

Kill clause (unchanged): if step 3's real ingestion shows the schema needs a
breaking change, fix `receipt.rs` FIRST and re-mirror (Rust stays canonical);
if a fleet render sweep cannot beat GPU-lane economics for drafts, the split
stays GPU-only and this doc records the measured reason.

### Named deviation from the integration design §3.2

The design sketched `baseline_secs` / `spec_secs` absolute wall-clock in the
quote block. This scaffold **deliberately omits absolute per-shot seconds** and
carries the speedup BAND only. Absolute render seconds for an arbitrary shot
require a measured render-time model (the render analog of the A100 tok/s
sweep); we have four integrated points at fixed configs, not a curve — so
emitting per-shot seconds would invent a number. The banked RUN 3 seconds
(4546.76s reference vs 817.36s spec) appear in `renderSpecQuoteBasis` as *cited
context for the anchor*, never as a claim about the buyer's shot. Modeling
absolute seconds is a later climb that must measure a render-time curve first —
the same "no measured curve → no number" boundary `routing.go` draws with
`interpolatedAggTokS` returning 0 for an unknown class.

## 6. Real gate output (this wave)

```
$ gofmt -l render_spec_job.go render_spec_job_test.go
(empty — both files formatted)

$ go build ./...        → exit 0
$ go vet ./...          → exit 0
$ go vet -tags integration ./...   → exit 0   (integration build compiles clean)

$ go test -run 'RenderSpec|ReceiptRenderSpec' -v ./...
--- PASS: TestRenderSpecQuoteDeterministic        (4 subtests)
--- PASS: TestRenderSpecQuoteNeverStrict          (full shape×tier matrix)
--- PASS: TestRenderSpecQuoteEnvelope             (in-envelope + 5 out-of-envelope levers)
--- PASS: TestRenderSpecQuoteValidate             (9 rejection cases)
--- PASS: TestReceiptRenderSpecRoundTrip          (run3 self-pruned + lane2 delivered)
--- PASS: TestReceiptRenderSpecHonestyBoundary    (nil + token → nil)
--- PASS: TestReceiptRenderSpecAbsentBaseline
ok  computeexchange/control  0.732s

$ go test -count=1 ./...   (full default unit suite)
285 subtests RUN / 285 PASS / 0 FAIL / 0 SKIP → ok  0.515s, exit 0
```

Zero existing files modified; the control plane compiles and its full unit
suite passes with the two new files being pure additions.

## 7. What this wave explicitly did NOT do

No job type registered; no schema column added; no endpoint, store method,
quote field, dispatch change, or receipt-hot-path wiring; no RunPod call, no
pod, no cloud spend (the receipt round-trip reuses blobs already committed in
`spec_receipt_test.go`). The quote surface is not attached to `buildQuote`; the
projection is not attached to `ClearingReceipt`. Those are the owner-gated climbs
in §5.
