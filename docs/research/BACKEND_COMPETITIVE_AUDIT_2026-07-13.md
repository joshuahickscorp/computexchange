# Backend competitive and cost-position audit — 2026-07-13

Date: 2026-07-13

Status: internal competitive research; not customer-facing price or performance proof

Sources: official provider pages and documentation, accessed 2026-07-13 unless a publication date is stated

## Bottom line

ComputeExchange should not compete on a blanket "cheapest GPU," "instant serverless," or "fastest inference" claim. RunPod and Vast can be very aggressive on raw GPU-hour price; Modal and RunPod have mature scale-to-zero controls; Lambda and the other GPU providers offer established multi-GPU capacity; Together, Fireworks, and Groq sell low-operations token inference.

The defensible current position is narrower:

> ComputeExchange foregrounds a bound quote, a checked work result, and a settlement receipt on explicitly admitted runtime cells. Its currently advertised production projection is Apple Metal; CUDA and physical multi-machine capacity remain separately gated.

Apple execution is a real point of differentiation among the providers reviewed: their public catalogs expose NVIDIA, AMD, or proprietary LPU capacity, not Apple Metal. That supports "Apple Metal is absent from the audited public catalogs as of 2026-07-13," not "ComputeExchange is the only Apple compute provider."

The commercial comparison must be **cost per verified completed workload at a stated quality and deadline**, not GPU-hour versus token sticker prices.

## Current ComputeExchange evidence boundary

### Current and repository-proven

- The generated [runtime matrix](../RUNTIME_MATRIX.md) advertises seven exact Candle/Metal cells: Llama 1B classification, extraction, and inference; MiniLM embedding and reranking; and Whisper tiny/base transcription.
- The control plane has quote, verification, receipt, and settlement machinery with local tests. This supports describing the product contract and implementation, but does not by itself prove live-market liquidity, live-money correctness, or a customer cost win.
- Apple M3 Pro Metal and rented A100 CUDA measurements exist in [GPU capability research](../GPU_CAPABILITY.md). They are platform-specific measurements, not yet an apples-to-apples competitive benchmark.

### Pending and not externally claimable

- Candle/CUDA cells are `hardware_pending`; vLLM/CUDA is `soak_only`; neither is currently advertised as production.
- Two-device Apple proof, two-or-more-worker CUDA proof, and a 24-hour physical lane soak are `external_pending` in the [5x5 gates](../../proof/5x5-gates.json).
- The competitive price position is `planned`; apples-to-apples benchmarks and a named buyer-workload win are `external_pending`.
- A physical fleet must not be inferred from the mock harness; see the [fleet-proof boundary](../fleet-proof/README.md).
- Priority, reserved capacity, gang scheduling, multi-machine deadlines, and an availability SLA are not current guarantees.

## Official-source market snapshot

Prices below are public USD list-price snapshots. Discounts, taxes, region, storage, bandwidth, CPU/RAM, and commitment terms can change the effective cost.

| Provider | Unit and current anchors | Billing/cold-start boundary | Customer trade-off |
|---|---|---|---|
| **RunPod** | Pods bill per second. The public page displayed RTX 4090 at $0.69/h, A100 80 GB at $1.39–$1.49/h, and H100 at $2.89–$3.19/h. Serverless ranged from $0.58 to $9.98/h, including A100 at $2.72/h and H100 at $4.55/h. [Pricing](https://www.runpod.io/pricing), [Pod billing](https://docs.runpod.io/pods/pricing) | Serverless bills initialization/model load, execution, and the idle timeout from worker start to stop, rounded to the second. Flex can scale to zero; Active workers remain warm and bill continuously. RunPod's "sub-200ms" FlashBoot statement is a vendor claim, not a universal workload guarantee. [Billing](https://docs.runpod.io/serverless/pricing), [workers](https://docs.runpod.io/serverless/workers/overview), [Serverless page, updated July 2026](https://www.runpod.io/product/serverless) | Strong commodity-GPU price and mature operational surface. Multi-GPU workers and clusters are available. Public hardware includes NVIDIA and AMD MI300X, not Apple Metal. [GPU configuration](https://docs.runpod.io/flash/configuration/gpu-types), [clusters](https://www.runpod.io/product/clusters), [hardware](https://docs.runpod.io/references/gpu-types) |
| **Vast.ai** | A dynamic host marketplace bills compute, storage, and bandwidth separately by the second. A 2026-07-13 homepage snapshot showed RTX 4090 around $0.35/h, H200 $3.53/h, and B200 $4.95/h; these are momentary offers, not stable list prices. Reserved pricing can discount up to 50%; interruptible bids are marketed as commonly 50%+ cheaper. [Marketplace](https://vast.ai/), [pricing model](https://docs.vast.ai/guides/instances/pricing) | Serverless uses the underlying marketplace rate. Ready and Loading workers incur GPU charges; inactive cold workers can still incur storage/bandwidth. Initial deployment is documented as typically 3–5 minutes, with larger models potentially longer. [Serverless pricing](https://docs.vast.ai/guides/serverless/pricing), [quickstart](https://docs.vast.ai/guides/serverless/quickstart), [scaling](https://docs.vast.ai/guides/serverless/managing-scale) | Excellent spot-market economics when interruption and host variance are acceptable. On-demand/reserved are high priority; interruptible work can pause when outbid. Multi-GPU and NVLink filters exist. Public hosting is Linux with NVIDIA or supported AMD, not Apple Metal. [Instance types](https://docs.vast.ai/guides/instances/choosing/instance-types), [hosting](https://docs.vast.ai/host/hosting-overview), [June 9, 2026 update](https://vast.ai/article/june-2026-product-update) |
| **Lambda Cloud** | Per-GPU-hour pricing billed in one-minute increments, with no egress fee. Examples: single H100 SXM $4.29/h, H100 PCIe $3.29/h, B200 $6.99/h, A100 40 GB $1.99/h; an 8x H100 node is $3.99/GPU-h. [Pricing](https://lambda.ai/pricing), [billing](https://docs.lambda.ai/public-cloud/billing/) | This is running VM/cluster capacity, not scale-to-zero serverless. Billing continues while an instance runs regardless of utilization. Cluster reservations bill in weekly increments. | Strong turnkey CUDA and dedicated multi-GPU clusters; less attractive for bursty work with substantial idle time. Public Cloud offers 1/2/4/8-GPU VMs, while 1-Click Cluster documentation covers 16–512 GPUs. [On-demand](https://docs.lambda.ai/public-cloud/on-demand/), [clusters](https://docs.lambda.ai/public-cloud/1-click-clusters/) |
| **Modal** | Per-second GPU, CPU, and RAM billing. GPU-only equivalents include H100 $3.9492/h, H200 $4.5396/h, B200 $6.2496/h, A100 80 GB $2.4984/h, and L4 $0.7992/h. [Pricing](https://modal.com/pricing), [billing](https://modal.com/docs/guide/billing) | Scale-to-zero is the default, but warm containers and the scaledown window are billable. Container boot may be around a second while dependency/model warming can take seconds or minutes. `min_containers` and `buffer_containers` exchange cost for latency. [Cold starts](https://modal.com/docs/guide/cold-start) | Developer-friendly serverless and up to eight GPUs on one machine. Multi-node training is beta, up to 64 devices. GPU functions are preemptible by default; the non-preemptible option does not support GPUs. NVIDIA only. [GPU guide](https://modal.com/docs/guide/gpu), [multi-node](https://modal.com/docs/guide/multi-node-training), [preemption](https://modal.com/docs/guide/preemption) |
| **Together AI** | Serverless is per million input/cached/output tokens, not GPU time. GPT-OSS 120B was $0.15 input/$0.60 output; GPT-OSS 20B $0.05/$0.20. Batch can discount up to 50%. Dedicated endpoints bill per minute. [Pricing](https://www.together.ai/pricing), [serverless](https://docs.together.ai/docs/serverless/models), [inference billing](https://docs.together.ai/docs/inference/pricing) | Serverless has no provisioning minimum but is rate- and capacity-managed. Dedicated endpoints support minimum/maximum replicas and default shutdown after 60 minutes of inactivity; running replicas continue billing until shutdown. [Rate limits](https://docs.together.ai/docs/serverless/rate-limits), [endpoint settings](https://docs.together.ai/docs/dedicated-endpoints/settings) | Low-operations inference and dedicated 1/2/4/8-GPU replicas. Official pages disagreed during this audit on the current dedicated H100 rate, approximately $5.40 versus $5.49/GPU-h. Any external comparison needs a dated captured price rather than an undated number. |
| **Fireworks AI** | Serverless is token-priced. GPT-OSS 120B was $0.15 input/$0.015 cached/$0.60 output; GPT-OSS 20B $0.07/$0.035/$0.30. Batch is 50% lower; Priority costs more. On-demand GPU-second rates were H100/H200 $7/h, B200 $10/h, and B300 $12/h. [Serverless pricing](https://docs.fireworks.ai/serverless/pricing), [pricing](https://fireworks.ai/pricing) | Managed serverless hides GPU sizing. Dedicated deployments default to scale to zero after one idle hour; requests during scale-up can receive an immediate 503 and require retry. Active GPU time bills even without requests. [Serverless overview](https://docs.fireworks.ai/serverless/overview), [on-demand](https://docs.fireworks.ai/guides/ondemand-deployments), [billing/scaling](https://docs.fireworks.ai/faq/deployment/ondemand/billing-scaling) | Strong managed inference, priority traffic, and multi-GPU replicas. It competes on model throughput and low operations, not Apple or arbitrary runtime execution. |
| **Groq** | Managed LPU inference is token-priced. GPT-OSS 20B was $0.075 input/$0.30 output, GPT-OSS 120B $0.15/$0.60, and Llama 3.1 8B $0.05/$0.08. Batch is 50% cheaper. [Pricing](https://groq.com/pricing), [batch](https://console.groq.com/docs/batch) | Customers do not manage containers or GPUs. On-demand can queue; Performance is the higher-reliability enterprise tier; Flex offers higher limits at the same token price but may reject work for capacity. [Service tiers](https://console.groq.com/docs/service-tiers), [Flex](https://console.groq.com/docs/flex-processing) | A strong latency-oriented managed-inference substitute, but not a general CUDA/Metal runtime. Official throughput figures varied between pricing and model pages and should be treated as vendor claims, not independent benchmark facts. |

## Customer trade-offs

| Customer need | Strongest current substitute | ComputeExchange opportunity | Honest limitation today |
|---|---|---|---|
| Cheapest fungible GPU-hour | Vast interruptible or low-cost marketplace offers; RunPod Pods | Do not fight on the sticker rate alone. Sell a bounded, verified deliverable when verification and operator effort matter. | No measured total-cost win yet. Verification and fallback can add work. |
| Low-operations token inference | Together, Fireworks, Groq, or Modal | Private batch, Apple execution, runtime control, and receipt-bound completed work. | Managed APIs have broader catalogs and mature elastic capacity; token prices are not directly comparable to GPU-hours. |
| Dedicated CUDA or multi-node training | Lambda, RunPod clusters, Modal beta multi-node, or direct reservations | Later, admit separately proven CUDA and reserved multi-machine cells. | CUDA is not advertised; no current cluster, priority, or availability guarantee. |
| Bursty scale-to-zero work | RunPod Flex, Modal, Vast Serverless | Quote the whole completed job and expose the evidence rather than forcing the buyer to reason about warm pools. | ComputeExchange must measure its own queue, provisioning, transfer, and cold/warm tails before claiming an advantage. |
| Apple/unified-memory workload | Local Apple hardware; no audited provider offers a public Metal execution catalog | Production-admitted Metal cells and a future supply market for otherwise idle Apple capacity. | Current proof is a bounded runtime projection, not demonstrated multi-Apple liquidity or a capacity SLA. |
| Fully local owned hardware | Local MLX/Ollama or a customer's own scheduler | Reduce scheduling, verification, and receipt/settlement operations for distributed work. | Local execution may have near-zero incremental provider cost and avoids marketplace fees. Operator time must be measured, not assumed. |

## Recommended competitive benchmark

The release metric should be:

```text
cost_per_verified_workload =
  (compute + token charges + reserved idle + storage + bandwidth
   + retries + fallback + verification + required operator time)
  / verified_outputs_delivered_within_the_declared_quality_and_deadline
```

Report this with p50 and p95 end-to-end completion time, success rate, and quality. A failed, incomplete, late, or unverifiable output is not a cheap output.

### Benchmark protocol

1. Name one buyer-shaped wedge workload. Freeze the exact model/weights, quantization, engine/version, input corpus, output contract, quality threshold, concurrency, privacy boundary, region, and deadline in a manifest.
2. Compare the current advertised ComputeExchange Metal cell against the strongest compatible alternatives: current RunPod/Vast/Modal GPU execution, a managed token provider where semantically compatible, OpenAI Batch as required by the proof gate, and local MLX/Ollama on declared hardware.
3. Measure from buyer submission to verified deliverable: upload, queue, provisioning, container/model load, compute, output transfer, retry, fallback, verification, publication, and cleanup. Also report provider-billed units and idle retention separately.
4. Run cold/scale-zero and warm/steady-state cohorts separately. Use enough repetitions to publish distributions and failures rather than the best run.
5. Hold output quality constant. Provider-specific caches, batch tiers, priority tiers, discounts, and rate limits must be named; a lower-quality or differently quantized result is a separate cell.
6. Timestamp and archive every price and configuration. Vast is dynamically priced and Together's official dedicated prices were inconsistent during this audit.
7. Publish a win only if ComputeExchange improves a declared customer outcome—total cost, p95 completion, privacy, memory fit, or operator burden—while quality is held constant. Narrow or withdraw the claim otherwise.

Raw `$ / GPU-hour`, vendor tokens/second, and isolated kernel throughput can accompany the benchmark as diagnostics; none is the product verdict.

## Future priority and multi-machine recommendation

Treat priority and multi-machine execution as a **future gated capacity-reservation product**, not a scheduler flag or current guarantee.

A buyer would request a deadline, capacity class, compatible runtime cell, reservation window, and spend ceiling. ComputeExchange would return a bound reservation component plus an executed-and-verified-work component. The quote must state whether replacement capacity is included and what happens if the reservation cannot be fulfilled.

Before advertising this product, separately prove Apple and CUDA cohorts with:

- independently addressed physical workers and non-overcommitted capacity;
- atomic reservation/admission and, where required, gang scheduling;
- worker loss, replacement, restart, OOM, corrupt-output, cancellation, and deadline behavior;
- per-worker identity, work, verification, artifact, and settlement receipts;
- bounded retry/fallback cost, reservation expiry, teardown, and reconciliation;
- truthful queue and availability reporting; and
- a sustained physical soak for every promoted cell.

Until those gates close, customer copy should say that multi-machine and reserved-priority capacity are planned and evidence-gated. Customers requiring guaranteed CUDA capacity or large synchronous clusters today should be directed to established dedicated/cluster providers rather than given an implied ComputeExchange SLA.

## Claim guardrails

Safe now:

- "The advertised production projection currently consists of explicit Apple Metal runtime cells."
- "ComputeExchange binds quotes, checked results, and receipts in its work contract," provided this is framed as implemented product behavior rather than proof of live-market scale.
- "The public catalogs audited on 2026-07-13 did not expose Apple Metal execution."
- "CUDA and multi-machine lanes are separately gated and not currently advertised."

Do not claim yet:

- cheapest, fastest, no cold starts, only pay for useful execution, or better than a named provider;
- production-ready portability across Apple and CUDA;
- live multi-Apple or multi-CUDA capacity, priority scheduling, reservation fulfillment, or an SLA;
- superior multi-GPU scaling; or
- uniqueness of verification. The official competitor billing surfaces reviewed do not foreground the same quote/result/settlement contract, but absence from public documentation is not proof that a provider performs no internal checking.

External pricing or performance copy must point to a reproducible, dated benchmark artifact. This audit is the comparison design and market snapshot, not that proof.

<!-- CLAIM-SCOPE: internal-engineering-non-authoritative -->
