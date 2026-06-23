# computeexchange — Python buyer SDK

A thin, **dependency-free** client for the Computexchange buyer REST API. Stdlib
`urllib` only — no `requests`, nothing to `pip install`. Drop the package on your
path (or copy `computeexchange/__init__.py` into your project).

```python
from computeexchange import Client

cx = Client("http://localhost:8080", api_key="<your buyer api key>")

# Low-level: submit, wait, fetch the merged JSONL artifact.
job = cx.submit_job(model="all-minilm-l6-v2", job_type="embed",
                    input='{"text":"hello"}\n{"text":"world"}\n')
cx.wait(job["job_id"])
print(cx.results_text(job["job_id"]))

# OpenAI-shaped convenience: submit -> wait -> reshape in one call.
out = cx.embeddings("all-minilm-l6-v2", ["hello", "world"])
print(out["data"][0]["embedding"][:3], out["model"])
```

**Methods:** `submit_job(...)`, `get_job(id)`, `results(id)`, `results_text(id)`,
`results_records(id)`, `cancel_job(id)`, `wait(id, timeout)`, `models()`,
`estimate(model, units, tier)`, and `embeddings(model, input)`.

Every non-2xx response raises `APIError` carrying the HTTP status and the
server's error body — failures are surfaced, never swallowed.

**Try it without a server:**

```bash
python3 -c "import sys; sys.path.insert(0,'.'); import computeexchange; print(computeexchange.__version__)"
python3 example.py --smoke        # builds + prints the request, no network
```

Job types: `embed`, `batch_infer`, `audio_transcribe`, `batch_classification`,
`json_extraction`, `rerank`. Variant params (`labels=`, `schema=`, `top_k=`,
`max_tokens=`, `temperature=`, `language=`, `timestamps=`) are passed as keyword
args to `submit_job` and folded into the tagged `job_type` only when given.
