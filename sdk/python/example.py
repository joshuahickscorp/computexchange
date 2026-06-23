#!/usr/bin/env python3
"""Submit an embed job, wait for it, and print the embedding dimensions.

Smoke mode (default, no server needed):

    python3 example.py --help
    python3 example.py --smoke        # builds the request, prints it, exits

Live mode (needs a running control plane + a buyer api key):

    CX_API_URL=http://localhost:8080 CX_API_KEY=... python3 example.py \\
        --model all-minilm-l6-v2 hello world

It uses the OpenAI-shaped ``Client.embeddings`` convenience, which submits the
embed job, waits to completion, and returns ``{"data":[{"embedding,index}],...}``.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from computeexchange import APIError, Client  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("texts", nargs="*", default=["hello world", "compute exchange"],
                   help="texts to embed (default: two sample strings)")
    p.add_argument("--model", default="all-minilm-l6-v2", help="embedding model id")
    p.add_argument("--base-url", default=os.environ.get("CX_API_URL", "http://localhost:8080"))
    p.add_argument("--api-key", default=os.environ.get("CX_API_KEY", ""))
    p.add_argument("--smoke", action="store_true",
                   help="build the request and print it; do NOT contact a server")
    args = p.parse_args(argv)
    texts = args.texts

    cx = Client(args.base_url, args.api_key)

    if args.smoke:
        # Prove the client + request shaping work without a live server: build
        # the exact submit body and print it. No network call is made.
        jsonl = "".join(json.dumps({"text": t}) + "\n" for t in texts)
        print("smoke mode — no server contacted")
        print(f"base_url = {cx.base_url}")
        print(f"would POST /v1/jobs with embed job over {len(texts)} record(s):")
        print(jsonl, end="")
        return 0

    if not args.api_key:
        print("set CX_API_KEY (and CX_API_URL) for live mode, or pass --smoke", file=sys.stderr)
        return 2

    try:
        out = cx.embeddings(args.model, texts)
    except (APIError, TimeoutError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    for d in out["data"]:
        print(f"[{d['index']}] dim={len(d['embedding'])} first3={d['embedding'][:3]}")
    print(f"model={out['model']} count={len(out['data'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
