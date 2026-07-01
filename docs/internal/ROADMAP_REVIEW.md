# Computexchange — Roadmap Review (for review 2026-07-01)

Amalgamated from this session's runs: the performance + capability + Hawking audit
([PERF_AND_CAPABILITY_AUDIT.md](PERF_AND_CAPABILITY_AUDIT.md)), the broker-pipeline +
exchange-strategy audit ([BROKER_PIPELINE_AUDIT.md](BROKER_PIPELINE_AUDIT.md)), and the
shipped build ([BUILD_STATUS.md](BUILD_STATUS.md)).

Expansion pack added 2026-06-30:
[FRONTIER_EXPANSION_ATLAS.md](FRONTIER_EXPANSION_ATLAS.md),
[AGGRESSIVE_BACKLOG_MOUNTAIN.md](AGGRESSIVE_BACKLOG_MOUNTAIN.md),
[SELF_COMPETITION_WARGAME.md](SELF_COMPETITION_WARGAME.md),
[CUSTOMIZATION_AND_OWNERSHIP_MAP.md](CUSTOMIZATION_AND_OWNERSHIP_MAP.md), and
[COMPETITOR_AND_FRONTIER_RESEARCH_2026.md](COMPETITOR_AND_FRONTIER_RESEARCH_2026.md).
These docs deliberately expand beyond Candle into launch contracts, supplier-independent
verification, receipts, intake safety, supplier app truth, private pools, credits, spot pricing,
attestation, artifact comparators, and ops/security gates.

Sequential and sectioned. Sections are ordered by dependency and ROI; tasks within a
section run top-to-bottom. Titles are at idea level; each line says how it improves things.
The governing rule from both audits: **every move preserves the verified-result moat, or it
does not ship.**

---

## §0 — Already shipped this session (branch `feat/perf-wave`, GREEN, awaiting your review/merge)
Independently verified: `cargo test` 88/0, `go build/vet` clean, +1,893 lines / 17 files. Nothing merged or deployed.

1. **KV-cache preallocation** — byte-identical decode, sets up ragged batching later.
2. **The determinism class `(device, engine, build_hash)`** — byte-exact redundancy/honeypots now pin to a class and refuse to cross it. *Fixes a latent live bug where two honest Macs could mismatch and one gets quarantined.*
3. **Prefix-KV sharing + length-bucketed embedder** — throughput on classification/extraction/embed (parity-tested in-process).
4. **Throughput-aware scheduling tiebreak** — faster idle workers win ties.
5. **Candle patch surface, vLLM lane seam, Hawking continuous-batch skeleton, golden-hash gate** — the scaffolds the lanes below plug into.

## §1 — Prove and seed the moat we just built (finishers; mostly need a real Mac/GPU)
1. **Seed golden-hash baselines per reference box** — turns the determinism guard from inert to live; catches silent byte-drift.
2. **Run the 13 GPU-ignored parity/throughput tests on Metal/CUDA** — confirms the §0 perf wins are byte-identical and measures the real speedups.
3. **Seed `hw_class`-aware honeypots** — re-enables byte-exact fraud-catching within a class (currently safe-but-off).
4. **Integration-test the scheduling tiebreak under live Postgres** — proves the tiebreak end to end.

## §2 — Make the moat legible (Wave 1, ship-now, highest ROI)
*The biggest unlock is not new capability; it is making the assets we already built visible and binding.*
1. **Wire the real quote into Launch** (price + ETA + live supply + risk before spend, bound to the invoice) — surfaces our single most differentiating built asset and ends the blind Launch; best-in-class vs vast/runpod opaque listings.
2. **Surface the verification receipt + settlement statement on completion** — makes the trust moat visible at the moment of payment (today the live run shows an empty receipt).
3. **Live capacity ticker + SLA flag on the drop card** — shows real-time feasibility before launch.

## §3 — Make the funnel safe and wide (Wave 1)
1. **Bound the inspected source** (per-file + aggregate caps, `io.LimitReader`) — closes a real untrusted-input control-plane OOM/cost vector.
2. **Fix the detection hazards** (`.pdf` supported-then-0-records bug, document-set catch-all, stray-`.csv`) — no more "supported then 0 records"; fewer confidently-wrong detections.
3. **Unify client + server detection + honor the spend cap (`max_usd`)** — one detection brain; no offers the server rejects; the promised cap is actually enforced.
4. **Add the code-repo → embed/index detection pattern** — captures the most common shape we refuse today, with a fully-verified job type.

## §4 — Grow supply (Wave 2)
1. **Self-serve fleet enrollment (N machines per supplier) + the same-supplier redundancy guard** — lets an operator offer a farm; the guard is *mandatory*, not optional (otherwise a multi-worker supplier verifies its own forged bytes and collapses the moat exactly where supply grows).
2. **User-activity (HID) idle detection** — a primary Mac earns only when truly idle; better trust and supply quality.

## §5 — Throughput lanes (perf; finish the §0 scaffolds, hardware-gated)
1. **vLLM CUDA determinism soak → then wire `VllmRunner`** — ~3–6x per GPU on the cloud lane, within-class verified. Soak first, wiring second.
2. **Port Hawking's continuous-batch scheduler (Apple lane)** — ~5x aggregate at B=8; the determinism-gated differentiator no rival sells.

## §6 — Bootstrap liquidity and price discovery (Wave 3 — this is the exchange)
1. **Compute-credit flywheel** (earn-by-lending, spend on your own jobs; gated by verified-task history) — turns every supplier into a latent buyer and solves two-sided cold-start; this is the liquidity an exchange actually wins on. *Watch: mint at verified-PASS not at payout; it needs a treasury/float design, not a ledger clone; verified history is the Sybil defense.*
2. **Bounded supply/demand spot index** (per job_type surge/discount over the catalogue floor) — turns fixed pricing into real price discovery. *Watch: discount the buyer price while holding `offered_rate` at/above the claim-filter floor, or a discount shrinks the supply it meant to fill.*
3. **Low take (~3%) + a first-class "cleared trade" settlement statement** — supplier keep-rate is the supply magnet; margin comes from the quote band and credit float, not from squeezing providers.

## §7 — Expand TAM (only after the verification primitive exists)
1. **Perceptual-hash / tolerance redundancy comparator for non-deterministic output** — THE gate that makes render and other float-nondeterministic work trustable. Ships *before* any render lane, never after.
2. **Render lane** (frame/segment splitting + non-JSONL progressive merge → video) — opens rendering as a workload; gated on §7.1.
3. **14B/32B unified-memory model band** — a model tier only Apple unified memory or the cloud lane can serve.
4. **RWKV-7 long-context lane · constrained JSON decode · opt-in F16-KV long context** — capabilities rivals cannot match (flat long-context, structured output, bigger context); each its own determinism lane.
5. **Plane B model-parallel cluster** — serve models too big for one Mac. Keep as a documented seam until Plane A (data-parallel) is profitable.

## §8 — Explicitly NOT doing (recorded so we do not chase them)
- From-scratch portable engine (Candle is already portable Metal+CUDA) · F16 activations (net loss) · the megakernel · speculative decode (net-negative on the tested Mac) · a from-scratch MLX FFI lane (superseded by the Hawking port).

---

### The one-paragraph strategy
Become the verified clearing layer for compute. vast/runpod rent a box and verify nothing;
render-network/sheepit verify only renders; none give a buyer a **price-it / trust-it /
settle-it** loop over idle consumer Macs. The sequence to own the lane: make the moat
**legible** (§2), make the funnel **safe and wide** (§3), grow **supply** (§4), bootstrap
**liquidity** with credits + a spot index (§6), and expand **TAM** only once the verification
primitive exists (§7). The moat made visible and liquid is how we become the exchange instead
of another box-rental.
