# Quickstart

Run your first job on computexchange in under a minute. Get a buyer key from Settings · API keys, then pick the path that fits: a raw `curl` against the native `/v1/jobs` API, the Python SDK, or the `cx` CLI. Every path runs the same job against the same control plane — an `embed` job over three short strings — and reads back the merged result. Swap `cx_live_…` for your real key.

Every example below submits a native job, then polls until it's done. There is no separate synchronous `/v1/embeddings` endpoint — `/v1/embeddings` and `/v1/chat/completions` are OpenAI-Batch-API endpoint *labels* you route to inside a batch file (see `docs/RUNBOOKS.md` / `control/openai.go` for that flow); for a first job, `POST /v1/jobs` is the direct, simplest path.

## curl

```bash
# submit an embed job for three rows, then poll for the result.
JOB=$(curl -s https://computexchange.net/v1/jobs \
  -H "Authorization: Bearer cx_live_…" \
  -H "Content-Type: application/json" \
  -d '{
        "job_type": {"type": "embed"},
        "model": {"kind": "gguf", "ref": "all-minilm-l6-v2"},
        "tier": "batch",
        "verification": {"redundancy_frac": 0, "honeypot_frac": 0, "payout_hold_secs": 0},
        "input": "{\"id\":\"a\",\"text\":\"first row\"}\n{\"id\":\"b\",\"text\":\"second row\"}\n{\"id\":\"c\",\"text\":\"third row\"}\n"
      }' | python3 -c 'import sys,json; print(json.load(sys.stdin)["job_id"])')

# poll until complete (a real job usually finishes in a few seconds on a warm worker)
curl -s "https://computexchange.net/v1/jobs/$JOB" \
  -H "Authorization: Bearer cx_live_…"

# once status is "complete", fetch the merged result
curl -s "https://computexchange.net/v1/jobs/$JOB/results" \
  -H "Authorization: Bearer cx_live_…"
```

## Python

```python
# The SDK is stdlib-only (no `requests` dependency) and not yet on PyPI — for
# now, install it straight from the repo: pip install ./sdk/python
# (docs/CREED_AND_PATH_TO_TEN.md tracks the PyPI/Homebrew publish as a
# separate, in-progress piece of work.)
from computeexchange import Client

# base_url defaults to http://localhost:8080 — pass the real host explicitly,
# or a bare Client(api_key=...) call will silently try to reach localhost.
cx = Client("https://computexchange.net", "cx_live_…")

job = cx.submit_job(
    model="all-minilm-l6-v2",
    job_type="embed",
    input='{"id":"a","text":"first row"}\n{"id":"b","text":"second row"}\n{"id":"c","text":"third row"}\n',
)
cx.wait(job["job_id"])
print(cx.results_text(job["job_id"]))

# OpenAI-shaped convenience: submit → wait → fetch in one call. Returns a
# plain dict (not an object) — index it like JSON, not attribute access.
out = cx.embeddings("all-minilm-l6-v2", ["first row", "second row", "third row"])
print(out["data"][0]["embedding"][:5])
```

## cx CLI

```bash
# The CLI is a single stdlib-only Go binary, not yet published to a tap — for
# now, build it from the repo: (cd cli && go build -o cx .)
export CX_API_URL=https://computexchange.net
export CX_API_KEY=cx_live_…

# --wait polls to completion and prints the merged result for you.
cx submit --model all-minilm-l6-v2 --type embed --input rows.jsonl --wait
```

Where `rows.jsonl` is:

```
{"id":"a","text":"first row"}
{"id":"b","text":"second row"}
{"id":"c","text":"third row"}
```

Without `--wait`, `cx submit` prints a job id you can follow up on yourself:

```bash
JOB=$(cx submit --model all-minilm-l6-v2 --type embed --input rows.jsonl)
cx status "$JOB"
cx results "$JOB"
```

Run `cx -h` (or see the header comment in `cli/main.go`) for the full command list — `submit`, `quote`, `status`, `results`, `invoice`, `events`, `failures`, `models`, `estimate`, `explain-scheduler`, `cancel`, `private-pool`.

### Private pool (a real premium, not just a sentence)

By default your job can run on any eligible supplier on the exchange. If you
need your data to stay on a fixed, named set of suppliers you have personally
vetted — never the shared pool — bind them to your own private pool first:

```bash
# add a supplier (you get their id from a prior job's worker_id, or from a
# direct relationship with that supplier)
cx private-pool add <supplier_id>

# see who's actually in your pool right now
cx private-pool list

# take someone back out
cx private-pool remove <supplier_id>
```

Then price and submit with `--private-pool`. The quote shows the real premium
(25% of the expected cost, already folded into the min/expected/max figures —
never a separate number you have to remember to add) and the exact written
guarantee of what "private" means:

```bash
cx quote  --model all-minilm-l6-v2 --type embed --input rows.jsonl --private-pool
cx submit --model all-minilm-l6-v2 --type embed --input rows.jsonl --private-pool --wait
```

The guarantee, verbatim (also returned as `private_pool_attestation` on the
quote): tasks are claimable ONLY by suppliers you've explicitly bound —
enforced by the control plane's dispatch filter, not merely a stated policy —
no other supplier on the exchange can ever claim a task from that job. It
guarantees WHO runs your work; it does not by itself claim encryption-at-rest
or network isolation beyond that supplier selection.

Submitting `--private-pool` with zero bound suppliers is refused at submit
time (400) — the job could never be claimed by anyone, so the platform tells
you that up front instead of leaving the job stuck at 0% with no explanation.
