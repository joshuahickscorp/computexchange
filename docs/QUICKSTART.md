# Quickstart

Run your first job on computexchange in under a minute. Get a buyer key from Settings · API keys, then pick the path that fits: a raw `curl` against the `/v1` API, the Python SDK, or the `cx` CLI. Every path runs the same job against the same control plane · the embeddings below land in a verified run you can open from the Runs list. Swap `cx_live_…` for your real key.

## curl

```bash
# embed three strings · same-origin /v1 API
curl https://compute.exchange/v1/embeddings \
  -H "Authorization: Bearer cx_live_…" \
  -H "Content-Type: application/json" \
  -d '{"model":"all-minilm-l6-v2",
       "input":["first row","second row","third row"]}'
```

## Python

```python
# pip install computeexchange
from computeexchange import Client

client = Client(api_key="cx_live_…")
res = client.embeddings(
    model="all-minilm-l6-v2",
    input=["first row", "second row", "third row"],
)
print(res.data[0].embedding[:5])
```

## cx CLI

```bash
# brew install computexchange/tap/cx
export CX_API_KEY=cx_live_…
cx embed --model all-minilm-l6-v2 rows.txt
cx jobs watch   # follow it to done
```
