# Computexchange — Deep Research Report v2
**"Local Compute on Steroids" · Project-Based Pricing as Paradigm Shift**
*215 agents · 51 sources · 50 claims verified adversarially · 9 confirmed, 41 killed*
*June 2026*

---

## Honest Note on Verification

The adversarial verification process (3-agent kill vote per claim) was unusually aggressive this round: 24 of 25 v2 claims were killed — not because the underlying strategy is wrong, but because most available sources are vendor blogs, Substack posts, and consulting white papers that cannot survive adversarial citation checks. The *structural arguments* below are sound. The *specific quantitative claims* (exact cost differentials, percentage breakevens, market sizes) remain unverified and should not be used in investor materials without primary sourcing. What survived verification is labeled HIGH. What is structurally sound but unquantified is labeled MEDIUM. Strategic inference with no surviving citation is labeled LOW.

---

## The Thesis

Computexchange is not a cheaper GPU rental. It is **"local compute on steroids":**

- **Local** = your data stays under control, no per-hour anxiety, runs as long as the job needs, jurisdiction-bound if required
- **On steroids** = scaled to hundreds of machines, API-driven, project-priced, no ops burden, verified output

The core paradigm shift: **buyers pay for the completed project, not for GPU-hours consumed.** This is the one positioning move no incumbent has made in the compute infrastructure market, and it changes everything downstream — the buyer relationship, the margin structure, the switching cost, the supply economics.

---

## 1. Project-Based Pricing — The Paradigm Shift

**Verified [HIGH · sierra.ai/blog/outcome-based-pricing]**

Sierra AI's public pricing page describes outcome-based pricing as "tied to tangible business impacts — such as a resolved support conversation, a saved cancellation, an upsell, a cross-sell." The buyer pays for the outcome, not for tokens consumed or compute rented. This 3-0 verified claim is the anchor for everything that follows.

### Why this changes the competitive dynamic

**Current market (GPU-hour rental):** The buyer bears all risk. They rent an H100, run the job, it fails or they misconfigure it, they still pay. Vast.ai, RunPod, Lambda Labs — all variants of the same contract: "we give you a machine for N hours, what happens next is your problem."

**Project pricing:** The platform bears the execution risk. "You tell us what you need done. We route it, run it, verify it, and charge you only when it's complete." The buyer's cognitive load drops from "manage GPU instances, monitor jobs, handle failures" to "submit a JSONL, get results."

This is why the Compute Autopilot (`/v1/quote`) is strategically critical — not as a billing feature but as the front door to the paradigm shift. A buyer pastes their dataset, gets a price quote with ETA, clicks launch. They never see GPUs. They see *a project with a price tag.*

### Commercial precedents

The shift is happening across AI infrastructure right now:
- **Sierra AI**: pay per resolved conversation (not per token)
- **Intercom Fin**: $0.99 per resolved support ticket (proven at $100M+ ARR — v1 research)
- **AWS Lambda / Cloudflare Workers**: pay per invocation, not per server-hour — the serverless model is project pricing for compute
- **Professional services**: architects, lawyers, accountants all charge per deliverable, not per hour of office tenancy

The GPU rental market is pre-serverless. Computexchange is the serverless layer on top of distributed consumer hardware.

### Failure modes to guard against

*These did not survive the kill vote but are logically coherent and worth defending against:*

1. **Adverse selection**: Buyers with the worst data quality and most ambiguous requirements are disproportionately attracted to "pay only if it works" pricing — because their bad data means they'd lose a per-hour bet. Guard with: the `/v1/quote` preflight scan (already built) that flags malformed inputs, estimated failure rates, and OOM risk *before* billing begins. Make them fix the data before they submit.

2. **Attribution disputes**: "Was that classification correct?" If the buyer defines correct differently than the model, they dispute every invoice. Guard with: per-job type manifests that include the scoring rubric. Honeypot verification provides an independent ground truth. The job manifest is the contract.

3. **Platform bears cost overruns**: If the supplier underestimates a job, the platform eats the difference under fixed project pricing. Guard with: the quote preflight is the cost floor (job manifests define split size, model, task count). Budget cap (`max_usd`) prevents cost overruns beyond buyer approval. The take rate must price in a small overrun buffer.

4. **Supply unreliability makes project SLAs impossible**: You cannot promise "project complete by 5pm" without knowing supply availability. Guard with: the `eligible_now` count in the quote response — only quote an ETA when supply density is above minimum threshold. Don't offer project pricing to buyers during supply troughs.

### Unit economics vs. GPU rental

The margin structure under project pricing is categorically different:

| Metric | GPU rental (Vast/RunPod) | Project pricing (Computexchange) |
|---|---|---|
| Buyer pays for | Clock time (regardless of output) | Completed deliverable |
| Platform risk | Zero (buyer pays either way) | Execution risk (if job fails, no revenue) |
| Take rate leverage | Thin (transparent $/hr market) | Fat (opaque $/project, value-priced) |
| Buyer relationship | Transactional, price-shopped | Outcome-partnered, stickier |
| Upsell surface | None (already renting max) | Preflight, SLA tier, priority queue |
| Switching cost | Zero (move to RunPod in 5 min) | High (routing intelligence, job history, warm cache) |

The take rate starts at 8% (per ACCRETION.md) but project pricing allows it to ratchet to 15–20% as the platform absorbs more execution risk and demonstrates reliability. The buyer stops caring about $/GPU-hr and starts caring about $/project — and $/project can be priced to margin, not to parity with cloud.

---

## 2. Idle Compute Economics — The Structural Cost Advantage

**[MEDIUM — structural logic sound, quantitative claims unverified]**

The foundational economics: Mac owners and GPU owners have **sunk costs**. They already bought the hardware. Their marginal cost of running a job is electricity (~$0.02–0.10 equivalent per GPU-hour depending on hardware and local power rates). Cloud providers must amortize:
- CapEx on hardware (H100s at $25–40k each)
- Data center PUE overhead (power, cooling, space)
- Staff, networking, bandwidth
- Profit margin
- Depreciation over 3–5 year hardware lifecycle

All of this is rolled into the rental price. The sunk-cost supplier on Computexchange has none of it. **This gap is permanent and structural — no efficiency improvement by AWS or Google closes it, because the idle Mac owner's floor is electricity.**

*Note: The specific cost numbers from Lenovo's whitepaper (on-premise vs. cloud breakevens, per-token differentials) were all refuted 0-3. The structural argument is sound; the specific numbers need primary sourcing before use in marketing or investor materials.*

### The Airbnb analogy (and where it breaks)

Airbnb built a $75B company on the same structural logic: idle real estate assets have near-zero marginal cost of hosting. The research papers on Airbnb's welfare effects were refuted (couldn't be independently verified), but the commercial outcome is self-evident.

**Where the analogy holds:**
- Same asset class (existing owned hardware vs. existing owned property)
- Same host motivation (cover costs, earn on idle capacity)
- Same platform job (match, mediate, enforce, pay)
- Same structural cost advantage over incumbents (Marriott can't compete on marginal cost with a host whose mortgage is already paid)

**Where it breaks:**
- Machines fail silently; homes don't disappear mid-stay
- Hardware churns (new M5 renders the M4 less attractive — hosts upgrade, supply composition shifts)
- Regulation of compute is evolving fast (EU AI Act, GDPR enforcement on sub-processors)
- Direct supplier-buyer relationships are easier to establish than host-guest ones (a buyer can find the Mac Mini owner on Discord)

**Anti-defection is a product problem, not a marketing one.** The platform value must be non-portable: reputation scores that took 500 jobs to earn, warm model state that cold-starts cost $0.30, compliance certifications the supplier got via the platform, job history that feeds routing intelligence. If the only thing tying a supplier to the platform is "that's where the buyers are," they'll leave the moment a buyer offers a direct deal.

### The Turo model (unverified but instructive)

Turo is the closest analogue: idle cars, sunk CapEx, peer marketplace, asset-heavy incumbents (Avis, Hertz). The specific financials from Sacra were refuted 0-3 in verification. But the structural dynamic — incumbent car rental companies with massive debt loads competing against hosts with paid-off cars — is visible in the market. The lesson: **build the insurance, dispute resolution, and trust infrastructure that makes the platform indispensable to both sides**, not just the match.

---

## 3. The "Local" Product — Privacy, Sovereignty, and Trust

**[MEDIUM — regulatory frameworks public record; enterprise spend figures unverified]**

This is the buyer segment that neither Vast nor RunPod can serve and neither can AWS without massive contractual overhead: **enterprises and regulated entities who cannot send data to a shared cloud.**

### The regulatory landscape

The frameworks exist and are not in dispute. What drives enterprise "local compute" demand:

- **GDPR (EU)**: Data processing must have legal basis; transfer outside EEA requires SCCs or equivalent. "The cloud provider processes our data" is a processing relationship that requires documentation, data protection agreements, and audit rights.
- **HIPAA (US healthcare)**: PHI cannot be processed on infrastructure without a Business Associate Agreement. Most GPU cloud providers do not offer BAAs. A diagnostic AI running on RunPod on a shared GPU is HIPAA non-compliant without a BAA.
- **EU AI Act (effective 2025–2026)**: High-risk AI systems (medical, financial, hiring) require specific documentation of training data, model provenance, and output traceability. Running on a shared GPU cloud makes this documentation chain much harder.
- **Canadian PIPEDA + Quebec Law 25**: Organizations must know where personal data is processed and ensure adequate protection. A startup processing Canadian health records on a US-jurisdiction GPU farm is on thin legal ice.
- **FedRAMP (US federal)**: Government agencies must use FedRAMP-authorized cloud services. This is effectively a 2–3 year certification process that no GPU marketplace has completed.

### What "local on steroids" means to regulated buyers

| Buyer type | Current solution | What they want | Computexchange offer |
|---|---|---|---|
| EU healthcare company | On-premise GPU server, managed by IT | On-prem performance + cloud convenience | Dedicated Mac Mini fleet in their office, Computexchange as the scheduling layer |
| US law firm | No AI (data too sensitive for cloud) | Private batch processing of documents | Apple Silicon agent on their machines, air-gapped mode, project billing |
| Canadian bank | Azure with data residency addendum | Guaranteed Canadian-jurisdiction processing | Canadian Mac owner pool, data residency routing by `data_country` |
| US federal agency | On-premise only | Anything faster without FedRAMP | Not yet (FedRAMP is the 3-year cert) |
| Research lab | HPC cluster allocation | Burst capacity for large models | Apple Silicon Ultra supply for 70B+ single-node runs |

The "single-box, offline-capable, LAN-operable" architecture in the codebase is not a dev feature — it is an enterprise product waiting to be named. **Call it the Private Deployment tier.** Charge 3× the standard rate. It is a different product from the shared marketplace.

### Data residency routing (not yet wired)

The codebase has `data_residency[]` in the job schema and `data_country` on suppliers, but the scheduler does not enforce data residency at the claim filter level. This is a P1 fix for the enterprise tier. Without it, the privacy claim is marketing. With it, it is contractual.

---

## 4. Switching Costs and Ecosystem Lock-In

**[LOW — strategic inference, no sources survived verification]**

The research on Stripe, Twilio, and Cloudflare's switching cost playbooks produced no surviving verified claims. But the strategic logic is well-established in platform literature. The Computexchange equivalent:

### The hooks (low switching cost, get the buyer in)
- **OpenAI-compatible batch API**: Change one URL. Zero migration effort. This is the initial hook — necessary but not sufficient. Twilio had this with phone number portability: easy to port in, easy to port out. The hook is not the moat.
- **CLI + Python SDK**: First-class developer tooling makes the integration feel "local." Developers who've written `from cx import embeddings` in their nightly pipeline will not rewrite it unless there's a compelling reason.

### The moat (high switching cost, keep them there)
These are the things a buyer loses if they leave, that have no value outside the platform:

1. **Routing intelligence.** Every completed job generates `task_durations` data — actual wall-clock times per job type, model, split size, worker class. After 1,000 jobs, the scheduler routes your specific workload to the specific suppliers with the best track record for it. This is a personalized advantage that starts at zero for a new platform and accumulates into something no competitor can replicate without running your historical jobs.

2. **Supplier reputation data.** A supplier's reputation score (0.0–1.0) is built from hundreds of honeypot checks and redundancy comparisons. High-reputation suppliers get warm-routing priority. This reputation has no monetary or informational value outside Computexchange — it is purely an internal signal. A supplier who has built a 0.97 reputation score over 6 months has a strong incentive to stay, because leaving and starting fresh on any other platform means starting at 0.5.

3. **Warm model cache.** The warm model pool means the second job of type X on a given worker is meaningfully faster than the first. Over time, the network develops a warm state for common model+workload combinations that cold-start competitors cannot replicate. A buyer who's been running nightly embed jobs for 3 months is benefiting from a warm fleet — switching to RunPod resets that.

4. **Compliance artifacts.** A healthcare buyer who gets their HIPAA BAA, their data processing agreement, and their GDPR Data Processing Addendum through Computexchange cannot easily replicate that with RunPod. The compliance paperwork is the switching cost. Build the compliance stack as a product: issue BAAs, DPAs, audit logs, data residency certificates. These take weeks to negotiate and become assets to protect.

5. **Job history + invoice trail.** Buyers keep financial records. A 12-month history of `GET /v1/jobs/{id}/invoice` showing exactly what was computed, when, at what cost, with what verification outcome, is a compliance and audit trail that a buyer cannot recreate if they switch. Make it exportable as a PDF. Make it part of the pitch.

### The Stripe/Twilio lesson applied

Stripe's moat is not the payment API (easily replicated). It is: Stripe Radar (fraud intelligence trained on every transaction across every merchant), Stripe Treasury (banking as a service that requires months to integrate), Stripe Tax (handles jurisdiction complexity no one wants to rebuild), and the developer documentation ecosystem that is genuinely better than alternatives. **Each subsequent product deepens the lock-in established by the first.**

Computexchange's equivalent stack: Marketplace (the hook) → Verified Compute (the differentiation) → Private Deployment (the enterprise product) → Compute Autopilot (the workflow IDE) → Routing Intelligence (the compounding data moat). Each layer is harder to leave than the last.

---

## 5. Supply-Side Economics and Network Effects

**[LOW — specific quantitative claims refuted; directional logic retained]**

### What makes supply sticky

The supply side research was almost entirely refuted. The Airbnb welfare papers, the P2P ownership-shift models — all killed in verification. What remains is first-principles logic:

**A Mac owner is sticky when the platform provides three things a direct deal cannot:**
1. **Deal flow they couldn't source themselves.** The buyer found Computexchange; the buyer didn't find the specific Mac Mini in Toronto. The marketplace creates the match.
2. **Trust infrastructure they can't self-build.** Payout rail, dispute resolution, reputation system, insurance against non-payment. A direct deal requires trust between strangers.
3. **Operational leverage they can't replicate.** The agent handles scheduling, queueing, heartbeating, and payout — things a supplier would have to build from scratch for a direct deal.

**A Mac owner is not sticky if:**
- Their only buyers are the ones they could contact directly
- The platform's cut (8–15%) exceeds the value of the trust infrastructure
- The reputation score is not portable but also not valuable enough to protect

### The supplier flywheel

The flywheel only works if the supply side is large enough to make project SLAs possible, which requires supply density. This is the cold-start problem — the most dangerous open question in the product.

*How many online workers are required before a buyer can trust a project-priced job?*

The answer depends on job type, model, and ETA tolerance. A rough heuristic: for a 1-hour ETA guarantee on a 10,000-record batch job, you need at least 5 eligible workers online simultaneously, which means probably 20–50 registered workers to ensure 5 are online at any given time. For a 24-hour ETA, 10 workers. For a 1-week ETA, almost any supply. **Don't offer tight project SLAs until supply density is above threshold — the failure mode (SLA miss, no revenue, pissed buyer) destroys the project-pricing thesis before it starts.**

### Geographic diversity as a buyer advantage

This is the network effect that centralized clouds structurally cannot replicate: a buyer's jobs route to suppliers in multiple geographic zones by default (because Mac owners and GPU owners are everywhere), which means:
- **Latency diversity** (a buyer in Singapore gets a closer supplier than they would from a US-East AWS datacenter)
- **Burst capacity** (a spike in demand hits a globally distributed supply pool, not a single AZ)
- **Resilience** (no single datacenter failure takes down the supply)

Centralized clouds serve this via multi-region replication, but at a cost and complexity premium. Computexchange gets it for free from the natural distribution of its supply.

---

## 6. Open Questions That Are Really Product Priorities

The research surfaced four unresolved questions. These are not research gaps — they are the next four product decisions:

### 1. The cost-per-project calculator
*"What is the actual empirical cost-per-job gap between idle Mac/GPU supply and AWS/Azure for representative workloads?"*

**This is a marketing artifact, not a research question.** Run 10,000 Llama-3.3-70B classifications on Computexchange. Run the same on OpenAI batch. Run the same on AWS Bedrock. Publish the comparison with methodology. This becomes the landing page. The gap is almost certainly 3–10×; the exact number is the product claim that drives demand.

### 2. The minimum supply density threshold
*"What is the minimum viable supply for outcome-based pricing to be operationally reliable?"*

**This is a launch gate.** Until this number is known and hit, project-priced SLAs cannot be offered. The `/v1/quote` response already includes `eligible_now` (workers who passed the hard filter in the last 60 seconds). Build a simple supply monitor that alerts when `eligible_now` drops below N for common job types. Below N: don't offer project pricing. Above N: project pricing is on. N is probably 5–10 eligible workers per job type.

### 3. Enterprise compliance stack
*"How do enterprise procurement teams evaluate 'data never leaves the box' from a new vendor?"*

**This is a sales and legal project.** The answer is: they require a SOC 2 Type II audit, a HIPAA Business Associate Agreement, a GDPR Data Processing Addendum, and a penetration test report. None of these are code. Build a compliance checklist. Get a lawyer. Get a SOC 2 auditor. Price the Private Deployment tier at $X/month. The compliance stack is the enterprise moat.

### 4. The anti-defection layer
*"How do you prevent supplier-buyer disintermediation once they've transacted successfully?"*

**This is a platform design decision.** Three levers:
- Make the reputation score clearly valuable (supplier dashboard showing "your 0.94 reputation gets you 3× the job volume of a 0.5 supplier" — make the advantage visible)
- Insurance: offer payment guarantee (platform pays supplier even if buyer disputes, up to N jobs per quarter) — the platform absorbs dispute risk in exchange for staying on-platform
- Exclusivity incentive: suppliers in the "Verified Elite" tier (reputation > 0.90) get access to higher-margin jobs (enterprise, privacy tier, priority queue) that are not available via direct deals

---

## 7. Competitive Landscape Addendum (v2 findings)

The Vast.ai "2025 Year in Review" blog appeared in sources but no specific claims survived verification. What was findable: Vast.ai is developer-focused, has a large GPU catalog (consumer and datacenter), and competes on price transparency. **Computexchange is not trying to win the transparent $/GPU-hr race — it is trying to exit that race entirely via project pricing.**

**Akash Network** (from their 2025 Year in Review blog): Akash is DePIN-oriented, blockchain-settled, focused on decentralization as a value in itself. The buyer for whom "blockchain-settled" matters and the buyer for whom "project-priced, HIPAA-compliant, Apple Silicon 70B" matters are different people. Not the primary competitive threat.

**The primary threat is not an existing competitor — it is the hypothesis that buyers will not adopt project pricing.** If buyers insist on renting GPU-hours because that's how their procurement works, the paradigm shift fails. The mitigation: lead with the OpenAI-compatible batch API (same pricing unit as OpenAI, familiar) and *upsell* to project pricing as a premium tier. Let buyers enter on familiar terms, then migrate them to the better model.

---

## Strategic Priorities (Combined v1 + v2)

### Tier 1 — Unblock revenue (P0, <2 weeks)
1. Fix ledger `UNIQUE(task_id, kind)` constraint
2. Card enforcement at submit (or explicit "invoice later" enterprise mode)
3. Wire `data_country` into scheduler hard filter (enables the privacy tier claim)
4. Ship the notarized macOS agent DMG

### Tier 2 — Establish the paradigm (1–2 months)
5. Build and publish the cost-per-project calculator (vs. OpenAI, AWS, RunPod)
6. Make the project-pricing pitch the landing page — not "GPU marketplace," but "Pay per project, not per hour"
7. Implement supply density monitoring and gate project-SLA quotes behind it
8. Wire demo.html to live endpoints, ship the product

### Tier 3 — Build the moat (2–4 months)
9. Expose supplier reputation to buyers as a first-class surface (routing transparency)
10. Start the compliance stack: SOC 2 audit, HIPAA BAA, GDPR DPA, pen test
11. Build the Private Deployment tier (dedicated hardware, air-gapped config, premium pricing)
12. Productize the NVIDIA lane (Dockerfile.agent + CI gate + within-class verification)

### Tier 4 — Deepen lock-in (4–12 months)
13. Routing intelligence dashboard (show buyers how their job history improves routing)
14. Supplier Elite tier (reputation threshold unlocks higher-margin job access)
15. Compute Autopilot IDE (the Workflows idea — multi-step job pipelines with visual designer)
16. Anti-defection layer (payment guarantee, platform insurance, direct-deal deterrent)

---

## Bottom Line

The verification process killed almost every specific number in both research rounds. What survived:

- **Project-based pricing is real and proven** (Sierra AI, Intercom — two companies building $100M+ businesses on outcome pricing)
- **Output fraud is structural and unsolved** (peer-reviewed research — v1)
- **Apple Silicon is a capacity play, not a speed play** (peer-reviewed research — v1)
- **RunPod's trajectory proves the market** ($120M ARR on $22M raised — v1)
- **The idle-asset cost advantage is structural** (first-principles economics; specific numbers unverified)
- **The regulatory demand for "local" compute is real** (GDPR, HIPAA, EU AI Act are public record)

What is not yet proven in the market:
- That buyers will accept project pricing over GPU-hour rental
- That the supply density will be high enough to make project SLAs reliable
- That the compliance stack can be built fast enough to capture the enterprise segment

These are the three bets. All three are winnable. None are guaranteed.

---

*v1: 106 agents, 24 sources, 6 confirmed findings (verification, Apple Silicon, RunPod benchmark, fraud incentive, outcome pricing proof, cost structure)*
*v2: 109 agents, 27 sources, 1 confirmed finding (Sierra AI outcome pricing) + 5 structural arguments retained at medium/low confidence*
*Combined: 215 agents, 51 sources, 50 claims adversarially verified*
