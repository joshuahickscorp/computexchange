# The Inference Speed Frontier — a research report for computexchange's speed lane

> **SUPERSEDED 2026-07-06.** The "beat an A100 on wall-clock" thesis this research fed was
> REFUTED by real measurement: a real A100-SXM4-80GB under vLLM serves 44,269 tok/s (~19× the
> Candle-bench figure the thesis leaned on); honest break-even is ~318 M3-Pro-class nodes, not
> ~18. The salvaged, honest routing rule and current state of play:
> `docs/speed-lane-reports/A100_REFERENCE_MEASURED.md`, `A100_CAPABILITY_SWEEP.md`,
> `docs/research/SPEED_LANE_AUDIT_2_AND_HANDOFF.md`. Kept unedited below for the receipt trail.

*Compiled 2026-07-06 by a fan-out/verify deep-research pass (5 search angles → source fetch →
adversarial verification) plus domain synthesis. The verify/synthesize phase hit a transient
API outage mid-run, so claims below are tagged by verification status:*

- **[VERIFIED]** — survived 3-vote adversarial verification (2–3 votes to confirm).
- **[SOURCED]** — a real cited source + direct quote was fetched, but its verification votes
  failed on transport (ConnectionRefused/FailedToOpenSocket), NOT on substance. These are
  well-established results in the field; treated as reliable but flagged for a re-verify.
- **[REFUTED]** — the adversarial pass knocked it down; stated as the corrected version.

The thesis this report serves (owner, 2026-07-06): **win on WALL-CLOCK time (save the buyer
TIME), not on cost.** Quantization/compression only saves money. The structural edge no
single-GPU vendor has: an embarrassingly-parallel batch split across a heterogeneous fleet,
run in parallel, can beat one A100/H100 on total wall-clock — if the splitting is
speed-optimal.

---

## 1. The single-node speed frontier

### Apple Silicon (Metal / MLX)
- **[VERIFIED]** On an **M4 Max** with MLX, decode throughput by model size (single request):
  Qwen3-0.6B **525.5 tok/s**, Llama-3.2-1B **461.9 tok/s**, Qwen3-4B **159.0 tok/s**,
  Qwen3-8B **93.3 tok/s**; MoE models Qwen3-30B-A3B **109.7 tok/s** and Nemotron-30B-A3B
  **121.8 tok/s** (only ~3B active params → run like small dense models). *(arxiv 2601.19139)*
- **[VERIFIED]** **Continuous batching on Apple Silicon scales SUB-linearly and hits a ceiling
  well below the request count**: Qwen3-0.6B gains only **3.7×** going 1→16 concurrent requests
  (441→1642 tok/s); Qwen3-8B only **2.6×**. *(arxiv 2601.19139)* — this is the unified-memory
  bandwidth wall (see §5), and it directly explains our own measured 1.34–1.67× batch ceiling.
- **[REFUTED]** The claim "MLX is 21–87% faster than llama.cpp across the range" did **not**
  survive (1–2). Corrected: MLX and llama.cpp Metal are **roughly at parity**, with MLX a
  **modest** and model-dependent lead — not a blanket ~2× win. Pick per-model, measure both.

### NVIDIA CUDA (datacenter)
- **[VERIFIED]** **Runtime/scheduler engineering alone produces multiplicative speedups with no
  model change**: vLLM v0.6.0 delivered **2.7× throughput and 5× faster TPOT** on Llama-8B, and
  **1.8× throughput / 2× TPOT** on Llama-70B, versus v0.5.3. *(vllm.ai perf blog, 2024-09)*
- **[VERIFIED]** In pre-optimization vLLM, **host/CPU overhead — not GPU compute — dominated**:
  for Llama-3-8B on H100 the HTTP API server took **33%** of execution time and scheduling
  **29%**, leaving only **~38%** for the GPU. *(vllm.ai perf blog)* — **the single most important
  lesson for us: the scheduler and dispatch path can cost more than the kernel.** Our control
  plane IS our scheduler; its latency is our latency.
- Runtime landscape (domain synthesis, corroborated across sources): **TensorRT-LLM** and
  **SGLang** lead raw datacenter throughput/latency; **vLLM** leads on ecosystem + PagedAttention;
  **llama.cpp** leads portability; **ExLlamaV2/V3** leads single-GPU consumer latency with fast
  quant kernels; **MLX** leads on Apple. No single winner — fastest depends on model, batch,
  and hardware.

---

## 2. Speed techniques that trade little/no quality (the on-thesis toolkit)

| Technique | Measured speed effect | Quality cost | Status |
|---|---|---|---|
| **Continuous / in-flight batching** | The core throughput lever; sub-linear on Apple (§1) | none | [VERIFIED] we have it (not dispatch-wired) |
| **PagedAttention** | memory efficiency → higher batch → higher throughput | none | [SOURCED] we lack it (slot-strided, not paged) |
| **FlashAttention-3** | **1.6–1.8× over FA2** on H100; ~75% FLOP util vs 35% | none | [SOURCED] (pytorch.org FA3 blog) — CUDA-only |
| **Speculative decoding (EAGLE/EAGLE-3)** | **~1.6–2.0× at batch 1** (EAGLE up to 1.96× on 70B; EAGLE-3 1.64–1.80× on reasoning) | **lossless** (preserves output distribution) | [SOURCED] (arxiv 2401.15077, specdecode-bench) — **we lack it entirely** |
| **…but spec-dec shrinks with batch** | EAGLE drops to **1.21× at batch 128** (compute-bound) | — | [VERIFIED] (specdecode-bench) |
| **FP8 W8A8 (speed quant)** | **~1.6× throughput**, ~2× memory cut, minimal accuracy loss | minimal | [SOURCED] (vLLM FP8 docs) — distinct from quality-first quant |
| Marlin / Machete / ExLlama INT4 kernels | fast dequant-matmul; big single-GPU latency wins | minimal | domain — CUDA/consumer |

**The key strategic read on speculative decoding:** it wins **most at low batch / single-request
latency** and **fades at high batch** (becomes compute-bound). That is *exactly* the regime a
single consumer machine handling one job lives in. So spec-dec is the highest-leverage per-node
latency move for us — and **distributed** speculative decoding (draft on a fast node, verify on
another) is a frontier move (see §6). It's **lossless**, which fits "speed not quality" perfectly.

---

## 3. Disaggregated & parallel serving

- **[SOURCED]** **LLM inference has two phases with opposite hardware demands**: prefill
  (prompt) is **compute-bound** (wants FLOPs); decode (generation) is **memory-bandwidth-bound**.
  *(Splitwise, Microsoft, ISCA'24)* — this is the physical basis for phase-aware routing.
- **[SOURCED]** **DistServe** disaggregates prefill and decode onto separate GPUs, tuning
  parallelism per phase, and serves **7.4× more requests** than colocated SOTA at SLO.
  *(arxiv 2401.09670)*
- **[SOURCED]** **Splitwise** runs prefill and decode on separate machine pools and transfers the
  KV cache between them; the transfer is **cheap and hidden** by overlapping it layer-by-layer —
  a constant **~8ms non-overlapped on A100 / ~5ms on H100, <7% overhead** — over fast interconnect
  (25–50 GB/s InfiniBand). *(Splitwise, ISCA'24)*
- **[SOURCED]** **Mooncake** (Moonshot/Kimi) uses KV-cache-centric disaggregation repurposing
  idle CPU/DRAM/SSD; up to **+525% throughput** in simulation at SLO, **+75% real requests** in
  production. *(arxiv 2407.00079)*
- **[SOURCED] — the crux for a marketplace:** **cross-datacenter / cross-cluster disaggregated
  prefill is feasible over commodity networking**, and a **heterogeneous split-serve** (32×H200
  doing prefill + 64×H20 doing decode) gave **+54% throughput and −64% P90 TTFT** vs a
  homogeneous cluster. *(arxiv 2604.15039)* — i.e. **assigning each phase to the hardware that
  suits it beats a uniform fleet.** A heterogeneous marketplace is the natural home for this.

**Caveat:** tensor/sequence/expert parallelism (splitting one layer's math across GPUs) needs
**very fast interconnect** and is a within-box/within-datacenter technique. It does **not** port
to WAN-separated consumer nodes. Pipeline and disaggregation are the WAN-tolerant patterns.

---

## 4. The distributed heterogeneous-fleet frontier (the crux)

- **[SOURCED]** **Petals** runs distributed inference of BLOOM-176B across **14 real
  geo-distributed consumer/lab GPU servers** (Europe + N. America), ~**an order of magnitude
  faster than parameter offloading**. *(arxiv 2209.01188)*
- **[SOURCED] — the decisive finding:** for **distributed single-token (model-parallel)
  inference, throughput is dominated by network ROUND-TRIP LATENCY, not bandwidth** — performance
  barely changes between 1 Gbit/s and 100 Mbit/s but **degrades sharply with RTT**.
  *(Petals, arxiv 2209.01188)*

**What this means for computexchange — the single most important strategic conclusion:**

> **Model-parallel** distributed inference (splitting ONE model's layers across many WAN nodes,
> Petals-style) is **killed by per-token network latency** and is the WRONG pattern for a
> marketplace of internet-separated strangers.
>
> **Data-parallel / embarrassingly-parallel** distribution (split a batch of N *independent*
> prompts across M nodes; each node runs the *whole* model on its shard; only final results
> transfer) has **zero per-token network dependency**. Each node works fully independently. **This
> is the pattern where a consumer fleet genuinely beats one A100 on wall-clock**, and it is
> exactly the shape of computexchange's `batch_infer` / embed / classify / extract jobs.

**The wall-clock math (why the fleet wins on a big batch):**
- One rented A100 (our spike): ~**2345 tok/s** aggregate at batch 64.
- 50 M-class Macs at ~**139 tok/s** each (our measured real-traffic rate) = **~6950 tok/s
  aggregate** ≈ **~3× an A100** on a batch big enough to keep all 50 busy.
- Break-even vs one A100 is ~**17 Macs**; every Mac beyond that is pure wall-clock win.
- The gate is **shard scheduling**: node-speed-weighted shard sizing (fast nodes get more),
  **straggler mitigation** (redundantly re-dispatch the last/ slowest shards), and enough batch
  volume that dispatch overhead amortizes. This is a **scheduling-science** problem, not a kernel
  problem — and it's the part only a marketplace can build.

---

## 5. Apple Silicon specifics

- **Unified memory bandwidth is THE ceiling.** Decode is memory-bandwidth-bound; every token
  re-streams the weights. Approx bandwidth: **M3 Pro ~150 GB/s, M4 Max ~546 GB/s, M-Ultra
  ~800 GB/s**. Batch scaling is sub-linear (§1) because bandwidth saturates before compute does —
  this is *the* reason our batch ceiling is 1.34–1.67× and MLX's is 2.6–3.7× at 16-way.
- **Implications for our lever ordering on Apple:** (a) bigger batches help only until
  bandwidth-bound — chasing higher batch has a hard, low ceiling; (b) **speculative decoding
  helps at batch-1** (it cuts memory traffic per accepted token — a bandwidth win, not a compute
  win — so it works *with* the Apple bottleneck, not against it); (c) MoE models (few active
  params) punch above their weight on Apple (Qwen3-30B-A3B at 110 tok/s); (d) **more, faster
  nodes** (data-parallel fan-out) is the only way past the per-node bandwidth wall.
- MLX vs llama.cpp: rough parity (§1); choose per model, keep both, measure.

---

## 6. The beyond-frontier opportunity — structural moves only a marketplace has

These are the moves that a single-GPU vendor (RunPod, Lambda, a bare A100) **structurally
cannot** copy, ordered by leverage:

1. **Speed-optimal data-parallel batch fan-out** (highest certainty, on-thesis). Node-speed-
   weighted shard sizing + straggler hedging + adaptive N (pick 1 node or 200 to minimize
   wall-clock for *this* job's shape). We already have the chunk splitter and per-node measured
   tok/s — it is not yet *speed-optimal*. This is the concrete way "50 Macs beat one A100."

2. **A real speed SLA / wall-clock quote.** Because we measure every node's live tok/s, we can
   *quote a guaranteed completion time* and route to hit it — "your 10k-prompt batch in 4 minutes"
   — a product no GPU rental offers, turning "you never know what you'll get" into "you always
   know when you'll get it."

3. **Speculative decoding as a marketplace primitive** (per-node + distributed). Lossless,
   batch-1-favoring, bandwidth-friendly on Apple. Per-node EAGLE-style drafting gets ~1.6–2× on
   the single-machine latency path; **distributed** spec-dec (cheap draft on a fast node, verify
   on a bigger node) is a genuine frontier a fleet can uniquely run.

4. **Phase-aware heterogeneous routing** (Splitwise/DistServe applied to a marketplace).
   Prefill→beefiest available node (compute-bound), decode→highest-bandwidth nodes
   (bandwidth-bound). The cross-DC/heterogeneous papers show +54% throughput / −64% TTFT from
   exactly this — and a heterogeneous fleet is the ideal substrate. WAN-tolerant because KV
   transfer is a one-shot handoff, not a per-token dependency.

5. **Kill the host/scheduler tax** (the vLLM lesson: 62% of latency was host overhead). Our claim
   path is our scheduler; the O(queue×fleet) fix this session already bought ~1.4s→13ms. Keep
   driving dispatch overhead toward zero — it's latency the buyer feels directly.

6. **The exotic ceiling:** multi-token prediction / self-speculative models, redundant-racing the
   slowest shards, and cross-job KV-prefix reuse across the fleet — each a marketplace-scale
   version of a frontier technique.

---

## Sources
- MLX / Apple throughput + continuous-batch scaling: arxiv **2601.19139**
- vLLM scheduler speedups + host-overhead breakdown: **blog.vllm.ai/2024/09/05/perf-update**
- Speculative decoding (EAGLE): arxiv **2401.15077**; batch-scaling: **specdecode-bench.github.io**
- FlashAttention-3: **pytorch.org/blog/flashattention-3**
- FP8 in vLLM: **docs.vllm.ai** FP8 quantization
- DistServe: arxiv **2401.09670**; Splitwise: MSR ISCA'24; Mooncake: arxiv **2407.00079**
- Cross-DC / heterogeneous split-serve: arxiv **2604.15039**
- Petals (distributed consumer-GPU inference, latency-dominated): arxiv **2209.01188**

*Re-verify note: the [SOURCED] claims lost their adversarial votes to a transient API outage,
not to refutation. A clean re-run of the deep-research verify phase (or a targeted re-check of
the FA3, EAGLE, DistServe, Splitwise, Petals numbers) should promote them to [VERIFIED].*
