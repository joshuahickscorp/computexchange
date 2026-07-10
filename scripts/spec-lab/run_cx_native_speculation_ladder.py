#!/usr/bin/env python3
"""Run CX-native speculation receipts across token and render branches.

This runner is deliberately library-light. vLLM/Hawking/Cycles can still be
mined for kernels, APIs, and reference behavior, but this file exercises the
ComputeExchange-owned draft/verify/accept/repair receipt shape.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import cx_speculative_core as cx
import run_speculative_render_ladder as render_ladder
import run_token_spec_decode_ladder as token_ladder


HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
DATE = "2026-07-09"
HARD_RENDER_FLOOR_X = 7.8721

LEDGER = REPO / "docs/speed-lane-reports/spec-lab/cx_native_speculation_ledger.jsonl"
BRANCH_LEDGER = REPO / "docs/speed-lane-reports/spec-lab/spec_render_token_branch_ledger.jsonl"
REPORT = REPO / f"docs/speed-lane-reports/spec-lab/SPEC_RENDER_AND_TOKEN_DECODE_ITERATION_{DATE}.md"
BRANCH_REPORT = REPO / f"docs/speed-lane-reports/spec-lab/SPEC_RENDER_TOKEN_DECODE_BRANCH_LEDGER_{DATE}.md"
ACTION_PRIORITY = {"grow": 3, "park": 2, "prune": 1, "kill_correctness": 0}
SPAN_STRATEGIES = {"adaptive_span"}
PREFIX_STRATEGIES = {
    "prefix_accept_adaptive",
    "prefix_accept_copy_match",
    "prefix_accept_copy_runway",
    "prefix_accept_json_template",
}
JSON_ROW_RE = re.compile(r'^\{"id":(\d+),"status":"([^"]+)","value":(\d+)\}$')


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({"ts": now(), **record}, sort_keys=True) + "\n")


def load_token_rows(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    with path.open() as f:
        for line in f:
            if not line.strip():
                continue
            record = json.loads(line)
            row = record.get("row")
            if row and row.get("modality") == "token":
                rows.append(row)
    return rows


def flatten(chunks: list[list[int]]) -> list[int]:
    out: list[int] = []
    for chunk in chunks:
        out.extend(chunk)
    return out


def token_units(stream: list[int], prefix_len: int, k: int) -> list[cx.SpecUnit]:
    units = []
    for pos in range(prefix_len, len(stream), k):
        units.append(
            cx.SpecUnit(
                unit_id=f"tok:{pos}",
                modality="token",
                payload={"pos": pos, "k": min(k, len(stream) - pos), "stream": stream},
                meta={"pos": pos},
            )
        )
    return units


def threshold_label(threshold: float) -> str:
    return f"{threshold:g}".replace(".", "p")


def prediction_from_counts(counts: Any) -> tuple[int, float] | None:
    if not counts:
        return None
    total = sum(counts.values())
    if total <= 0:
        return None
    token, count = counts.most_common(1)[0]
    return token, count / total


def ngram_next_prediction_confidence(predictor: token_ladder.NGramDraft, ctx_tokens: list[int]) -> tuple[int, float]:
    if len(ctx_tokens) >= predictor.ctx:
        key = tuple(ctx_tokens[-predictor.ctx :])
        local = prediction_from_counts(predictor.table.get(key))
        if local is not None:
            return local
    global_prediction = prediction_from_counts(predictor.global_counts)
    if global_prediction is not None:
        return global_prediction
    return 0, 0.0


def ngram_span_confidence(predictor: token_ladder.NGramDraft, width: int) -> float:
    ctx_tokens = list(predictor.history[-predictor.ctx :])
    confidences = []
    for _ in range(width):
        token, confidence = ngram_next_prediction_confidence(predictor, ctx_tokens)
        confidences.append(confidence)
        ctx_tokens.append(token)
    return min(confidences) if confidences else 0.0


class ContextCopyNGramDraft:
    """CX-owned prompt-lookup/copy-match predictor with n-gram fallback.

    This mines the useful suffix/prompt-lookup idea without making any external
    speculative decode backend the control-plane boundary. The index is updated
    only from verified tokens, so the verifier still owns correctness.
    """

    def __init__(self, ctx: int, min_match: int = 3, max_candidates: int = 16):
        self.ctx = max(1, ctx)
        self.min_match = max(1, min(min_match, self.ctx))
        self.max_candidates = max(1, max_candidates)
        self.history: list[int] = []
        self.ngram = token_ladder.NGramDraft(ctx)
        self.index: dict[tuple[int, ...], list[int]] = defaultdict(list)

    def observe(self, token: int) -> None:
        for width in range(self.min_match, self.ctx + 1):
            if len(self.history) >= width:
                self.index[tuple(self.history[-width:])].append(len(self.history) - width)
        self.history.append(token)
        self.ngram.observe(token)

    def prime(self, tokens: list[int]) -> None:
        for token in tokens:
            self.observe(token)

    def predict_next_with_confidence(self, ctx_tokens: list[int]) -> tuple[int, float, dict[str, Any]]:
        max_width = min(self.ctx, len(ctx_tokens))
        for width in range(max_width, self.min_match - 1, -1):
            positions = self.index.get(tuple(ctx_tokens[-width:]))
            if not positions:
                continue
            for pos in reversed(positions[-self.max_candidates :]):
                follower = pos + width
                if follower < len(self.history):
                    confidence = min(0.99, 0.62 + 0.04 * width)
                    return self.history[follower], confidence, {
                        "source": "copy_match",
                        "match_len": width,
                    }
        token, confidence = ngram_next_prediction_confidence(self.ngram, ctx_tokens)
        return token, confidence, {"source": "ngram", "match_len": 0}

    def copy_seed_for_width(self, ctx_tokens: list[int], needed: int) -> tuple[int, int, float] | None:
        max_width = min(self.ctx, len(ctx_tokens))
        for width in range(max_width, self.min_match - 1, -1):
            positions = self.index.get(tuple(ctx_tokens[-width:]))
            if not positions:
                continue
            for pos in reversed(positions[-self.max_candidates :]):
                follower = pos + width
                if follower + needed <= len(self.history):
                    confidence = min(0.99, 0.62 + 0.04 * width)
                    return follower, width, confidence
        return None


class JsonTemplateDraft:
    """CX-owned structured-output predictor for repeated JSON rows.

    The predictor only learns from verified bytes observed through `observe`.
    Proposed bytes still pass through the same verifier/prefix-accept repair
    path, so a wrong structural guess cannot corrupt the output.
    """

    def __init__(self, ctx: int):
        self.ctx = max(1, ctx)
        self.history: list[int] = []
        self.ngram = token_ladder.NGramDraft(ctx)
        self.rows: list[dict[str, Any]] = []
        self.current_line: list[int] = []
        self.params: dict[str, Any] | None = None
        self._cached_row_key: tuple[Any, ...] | None = None
        self._cached_row_bytes: bytes | None = None

    def observe(self, token: int) -> None:
        self.history.append(token)
        self.ngram.observe(token)
        if not 0 <= token <= 255:
            self.current_line = []
            return
        if token == 10:
            self._observe_line(self.current_line)
            self.current_line = []
        else:
            self.current_line.append(token)

    def prime(self, tokens: list[int]) -> None:
        for token in tokens:
            self.observe(token)

    def _observe_line(self, line_tokens: list[int]) -> None:
        try:
            line = bytes(line_tokens).decode("utf-8")
        except UnicodeDecodeError:
            return
        row = parse_json_template_row(line)
        if row is None:
            self._invalidate_cache()
            return
        self.rows.append(row)
        self.params = infer_json_template_params(self.rows)
        self._invalidate_cache()

    def _invalidate_cache(self) -> None:
        self._cached_row_key = None
        self._cached_row_bytes = None

    def _next_row_bytes(self, last_row: dict[str, Any]) -> tuple[dict[str, Any], bytes] | None:
        row = next_json_template_row_data(last_row, self.params)
        if row is None:
            return None
        key = json_template_row_cache_key(row, self.params)
        if self._cached_row_key != key:
            self._cached_row_key = key
            self._cached_row_bytes = format_json_template_row(row).encode("utf-8")
        if self._cached_row_bytes is None:
            return None
        return row, self._cached_row_bytes

    def predict_many_with_sources(
        self,
        width: int,
        stop_below: float | None = None,
    ) -> tuple[list[int], list[dict[str, Any]]]:
        proposal: list[int] = []
        source_meta: list[dict[str, Any]] = []
        if self.params and self.rows:
            working_last_row = dict(self.rows[-1])
            working_line = list(self.current_line)
        else:
            working_last_row = None
            working_line = list(self.current_line)

        ctx_tokens = list(self.history[-self.ctx :])
        running_min = 1.0
        while len(proposal) < width:
            predicted = None
            next_row: dict[str, Any] | None = None
            if self.params and working_last_row is not None:
                next_row_pair = self._next_row_bytes(working_last_row)
                if next_row_pair is not None:
                    next_row, encoded = next_row_pair
                    if line_tokens_match_prefix(encoded, working_line):
                        offset = len(working_line)
                        take = min(width - len(proposal), len(encoded) - offset)
                        if take > 0:
                            chunk = list(encoded[offset : offset + take])
                            proposal.extend(chunk)
                            source_meta.extend(
                                {
                                    "source": "json_template",
                                    "match_len": offset + idx,
                                    "confidence": 0.97,
                                }
                                for idx in range(take)
                            )
                            ctx_tokens.extend(chunk)
                            running_min = min(running_min, 0.97)
                            working_line.extend(chunk)
                            if len(working_line) == len(encoded) and encoded.endswith(b"\n"):
                                working_last_row = next_row
                                working_line = []
                            if stop_below is not None and running_min < stop_below:
                                break
                            continue
                        predicted = None
            if predicted is None:
                token, confidence = ngram_next_prediction_confidence(self.ngram, ctx_tokens)
                meta = {"source": "ngram", "match_len": 0, "confidence": confidence}
            else:
                token = predicted
                meta = {"source": "json_template", "match_len": len(working_line), "confidence": 0.97}

            proposal.append(token)
            source_meta.append(meta)
            ctx_tokens.append(token)
            running_min = min(running_min, float(meta.get("confidence") or 0.0))

            if 0 <= token <= 255:
                if token == 10:
                    row = self._parse_working_line(working_line)
                    if row is not None:
                        working_last_row = row
                    working_line = []
                else:
                    working_line.append(token)
            else:
                working_line = []
            if stop_below is not None and running_min < stop_below:
                break
        return proposal, source_meta

    def _parse_working_line(self, line_tokens: list[int]) -> dict[str, Any] | None:
        try:
            line = bytes(line_tokens).decode("utf-8")
        except UnicodeDecodeError:
            return None
        return parse_json_template_row(line)


def line_tokens_match_prefix(encoded: bytes, line_tokens: list[int]) -> bool:
    if len(line_tokens) > len(encoded):
        return False
    for idx, token in enumerate(line_tokens):
        if token != encoded[idx]:
            return False
    return True


def parse_json_template_row(line: str) -> dict[str, Any] | None:
    match = JSON_ROW_RE.match(line)
    if not match:
        return None
    return {
        "id": int(match.group(1)),
        "id_width": len(match.group(1)),
        "status": match.group(2),
        "value": int(match.group(3)),
    }


def infer_json_template_params(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(rows) < 4:
        return None
    id_deltas = [
        rows[idx + 1]["id"] - rows[idx]["id"]
        for idx in range(len(rows) - 1)
        if rows[idx + 1]["id"] > rows[idx]["id"]
    ]
    value_deltas = [
        rows[idx + 1]["value"] - rows[idx]["value"]
        for idx in range(len(rows) - 1)
        if rows[idx + 1]["value"] > rows[idx]["value"]
    ]
    id_delta = Counter(id_deltas).most_common(1)[0][0] if id_deltas else 1
    value_delta = Counter(value_deltas).most_common(1)[0][0] if value_deltas else 0
    value_mod_candidates = [
        rows[idx]["value"] + value_delta - rows[idx + 1]["value"]
        for idx in range(len(rows) - 1)
        if value_delta and rows[idx + 1]["value"] < rows[idx]["value"]
    ]
    value_mod = Counter(value_mod_candidates).most_common(1)[0][0] if value_mod_candidates else None
    id_mod_candidates = [
        rows[idx]["id"] + id_delta - rows[idx + 1]["id"]
        for idx in range(len(rows) - 1)
        if id_delta and rows[idx + 1]["id"] < rows[idx]["id"]
    ]
    id_mod = Counter(id_mod_candidates).most_common(1)[0][0] if id_mod_candidates else None

    status_period = 1
    status_by_residue = {0: rows[-1]["status"]}
    for period in range(2, 33):
        mapping: dict[int, str] = {}
        ok = True
        for row in rows:
            residue = row["id"] % period
            previous = mapping.get(residue)
            if previous is not None and previous != row["status"]:
                ok = False
                break
            mapping[residue] = row["status"]
        if ok and len(mapping) >= min(period, len(rows)):
            status_period = period
            status_by_residue = mapping
            break

    return {
        "id_delta": id_delta,
        "id_width": rows[-1]["id_width"],
        "id_mod": id_mod,
        "value_delta": value_delta,
        "value_mod": value_mod,
        "status_period": status_period,
        "status_by_residue": status_by_residue,
    }


def next_json_template_row_data(last_row: dict[str, Any], params: dict[str, Any] | None) -> dict[str, Any] | None:
    if params is None:
        return None
    next_id = int(last_row["id"]) + int(params.get("id_delta") or 1)
    id_mod = params.get("id_mod")
    if id_mod and next_id >= int(id_mod):
        next_id %= int(id_mod)
    period = int(params.get("status_period") or 1)
    status_by_residue = params.get("status_by_residue") or {}
    status = status_by_residue.get(next_id % period, last_row.get("status", "ok"))
    value = int(last_row["value"]) + int(params.get("value_delta") or 0)
    value_mod = params.get("value_mod")
    if value_mod and value >= int(value_mod):
        value %= int(value_mod)
    id_width = int(params.get("id_width") or last_row.get("id_width") or 1)
    return {
        "id": next_id,
        "id_width": id_width,
        "status": status,
        "value": value,
    }


def format_json_template_row(row: dict[str, Any]) -> str:
    return (
        f'{{"id":{int(row["id"]):0{int(row.get("id_width") or 1)}d},'
        f'"status":"{row.get("status", "ok")}","value":{int(row["value"])}}}\n'
    )


def next_json_template_row(last_row: dict[str, Any], params: dict[str, Any] | None) -> str | None:
    row = next_json_template_row_data(last_row, params)
    if row is None:
        return None
    return format_json_template_row(row)


def json_template_row_cache_key(row: dict[str, Any], params: dict[str, Any] | None) -> tuple[Any, ...]:
    return (
        row.get("id"),
        row.get("id_width"),
        row.get("status"),
        row.get("value"),
        (params or {}).get("id_delta"),
        (params or {}).get("id_mod"),
        (params or {}).get("value_delta"),
        (params or {}).get("value_mod"),
        (params or {}).get("status_period"),
    )


def prediction_step(predictor: Any, ctx_tokens: list[int]) -> tuple[int, float, dict[str, Any]]:
    custom = getattr(predictor, "predict_next_with_confidence", None)
    if custom is not None:
        return custom(ctx_tokens)
    token, confidence = ngram_next_prediction_confidence(predictor, ctx_tokens)
    return token, confidence, {"source": "ngram", "match_len": 0}


def confidence_summary(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"checks": 0, "min": 0.0, "mean": 0.0, "max": 0.0}
    return {
        "checks": len(values),
        "min": round(min(values), 6),
        "mean": round(sum(values) / len(values), 6),
        "max": round(max(values), 6),
    }


def int_summary(values: list[int]) -> dict[str, float | int]:
    if not values:
        return {"checks": 0, "min": 0, "mean": 0.0, "max": 0}
    return {
        "checks": len(values),
        "min": min(values),
        "mean": round(sum(values) / len(values), 6),
        "max": max(values),
    }


def decide_prefix_accept_row(
    row: dict,
    min_speedup: float = 1.2,
    min_accept: float = 0.35,
    min_target_call_reduction: float = 1.2,
) -> str:
    if not row.get("exact") or not row.get("quality_gate"):
        return "kill_correctness"
    speedup = float(row.get("speedup_x") or 0.0)
    accepted = float(row.get("accepted_fraction") or 0.0)
    call_reduction = float(row.get("target_call_reduction_x") or 0.0)
    if speedup >= min_speedup and accepted >= min_accept and call_reduction >= min_target_call_reduction:
        return "grow"
    if speedup < 1.0 or accepted < 0.08 or call_reduction <= 1.0:
        return "prune"
    return "park"


def run_token_unit_branch(
    scenario: str,
    n_tokens: int,
    prefix_frac: float,
    ctx: int,
    k: int,
    verifier_work: int,
    confidence_threshold: float = 0.0,
) -> dict:
    stream = token_ladder.SCENARIOS[scenario](n_tokens)
    prefix_len = max(ctx + 2, int(len(stream) * prefix_frac))
    prefix_len = min(prefix_len, len(stream) - 1)
    units = token_units(stream, prefix_len, k)
    predictor = token_ladder.NGramDraft(ctx)
    predictor.prime(stream[:prefix_len])
    gate_enabled = confidence_threshold > 0.0
    gate_confidences: list[float] = []
    fallback_token_calls = 0

    def baseline(unit: cx.SpecUnit) -> list[int]:
        pos = int(unit.payload["pos"])
        width = int(unit.payload["k"])
        out = []
        for idx in range(pos, pos + width):
            token_ladder.burn_work(verifier_work, idx)
            out.append(stream[idx])
        return out

    def should_speculate(unit: cx.SpecUnit) -> bool:
        if not gate_enabled:
            return True
        width = int(unit.payload["k"])
        confidence = ngram_span_confidence(predictor, width)
        gate_confidences.append(confidence)
        return confidence >= confidence_threshold

    def fallback(unit: cx.SpecUnit) -> list[int]:
        nonlocal fallback_token_calls
        pos = int(unit.payload["pos"])
        width = int(unit.payload["k"])
        out = []
        for idx in range(pos, pos + width):
            token_ladder.burn_work(verifier_work, idx)
            tok = stream[idx]
            predictor.observe(tok)
            out.append(tok)
        fallback_token_calls += len(out)
        return out

    def draft(unit: cx.SpecUnit) -> cx.DraftProposal:
        width = int(unit.payload["k"])
        return cx.DraftProposal(unit=unit, draft=predictor.predict_many(width))

    def verify(proposal: cx.DraftProposal) -> cx.Verification:
        pos = int(proposal.unit.payload["pos"])
        width = int(proposal.unit.payload["k"])
        token_ladder.burn_work(verifier_work, pos * 1315423911 + width)
        truth = stream[pos : pos + width]
        accepted = proposal.draft == truth
        if accepted:
            for tok in truth:
                predictor.observe(tok)
        return cx.Verification(accepted=accepted, truth=truth, quality=1.0 if accepted else 0.0)

    def repair(_proposal: cx.DraftProposal, verification: cx.Verification) -> cx.RepairResult:
        for tok in verification.truth:
            predictor.observe(tok)
        return cx.RepairResult(output=verification.truth)

    engine = cx.SpeculativeEngine(
        branch_id=(
            f"cx_native_token_gate_{scenario}_k{k}_c{threshold_label(confidence_threshold)}"
            if gate_enabled
            else f"cx_native_token_unit_{scenario}_k{k}"
        ),
        modality="token",
        draft=draft,
        verify=verify,
        repair=repair,
        baseline=baseline,
        should_speculate=should_speculate if gate_enabled else None,
        fallback=fallback,
    )
    meta = {
        "scenario": scenario,
        "n_tokens": len(stream),
        "prefix_tokens": prefix_len,
        "generated_tokens": len(stream) - prefix_len,
        "ctx": ctx,
        "unit_tokens": k,
        "verifier_work": verifier_work,
        "confidence_threshold": confidence_threshold,
        "gate_enabled": gate_enabled,
        "confidence_model": "minimum predicted-token probability across the proposed span",
        "claim_scope": "CX-native local protocol receipt; not model-backed LLM throughput",
        "protocol": "SpecUnit(token_span)->DraftProducer(ngram)->Verifier(span)->RepairPolicy(span_truth)",
    }
    outputs, receipt = engine.run(
        units,
        meta=meta,
    )
    if gate_enabled:
        meta["gate_confidence"] = confidence_summary(gate_confidences)
    baseline_truth = stream[prefix_len:]
    exact = flatten(outputs) == baseline_truth
    receipt = cx.SpecReceipt(
        **{**receipt.__dict__, "exact": exact, "quality_gate": exact}
    )
    action = cx.decide_branch(receipt)
    row = receipt.to_dict()
    row["branch_action"] = action
    row["output_tokens"] = len(flatten(outputs))
    row["fallback_token_calls"] = fallback_token_calls
    row["target_calls_baseline"] = len(baseline_truth)
    row["target_calls_spec"] = row["attempted_units"] + fallback_token_calls
    row["target_call_reduction_x"] = round((len(baseline_truth) / max(row["target_calls_spec"], 1)), 6)
    return row


def width_label(widths: list[int]) -> str:
    return "x".join(str(width) for width in widths)


def choose_adaptive_width_with_sources(
    predictor: Any,
    remaining: int,
    widths: list[int],
    threshold: float,
) -> tuple[int, float, list[int], list[dict[str, Any]]]:
    candidates = [width for width in widths if width <= remaining]
    if not candidates:
        return 0, 0.0, [], []

    max_width = candidates[0]
    ctx_tokens = list(predictor.history[-predictor.ctx :])
    proposal: list[int] = []
    source_meta: list[dict[str, Any]] = []
    prefix_mins: list[float] = []
    running_min = 1.0

    for _ in range(max_width):
        token, confidence, meta = prediction_step(predictor, ctx_tokens)
        proposal.append(token)
        source_meta.append(meta)
        ctx_tokens.append(token)
        running_min = min(running_min, confidence)
        prefix_mins.append(running_min)
        if running_min < threshold:
            break

    for width in candidates:
        if width <= len(prefix_mins) and prefix_mins[width - 1] >= threshold:
            return width, prefix_mins[width - 1], proposal[:width], source_meta[:width]
    return 0, prefix_mins[-1] if prefix_mins else 0.0, [], source_meta


def choose_copy_runway_width_with_sources(
    predictor: ContextCopyNGramDraft,
    remaining: int,
    widths: list[int],
    threshold: float,
) -> tuple[int, float, list[int], list[dict[str, Any]]]:
    candidates = [width for width in widths if width <= remaining]
    if not candidates:
        return 0, 0.0, [], []

    ctx_tokens = list(predictor.history[-predictor.ctx :])
    for width in candidates:
        seed = predictor.copy_seed_for_width(ctx_tokens, width)
        if seed is None:
            continue
        follower, match_len, confidence = seed
        if confidence < threshold:
            continue
        proposal = predictor.history[follower : follower + width]
        meta = {
            "source": "copy_runway",
            "match_len": match_len,
            "runway_tokens": width,
        }
        return width, confidence, proposal, [meta] * len(proposal)

    return choose_adaptive_width_with_sources(predictor, remaining, widths, threshold)


def choose_json_template_width_with_sources(
    predictor: JsonTemplateDraft,
    remaining: int,
    widths: list[int],
    threshold: float,
) -> tuple[int, float, list[int], list[dict[str, Any]]]:
    candidates = [width for width in widths if width <= remaining]
    if not candidates:
        return 0, 0.0, [], []

    max_width = candidates[0]
    proposal, source_meta = predictor.predict_many_with_sources(max_width, stop_below=threshold)
    prefix_mins: list[float] = []
    running_min = 1.0
    for meta in source_meta:
        running_min = min(running_min, float(meta.get("confidence") or 0.0))
        prefix_mins.append(running_min)
        if running_min < threshold:
            break

    for width in candidates:
        if width <= len(prefix_mins) and prefix_mins[width - 1] >= threshold:
            return width, prefix_mins[width - 1], proposal[:width], source_meta[:width]
    return 0, prefix_mins[-1] if prefix_mins else 0.0, [], source_meta[: len(prefix_mins)]


def choose_adaptive_width(
    predictor: token_ladder.NGramDraft,
    remaining: int,
    widths: list[int],
    threshold: float,
) -> tuple[int, float, list[int]]:
    width, confidence, proposal, _source_meta = choose_adaptive_width_with_sources(
        predictor,
        remaining,
        widths,
        threshold,
    )
    return width, confidence, proposal


def run_token_adaptive_span_branch(
    scenario: str,
    n_tokens: int,
    prefix_frac: float,
    ctx: int,
    widths: list[int],
    verifier_work: int,
    confidence_threshold: float,
    fallback_tokens: int,
) -> dict:
    stream = token_ladder.SCENARIOS[scenario](n_tokens)
    prefix_len = max(ctx + 2, int(len(stream) * prefix_frac))
    prefix_len = min(prefix_len, len(stream) - 1)
    widths = sorted({width for width in widths if width > 0}, reverse=True)
    if not widths:
        raise ValueError("adaptive widths must include at least one positive integer")
    fallback_tokens = max(1, fallback_tokens)

    baseline_output = []
    baseline_start = time.perf_counter()
    for idx in range(prefix_len, len(stream)):
        token_ladder.burn_work(verifier_work, idx)
        baseline_output.append(stream[idx])
    baseline_s = time.perf_counter() - baseline_start

    predictor = token_ladder.NGramDraft(ctx)
    predictor.prime(stream[:prefix_len])

    outputs: list[list[int]] = []
    pos = prefix_len
    accepted = 0
    rejected = 0
    repaired = 0
    attempted = 0
    fallback_units = 0
    fallback_token_calls = 0
    draft_s = 0.0
    verify_s = 0.0
    repair_s = 0.0
    fallback_s = 0.0
    chosen_widths: list[int] = []
    confidence_values: list[float] = []
    selector_s = 0.0

    while pos < len(stream):
        remaining = len(stream) - pos
        start = time.perf_counter()
        width, confidence, proposal = choose_adaptive_width(predictor, remaining, widths, confidence_threshold)
        elapsed = time.perf_counter() - start
        selector_s += elapsed
        draft_s += elapsed
        if width <= 0:
            width = min(fallback_tokens, remaining)
            start = time.perf_counter()
            truth = []
            for idx in range(pos, pos + width):
                token_ladder.burn_work(verifier_work, idx)
                tok = stream[idx]
                predictor.observe(tok)
                truth.append(tok)
            fallback_s += time.perf_counter() - start
            fallback_units += 1
            fallback_token_calls += len(truth)
            outputs.append(truth)
            chosen_widths.append(width)
            confidence_values.append(confidence)
            pos += width
            continue

        attempted += 1
        confidence_values.append(confidence)
        chosen_widths.append(width)

        start = time.perf_counter()
        token_ladder.burn_work(verifier_work, pos * 1315423911 + width)
        truth = stream[pos : pos + width]
        verify_s += time.perf_counter() - start

        if proposal == truth:
            accepted += 1
            for tok in truth:
                predictor.observe(tok)
            outputs.append(proposal)
        else:
            rejected += 1
            start = time.perf_counter()
            for tok in truth:
                predictor.observe(tok)
            repair_s += time.perf_counter() - start
            repaired += 1
            outputs.append(truth)
        pos += width

    speculative_s = draft_s + verify_s + repair_s + fallback_s
    output = flatten(outputs)
    exact = output == baseline_output
    units = attempted + fallback_units
    receipt = cx.SpecReceipt(
        branch_id=f"cx_native_token_adaptive_{scenario}_w{width_label(widths)}_c{threshold_label(confidence_threshold)}",
        modality="token",
        units=units,
        accepted_units=accepted,
        repaired_units=repaired,
        rejected_units=rejected,
        draft_s=draft_s,
        verify_s=verify_s,
        repair_s=repair_s,
        fallback_s=fallback_s,
        baseline_s=baseline_s,
        speculative_s=speculative_s,
        speedup_x=baseline_s / speculative_s if speculative_s > 0 else 0.0,
        exact=exact,
        quality_gate=exact,
        attempted_units=attempted,
        fallback_units=fallback_units,
        meta={
            "scenario": scenario,
            "n_tokens": len(stream),
            "prefix_tokens": prefix_len,
            "generated_tokens": len(stream) - prefix_len,
            "ctx": ctx,
            "unit_tokens": f"adaptive:{width_label(widths)}",
            "adaptive_unit_tokens": widths,
            "fallback_tokens": fallback_tokens,
            "verifier_work": verifier_work,
            "confidence_threshold": confidence_threshold,
            "gate_enabled": True,
            "strategy": "adaptive_span",
            "selector": "single_pass_prefix_min",
            "selector_s": round(selector_s, 6),
            "confidence_model": "largest candidate span whose minimum predicted-token probability clears threshold",
            "confidence": confidence_summary(confidence_values),
            "chosen_widths": {
                "min": min(chosen_widths) if chosen_widths else 0,
                "max": max(chosen_widths) if chosen_widths else 0,
                "mean": round(sum(chosen_widths) / len(chosen_widths), 6) if chosen_widths else 0.0,
            },
            "claim_scope": "CX-native local adaptive-span protocol receipt; not model-backed LLM throughput",
            "protocol": "adaptive SpecUnit(token_span)->DraftProducer(ngram)->Verifier(span)->RepairPolicy(span_truth/direct_fallback)",
        },
    )
    row = receipt.to_dict()
    row["branch_action"] = cx.decide_branch(receipt)
    row["output_tokens"] = len(output)
    row["fallback_token_calls"] = fallback_token_calls
    row["target_calls_baseline"] = len(baseline_output)
    row["target_calls_spec"] = attempted + fallback_token_calls
    row["target_call_reduction_x"] = round((len(baseline_output) / max(row["target_calls_spec"], 1)), 6)
    return row


def run_token_prefix_accept_adaptive_branch(
    scenario: str,
    n_tokens: int,
    prefix_frac: float,
    ctx: int,
    widths: list[int],
    verifier_work: int,
    confidence_threshold: float,
    fallback_tokens: int,
    proposal_source: str = "ngram",
    copy_min_match: int = 3,
    copy_max_candidates: int = 16,
) -> dict:
    stream = token_ladder.SCENARIOS[scenario](n_tokens)
    prefix_len = max(ctx + 2, int(len(stream) * prefix_frac))
    prefix_len = min(prefix_len, len(stream) - 1)
    widths = sorted({width for width in widths if width > 0}, reverse=True)
    if not widths:
        raise ValueError("adaptive widths must include at least one positive integer")
    fallback_tokens = max(1, fallback_tokens)

    baseline_output = []
    baseline_start = time.perf_counter()
    for idx in range(prefix_len, len(stream)):
        token_ladder.burn_work(verifier_work, idx)
        baseline_output.append(stream[idx])
    baseline_s = time.perf_counter() - baseline_start

    copy_source = proposal_source in {"copy_match", "copy_runway"}
    if proposal_source == "ngram":
        predictor: Any = token_ladder.NGramDraft(ctx)
        strategy = "prefix_accept_adaptive"
        branch_id = f"cx_native_token_prefix_adaptive_{scenario}_w{width_label(widths)}_c{threshold_label(confidence_threshold)}"
    elif proposal_source == "json_template":
        predictor = JsonTemplateDraft(ctx)
        strategy = "prefix_accept_json_template"
        branch_id = (
            f"cx_native_token_prefix_json_template_{scenario}"
            f"_w{width_label(widths)}_c{threshold_label(confidence_threshold)}"
        )
    elif copy_source:
        predictor = ContextCopyNGramDraft(
            ctx=ctx,
            min_match=copy_min_match,
            max_candidates=copy_max_candidates,
        )
        if proposal_source == "copy_match":
            strategy = "prefix_accept_copy_match"
            branch_prefix = "cx_native_token_prefix_copy_adaptive"
        else:
            strategy = "prefix_accept_copy_runway"
            branch_prefix = "cx_native_token_prefix_copy_runway_adaptive"
        branch_id = (
            f"{branch_prefix}_{scenario}_m{copy_min_match}_q{copy_max_candidates}"
            f"_w{width_label(widths)}_c{threshold_label(confidence_threshold)}"
        )
    else:
        raise ValueError(f"unknown proposal source {proposal_source!r}")
    predictor.prime(stream[:prefix_len])

    outputs: list[list[int]] = []
    pos = prefix_len
    accepted_tokens = 0
    repaired_tokens = 0
    reject_events = 0
    attempted_tokens = 0
    fallback_token_calls = 0
    verifier_calls = 0
    draft_s = 0.0
    verify_s = 0.0
    repair_s = 0.0
    fallback_s = 0.0
    chosen_widths: list[int] = []
    emitted_widths: list[int] = []
    accepted_prefixes: list[int] = []
    confidence_values: list[float] = []
    proposal_sources: Counter[str] = Counter()
    copy_match_lengths: list[int] = []
    copy_runway_lengths: list[int] = []
    json_template_lengths: list[int] = []
    selector_s = 0.0

    while pos < len(stream):
        remaining = len(stream) - pos
        start = time.perf_counter()
        if proposal_source == "copy_runway":
            width, confidence, proposal, source_meta = choose_copy_runway_width_with_sources(
                predictor,
                remaining,
                widths,
                confidence_threshold,
            )
        elif proposal_source == "json_template":
            width, confidence, proposal, source_meta = choose_json_template_width_with_sources(
                predictor,
                remaining,
                widths,
                confidence_threshold,
            )
        else:
            width, confidence, proposal, source_meta = choose_adaptive_width_with_sources(
                predictor,
                remaining,
                widths,
                confidence_threshold,
            )
        elapsed = time.perf_counter() - start
        selector_s += elapsed
        draft_s += elapsed
        for meta in source_meta:
            source = str(meta.get("source") or "unknown")
            proposal_sources[source] += 1
            if source == "copy_match":
                copy_match_lengths.append(int(meta.get("match_len") or 0))
            elif source == "copy_runway":
                copy_runway_lengths.append(int(meta.get("match_len") or 0))
            elif source == "json_template":
                json_template_lengths.append(int(meta.get("match_len") or 0))
        if width <= 0:
            width = min(fallback_tokens, remaining)
            start = time.perf_counter()
            truth = []
            for idx in range(pos, pos + width):
                token_ladder.burn_work(verifier_work, idx)
                tok = stream[idx]
                predictor.observe(tok)
                truth.append(tok)
            fallback_s += time.perf_counter() - start
            fallback_token_calls += len(truth)
            outputs.append(truth)
            chosen_widths.append(width)
            emitted_widths.append(width)
            accepted_prefixes.append(0)
            confidence_values.append(confidence)
            pos += width
            continue

        attempted_tokens += width
        verifier_calls += 1
        confidence_values.append(confidence)
        chosen_widths.append(width)

        start = time.perf_counter()
        token_ladder.burn_work(verifier_work, pos * 1315423911 + width)
        truth = stream[pos : pos + width]
        verify_s += time.perf_counter() - start

        accepted = 0
        for cand, real in zip(proposal, truth):
            if cand != real:
                break
            accepted += 1

        if accepted == width:
            emitted = truth
        else:
            reject_events += 1
            repaired_tokens += 1
            start = time.perf_counter()
            emitted = truth[: accepted + 1]
            repair_s += time.perf_counter() - start

        for tok in emitted:
            predictor.observe(tok)
        outputs.append(emitted)
        accepted_tokens += accepted
        accepted_prefixes.append(accepted)
        emitted_widths.append(len(emitted))
        pos += len(emitted)

    speculative_s = draft_s + verify_s + repair_s + fallback_s
    output = flatten(outputs)
    exact = output == baseline_output
    generated_tokens = len(baseline_output)
    receipt = cx.SpecReceipt(
        branch_id=branch_id,
        modality="token",
        units=generated_tokens,
        accepted_units=accepted_tokens,
        repaired_units=repaired_tokens,
        rejected_units=reject_events,
        draft_s=draft_s,
        verify_s=verify_s,
        repair_s=repair_s,
        fallback_s=fallback_s,
        baseline_s=baseline_s,
        speculative_s=speculative_s,
        speedup_x=baseline_s / speculative_s if speculative_s > 0 else 0.0,
        exact=exact,
        quality_gate=exact,
        attempted_units=attempted_tokens,
        fallback_units=fallback_token_calls,
        meta={
            "scenario": scenario,
            "n_tokens": len(stream),
            "prefix_tokens": prefix_len,
            "generated_tokens": generated_tokens,
            "ctx": ctx,
            "unit_tokens": f"prefix_adaptive:{width_label(widths)}",
            "adaptive_unit_tokens": widths,
            "fallback_tokens": fallback_tokens,
            "verifier_work": verifier_work,
            "confidence_threshold": confidence_threshold,
            "gate_enabled": True,
            "strategy": strategy,
            "accounting_unit": "tokens",
            "proposal_source": proposal_source,
            "copy_min_match": copy_min_match if copy_source else None,
            "copy_max_candidates": copy_max_candidates if copy_source else None,
            "proposal_sources": dict(proposal_sources),
            "copy_match_lengths": int_summary(copy_match_lengths),
            "copy_runway_lengths": int_summary(copy_runway_lengths),
            "json_template_lengths": int_summary(json_template_lengths),
            "selector": (
                "copy_runway_then_prefix_min" if proposal_source == "copy_runway"
                else "json_template_prefix_min" if proposal_source == "json_template"
                else "single_pass_prefix_min"
            ),
            "selector_s": round(selector_s, 6),
            "verifier_policy": "verify_span_accept_valid_prefix_repair_one",
            "verifier_calls": verifier_calls,
            "reject_events": reject_events,
            "confidence_model": "largest candidate span whose minimum predicted-token probability clears threshold",
            "confidence": confidence_summary(confidence_values),
            "chosen_widths": {
                "min": min(chosen_widths) if chosen_widths else 0,
                "max": max(chosen_widths) if chosen_widths else 0,
                "mean": round(sum(chosen_widths) / len(chosen_widths), 6) if chosen_widths else 0.0,
            },
            "accepted_prefixes": {
                "min": min(accepted_prefixes) if accepted_prefixes else 0,
                "max": max(accepted_prefixes) if accepted_prefixes else 0,
                "mean": round(sum(accepted_prefixes) / len(accepted_prefixes), 6) if accepted_prefixes else 0.0,
            },
            "emitted_widths": {
                "min": min(emitted_widths) if emitted_widths else 0,
                "max": max(emitted_widths) if emitted_widths else 0,
                "mean": round(sum(emitted_widths) / len(emitted_widths), 6) if emitted_widths else 0.0,
            },
            "claim_scope": "CX-native local prefix-accept adaptive protocol receipt; not model-backed LLM throughput",
            "protocol": f"adaptive SpecUnit(token_span)->DraftProducer({proposal_source})->Verifier(span)->AcceptPrefix->RepairOne/direct_fallback",
        },
    )
    row = receipt.to_dict()
    row["output_tokens"] = len(output)
    row["fallback_token_calls"] = fallback_token_calls
    row["target_calls_baseline"] = generated_tokens
    row["target_calls_spec"] = verifier_calls + fallback_token_calls
    row["target_call_reduction_x"] = round((generated_tokens / max(row["target_calls_spec"], 1)), 6)
    row["attempted_draft_tokens"] = attempted_tokens
    row["accepted_draft_tokens"] = accepted_tokens
    row["draft_token_acceptance"] = round(accepted_tokens / attempted_tokens, 6) if attempted_tokens else 0.0
    row["branch_policy"] = "prefix_accept_delivered_tokens_wall_clock_target_calls"
    row["draft_pressure"] = (
        "high" if row["draft_token_acceptance"] < 0.35
        else "medium" if row["draft_token_acceptance"] < 0.75
        else "low"
    )
    row["branch_action"] = decide_prefix_accept_row(row)
    return row


def measured_render_gates() -> list[dict]:
    quality_rows = render_ladder.filter_rows(
        render_ladder.load_quality_rows(render_ladder.DEFAULT_SOURCE_LEDGER),
        scene_pattern="",
        include_variants="raw,oidn,tile_refine",
    )
    tile_rows = render_ladder.filter_rows(
        render_ladder.load_tile_refinement_rows(render_ladder.DEFAULT_TILE_LEDGER),
        scene_pattern="",
        include_variants="raw,oidn,tile_refine",
    )
    gates = [render_ladder.gate_row(row) for row in quality_rows]
    gates.extend(render_ladder.gate_tile_refinement_row(row) for row in tile_rows)
    return gates


def render_branch_action(tier: str | None, speedup_x: float) -> str:
    if tier == "delivery" and speedup_x >= HARD_RENDER_FLOOR_X:
        return "grow"
    if tier in {"delivery", "preview"}:
        return "park"
    return "prune"


def render_gate_to_receipt(gate: dict) -> dict:
    tile_count = int(gate.get("tile_count") or 1)
    refined = int(gate.get("selected_tile_count") or gate.get("failed_tile_count") or 0)
    tier = gate.get("tier")
    if gate.get("accepted_tile_fraction_actual") is not None:
        accepted = max(tile_count - refined, 0)
    elif gate.get("accepted_tile_fraction_model") is not None:
        accepted = int(round(tile_count * float(gate.get("accepted_tile_fraction_model") or 0.0)))
    else:
        accepted = tile_count if gate.get("tier") == "delivery" else 0
    accepted = min(max(accepted, 0), tile_count)
    refined = min(max(refined, tile_count - accepted if gate.get("variant") == "tile_refine" else refined), tile_count)
    if gate.get("variant") == "tile_refine":
        rejected = refined
        repaired = refined
    else:
        rejected = 0 if tier == "delivery" else max(tile_count - accepted, 0)
        repaired = 0
    speedup_x = float(gate.get("net_speedup_if_shipped_x") or 0.0)
    receipt = cx.SpecReceipt(
        branch_id=f"cx_native_render_adapter_{gate.get('variant')}_{gate.get('scene')}",
        modality="render",
        units=tile_count,
        accepted_units=accepted,
        repaired_units=repaired,
        rejected_units=rejected,
        draft_s=float(gate.get("draft_time_s") or 0.0),
        verify_s=0.0,
        repair_s=max(float(gate.get("total_time_s") or 0.0) - float(gate.get("draft_time_s") or 0.0), 0.0),
        baseline_s=float(gate.get("ref_time_s") or 0.0),
        speculative_s=float(gate.get("total_time_s") or 0.0),
        speedup_x=speedup_x,
        exact=False,
        quality_gate=tier == "delivery",
        attempted_units=tile_count,
        fallback_units=0,
        meta={
            "claim_scope": "imported measured render receipt adapted to CX SpecReceipt; not a new render",
            "scene": gate.get("scene"),
            "variant": gate.get("variant"),
            "tier": tier,
            "global_ssim": gate.get("global_ssim"),
            "worst_tile_ssim": gate.get("worst_tile_ssim"),
            "p5_tile_ssim": gate.get("p5_tile_ssim"),
            "evidence_type": gate.get("evidence_type"),
            "source_ledger": gate.get("source_ledger"),
            "source_line": gate.get("source_line"),
            "action": gate.get("action"),
            "accepted_tile_fraction_model": gate.get("accepted_tile_fraction_model"),
            "accepted_tile_fraction_actual": gate.get("accepted_tile_fraction_actual"),
        },
    )
    row = receipt.to_dict()
    row["branch_action"] = render_branch_action(tier, receipt.speedup_x)
    return row


def render_receipts() -> list[dict]:
    gates = measured_render_gates()
    speculative_gates = [
        gate for gate in gates
        if gate.get("variant") != "raw" or gate.get("evidence_type") == "measured_tile_refinement"
    ]
    candidates = speculative_gates or gates
    return [render_gate_to_receipt(gate) for gate in candidates]


def best_render_receipt(render_rows: list[dict]) -> dict | None:
    return max(render_rows, key=lambda row: row.get("speedup_x") or 0.0, default=None)


def branch_rank(row: dict) -> tuple[int, float]:
    return ACTION_PRIORITY.get(row.get("branch_action"), 0), float(row.get("speedup_x") or 0.0)


def best_preferred_row(rows: list[dict]) -> dict | None:
    return max(rows, key=branch_rank, default=None)


def fastest_row(rows: list[dict]) -> dict | None:
    return max(rows, key=lambda row: float(row.get("speedup_x") or 0.0), default=None)


def best_rows_by_scenario(rows: list[dict]) -> dict:
    by_scenario = {}
    for row in rows:
        scenario = row["meta"]["scenario"]
        prev = by_scenario.get(scenario)
        if prev is None or branch_rank(row) > branch_rank(prev):
            by_scenario[scenario] = row
    return by_scenario


def summarize_token(rows: list[dict]) -> dict:
    gated_rows = [row for row in rows if row["meta"].get("gate_enabled")]
    ungated_rows = [row for row in rows if not row["meta"].get("gate_enabled")]
    span_adaptive_rows = [row for row in rows if row["meta"].get("strategy") in SPAN_STRATEGIES]
    prefix_ngram_rows = [row for row in rows if row["meta"].get("strategy") == "prefix_accept_adaptive"]
    prefix_copy_rows = [row for row in rows if row["meta"].get("strategy") == "prefix_accept_copy_match"]
    prefix_copy_runway_rows = [row for row in rows if row["meta"].get("strategy") == "prefix_accept_copy_runway"]
    prefix_json_template_rows = [row for row in rows if row["meta"].get("strategy") == "prefix_accept_json_template"]
    prefix_adaptive_rows = [row for row in rows if row["meta"].get("strategy") in PREFIX_STRATEGIES]
    adaptive_rows = span_adaptive_rows + prefix_adaptive_rows
    adaptive_strategies = SPAN_STRATEGIES | PREFIX_STRATEGIES
    fixed_gated_rows = [row for row in gated_rows if row["meta"].get("strategy") not in adaptive_strategies]
    grow_rows = [r for r in rows if r["branch_action"] == "grow"]
    park_rows = [r for r in rows if r["branch_action"] == "park"]
    prune_rows = [r for r in rows if r["branch_action"] == "prune"]
    by_scenario = best_rows_by_scenario(rows)
    by_scenario_gated = best_rows_by_scenario(gated_rows)
    by_scenario_fixed_gated = best_rows_by_scenario(fixed_gated_rows)
    by_scenario_adaptive = best_rows_by_scenario(span_adaptive_rows)
    by_scenario_prefix_adaptive = best_rows_by_scenario(prefix_ngram_rows)
    by_scenario_prefix_copy = best_rows_by_scenario(prefix_copy_rows)
    by_scenario_prefix_copy_runway = best_rows_by_scenario(prefix_copy_runway_rows)
    by_scenario_prefix_json_template = best_rows_by_scenario(prefix_json_template_rows)
    by_scenario_ungated = best_rows_by_scenario(ungated_rows)
    best = best_preferred_row(rows)
    best_gated = best_preferred_row(gated_rows)
    best_adaptive = best_preferred_row(adaptive_rows)
    best_prefix_adaptive = best_preferred_row(prefix_adaptive_rows)
    best_prefix_copy = best_preferred_row(prefix_copy_rows)
    best_prefix_copy_runway = best_preferred_row(prefix_copy_runway_rows)
    best_prefix_json_template = best_preferred_row(prefix_json_template_rows)
    return {
        "rows": len(rows),
        "best": best,
        "fastest": fastest_row(rows),
        "best_grow": fastest_row(grow_rows),
        "best_gated": best_gated,
        "fastest_gated": fastest_row(gated_rows),
        "best_adaptive": best_adaptive,
        "fastest_adaptive": fastest_row(adaptive_rows),
        "best_prefix_adaptive": best_prefix_adaptive,
        "fastest_prefix_adaptive": fastest_row(prefix_adaptive_rows),
        "best_prefix_copy": best_prefix_copy,
        "fastest_prefix_copy": fastest_row(prefix_copy_rows),
        "best_prefix_copy_runway": best_prefix_copy_runway,
        "fastest_prefix_copy_runway": fastest_row(prefix_copy_runway_rows),
        "best_prefix_json_template": best_prefix_json_template,
        "fastest_prefix_json_template": fastest_row(prefix_json_template_rows),
        "by_scenario": by_scenario,
        "by_scenario_gated": by_scenario_gated,
        "by_scenario_fixed_gated": by_scenario_fixed_gated,
        "by_scenario_adaptive": by_scenario_adaptive,
        "by_scenario_prefix_adaptive": by_scenario_prefix_adaptive,
        "by_scenario_prefix_copy": by_scenario_prefix_copy,
        "by_scenario_prefix_copy_runway": by_scenario_prefix_copy_runway,
        "by_scenario_prefix_json_template": by_scenario_prefix_json_template,
        "by_scenario_ungated": by_scenario_ungated,
        "gated_rows": len(gated_rows),
        "fixed_gated_rows": len(fixed_gated_rows),
        "adaptive_rows": len(adaptive_rows),
        "span_adaptive_rows": len(span_adaptive_rows),
        "prefix_ngram_rows": len(prefix_ngram_rows),
        "prefix_adaptive_rows": len(prefix_adaptive_rows),
        "prefix_copy_rows": len(prefix_copy_rows),
        "prefix_copy_runway_rows": len(prefix_copy_runway_rows),
        "prefix_json_template_rows": len(prefix_json_template_rows),
        "ungated_rows": len(ungated_rows),
        "grow_rows": len(grow_rows),
        "park_rows": len(park_rows),
        "prune_rows": len(prune_rows),
    }


def summarize_render(rows: list[dict]) -> dict:
    best = max(rows, key=lambda row: row.get("speedup_x") or 0.0, default=None)
    return {
        "rows": len(rows),
        "best": best,
        "grow_rows": len([row for row in rows if row["branch_action"] == "grow"]),
        "park_rows": len([row for row in rows if row["branch_action"] == "park"]),
        "prune_rows": len([row for row in rows if row["branch_action"] == "prune"]),
    }


def upsert_report_block(token_summary: dict, render_row: dict | None, render_summary: dict | None = None) -> None:
    best = token_summary.get("best") or {}
    fastest = token_summary.get("fastest") or {}
    best_gated = token_summary.get("best_gated") or {}
    best_adaptive = token_summary.get("best_adaptive") or {}
    best_prefix_adaptive = token_summary.get("best_prefix_adaptive") or {}
    best_prefix_copy = token_summary.get("best_prefix_copy") or {}
    best_prefix_copy_runway = token_summary.get("best_prefix_copy_runway") or {}
    best_prefix_json_template = token_summary.get("best_prefix_json_template") or {}
    lines = [
        "<!-- CX_NATIVE_SPINE_START -->",
        "## CX-Native Speculation Spine",
        "",
        "This is the primary branch now: ComputeExchange-owned `SpecUnit -> DraftProducer -> Verifier -> AcceptancePolicy -> RepairPolicy -> SpecReceipt` machinery.",
        "vLLM, Hawking, Cycles, ffmpeg, and future custom kernels are reference material or accelerators to mine, not the architecture boundary.",
        "",
        "Local CX-native token unit receipt:",
        "",
        f"- Rows: `{token_summary['rows']}`.",
        f"- Ungated rows: `{token_summary['ungated_rows']}`; fixed confidence-gated rows: `{token_summary['fixed_gated_rows']}`; adaptive rows: `{token_summary['adaptive_rows']}`.",
        f"- Span-adaptive rows: `{token_summary.get('span_adaptive_rows', 0)}`; prefix-accept adaptive rows: `{token_summary.get('prefix_adaptive_rows', 0)}`.",
        f"- Prefix proposal rows: n-gram `{token_summary.get('prefix_ngram_rows', 0)}`; copy-match `{token_summary.get('prefix_copy_rows', 0)}`; copy-runway `{token_summary.get('prefix_copy_runway_rows', 0)}`; JSON-template `{token_summary.get('prefix_json_template_rows', 0)}`.",
        f"- Grow rows: `{token_summary['grow_rows']}`; park rows: `{token_summary['park_rows']}`; prune rows: `{token_summary['prune_rows']}`.",
    ]
    if best:
        gate_note = (
            f"confidence threshold `{best['meta']['confidence_threshold']}`"
            if best["meta"].get("gate_enabled")
            else "ungated"
        )
        lines.extend([
            f"- Best preferred CX-native token speedup: `{best['speedup_x']}x` on `{best['meta']['scenario']}` with unit size `{best['meta']['unit_tokens']}`; action `{best['branch_action']}`.",
            f"- Best-row gate: {gate_note}; attempted fraction `{best['attempted_fraction']}`; fallback fraction `{best['fallback_fraction']}`.",
            f"- Accepted total fraction: `{best['accepted_fraction']}`; accepted attempted fraction: `{best['accepted_attempt_fraction']}`; exact: `{best['exact']}`.",
            "- Claim scope: local CX protocol receipt, not model-backed LLM throughput.",
        ])
    if fastest and fastest != best:
        lines.extend([
            f"- Fastest raw row: `{fastest['speedup_x']}x` on `{fastest['meta']['scenario']}`, action `{fastest['branch_action']}`; it is not treated as a grow row unless acceptance gates pass.",
        ])
    if best_gated:
        lines.extend([
            "",
            "Confidence-gated token receipt:",
            "",
            f"- Best gated row: `{best_gated['branch_id']}` at `{best_gated['speedup_x']}x`.",
            f"- Attempted fraction: `{best_gated['attempted_fraction']}`; fallback fraction: `{best_gated['fallback_fraction']}`.",
            f"- Accepted attempted fraction: `{best_gated['accepted_attempt_fraction']}`; target-call reduction: `{best_gated['target_call_reduction_x']}x`.",
        ])
    if best_adaptive:
        lines.extend([
            "",
            "Adaptive token receipt:",
            "",
            f"- Best adaptive row: `{best_adaptive['branch_id']}` at `{best_adaptive['speedup_x']}x`.",
            f"- Widths: `{best_adaptive['meta'].get('adaptive_unit_tokens')}`; threshold: `{best_adaptive['meta'].get('confidence_threshold')}`.",
            f"- Attempted fraction: `{best_adaptive['attempted_fraction']}`; fallback fraction: `{best_adaptive['fallback_fraction']}`; accepted attempted fraction: `{best_adaptive['accepted_attempt_fraction']}`.",
            f"- Target-call reduction: `{best_adaptive['target_call_reduction_x']}x`; chosen width mean: `{best_adaptive['meta'].get('chosen_widths', {}).get('mean')}`.",
        ])
    if best_prefix_adaptive:
        lines.extend([
            "",
            "Prefix-accept adaptive token receipt:",
            "",
            f"- Best prefix row: `{best_prefix_adaptive['branch_id']}` at `{best_prefix_adaptive['speedup_x']}x`.",
            f"- Widths: `{best_prefix_adaptive['meta'].get('adaptive_unit_tokens')}`; threshold: `{best_prefix_adaptive['meta'].get('confidence_threshold')}`.",
            f"- Proposal source: `{best_prefix_adaptive['meta'].get('proposal_source', 'ngram')}`; strategy: `{best_prefix_adaptive['meta'].get('strategy')}`.",
            f"- Accepted token fraction: `{best_prefix_adaptive['accepted_fraction']}`; draft-token acceptance: `{best_prefix_adaptive.get('draft_token_acceptance')}`.",
            f"- Target-call reduction: `{best_prefix_adaptive['target_call_reduction_x']}x`; verifier calls: `{best_prefix_adaptive['meta'].get('verifier_calls')}`.",
        ])
    if best_prefix_copy:
        lines.extend([
            "",
            "Copy-match prefix token receipt:",
            "",
            f"- Best copy-match prefix row: `{best_prefix_copy['branch_id']}` at `{best_prefix_copy['speedup_x']}x`.",
            f"- Min match: `{best_prefix_copy['meta'].get('copy_min_match')}`; proposal sources: `{best_prefix_copy['meta'].get('proposal_sources')}`.",
            f"- Accepted token fraction: `{best_prefix_copy['accepted_fraction']}`; target-call reduction: `{best_prefix_copy['target_call_reduction_x']}x`.",
            f"- Draft pressure: `{best_prefix_copy.get('draft_pressure')}`; exact: `{best_prefix_copy['exact']}`; action: `{best_prefix_copy['branch_action']}`.",
        ])
    if best_prefix_copy_runway:
        lines.extend([
            "",
            "Copy-runway prefix token receipt:",
            "",
            f"- Best copy-runway prefix row: `{best_prefix_copy_runway['branch_id']}` at `{best_prefix_copy_runway['speedup_x']}x`.",
            f"- Min match: `{best_prefix_copy_runway['meta'].get('copy_min_match')}`; candidate depth: `{best_prefix_copy_runway['meta'].get('copy_max_candidates')}`; proposal sources: `{best_prefix_copy_runway['meta'].get('proposal_sources')}`.",
            f"- Accepted token fraction: `{best_prefix_copy_runway['accepted_fraction']}`; target-call reduction: `{best_prefix_copy_runway['target_call_reduction_x']}x`.",
            f"- Draft pressure: `{best_prefix_copy_runway.get('draft_pressure')}`; exact: `{best_prefix_copy_runway['exact']}`; action: `{best_prefix_copy_runway['branch_action']}`.",
        ])
    if best_prefix_json_template:
        lines.extend([
            "",
            "JSON-template prefix token receipt:",
            "",
            f"- Best JSON-template prefix row: `{best_prefix_json_template['branch_id']}` at `{best_prefix_json_template['speedup_x']}x`.",
            f"- Widths: `{best_prefix_json_template['meta'].get('adaptive_unit_tokens')}`; proposal sources: `{best_prefix_json_template['meta'].get('proposal_sources')}`.",
            f"- Accepted token fraction: `{best_prefix_json_template['accepted_fraction']}`; target-call reduction: `{best_prefix_json_template['target_call_reduction_x']}x`.",
            f"- Draft pressure: `{best_prefix_json_template.get('draft_pressure')}`; exact: `{best_prefix_json_template['exact']}`; action: `{best_prefix_json_template['branch_action']}`.",
        ])
    if render_row:
        lines.extend([
            "",
            "Measured render receipt adapted into the same CX receipt shape:",
            "",
            (
                f"- Render adapter rows: `{render_summary.get('rows')}`; grow `{render_summary.get('grow_rows')}`, "
                f"park `{render_summary.get('park_rows')}`, prune `{render_summary.get('prune_rows')}`."
                if render_summary else
                "- Render adapter rows: `1`."
            ),
            f"- Branch: `{render_row['branch_id']}`.",
            f"- Scene: `{render_row['meta'].get('scene')}`; variant: `{render_row['meta'].get('variant')}`.",
            f"- Tier: `{render_row['meta'].get('tier')}`; speedup: `{render_row['speedup_x']}x`; action: `{render_row['branch_action']}`.",
            "- Claim scope: imported measured render receipt, not a new render.",
        ])
    lines.extend([
        "",
        "Next branch: grow the copy-match/copy-runway/JSON-template prefix predictors where they beat n-gram, and split into hotter bounded-index/selector and copy-aware confidence branches before any dependency-centered probe.",
        "<!-- CX_NATIVE_SPINE_END -->",
        "",
    ])
    block = "\n".join(lines)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    text = REPORT.read_text() if REPORT.exists() else f"# Spec Render + Token Decode Iteration - {DATE}\n\n"
    start = text.find("<!-- CX_NATIVE_SPINE_START -->")
    end = text.find("<!-- CX_NATIVE_SPINE_END -->")
    if start >= 0 and end >= start:
        end += len("<!-- CX_NATIVE_SPINE_END -->")
        text = text[:start] + block + text[end:]
    else:
        marker = "\n## Modality-General Local Receipts\n"
        if marker in text:
            text = text.replace(marker, "\n" + block + marker, 1)
        else:
            text += "\n" + block
    REPORT.write_text(text)


def insert_before_modality_general(text: str, insert: str) -> str:
    needle = "| modality_general_protocol |"
    idx = text.find(needle)
    if idx >= 0:
        return text[:idx] + insert + text[idx:]
    return text.rstrip() + "\n" + insert


def upsert_table_row(text: str, row: str) -> str:
    parts = row.split("|")
    if len(parts) < 3:
        return text
    branch = parts[1].strip()
    prefix = f"| {branch} |"
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if line.startswith(prefix):
            lines[idx] = row
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    return insert_before_modality_general(text, row + "\n")


def append_branch_report(token_summary: dict, render_row: dict | None) -> None:
    if not BRANCH_REPORT.exists():
        return
    text = BRANCH_REPORT.read_text()
    rows = []
    for scenario, row in sorted(token_summary["by_scenario_ungated"].items()):
        if row["branch_action"] == "grow":
            next_action = "grow predictor/unit sizes if accepted fraction stays high"
        elif row["branch_action"] == "park":
            next_action = "try smaller units or better predictor before pruning"
        else:
            next_action = "keep as negative control unless a stronger predictor appears"
        rows.append(
            f"| cx_native_token_unit_{scenario} | {row['branch_action']} | best `{row['speedup_x']}x`, "
            f"accepted units `{row['accepted_fraction']}`, exact `{row['exact']}` | {next_action} |"
        )

    for scenario, row in sorted(token_summary["by_scenario_fixed_gated"].items()):
        if row["branch_action"] == "grow":
            next_action = "grow the gate with larger/smarter units or a production hot path"
        elif row["branch_action"] == "park":
            next_action = "adjust confidence threshold/context before pruning"
        else:
            next_action = "keep as confidence-gate negative control"
        rows.append(
            f"| cx_native_token_gate_{scenario} | {row['branch_action']} | best `{row['speedup_x']}x`, "
            f"threshold `{row['meta']['confidence_threshold']}`, attempted `{row.get('attempted_fraction', 'n/a')}`, "
            f"fallback `{row.get('fallback_fraction', 'n/a')}`, accepted attempted `{row.get('accepted_attempt_fraction', 'n/a')}` | {next_action} |"
        )

    for scenario, row in sorted(token_summary["by_scenario_adaptive"].items()):
        if row["branch_action"] == "grow":
            next_action = "grow adaptive subspan policy and move hot path closer to production"
        elif row["branch_action"] == "park":
            next_action = "tune width ladder/threshold before pruning"
        else:
            next_action = "keep as adaptive negative control unless predictor improves"
        rows.append(
            f"| cx_native_token_adaptive_{scenario} | {row['branch_action']} | best `{row['speedup_x']}x`, "
            f"widths `{row['meta'].get('adaptive_unit_tokens')}`, threshold `{row['meta']['confidence_threshold']}`, "
            f"attempted `{row.get('attempted_fraction', 'n/a')}`, fallback `{row.get('fallback_fraction', 'n/a')}`, accepted attempted `{row.get('accepted_attempt_fraction', 'n/a')}` | {next_action} |"
        )

    for scenario, row in sorted(token_summary["by_scenario_prefix_adaptive"].items()):
        if row["branch_action"] == "grow":
            next_action = "grow prefix acceptance with smarter proposals and a hotter selector"
        elif row["branch_action"] == "park":
            next_action = "raise accepted coverage or reduce verifier/fallback calls before pruning"
        else:
            next_action = "keep as prefix-accept negative control unless predictor improves"
        rows.append(
            f"| {token_branch_name(row)} | {row['branch_action']} | best `{row['speedup_x']}x`, "
            f"source `{row['meta'].get('proposal_source', 'ngram')}`, "
            f"widths `{row['meta'].get('adaptive_unit_tokens')}`, threshold `{row['meta']['confidence_threshold']}`, "
            f"accepted tokens `{row['accepted_fraction']}`, draft acceptance `{row.get('draft_token_acceptance')}`, "
            f"target calls `{row.get('target_call_reduction_x')}x` | {next_action} |"
        )

    for scenario, row in sorted(token_summary["by_scenario_prefix_copy"].items()):
        if row["branch_action"] == "grow":
            next_action = "grow copy-match predictor or hot selector if it beats n-gram prefix for this scenario"
        elif row["branch_action"] == "park":
            next_action = "tune match length/confidence before pruning the copy proposal source"
        else:
            next_action = "keep as copy-match negative control unless structured reuse appears"
        rows.append(
            f"| {token_branch_name(row)} | {row['branch_action']} | best `{row['speedup_x']}x`, "
            f"source `{row['meta'].get('proposal_source', 'copy_match')}`, min match `{row['meta'].get('copy_min_match')}`, "
            f"candidate depth `{row['meta'].get('copy_max_candidates')}`, "
            f"widths `{row['meta'].get('adaptive_unit_tokens')}`, threshold `{row['meta']['confidence_threshold']}`, "
            f"accepted tokens `{row['accepted_fraction']}`, draft acceptance `{row.get('draft_token_acceptance')}`, "
            f"target calls `{row.get('target_call_reduction_x')}x` | {next_action} |"
        )

    for scenario, row in sorted(token_summary["by_scenario_prefix_copy_runway"].items()):
        if row["branch_action"] == "grow":
            next_action = "grow copy-runway selector depth/widths where whole-span reuse beats token-by-token copy"
        elif row["branch_action"] == "park":
            next_action = "tune runway depth/width caps before pruning this proposal source"
        else:
            next_action = "keep as copy-runway negative control unless verified runway appears"
        rows.append(
            f"| {token_branch_name(row)} | {row['branch_action']} | best `{row['speedup_x']}x`, "
            f"source `{row['meta'].get('proposal_source', 'copy_runway')}`, min match `{row['meta'].get('copy_min_match')}`, "
            f"candidate depth `{row['meta'].get('copy_max_candidates')}`, "
            f"widths `{row['meta'].get('adaptive_unit_tokens')}`, threshold `{row['meta']['confidence_threshold']}`, "
            f"accepted tokens `{row['accepted_fraction']}`, draft acceptance `{row.get('draft_token_acceptance')}`, "
            f"target calls `{row.get('target_call_reduction_x')}x` | {next_action} |"
        )

    for scenario, row in sorted(token_summary["by_scenario_prefix_json_template"].items()):
        if row["branch_action"] == "grow":
            next_action = "grow JSON-template parser/cache and structural predictors around the measured width knee"
        elif row["branch_action"] == "park":
            next_action = "tighten structural inference before pruning this proposal source"
        else:
            next_action = "keep as structured-output negative control unless JSON-like rows appear"
        rows.append(
            f"| {token_branch_name(row)} | {row['branch_action']} | best `{row['speedup_x']}x`, "
            f"source `{row['meta'].get('proposal_source', 'json_template')}`, "
            f"widths `{row['meta'].get('adaptive_unit_tokens')}`, threshold `{row['meta']['confidence_threshold']}`, "
            f"accepted tokens `{row['accepted_fraction']}`, draft acceptance `{row.get('draft_token_acceptance')}`, "
            f"target calls `{row.get('target_call_reduction_x')}x` | {next_action} |"
        )

    if render_row:
        rows.append(
            f"| cx_native_render_adapter | {render_row['branch_action']} | `{render_row['meta'].get('variant')}` `{render_row['speedup_x']}x`, tier `{render_row['meta'].get('tier')}` | grow resident/warm measured render path before more cold tile spend |"
        )

    if rows:
        for row in rows:
            text = upsert_table_row(text, row)
        BRANCH_REPORT.write_text(text)


def token_branch_name(row: dict) -> str:
    scenario = row["meta"]["scenario"]
    if row["meta"].get("strategy") == "prefix_accept_json_template":
        return f"cx_native_token_prefix_json_template_{scenario}"
    if row["meta"].get("strategy") == "prefix_accept_copy_runway":
        return f"cx_native_token_prefix_copy_runway_{scenario}"
    if row["meta"].get("strategy") == "prefix_accept_copy_match":
        return f"cx_native_token_prefix_copy_{scenario}"
    if row["meta"].get("strategy") == "prefix_accept_adaptive":
        return f"cx_native_token_prefix_{scenario}"
    if row["meta"].get("strategy") == "adaptive_span":
        return f"cx_native_token_adaptive_{scenario}"
    if row["meta"].get("gate_enabled"):
        return f"cx_native_token_gate_{scenario}"
    return f"cx_native_token_unit_{scenario}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scenarios", default="repeat,json,code,prose,random")
    ap.add_argument("--unit-tokens", default="4,8,16,32")
    ap.add_argument("--tokens", type=int, default=8192)
    ap.add_argument("--prefix-frac", type=float, default=0.25)
    ap.add_argument("--ctx", type=int, default=8)
    ap.add_argument("--verifier-work", type=int, default=4096)
    ap.add_argument("--confidence-thresholds", default="0,0.55,0.75,0.9")
    ap.add_argument("--adaptive-unit-tokens", default="")
    ap.add_argument("--adaptive-thresholds", default="0.55,0.75,0.9")
    ap.add_argument("--adaptive-fallback-tokens", type=int, default=1)
    ap.add_argument("--adaptive-modes", default="span", help="comma-separated adaptive modes: span,prefix,prefix_copy,prefix_copy_runway,prefix_json_template")
    ap.add_argument("--copy-min-match", default="3", help="comma-separated minimum copy-match context lengths")
    ap.add_argument("--copy-max-candidates", default="16", help="comma-separated copy index candidate depths")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    ks = [int(s.strip()) for s in args.unit_tokens.split(",") if s.strip()]
    confidence_thresholds = [float(s.strip()) for s in args.confidence_thresholds.split(",") if s.strip()]
    adaptive_widths = [int(s.strip()) for s in args.adaptive_unit_tokens.split(",") if s.strip()]
    adaptive_thresholds = [float(s.strip()) for s in args.adaptive_thresholds.split(",") if s.strip()]
    adaptive_modes = {s.strip() for s in args.adaptive_modes.split(",") if s.strip()}
    copy_min_matches = [int(s.strip()) for s in args.copy_min_match.split(",") if s.strip()]
    copy_max_candidates_values = [int(s.strip()) for s in args.copy_max_candidates.split(",") if s.strip()]
    unknown_modes = adaptive_modes - {"span", "prefix", "prefix_copy", "prefix_copy_runway", "prefix_json_template"}
    if unknown_modes:
        raise SystemExit(f"unknown adaptive mode(s): {', '.join(sorted(unknown_modes))}")
    copy_modes = adaptive_modes & {"prefix_copy", "prefix_copy_runway"}
    if copy_modes and not copy_min_matches:
        raise SystemExit("--copy-min-match produced no values")
    if copy_modes and not copy_max_candidates_values:
        raise SystemExit("--copy-max-candidates produced no values")

    token_rows = []
    for scenario in scenarios:
        if scenario not in token_ladder.SCENARIOS:
            raise SystemExit(f"unknown scenario {scenario!r}")
        for k in ks:
            for threshold in confidence_thresholds:
                row = run_token_unit_branch(
                    scenario=scenario,
                    n_tokens=args.tokens,
                    prefix_frac=args.prefix_frac,
                    ctx=args.ctx,
                    k=k,
                    verifier_work=args.verifier_work,
                    confidence_threshold=threshold,
                )
                token_rows.append(row)
                gate = f" c={threshold:g}" if threshold > 0 else ""
                print(
                    f"[cx-native] token {scenario} unit={k}{gate}: {row['speedup_x']}x "
                    f"attempt={row['attempted_fraction']} fallback={row['fallback_fraction']} "
                    f"accept_attempt={row['accepted_attempt_fraction']} action={row['branch_action']}"
                )
        if adaptive_widths and "span" in adaptive_modes:
            for threshold in adaptive_thresholds:
                row = run_token_adaptive_span_branch(
                    scenario=scenario,
                    n_tokens=args.tokens,
                    prefix_frac=args.prefix_frac,
                    ctx=args.ctx,
                    widths=adaptive_widths,
                    verifier_work=args.verifier_work,
                    confidence_threshold=threshold,
                    fallback_tokens=args.adaptive_fallback_tokens,
                )
                token_rows.append(row)
                print(
                    f"[cx-native] token {scenario} adaptive={width_label(sorted(set(adaptive_widths), reverse=True))} "
                    f"c={threshold:g}: {row['speedup_x']}x attempt={row['attempted_fraction']} "
                    f"fallback={row['fallback_fraction']} accept_attempt={row['accepted_attempt_fraction']} "
                    f"calls={row['target_call_reduction_x']} action={row['branch_action']}"
                )
        if adaptive_widths and "prefix" in adaptive_modes:
            for threshold in adaptive_thresholds:
                row = run_token_prefix_accept_adaptive_branch(
                    scenario=scenario,
                    n_tokens=args.tokens,
                    prefix_frac=args.prefix_frac,
                    ctx=args.ctx,
                    widths=adaptive_widths,
                    verifier_work=args.verifier_work,
                    confidence_threshold=threshold,
                    fallback_tokens=args.adaptive_fallback_tokens,
                )
                token_rows.append(row)
                print(
                    f"[cx-native] token {scenario} prefix-adaptive={width_label(sorted(set(adaptive_widths), reverse=True))} "
                    f"c={threshold:g}: {row['speedup_x']}x accepted={row['accepted_fraction']} "
                    f"draft_accept={row['draft_token_acceptance']} calls={row['target_call_reduction_x']} "
                    f"action={row['branch_action']}"
                )
        if adaptive_widths and "prefix_copy" in adaptive_modes:
            for min_match in copy_min_matches:
                for max_candidates in copy_max_candidates_values:
                    for threshold in adaptive_thresholds:
                        row = run_token_prefix_accept_adaptive_branch(
                            scenario=scenario,
                            n_tokens=args.tokens,
                            prefix_frac=args.prefix_frac,
                            ctx=args.ctx,
                            widths=adaptive_widths,
                            verifier_work=args.verifier_work,
                            confidence_threshold=threshold,
                            fallback_tokens=args.adaptive_fallback_tokens,
                            proposal_source="copy_match",
                            copy_min_match=min_match,
                            copy_max_candidates=max_candidates,
                        )
                        token_rows.append(row)
                        print(
                            f"[cx-native] token {scenario} prefix-copy={width_label(sorted(set(adaptive_widths), reverse=True))} "
                            f"m={min_match} q={max_candidates} c={threshold:g}: {row['speedup_x']}x accepted={row['accepted_fraction']} "
                            f"draft_accept={row['draft_token_acceptance']} calls={row['target_call_reduction_x']} "
                            f"source={row['meta'].get('proposal_sources')} action={row['branch_action']}"
                        )
        if adaptive_widths and "prefix_copy_runway" in adaptive_modes:
            for min_match in copy_min_matches:
                for max_candidates in copy_max_candidates_values:
                    for threshold in adaptive_thresholds:
                        row = run_token_prefix_accept_adaptive_branch(
                            scenario=scenario,
                            n_tokens=args.tokens,
                            prefix_frac=args.prefix_frac,
                            ctx=args.ctx,
                            widths=adaptive_widths,
                            verifier_work=args.verifier_work,
                            confidence_threshold=threshold,
                            fallback_tokens=args.adaptive_fallback_tokens,
                            proposal_source="copy_runway",
                            copy_min_match=min_match,
                            copy_max_candidates=max_candidates,
                        )
                        token_rows.append(row)
                        print(
                            f"[cx-native] token {scenario} prefix-copy-runway={width_label(sorted(set(adaptive_widths), reverse=True))} "
                            f"m={min_match} q={max_candidates} c={threshold:g}: {row['speedup_x']}x accepted={row['accepted_fraction']} "
                            f"draft_accept={row['draft_token_acceptance']} calls={row['target_call_reduction_x']} "
                            f"source={row['meta'].get('proposal_sources')} action={row['branch_action']}"
                        )
        if adaptive_widths and "prefix_json_template" in adaptive_modes:
            for threshold in adaptive_thresholds:
                row = run_token_prefix_accept_adaptive_branch(
                    scenario=scenario,
                    n_tokens=args.tokens,
                    prefix_frac=args.prefix_frac,
                    ctx=args.ctx,
                    widths=adaptive_widths,
                    verifier_work=args.verifier_work,
                    confidence_threshold=threshold,
                    fallback_tokens=args.adaptive_fallback_tokens,
                    proposal_source="json_template",
                )
                token_rows.append(row)
                print(
                    f"[cx-native] token {scenario} prefix-json-template={width_label(sorted(set(adaptive_widths), reverse=True))} "
                    f"c={threshold:g}: {row['speedup_x']}x accepted={row['accepted_fraction']} "
                    f"draft_accept={row['draft_token_acceptance']} calls={row['target_call_reduction_x']} "
                    f"source={row['meta'].get('proposal_sources')} action={row['branch_action']}"
                )

    render_rows = render_receipts()
    render_row = best_render_receipt(render_rows)
    render_summary = summarize_render(render_rows)
    if render_row:
        print(
            f"[cx-native] render adapter {render_row['branch_id']}: "
            f"{render_row['speedup_x']}x tier={render_row['meta'].get('tier')} "
            f"action={render_row['branch_action']} rows={render_summary['rows']}"
        )

    token_summary = summarize_token(token_rows)
    result = {
        "ok": all(r["exact"] for r in token_rows),
        "token_summary": token_summary,
        "render_summary": render_summary,
        "render_receipt": render_row,
        "reports": {
            "ledger": str(LEDGER),
            "branch_ledger": str(BRANCH_LEDGER),
            "report": str(REPORT),
            "branch_report": str(BRANCH_REPORT),
        },
    }

    if not args.no_write:
        for row in token_rows:
            append_jsonl(LEDGER, {"event": "cx_native_token_unit_receipt", "row": row})
            append_jsonl(
                BRANCH_LEDGER,
                {
                    "event": "branch_receipt",
                    "branch": token_branch_name(row),
                    "status": row["branch_action"],
                    "speedup_x": row["speedup_x"],
                    "accepted_fraction": row["accepted_fraction"],
                    "exact": row["exact"],
                    "row": row,
                },
            )
        for row in render_rows:
            append_jsonl(LEDGER, {"event": "cx_native_render_adapter_receipt", "row": row})
            append_jsonl(
                BRANCH_LEDGER,
                {
                    "event": "branch_receipt",
                    "branch": "cx_native_render_adapter",
                    "status": row["branch_action"],
                    "speedup_x": row["speedup_x"],
                    "quality_gate": row["quality_gate"],
                    "row": row,
                },
            )
        append_jsonl(LEDGER, {"event": "result", "result": result})
        cumulative_token_rows = load_token_rows(LEDGER)
        cumulative_token_summary = summarize_token(cumulative_token_rows)
        upsert_report_block(cumulative_token_summary, render_row, render_summary)
        append_branch_report(cumulative_token_summary, render_row)

    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
