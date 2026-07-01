# Self-Competition Wargame

Audit date: 2026-06-30

This document asks how a smart competitor, or a sharper future version of us, beats the current
Compute Exchange build. The purpose is not paranoia. The purpose is to keep pressure on the
product until the answer becomes "they would have to rebuild the verified market itself."

## Rule

Assume the opponent has read our public product, can copy generic GPU orchestration, can wrap
vLLM, can undercut hourly prices for a while, and can raise enough money to buy launch supply.
They cannot instantly copy our repo, our history, our supplier relationships, or our verified
settlement data.

## Attack 1. They Ship the Quote-First Product Before Us

Opponent move:

- Buyer uploads JSONL or connects GitHub.
- They show a cost band, ETA, warm supply, risk, and cancellation cap before spend.
- They bind that quote to submit and invoice.
- Their receipt says exactly what happened.

Why this hurts:

- We already have much of this in `control/quote.go`, `control/api.go`, and invoice/status
  projections, but live launch paths can bypass it through intake/pipeline surfaces.
- If they make the proof visible first, buyers perceive them as safer even if our backend proof
  is stronger.

Counter-build:

- Pipeline-safe launch contract.
- Composite quote for detected pipelines.
- Receipt as the completion artifact.
- UI must never launch paid work from a path that cannot quote and cap the work.

Kill condition:

- A live buyer cannot spend without seeing and binding price/risk/proof terms unless they call an
  explicitly marked low-level API.

## Attack 2. They Copy OpenAI Batch Shape More Cleanly

Opponent move:

- They publish a drop-in async batch endpoint.
- Existing scripts need only base URL and key changes.
- They document exact request/response compatibility and error file behavior.
- They add "verified receipt" as metadata.

Why this hurts:

- We have OpenAI-compatible routes, but this needs to become a polished migration lane.
- The buyer's switching cost matters more than our internal architecture.

Counter-build:

- Compatibility matrix and cookbook.
- Quote endpoint for OpenAI-shaped JSONL.
- Batch output/error file parity tests.
- Receipt companion endpoint.

Kill condition:

- A sample script that works against OpenAI Batch can be adapted in under five minutes and returns
  a Compute Exchange receipt.

## Attack 3. They Avoid Same-Supplier Self-Verification

Opponent move:

- They market "independent verification" and enforce supplier-distinct redundancy.
- They show independent-peer coverage on every receipt.

Why this hurts:

- Our current code excludes the anchor worker, not necessarily the supplier. That is fine for a
  one-worker supplier world, but fleet enrollment turns it into a trust issue.

Counter-build:

- Supplier-distinct peer selection.
- Supplier-distinct dispute reverification.
- Receipt labels for independent peer missing, cross-class skip, and verified.

Kill condition:

- No paid job can be marked independently verified by two workers owned by the same supplier.

## Attack 4. They Use Attestation as a Trust Shortcut

Opponent move:

- They offer TEE or confidential GPU-backed inference.
- Their receipt includes an attestation measurement.
- Enterprise buyers check a box that says "attested runtime."

Why this hurts:

- Cross-worker verification is stronger for many market settings, but attestation is easier to
  explain to enterprise buyers.
- We need a story that can include attestation without replacing verification.

Counter-build:

- Optional attestation fields in heartbeat and receipt.
- Routing tier for attested workers.
- Research spike on NVIDIA confidential computing, Phala-style confidential VMs, and what data a
  buyer receipt should include.

Kill condition:

- We can say "verified, attested, or both" and show which was used.

## Attack 5. They Win Supplier Trust

Opponent move:

- Their Mac app clearly shows earnings, payout readiness, heat, idle status, recent verification
  pass/fail history, and exactly when the machine will run.
- Their controls actually affect the worker.

Why this hurts:

- Supply quality compounds. A supplier who trusts the app leaves it on.
- Our Mac app surface is promising, but prefs/status data must be wired all the way through.

Counter-build:

- Agent prefs overlay consumed at runtime.
- Trust panel fed by control-plane state.
- Idle detection.
- Per-supplier receipt history.

Kill condition:

- The app is never decorative: every visible toggle changes agent behavior, and every trust field
  comes from a real source.

## Attack 6. They Build a Credit Loop First

Opponent move:

- Earn credits by lending compute.
- Spend credits on your own jobs.
- Credits clear only after verified work.
- The loop bootstraps both sides of the market.

Why this hurts:

- This directly addresses marketplace cold start.
- Our ledger can support it, but the treasury and clawback design must be deliberate.

Counter-build:

- Credit mint on verified pass.
- Capped liability and expiry.
- Credit spend converted through platform take or treasury pool.
- Sybil gates from Connect identity, reputation, and verified-task history.

Kill condition:

- A supplier can earn and spend without creating uncapped platform liabilities.

## Attack 7. They Offer "Private AI Batch" to Teams

Opponent move:

- A company enrolls its own machines.
- Jobs run only on approved machines.
- The product feels like private batch inference, not a public marketplace gamble.

Why this hurts:

- Private pools are already partly in our scheduler, but not productized.
- This is likely easier to sell than public heterogeneous compute for conservative buyers.

Counter-build:

- Private pool membership UI/API.
- Team invitations.
- Quote response separated by public/private capacity.
- Receipt showing private routing.

Kill condition:

- A buyer can create a pool, invite suppliers, quote against that pool, launch, and receive a
  private-pool receipt.

## Attack 8. They Beat Us on CUDA Throughput

Opponent move:

- They wrap vLLM well.
- They own A100/H100 batch throughput.
- They publish strong price/perf charts.

Why this hurts:

- Candle CUDA should not be our only CUDA story.
- If buyers only compare NVIDIA throughput, vLLM is the reference bar.

Counter-build:

- vLLM lane with class isolation and restart/cross-SKU determinism soak.
- CUDA quote tiers separate from Apple tiers.
- Do not compare vLLM bytes against Candle/Apple.

Kill condition:

- Our CUDA lane is either faster enough to matter or explicitly not the product focus. No half-lane.

## Attack 9. They Beat Us on Apple Continuous Batching

Opponent move:

- They realize idle Apple Silicon is the differentiated supply.
- They build a continuous-batch server with stable batch behavior.
- They turn many small jobs into high utilization.

Why this hurts:

- This is the lane where generic GPU clouds do not have the same supply story.
- We have Hawking research and skeletons, but not a production runner.

Counter-build:

- Per-model serve loop.
- Hawking kernel port or batch-invariant Candle-side batching.
- Batch-composition determinism decision before paid byte-exact generation.

Kill condition:

- Concurrent same-model tasks improve aggregate throughput without creating false verification
  mismatches.

## Attack 10. They Expand TAM With Render Before We Do

Opponent move:

- They sell render/video splitting.
- They use perceptual comparison and watermarked honeypots.
- They become "verified distributed rendering plus AI batch."

Why this hurts:

- Render is an obvious distributed-compute story.
- But shipping render before a comparator would weaken our proof.

Counter-build:

- Perceptual comparator first.
- Artifact codec and streaming merge.
- Anti-undersampling defense.
- Decompression-bomb limits.

Kill condition:

- No render lane launches until honest jitter and dishonest quality shaving can be distinguished
  well enough to settle money.

## Attack 11. They Make Our Docs Look Bigger Than Our Product

Opponent move:

- They ship fewer ideas, but every buyer path works.
- They avoid half-wired surfaces.

Why this hurts:

- This is the self-competition risk. We can drown in frontier docs and leave launch paths leaky.

Counter-build:

- Every doc lane needs a proof gate.
- P0 is contracts through existing paths, not new speculative TAM.
- Keep a "do not ship as verified" label for every incomplete lane.

Kill condition:

- Docs create executable checks, not vibes.

## Forced Moves

The next five moves that most reduce competitive exposure:

1. Quote/budget/verification contract through intake and pipeline launch.
2. Supplier-distinct verification.
3. Receipt as a first-class product artifact.
4. Mac app prefs/status truth.
5. OpenAI Batch migration polish.

The next five moves that most increase moat:

1. Generation honeypot activation.
2. Private pools.
3. Credit flywheel with treasury cap.
4. Spot index.
5. Engine registry with verification-class receipts.

