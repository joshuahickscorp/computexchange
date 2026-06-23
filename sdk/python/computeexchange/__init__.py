"""Computexchange — thin, dependency-free Python client for the buyer REST API.

Stdlib ``urllib`` only — NO ``requests`` dependency (BLACKHOLE: starve deps; the
buyer surface is small enough that pulling a transport library would be pure
mass). Every non-2xx response raises :class:`APIError` carrying the status code
and the server's error body — there is no silent soft-fail.

    from computeexchange import Client
    cx = Client("http://localhost:8080", "my-api-key")
    job = cx.submit_job(model="all-minilm-l6-v2", job_type="embed",
                        input='{"text":"hello"}\\n{"text":"world"}\\n')
    done = cx.wait(job["job_id"])
    text = cx.results_text(job["job_id"])        # the merged JSONL artifact

    # OpenAI-shaped convenience: submit → wait → fetch in one call.
    out = cx.embeddings("all-minilm-l6-v2", ["hello", "world"])
    out["data"][0]["embedding"]                   # -> [float, ...]
"""

import json
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

__all__ = [
    "Client",
    "APIError",
    "JobError",
    "BudgetStoppedError",
    "BadInputError",
    "decode_embeddings_binary",
    "is_embeddings_binary",
]
__version__ = "0.1.0"

# Binary embedding artifact (PLANE_D D5/D15). The agent emits this compact float32
# container instead of the JSON `vectors` array when an embed job is submitted with
# binary=True; the control plane merges per-chunk artifacts into one of these. The
# SDK decodes it so callers never see the wire format. Layout (little-endian):
#   magic "CXEM" (4) | version u32 | dim u32 | count u32 | count*dim packed f32.
_EMBED_BIN_MAGIC = b"CXEM"
_EMBED_BIN_VERSION = 1
_EMBED_BIN_HEADER = 16


def is_embeddings_binary(data):
    """True if ``data`` (bytes) is a Computexchange binary embedding artifact —
    i.e. it carries the ``CXEM`` magic prefix. Lets a caller branch on format
    without catching a decode error."""
    return len(data) >= 4 and data[:4] == _EMBED_BIN_MAGIC


def decode_embeddings_binary(data):
    """Decode a binary embedding artifact (PLANE_D D5/D15) into a list of rows,
    each a ``list[float]`` of length ``dim``. NumPy-free (``struct`` only).

    Raises :class:`ValueError` on any malformation — a bad magic, an unknown
    version, or a body whose size disagrees with its header. Never returns a
    truncated or fabricated result.
    """
    if len(data) < _EMBED_BIN_HEADER:
        raise ValueError(
            f"binary embeddings: {len(data)} bytes < {_EMBED_BIN_HEADER}-byte header"
        )
    if data[:4] != _EMBED_BIN_MAGIC:
        raise ValueError("binary embeddings: bad magic (not a CXEM artifact)")
    version, dim, count = struct.unpack_from("<III", data, 4)
    if version != _EMBED_BIN_VERSION:
        raise ValueError(f"binary embeddings: unsupported version {version}")
    want = _EMBED_BIN_HEADER + count * dim * 4
    if len(data) != want:
        raise ValueError(
            f"binary embeddings: body is {len(data)} bytes, header implies {want} "
            f"({count}x{dim} f32)"
        )
    # One bulk unpack of all floats, then slice into rows — fast and allocation-light.
    floats = struct.unpack_from(f"<{count * dim}f", data, _EMBED_BIN_HEADER)
    return [list(floats[r * dim : (r + 1) * dim]) for r in range(count)]

# Job-type tags accepted by the control plane (control/types.go validJobTypes).
JOB_TYPES = (
    "embed",
    "batch_infer",
    "audio_transcribe",
    "image_gen",
    "eval",
    "lora_finetune",
    "batch_classification",
    "json_extraction",
    "rerank",
)


class APIError(Exception):
    """A non-2xx response (or a transport failure). Carries the HTTP status and
    the raw server body so failures are never swallowed."""

    def __init__(self, status, body, method="", path=""):
        self.status = status
        self.body = body
        where = f"{method} {path}".strip()
        super().__init__(f"{where} -> HTTP {status}: {body}".strip())


class JobError(Exception):
    """A job that ended in a buyer-visible non-success state (budget stop or
    buyer-bad-input). Carries the ``job_id`` and the final status dict so the
    caller can inspect ``budget_state``/``status`` without a second fetch. The
    typed subclasses let a headless caller branch on *why* a job stopped instead
    of catching a bare :class:`APIError` and string-matching the message."""

    def __init__(self, job_id, message, status=None):
        self.job_id = job_id
        self.status = status or {}
        super().__init__(f"job {job_id}: {message}")


class BudgetStoppedError(JobError):
    """The Budget Governor (Plane C §12 / Plane D D8) stopped this job before it
    could complete: its ``budget_state`` is ``paused_for_budget`` (dispatch halted
    at the cap; will not progress until the cap is raised) or ``cancelled_by_budget``.
    No money was moved by the stop — the cap GATES dispatch, it never charges or
    refunds. Raise/inspect ``status['budget_state']`` for which of the two it is."""


class BadInputError(JobError):
    """The job failed because of the buyer's own input — a ``buyer_fault`` failure
    (e.g. ``bad_input``, ``bad_jsonl``, ``unsupported_model``, ``unsupported_job_type``).
    These are NOT retried elsewhere (failing fast is correct) and the charges for
    failed work are refundable. ``failures`` holds the typed failure rows that
    triggered this, so the caller can see exactly what was malformed."""

    def __init__(self, job_id, message, status=None, failures=None):
        self.failures = failures or []
        super().__init__(job_id, message, status=status)


class Client:
    """A buyer-side client for the Computexchange control plane.

    Parameters
    ----------
    base_url:
        Control-plane base URL, e.g. ``http://localhost:8080``. A trailing
        slash is trimmed.
    api_key:
        Buyer api key, sent as ``Authorization: Bearer <key>``.
    timeout:
        Per-request socket timeout in seconds (default 60).
    """

    def __init__(self, base_url="http://localhost:8080", api_key="", timeout=60.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    # ---- low-level HTTP ----

    def _request(self, method, path, body=None, query=None):
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None
        headers = {}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            # Surface the server's JSON error body verbatim — never hide it.
            detail = e.read().decode("utf-8", "replace")
            raise APIError(e.code, detail.strip(), method, path) from None
        except urllib.error.URLError as e:
            raise APIError(0, str(e.reason), method, path) from None
        if not raw:
            return {}
        return json.loads(raw)

    # ---- buyer endpoints ----

    def submit_job(
        self,
        model,
        job_type,
        input,
        *,
        tier="batch",
        params=None,
        labels=None,
        max_tokens=None,
        temperature=None,
        top_k=None,
        schema=None,
        language=None,
        timestamps=None,
        batch_size=None,
        binary=None,
        split_size=None,
        min_memory_gb=0.0,
        hw_classes=None,
        data_residency=None,
        redundancy_frac=0.0,
        honeypot_frac=0.0,
        payout_hold_secs=0,
        webhook_url=None,
        quote_id=None,
        max_usd=None,
        s3_key=None,
        model_kind="gguf",
    ):
        """Submit a job. Returns the 202 body
        ``{job_id, task_count, estimated_usd, eta_secs, estimated_completion}``.

        ``input`` is the inline JSONL as a single ``str`` (one JSON record per
        line). Pass ``s3_key=...`` instead to point at an already-uploaded
        object; then ``input`` is ignored. Variant fields (``labels`` for
        batch_classification, ``schema`` for json_extraction, ``top_k`` for
        rerank, ``max_tokens``/``temperature`` for batch_infer,
        ``language``/``timestamps`` for audio_transcribe, ``binary`` for embed)
        are folded into the tagged ``job_type`` only when given, so the wire shape
        matches. ``binary=True`` (embed only) requests the compact float32 artifact
        (PLANE_D D5/D15) instead of JSON vectors.

        ``max_usd`` sets a hard buyer spend cap (Budget Governor, Plane C §12 /
        Plane D D8): once the job's projected charge would breach it, dispatch of
        new tasks stops and :meth:`wait` raises :class:`BudgetStoppedError`. Leave
        it ``None`` (or 0) for no cap. ``quote_id`` binds this submission to an
        advisory quote (the ``"q_<uuid>"`` returned by :meth:`quote`, Plane D D7);
        the control plane verifies it matches and echoes the quoted price on the
        invoice, and a mismatch is rejected with :class:`APIError` (409). Both are
        sent only when given, so the unbound/uncapped wire shape is unchanged.
        """
        if job_type not in JOB_TYPES:
            raise ValueError(f"unknown job_type {job_type!r}; one of {JOB_TYPES}")

        jt = {"type": job_type}
        if batch_size is not None:
            jt["batch_size"] = int(batch_size)
        if binary is not None:
            # embed-only opt-in: compact float32 artifact (PLANE_D D5/D15). Folded
            # into the tagged job_type so it round-trips to the agent via job_type_spec.
            jt["binary"] = bool(binary)
        if max_tokens is not None:
            jt["max_tokens"] = int(max_tokens)
        if temperature is not None:
            jt["temperature"] = float(temperature)
        if top_k is not None:
            jt["top_k"] = int(top_k)
        if labels is not None:
            jt["labels"] = list(labels)
        if schema is not None:
            jt["schema"] = schema
        if language is not None:
            jt["language"] = language
        if timestamps is not None:
            jt["timestamps"] = bool(timestamps)

        if s3_key is not None:
            input_field = {"s3_key": s3_key}
        elif isinstance(input, str):
            input_field = input  # a JSON string IS the inline JSONL
        else:
            # Convenience: a list/iterable of records -> JSONL text.
            input_field = "".join(json.dumps(rec) + "\n" for rec in input)

        body_params = dict(params) if params else {}
        if split_size is not None:
            body_params["split_size"] = int(split_size)

        body = {
            "job_type": jt,
            "model": {"kind": model_kind, "ref": model},
            "params": body_params or None,
            "constraints": {
                "min_memory_gb": float(min_memory_gb),
                "hw_classes": list(hw_classes) if hw_classes else None,
                "data_residency": list(data_residency) if data_residency else None,
            },
            "verification": {
                "redundancy_frac": float(redundancy_frac),
                "honeypot_frac": float(honeypot_frac),
                "payout_hold_secs": int(payout_hold_secs),
            },
            "tier": tier,
            "input": input_field,
        }
        if webhook_url:
            body["webhook_url"] = webhook_url
        # max_usd / quote_id are sent only when given so the wire shape matches the
        # control plane's `omitempty` zero (no cap / unbound) for callers who skip them.
        if max_usd is not None and float(max_usd) > 0:
            body["max_usd"] = float(max_usd)
        if quote_id:
            body["quote_id"] = quote_id
        return self._request("POST", "/v1/jobs", body=body)

    def get_job(self, job_id):
        """``GET /v1/jobs/{id}`` — status + progress.

        Raises the typed :class:`BudgetStoppedError` when the Budget Governor has
        stopped the job (``budget_state`` is ``paused_for_budget``/``cancelled_by_budget``),
        and :class:`BadInputError` when a terminal failure was the buyer's fault
        (a ``buyer_fault`` failure row). A plain status read for a healthy or
        generically-failed job returns the dict unchanged."""
        job = self._request("GET", "/v1/jobs/" + str(job_id))
        self._raise_if_job_stopped(job_id, job)
        return job

    def _raise_if_job_stopped(self, job_id, job):
        """Inspect a ``GET /v1/jobs/{id}`` dict (plus, only when terminally failed,
        the typed ``/failures`` rows) and raise the matching typed exception when the
        job ended via a budget stop or buyer-bad-input. Returns ``None`` otherwise —
        a healthy job, or a generic failure that callers still surface as
        :class:`APIError` through :meth:`wait`. Budget state is read straight from the
        job dict (no extra request); failures are fetched only on a failed/cancelled
        status, off the polling hot path."""
        budget_state = job.get("budget_state")
        if budget_state in ("paused_for_budget", "cancelled_by_budget"):
            raise BudgetStoppedError(
                job_id,
                f"stopped by the budget governor (budget_state={budget_state!r})",
                status=job,
            )
        if job.get("status") in ("failed", "cancelled"):
            # Only a terminal-failed job can be a buyer-fault failure; fetch the typed
            # failure rows and raise BadInputError when any is buyer_fault. A non-buyer
            # failure falls through so wait() raises the generic APIError it always has.
            try:
                fails = self.failures(job_id)
            except APIError:
                fails = []  # never mask the original outcome on a failures-read hiccup
            buyer_faults = [f for f in fails if f.get("buyer_fault")]
            if buyer_faults:
                classes = ", ".join(sorted({f.get("failure_class", "?") for f in buyer_faults}))
                raise BadInputError(
                    job_id,
                    f"failed on buyer input ({classes})",
                    status=job,
                    failures=buyer_faults,
                )

    def results(self, job_id):
        """``GET /v1/jobs/{id}/results`` —
        ``{job_id, status, results_url?, result_urls[]}`` (presigned URLs)."""
        return self._request("GET", "/v1/jobs/" + str(job_id) + "/results")

    def cancel_job(self, job_id):
        """``DELETE /v1/jobs/{id}`` — cancel a not-yet-started job."""
        return self._request("DELETE", "/v1/jobs/" + str(job_id))

    def events(self, job_id):
        """``GET /v1/jobs/{id}/events`` (Plane C/D) — the buyer-visible event
        timeline: ``[{event, buyer_text, task_id?, created_at}, ...]`` covering
        job_created, task_failed, task_requeued, job_failed, job_completed, etc.
        The buyer never has to infer state from a status field alone."""
        return self._request("GET", "/v1/jobs/" + str(job_id) + "/events")

    def failures(self, job_id):
        """``GET /v1/jobs/{id}/failures`` (Plane C/D) — typed failure history:
        ``[{failure_class, retryable, buyer_fault, message, backend, model_ref,
        created_at}, ...]``. Structured reasons, not log scraping."""
        return self._request("GET", "/v1/jobs/" + str(job_id) + "/failures")

    def invoice(self, job_id):
        """``GET /v1/jobs/{id}/invoice`` (Plane D) — the buyer-facing, ledger-backed
        invoice for one job: ``{job_id, buyer_id, status, job_type, created_at,
        estimated_usd, actual_usd, charged_usd, supplier_credit_usd,
        platform_take_usd, quoted_usd?}``. Every figure is computed from real ledger
        rows — nothing is fabricated. ``quoted_usd`` is present only when the job was
        bound to a quote (via ``submit_job(quote_id=...)``), letting you compare
        quoted-vs-actual."""
        return self._request("GET", "/v1/jobs/" + str(job_id) + "/invoice")

    def models(self):
        """``GET /v1/models`` — the model + pricing catalogue."""
        return self._request("GET", "/v1/models")

    def estimate(self, model, units, tier="batch"):
        """``GET /v1/price-estimate`` — cost for ``units`` of ``model`` at ``tier``."""
        return self._request(
            "GET",
            "/v1/price-estimate",
            query={"model": model, "units": int(units), "tier": tier},
        )

    def quote(
        self,
        model,
        job_type,
        input,
        *,
        tier="batch",
        split_size=None,
        min_memory_gb=0.0,
        redundancy_frac=0.0,
        honeypot_frac=0.0,
        model_kind="gguf",
    ):
        """``POST /v1/quote`` (Plane C / Compute Autopilot) — scan the input and
        return a conservative quote WITHOUT spending or creating a job.

        The returned dict has ``input`` (record/byte/token scan + malformed
        report), ``execution`` (recommended split, estimated tasks, eligible
        workers now, OOM/cold-start risk), ``cost`` (min/expected/max), ``time``
        (p50/p90/worst-case secs), ``confidence`` (score + **structured
        reasons**), ``budget`` (suggested cap), and ``warnings``. The token count
        is a byte heuristic, not an exact tokenizer count — read
        ``confidence.reasons`` before trusting it.
        """
        if job_type not in JOB_TYPES:
            raise ValueError(f"unknown job_type {job_type!r}; one of {JOB_TYPES}")
        if isinstance(input, str):
            input_field = input
        else:
            input_field = "".join(json.dumps(rec) + "\n" for rec in input)
        body_params = {"split_size": int(split_size)} if split_size else None
        body = {
            "job_type": {"type": job_type},
            "model": {"kind": model_kind, "ref": model},
            "params": body_params,
            "constraints": {"min_memory_gb": float(min_memory_gb)},
            "verification": {
                "redundancy_frac": float(redundancy_frac),
                "honeypot_frac": float(honeypot_frac),
                "payout_hold_secs": 0,
            },
            "tier": tier,
            "input": input_field,
        }
        return self._request("POST", "/v1/quote", body=body)

    # ---- convenience ----

    def wait(self, job_id, timeout=1800.0, poll=3.0):
        """Poll ``get_job`` until the job reaches a terminal status. Returns the
        final status dict on ``complete``.

        Because the poll goes through :meth:`get_job`, a job the Budget Governor
        stopped raises :class:`BudgetStoppedError` (it would otherwise never reach
        ``complete`` and only time out), and a buyer-fault failure raises the more
        specific :class:`BadInputError`. A generic ``failed``/``cancelled`` still
        raises :class:`APIError`, and exceeding ``timeout`` raises
        :class:`TimeoutError`."""
        deadline = time.monotonic() + timeout
        while True:
            job = self.get_job(job_id)  # raises BudgetStoppedError / BadInputError as warranted
            status = job.get("status")
            if status == "complete":
                return job
            if status in ("failed", "cancelled"):
                raise APIError(0, f"job {job_id} ended with status {status!r}")
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"job {job_id} not complete after {timeout}s (last status {status!r})"
                )
            time.sleep(poll)

    def results_text(self, job_id):
        """Download and return the merged result artifact as text (JSONL). Falls
        back to concatenating the per-task result objects when the control plane
        has no merged ``results_url`` yet. Presigned URLs are fetched WITHOUT the
        auth header — the signature carries authorization."""
        res = self.results(job_id)
        urls = []
        if res.get("results_url"):
            urls = [res["results_url"]]
        elif res.get("result_urls"):
            urls = res["result_urls"]
        else:
            raise APIError(0, f"job {job_id} has no results (status {res.get('status')!r})")
        out = []
        for u in urls:
            try:
                with urllib.request.urlopen(u, timeout=self.timeout) as resp:
                    out.append(resp.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                raise APIError(e.code, e.read().decode("utf-8", "replace"), "GET", u) from None
            except urllib.error.URLError as e:
                raise APIError(0, str(e.reason), "GET", u) from None
        return "".join(out)

    def results_bytes(self, job_id):
        """Download and return the merged result artifact as raw ``bytes`` (no text
        decoding). Use this for the binary embedding artifact (PLANE_D D5/D15);
        :meth:`results_text` is for the JSONL default. Presigned URLs are fetched
        WITHOUT the auth header — the signature carries authorization."""
        res = self.results(job_id)
        if res.get("results_url"):
            urls = [res["results_url"]]
        elif res.get("result_urls"):
            urls = res["result_urls"]
        else:
            raise APIError(0, f"job {job_id} has no results (status {res.get('status')!r})")
        out = bytearray()
        for u in urls:
            try:
                with urllib.request.urlopen(u, timeout=self.timeout) as resp:
                    out.extend(resp.read())
            except urllib.error.HTTPError as e:
                raise APIError(e.code, e.read().decode("utf-8", "replace"), "GET", u) from None
            except urllib.error.URLError as e:
                raise APIError(0, str(e.reason), "GET", u) from None
        return bytes(out)

    def results_records(self, job_id):
        """Like :meth:`results_text` but parses each JSONL line into a dict and
        returns the list (blank lines skipped)."""
        text = self.results_text(job_id)
        return [json.loads(ln) for ln in text.splitlines() if ln.strip()]

    def embeddings(self, model, input, *, binary=None, timeout=1800.0, poll=3.0, **submit_kwargs):
        """OpenAI-shaped convenience: submit an ``embed`` job over ``input``,
        wait for completion, and return
        ``{"data": [{"embedding": [...], "index": i}, ...], "model": ..., "usage": {...}}``.

        ``input`` may be a single string, a list of strings, or pre-built JSONL
        text. Pass ``binary=True`` to request the compact float32 artifact
        (PLANE_D D5/D15) — a smaller transfer for large outputs; the SDK decodes it
        transparently, so the returned shape is identical either way. With the JSON
        default the merged artifact is one ``{"index","vector"}`` line per input
        record (control/api.go mergeResultObject); with binary it is one ``CXEM``
        float32 file (decoded here). We auto-detect the format on download so an
        artifact produced in either mode is handled correctly.
        """
        if isinstance(input, str):
            texts = [input]
        else:
            texts = list(input)
        jsonl = "".join(json.dumps({"text": t}) + "\n" for t in texts)
        if binary is not None:
            submit_kwargs["binary"] = bool(binary)
        job = self.submit_job(model=model, job_type="embed", input=jsonl, **submit_kwargs)
        job_id = job["job_id"]
        self.wait(job_id, timeout=timeout, poll=poll)

        # Fetch raw bytes and branch on the artifact format. Binary → decode the
        # CXEM float32 rows (index is positional); JSON → parse the JSONL lines.
        raw = self.results_bytes(job_id)
        data = []
        if is_embeddings_binary(raw):
            for i, vec in enumerate(decode_embeddings_binary(raw)):
                data.append({"object": "embedding", "embedding": vec, "index": i})
        else:
            for ln in raw.decode("utf-8", "replace").splitlines():
                if not ln.strip():
                    continue
                rec = json.loads(ln)
                # Merged embed line: {"index": <int>, "vector": [...]}. Tolerate an
                # "embedding" alias defensively, but never fabricate a vector.
                vec = rec.get("vector", rec.get("embedding"))
                if vec is None:
                    raise APIError(0, f"embed result line missing vector: {rec!r}")
                data.append({"object": "embedding", "embedding": vec, "index": rec.get("index", len(data))})
        data.sort(key=lambda d: d["index"])
        return {
            "object": "list",
            "data": data,
            "model": model,
            "usage": {"prompt_tokens": 0, "total_tokens": 0},
        }
