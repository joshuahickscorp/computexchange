#!/usr/bin/env python3
#
# Computexchange — doc-as-test stand-in worker drainer.
#
# The MINIMAL worker loop (poll → PUT a valid embed result → commit), used ONLY by
# scripts/doc-as-test.sh's STANDALONE mode so the documented buyer commands (submit →
# poll → results) run through to a real, complete job on a GPU-less CI runner. It is
# the exact loop the Go integration suite's driveOneTask helper runs, in Python so
# doc-as-test.sh needs no extra Go build.
#
# HONESTY: this is a STAND-IN. It fakes only the VECTORS (unit vectors, not real
# Metal/Candle output); every byte of the buyer-facing API/SDK/CLI surface that
# doc-as-test.sh exercises is the real shipped code. prove-local's ATTACHED path runs
# the REAL agent and is the one that produces genuine embeddings. This drainer exists
# so the DOC COMMANDS can be proven valid without a GPU — not to fake inference.
#
# It is honeypot-aware exactly like driveOneTask: is_honeypot is never signalled over
# the wire, so a honeypot task is recognized by its input_url path and answered with a
# canned-but-valid embed doc; the control plane's verifier decides correctness. Since
# this drainer is the ONLY worker in standalone mode, it just needs to produce a
# well-formed embed artifact so the merge + results endpoints succeed.

import json
import os
import sys
import time
import urllib.error
import urllib.request

CONTROL = os.environ["CX_DRAIN_CONTROL_URL"].rstrip("/")
TOKEN = os.environ["CX_DRAIN_WORKER_TOKEN"]
DIM = 384

# The server-side verification FLOOR injects the seeded demo honeypot into a buyer's
# job even when the buyer requested honeypot_frac=0 (Verification & Result Trust
# 6->7). is_honeypot is never signalled over the wire AND — by a deliberate security
# fix (Verification & Result Trust 5->5.5) — the honeypot's input is copied to an
# OPAQUE per-task key ("jobs/{job}/tasks/{taskID}/input.jsonl"), so its input_url path
# no longer reveals it. The ONLY surviving way to recognize the probe is by its INPUT
# CONTENT: the honeypot input row carries the fixed probe TEXT. We match that text and
# answer with the honeypot's REAL known answer (any other embed result requeues the
# task forever and the job never completes). All three values come from control/seed.go
# via env (extracted by doc-as-test.sh), so this file holds no hand-copied literal that
# could silently drift from the seed.
HONEYPOT_EMBED_TEXT = os.environ.get("CX_DRAIN_HONEYPOT_TEXT", "")
HONEYPOT_EMBED_ANSWER = os.environ.get("CX_DRAIN_HONEYPOT_ANSWER", "")


def _req(method, url, data=None, headers=None, timeout=30):
    r = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    return urllib.request.urlopen(r, timeout=timeout)


def embed_result(n_rows):
    """A well-formed embed artifact: one distinct unit vector per row (mirrors the
    integration suite's embedResultJSON)."""
    vecs = []
    for i in range(max(n_rows, 1)):
        v = [0.0] * DIM
        v[i % DIM] = 1.0
        vecs.append(v)
    return json.dumps(
        {"job_type": "embed", "model": "all-minilm-l6-v2", "dim": DIM, "count": len(vecs), "vectors": vecs}
    ).encode()


def fetch_input(input_url):
    """Fetch the presigned input body. Returns "" on any failure (the caller then
    falls back to a single-row result — single-row tasks are the common case for the
    quickstart's tiny jobs)."""
    try:
        with _req("GET", input_url, timeout=20) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception:
        return ""


def input_rows(body):
    return sum(1 for ln in body.splitlines() if ln.strip()) or 1


def drain_once():
    """Claim one task if available; return True if one was processed."""
    try:
        with _req("GET", CONTROL + "/v1/worker/poll", headers={"X-Worker-Token": TOKEN}) as resp:
            if resp.status == 204:
                return False
            disp = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 204:
            return False
        # A transient error (e.g. worker briefly not registered) — back off, retry.
        print(f"poll error {e.code}: {e.read().decode('utf-8','replace')[:200]}", file=sys.stderr)
        return False
    except urllib.error.URLError as e:
        print(f"poll url error: {e.reason}", file=sys.stderr)
        return False

    task_id = disp["task_id"]
    result_key = disp["result_key"]
    output_url = disp["output_url"]
    input_body = fetch_input(disp.get("input_url", ""))
    # Honeypot-aware by INPUT CONTENT (the opaque-key security fix means the honeypot's
    # input_url path no longer reveals it — the surviving tell is the probe TEXT). A
    # task whose input carries the known probe text must be answered with the
    # honeypot's real known answer, or the verifier correctly requeues it forever and
    # the job never finalizes.
    if HONEYPOT_EMBED_ANSWER and HONEYPOT_EMBED_TEXT and HONEYPOT_EMBED_TEXT in input_body:
        body = HONEYPOT_EMBED_ANSWER.encode()
        n = -1  # signals "honeypot" in the log line below
    else:
        n = input_rows(input_body)
        body = embed_result(n)

    # PUT the result to the presigned output_url (no auth header — the signature
    # carries authorization), then commit.
    try:
        _req("PUT", output_url, data=body, headers={"Content-Type": "application/json"}).read()
    except urllib.error.HTTPError as e:
        print(f"PUT result failed {e.code}: {e.read().decode('utf-8','replace')[:200]}", file=sys.stderr)
        return False

    commit = json.dumps(
        {"task_id": task_id, "result_key": result_key, "duration_ms": 10, "tokens_used": 8}
    ).encode()
    try:
        _req(
            "POST",
            CONTROL + f"/v1/worker/task/{task_id}/commit",
            data=commit,
            headers={"X-Worker-Token": TOKEN, "Content-Type": "application/json"},
        ).read()
    except urllib.error.HTTPError as e:
        # A 4xx here (e.g. a honeypot answered wrong, or a duplicate) is not fatal to
        # the loop — the control plane requeues as needed. Log and move on.
        print(f"commit {task_id} -> {e.code}: {e.read().decode('utf-8','replace')[:200]}", file=sys.stderr)
        return True
    print(f"committed task {task_id} ({'honeypot' if n == -1 else str(n) + ' rows'})", file=sys.stderr)
    return True


def main():
    # Tight loop with a short poll interval so the quickstart's tiny jobs complete in
    # a couple of seconds. Runs until killed by doc-as-test.sh's cleanup.
    while True:
        did = drain_once()
        time.sleep(0.25 if did else 0.75)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
