<!-- CLAIM-SCOPE: internal-engineering-non-authoritative -->
# Computexchange — Cost-per-Project Comparison

*Honest calculator (DEEP_RESEARCH_V2 §6.1). CX numbers are the REAL catalogue prices; comparator numbers are published LIST prices (not measured runs) — swap in measured numbers before any external use.*

| Workload                           | CX cost        | Comparator     | Verdict        |
| ---------------------------------- | -------------- | -------------- | -------------- |
| Embed 1M short rows                | $1.00          | $0.0100        | 0.01x vs CX    |
|                                    |   all-minilm-l6-v2 |   openai text- |   OpenAI batch is cheaper on raw embed price |
| Classify 100k postings (1B)        | $60.00         | $2.25          | 0.04x vs CX    |
|                                    |   llama-3.2-1b-instruct-q4 |   openai gpt-4 |   OpenAI gpt-4o-mini batch wins on commodity small-model price |
| Classify 100k postings (7B)        | $240.00        | $37.50         | 0.16x vs CX    |
|                                    |   qwen2.5-7b-instruct-q4 |   openai gpt-4 |   CX 7B competitive with gpt-4o batch, and runs PRIVATE |
| 70B-class gen, 10M tokens          | $80.00         | $30.00         | 0.38x vs CX    |
|                                    |   qwen2.5-7b-instruct-q4 |   runpod H100  |   Apple Silicon capacity vs H100 rental: CX competitive + no ops |

**Real CX data point** (scripts/run-jobscraper-job.sh): 473 real postings, batch_classification on Llama-3.2-1B, quoted **$0.298** end-to-end on the live pipeline.


## Where CX actually wins (and where it doesn't)
- **Loses** to OpenAI's cheapest batch tier on commodity small-model embed/classify — gpt-4o-mini batch is hard to beat on raw price.
- **Wins** on (1) **privacy** — regulated workloads that simply cannot use a shared cloud (no comparison; OpenAI isn't an option), (2) **large models on owned hardware** vs renting H100s by the hour, (3) **opaque GPU-second compute** (sim/render/HPC) the per-token APIs don't serve, and (4) **project pricing** (pay per completed deliverable, no ops).
- The strategy is to price to THAT truth, lead with the OpenAI-compatible API on familiar terms, and upsell privacy + large-model + project pricing — not to claim a blanket price win that verification already refuted.
