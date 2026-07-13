#!/usr/bin/env python3
"""CX-native speculative execution primitives.

This is the ComputeExchange-owned spine for speculation across modalities. A
backend can mine vLLM, Hawking, Cycles, ffmpeg, or custom kernels later, but the
control loop and receipts here are ours:

    draft units -> verify -> accept or repair -> receipt -> grow/prune
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable


MAX_META_JSON_BYTES = 512 << 10
MAX_META_JSON_DEPTH = 32


def _canonical_json_object_bytes(value: Any, *, label: str) -> bytes:
    """Validate one wire metadata object and return its canonical JSON bytes.

    Python's ``json.dumps`` accepts non-string mapping keys and can consequently
    serialize ``{1: ..., "1": ...}`` as an ambiguous duplicate-key object.  The
    Rust ingress correctly rejects that wire shape, so fail at the common source
    instead.  The depth/size bounds also keep arbitrary adapter metadata from
    turning a tiny receipt into unbounded worker or collector work.
    """
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a dict")
    stack: list[tuple[Any, int]] = [(value, 1)]
    seen_depth: dict[int, int] = {}
    while stack:
        current, depth = stack.pop()
        if isinstance(current, (dict, list, tuple)):
            if depth > MAX_META_JSON_DEPTH:
                raise ValueError(
                    f"{label} JSON nesting exceeds {MAX_META_JSON_DEPTH}"
                )
            identity = id(current)
            if seen_depth.get(identity, 0) >= depth:
                continue
            seen_depth[identity] = depth
        if isinstance(current, dict):
            for key, child in current.items():
                if not isinstance(key, str):
                    raise ValueError(f"{label} JSON object keys must be strings")
                stack.append((child, depth + 1))
        elif isinstance(current, (list, tuple)):
            stack.extend((child, depth + 1) for child in current)
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(
            f"{label} must be finite, acyclic, and JSON serializable"
        ) from exc
    if len(encoded) > MAX_META_JSON_BYTES:
        raise ValueError(
            f"{label} is {len(encoded)} UTF-8 bytes; limit is "
            f"{MAX_META_JSON_BYTES}"
        )
    return encoded


def _safe_error(exc: BaseException | str) -> str:
    """Bound diagnostics without trusting an exception's ``__str__`` method."""
    if isinstance(exc, str):
        text = exc
    else:
        try:
            text = f"{type(exc).__name__}: {exc}"
        except BaseException:
            text = f"{type(exc).__name__}: <unprintable exception>"
    return text.encode("utf-8", errors="replace")[:500].decode(
        "utf-8", errors="replace"
    )


def _valid_wire_id(value: Any, *, max_bytes: int) -> bool:
    return (isinstance(value, str) and bool(value.strip())
            and len(value.encode("utf-8")) <= max_bytes)


def _valid_modality(value: Any) -> bool:
    return (_valid_wire_id(value, max_bytes=64) and value.isascii()
            and all(ch.isalnum() or ch in "._-" for ch in value))


@dataclass(frozen=True)
class SpecUnit:
    unit_id: str
    modality: str
    payload: Any
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _valid_wire_id(self.unit_id, max_bytes=256):
            raise ValueError("unit_id must be non-empty and <= 256 UTF-8 bytes")
        if not _valid_modality(self.modality):
            raise ValueError("modality must be 1..64 ASCII alphanumeric/._- bytes")
        _canonical_json_object_bytes(self.meta, label="unit meta")


@dataclass(frozen=True)
class DraftProposal:
    unit: SpecUnit
    draft: Any
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.unit, SpecUnit):
            raise TypeError("proposal.unit must be a SpecUnit")
        _canonical_json_object_bytes(self.meta, label="proposal meta")


@dataclass(frozen=True)
class Verification:
    accepted: bool
    truth: Any
    quality: float | None = None
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool):
            raise TypeError("accepted must be a bool")
        if self.quality is not None and (
            not isinstance(self.quality, (int, float))
            or isinstance(self.quality, bool)
            or not math.isfinite(self.quality)
        ):
            raise ValueError("quality must be null or a finite number")
        if not isinstance(self.reason, str) or len(self.reason.encode("utf-8")) > 1_000:
            raise ValueError("reason must be a string <= 1000 UTF-8 bytes")
        _canonical_json_object_bytes(self.meta, label="verification meta")


@dataclass(frozen=True)
class RepairResult:
    output: Any
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _canonical_json_object_bytes(self.meta, label="repair meta")


@dataclass(frozen=True)
class SpecReceipt:
    branch_id: str
    modality: str
    units: int
    accepted_units: int
    repaired_units: int
    rejected_units: int
    draft_s: float
    verify_s: float
    repair_s: float
    baseline_s: float
    speculative_s: float
    speedup_x: float
    exact: bool
    quality_gate: bool
    # Explicit local modality-contract proof. This is not server attestation,
    # but even branch promotion must not infer it from speed/evidence labels.
    artifact_verified: bool = False
    attempted_units: int | None = None
    fallback_units: int = 0
    fallback_s: float = 0.0
    # Wall time not owned by draft/verify/repair/fallback callbacks: policy,
    # comparison, assembly and accounting. It is explicit so the headline never
    # hides orchestration overhead.
    overhead_s: float = 0.0
    baseline_source: str = "measured"
    evidence: str = "synthetic"
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _valid_wire_id(self.branch_id, max_bytes=256):
            raise ValueError("branch_id must be non-empty and <= 256 UTF-8 bytes")
        if not _valid_modality(self.modality):
            raise ValueError("modality must be 1..64 ASCII alphanumeric/._- bytes")
        for name in (
            "units", "accepted_units", "repaired_units", "rejected_units",
            "fallback_units",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.attempted_units is not None and (
            not isinstance(self.attempted_units, int)
            or isinstance(self.attempted_units, bool)
            or self.attempted_units < 0
        ):
            raise ValueError("attempted_units must be null or a non-negative integer")
        if self.accepted_units > self.effective_attempted_units:
            raise ValueError("accepted_units cannot exceed attempted_units")
        if self.effective_attempted_units > self.units:
            raise ValueError("attempted_units cannot exceed units")
        if self.accepted_units + self.rejected_units != self.effective_attempted_units:
            raise ValueError("accepted_units + rejected_units must equal attempted_units")
        if self.repaired_units > self.rejected_units:
            raise ValueError("repaired_units cannot exceed rejected_units")
        if self.fallback_units > self.units:
            raise ValueError("fallback_units cannot exceed units")
        for name in (
            "draft_s", "verify_s", "repair_s", "fallback_s", "overhead_s",
            "baseline_s", "speculative_s", "speedup_x",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{name} must be finite and >= 0, got {value!r}")
        if self.speculative_s > 0:
            parts = self.draft_s + self.verify_s + self.repair_s + self.fallback_s + self.overhead_s
            # Direct constructors in older experiment ladders measure their
            # phase sum, while the engine below measures enclosing wall time.
            if not math.isclose(self.speculative_s, parts, rel_tol=0.0, abs_tol=5e-6):
                raise ValueError(
                    f"speculative_s {self.speculative_s} contradicts charged phase sum {parts}"
                )
        if (not isinstance(self.exact, bool) or not isinstance(self.quality_gate, bool)
                or not isinstance(self.artifact_verified, bool)):
            raise TypeError("exact, quality_gate and artifact_verified must be bools")
        if self.baseline_source not in {"measured", "modeled", "absent"}:
            raise ValueError("baseline_source must be measured|modeled|absent")
        if self.evidence not in {"measured", "modeled", "synthetic", "imported"}:
            raise ValueError("evidence must be measured|modeled|synthetic|imported")
        if self.artifact_verified and (
            self.evidence != "measured" or not self.quality_gate
        ):
            raise ValueError(
                "artifact_verified requires measured evidence and a passing quality gate"
            )
        if self.baseline_source == "absent":
            if self.baseline_s != 0 or self.speedup_x != 0:
                raise ValueError("baseline_source=absent requires zero baseline and speedup")
        elif self.baseline_s <= 0:
            raise ValueError("measured/modeled baseline_source requires baseline_s > 0")
        if self.units == 0:
            if any((self.accepted_units, self.repaired_units, self.rejected_units,
                    self.fallback_units, self.effective_attempted_units)):
                raise ValueError("an empty receipt must have zero unit counts")
            if any((self.draft_s, self.verify_s, self.repair_s, self.fallback_s,
                    self.overhead_s, self.speculative_s)) or self.exact or self.quality_gate or self.artifact_verified:
                raise ValueError("an empty receipt cannot claim work or correctness")
        elif self.speculative_s <= 0:
            raise ValueError("a non-empty receipt must charge positive speculative_s")
        if self.baseline_source != "absent" and self.speculative_s > 0:
            expected_speedup = self.baseline_s / self.speculative_s
            if not math.isclose(self.speedup_x, expected_speedup, rel_tol=0.0, abs_tol=5e-6):
                raise ValueError("speedup_x contradicts baseline_s/speculative_s")
        _canonical_json_object_bytes(self.meta, label="receipt meta")

    @property
    def effective_attempted_units(self) -> int:
        if self.attempted_units is not None:
            return self.attempted_units
        return max(self.units - self.fallback_units, 0)

    @property
    def accepted_fraction(self) -> float:
        return self.accepted_units / self.units if self.units else 0.0

    @property
    def accepted_attempt_fraction(self) -> float:
        attempted = self.effective_attempted_units
        return self.accepted_units / attempted if attempted else 0.0

    @property
    def repaired_fraction(self) -> float:
        return self.repaired_units / self.units if self.units else 0.0

    @property
    def fallback_fraction(self) -> float:
        return self.fallback_units / self.units if self.units else 0.0

    @property
    def attempted_fraction(self) -> float:
        return self.effective_attempted_units / self.units if self.units else 0.0

    def to_dict(self) -> dict[str, Any]:
        # Frozen dataclasses do not recursively freeze a dict. Revalidate at the
        # wire boundary and return a detached JSON value so post-construction
        # mutation cannot emit non-finite, cyclic, duplicate-key metadata.
        meta_wire = json.loads(
            _canonical_json_object_bytes(self.meta, label="receipt meta")
        )
        baseline_wire = round(self.baseline_s, 6)
        speculative_wire = round(self.speculative_s, 6)
        speedup_wire = (
            baseline_wire / speculative_wire
            if self.baseline_source != "absent" and speculative_wire > 0
            else None
        )
        return {
            "schema_version": 1,
            "branch_id": self.branch_id,
            "modality": self.modality,
            "units": self.units,
            "attempted_units": self.effective_attempted_units,
            "fallback_units": self.fallback_units,
            "accepted_units": self.accepted_units,
            "repaired_units": self.repaired_units,
            "rejected_units": self.rejected_units,
            "attempted_fraction": round(self.attempted_fraction, 6),
            "fallback_fraction": round(self.fallback_fraction, 6),
            "accepted_fraction": round(self.accepted_fraction, 6),
            "accepted_attempt_fraction": round(self.accepted_attempt_fraction, 6),
            "repaired_fraction": round(self.repaired_fraction, 6),
            "draft_s": round(self.draft_s, 6),
            "verify_s": round(self.verify_s, 6),
            "repair_s": round(self.repair_s, 6),
            "fallback_s": round(self.fallback_s, 6),
            # Canonical SpecReceipt has no separate fallback phase. Fold the
            # authoritative fallback path into canonical overhead while keeping
            # fallback_s as a transparency extra.
            "overhead_s": round(self.overhead_s + self.fallback_s, 6),
            "baseline_s": baseline_wire,
            "speculative_s": speculative_wire,
            "speedup_x": round(speedup_wire, 6) if speedup_wire is not None else None,
            "exact": self.exact,
            "artifact_verified": self.artifact_verified,
            "quality_gate": self.quality_gate,
            "quality_tier": "delivery" if self.quality_gate else "fail",
            "baseline_source": self.baseline_source,
            "evidence": self.evidence,
            "meta": meta_wire,
        }


def timed(fn: Callable[[], Any]) -> tuple[Any, float]:
    start = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - start


def decide_branch(receipt: SpecReceipt, min_speedup: float = 1.2, min_accept: float = 0.35) -> str:
    if not all(math.isfinite(v) for v in (min_speedup, min_accept)):
        raise ValueError("branch thresholds must be finite")
    if min_speedup <= 0 or not 0 <= min_accept <= 1:
        raise ValueError("min_speedup must be > 0 and min_accept must be in [0,1]")
    if not receipt.exact or not receipt.quality_gate:
        return "kill_correctness"
    if (
        receipt.evidence != "measured"
        or receipt.baseline_source != "measured"
        or not receipt.artifact_verified
    ):
        return "park"
    if (
        receipt.speedup_x >= min_speedup
        and receipt.accepted_fraction >= min_accept
        and receipt.accepted_attempt_fraction >= min_accept
    ):
        return "grow"
    if receipt.speedup_x < 1.0 or receipt.accepted_fraction < 0.08 or receipt.accepted_attempt_fraction < 0.08:
        return "prune"
    return "park"


class SpeculativeEngine:
    """Generic single-pass speculative resolver.

    This is intentionally tiny. Custom token loops, tile schedulers, or kernels
    can bypass the Python loop later while still emitting the same SpecReceipt.
    """

    def __init__(
        self,
        branch_id: str,
        modality: str,
        draft: Callable[[SpecUnit], DraftProposal],
        verify: Callable[[DraftProposal], Verification],
        repair: Callable[[DraftProposal, Verification], RepairResult],
        baseline: Callable[[SpecUnit], Any],
        should_speculate: Callable[[SpecUnit], bool] | None = None,
        fallback: Callable[[SpecUnit], Any] | None = None,
        equal: Callable[[Any, Any], bool] | None = None,
        benchmark_equal: Callable[[Any, Any], bool] | None = None,
        quality_gate: Callable[[SpecReceipt], bool] | None = None,
        evidence: str = "synthetic",
        max_units: int = 100_000,
    ):
        if not _valid_wire_id(branch_id, max_bytes=256):
            raise ValueError("branch_id must be non-empty and <= 256 UTF-8 bytes")
        if not _valid_modality(modality):
            raise ValueError("modality must be 1..64 ASCII alphanumeric/._- bytes")
        if not isinstance(max_units, int) or isinstance(max_units, bool) or not 1 <= max_units <= 1_000_000:
            raise ValueError("max_units must be an integer in [1,1000000]")
        if evidence not in {"measured", "modeled", "synthetic", "imported"}:
            raise ValueError("evidence must be measured|modeled|synthetic|imported")
        for name, callback in (
            ("draft", draft), ("verify", verify), ("repair", repair),
            ("baseline", baseline),
        ):
            if not callable(callback):
                raise TypeError(f"{name} must be callable")
        for name, callback in (
            ("should_speculate", should_speculate), ("fallback", fallback),
            ("equal", equal), ("benchmark_equal", benchmark_equal),
            ("quality_gate", quality_gate),
        ):
            if callback is not None and not callable(callback):
                raise TypeError(f"{name} must be callable or None")
        self.branch_id = branch_id
        self.modality = modality
        self.draft = draft
        self.verify = verify
        self.repair = repair
        self.baseline = baseline
        self.should_speculate = should_speculate
        self.fallback = fallback if fallback is not None else baseline
        self._fallback_is_baseline = fallback is None or fallback is baseline
        self.equal = equal or (lambda a, b: a == b)
        # Product acceptance is checked against Verification.truth. The
        # counterfactual baseline is compared separately and can invalidate the
        # speed claim, but it must never steer a successful artifact without its
        # full cost being charged. A separate hook lets large-artifact lanes use
        # a digest comparison for the benchmark audit.
        self.benchmark_equal = benchmark_equal or self.equal
        self.quality_gate = quality_gate
        self.evidence = evidence
        self.max_units = max_units

    def run(
        self,
        units: Iterable[SpecUnit],
        meta: dict[str, Any] | None = None,
        *,
        measure_baseline: bool = True,
    ) -> tuple[list[Any], SpecReceipt]:
        # Materialize only up to a hard cap. The previous unbounded list() made a
        # bad generator sufficient to exhaust a worker before any gate ran.
        iterator = iter(units)
        unit_list = []
        for _ in range(self.max_units + 1):
            try:
                unit_list.append(next(iterator))
            except StopIteration:
                break
        if len(unit_list) > self.max_units:
            raise ValueError(f"unit count exceeds max_units={self.max_units}")
        if not unit_list:
            raise ValueError("speculative execution requires at least one unit")
        if meta is not None and not isinstance(meta, dict):
            raise TypeError("meta must be a dict or None")
        if meta is not None:
            _canonical_json_object_bytes(meta, label="run meta")
        if not isinstance(measure_baseline, bool):
            raise TypeError("measure_baseline must be a bool")
        if not measure_baseline and not self._fallback_is_baseline:
            raise ValueError(
                "production mode requires the authoritative baseline as fallback"
            )

        # Validate the whole identity ledger before running even the first
        # callback. Duplicate ids make failure events and per-unit evidence
        # ambiguous, and discovering one after earlier product work has run is
        # unnecessarily unsafe.
        unit_ids: set[str] = set()
        for unit in unit_list:
            if not isinstance(unit, SpecUnit):
                raise TypeError(
                    f"units must contain SpecUnit, got {type(unit).__name__}"
                )
            if unit.modality != self.modality:
                raise ValueError(
                    f"unit {unit.unit_id!r} modality {unit.modality!r} does not match "
                    f"engine modality {self.modality!r}"
                )
            if unit.unit_id in unit_ids:
                raise ValueError(f"duplicate unit_id {unit.unit_id!r}")
            unit_ids.add(unit.unit_id)

        outputs: list[Any] = []
        baseline_outputs: list[Any] = []
        baseline_elapsed_by_unit: list[float] = []
        used_baseline_output: list[bool] = []
        baseline_product_charged: list[bool] = []
        accepted = 0
        repaired = 0
        rejected = 0
        attempted = 0
        fallback_units = 0
        reused_baseline_fallback_units = 0
        draft_s = 0.0
        verify_s = 0.0
        repair_s = 0.0
        fallback_s = 0.0
        # Baseline time reused as the delivered fallback is a real product
        # charge, but it ran before the per-unit product-wall clock. Keep it
        # separate so it cannot hide policy/assembly overhead.
        virtual_fallback_s = 0.0
        overhead_s = 0.0
        measured_baseline_s = 0.0
        baseline_audit_s = 0.0
        baseline_comparable = measure_baseline
        candidate_exact = bool(unit_list)
        failure_events: list[dict[str, str]] = []
        benchmark_events: list[dict[str, str]] = []
        missing = object()

        def record_failure(unit: SpecUnit, phase: str, exc: BaseException | str) -> None:
            nonlocal candidate_exact
            candidate_exact = False
            if len(failure_events) < 100:
                failure_events.append({
                    "unit_id": unit.unit_id,
                    "phase": phase,
                    "error": _safe_error(exc),
                })

        def record_benchmark_failure(unit: SpecUnit, exc: BaseException | str) -> None:
            nonlocal baseline_comparable
            baseline_comparable = False
            if len(benchmark_events) < 100:
                benchmark_events.append({
                    "unit_id": unit.unit_id,
                    "phase": "baseline_comparability",
                    "error": _safe_error(exc),
                })

        for unit in unit_list:
            ref: Any = missing
            baseline_elapsed = 0.0
            baseline_precomputed = False
            if measure_baseline:
                # Benchmark-only counterfactual. Verification.truth remains the
                # charged product oracle on successful speculation.
                try:
                    ref, baseline_elapsed = timed(
                        lambda unit=unit: self.baseline(unit)
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f"baseline failed for unit {unit.unit_id!r}: {_safe_error(exc)}"
                    ) from exc
                measured_baseline_s += baseline_elapsed
                baseline_precomputed = True
            baseline_outputs.append(ref)
            baseline_elapsed_by_unit.append(baseline_elapsed)

            def load_product_baseline() -> tuple[Any, float, bool]:
                nonlocal ref, baseline_elapsed, baseline_precomputed
                if ref is missing:
                    try:
                        ref, baseline_elapsed = timed(
                            lambda unit=unit: self.baseline(unit)
                        )
                    except Exception as exc:
                        raise RuntimeError(
                            f"baseline fallback failed for unit {unit.unit_id!r}: "
                            f"{_safe_error(exc)}"
                        ) from exc
                    baseline_outputs[-1] = ref
                    baseline_elapsed_by_unit[-1] = baseline_elapsed
                return ref, baseline_elapsed, baseline_precomputed

            spec_wall_start = time.perf_counter()
            phase_before = (
                draft_s + verify_s + repair_s + fallback_s - virtual_fallback_s
            )

            try:
                if self.should_speculate is None:
                    speculate = True
                else:
                    speculate = self.should_speculate(unit)
                    if not isinstance(speculate, bool):
                        raise TypeError("should_speculate must return a bool")
            except Exception as exc:
                record_failure(unit, "should_speculate", exc)
                speculate = False

            if not speculate:
                fallback_units += 1
                if self._fallback_is_baseline:
                    # The authoritative baseline artifact is exactly the default
                    # fallback. Reuse the already measured call instead of doing
                    # the same expensive render/decode twice. Charge its elapsed
                    # time to the delivered fallback path as well as the benchmark
                    # denominator, yielding the honest 1.0x fallback ratio.
                    ref, baseline_elapsed, was_precomputed = load_product_baseline()
                    if was_precomputed:
                        reused_baseline_fallback_units += 1
                    fallback_s += baseline_elapsed
                    if was_precomputed:
                        virtual_fallback_s += baseline_elapsed
                    outputs.append(ref)
                    used_baseline_output.append(True)
                    baseline_product_charged.append(True)
                else:
                    fallback_start = time.perf_counter()
                    # The custom fallback is authorized against the baseline, so
                    # the already-measured baseline is product work on this path.
                    fallback_s += baseline_elapsed
                    virtual_fallback_s += baseline_elapsed
                    try:
                        out = self.fallback(unit)
                        try:
                            same = self.equal(out, ref)
                            if not isinstance(same, bool):
                                raise TypeError("equal must return a bool")
                        except Exception as exc:
                            record_failure(unit, "compare_fallback", exc)
                            same = False
                        if not same:
                            record_failure(unit, "fallback", "fallback output diverged from baseline")
                            out = ref
                            used_baseline_output.append(True)
                        else:
                            used_baseline_output.append(False)
                        outputs.append(out)
                    except Exception as exc:
                        record_failure(unit, "fallback", exc)
                        outputs.append(ref)
                        used_baseline_output.append(True)
                    finally:
                        fallback_s += time.perf_counter() - fallback_start
                    baseline_product_charged.append(True)
                overhead_s += max(
                    time.perf_counter() - spec_wall_start
                    - (
                        (draft_s + verify_s + repair_s + fallback_s - virtual_fallback_s)
                        - phase_before
                    ),
                    0.0,
                )
                continue

            attempted += 1
            candidate: Any = None
            candidate_ok = False
            claimed_accept = False
            try:
                phase_start = time.perf_counter()
                try:
                    proposal = self.draft(unit)
                finally:
                    draft_s += time.perf_counter() - phase_start
                if not isinstance(proposal, DraftProposal):
                    raise TypeError(f"draft returned {type(proposal).__name__}, expected DraftProposal")
                if proposal.unit is not unit:
                    raise ValueError(
                        "draft proposal must retain the exact input SpecUnit object"
                    )
                phase_start = time.perf_counter()
                try:
                    verification = self.verify(proposal)
                finally:
                    verify_s += time.perf_counter() - phase_start
                if not isinstance(verification, Verification):
                    raise TypeError(
                        f"verify returned {type(verification).__name__}, expected Verification"
                    )
                if not isinstance(verification.accepted, bool):
                    raise TypeError("verification.accepted must be a bool")
                claimed_accept = verification.accepted
                if claimed_accept:
                    candidate = proposal.draft
                else:
                    phase_start = time.perf_counter()
                    try:
                        repair_result = self.repair(proposal, verification)
                    finally:
                        repair_s += time.perf_counter() - phase_start
                    if not isinstance(repair_result, RepairResult):
                        raise TypeError(
                            f"repair returned {type(repair_result).__name__}, expected RepairResult"
                        )
                    candidate = repair_result.output
                # Verification.truth is the charged, product-authoritative
                # result. The counterfactual baseline is deliberately not used
                # here: doing so would hide a full baseline execution outside
                # speculative_s while still letting it steer delivery.
                try:
                    candidate_ok = self.equal(candidate, verification.truth)
                    if not isinstance(candidate_ok, bool):
                        raise TypeError("equal must return a bool")
                except Exception as exc:
                    record_failure(unit, "compare", exc)
                    candidate_ok = False
                if not candidate_ok:
                    raise ValueError("candidate output diverged from verifier truth")
            except Exception as exc:
                record_failure(unit, "accepted_output" if claimed_accept else "speculation", exc)
                ref, baseline_elapsed, was_precomputed = load_product_baseline()
                candidate = ref
                candidate_ok = False

            if candidate_ok and claimed_accept:
                accepted += 1
            elif candidate_ok:
                rejected += 1
                repaired += 1
            else:
                rejected += 1
                fallback_units += 1
                fallback_s += baseline_elapsed
                if was_precomputed:
                    virtual_fallback_s += baseline_elapsed
            outputs.append(candidate)
            used_baseline_output.append(not candidate_ok)
            baseline_product_charged.append(not candidate_ok)
            overhead_s += max(
                time.perf_counter() - spec_wall_start
                - (
                    (draft_s + verify_s + repair_s + fallback_s - virtual_fallback_s)
                    - phase_before
                ),
                0.0,
            )
            if candidate_ok and measure_baseline:
                # Audit that the denominator represents the same delivered
                # result. This is post-hoc benchmark validation only: a mismatch
                # removes the speed claim but cannot replace an artifact already
                # accepted against the charged verifier truth.
                audit_start = time.perf_counter()
                try:
                    comparable = self.benchmark_equal(candidate, ref)
                    if not isinstance(comparable, bool):
                        raise TypeError("benchmark_equal must return a bool")
                    if not comparable:
                        record_benchmark_failure(
                            unit, "baseline output diverged from verified product output"
                        )
                except Exception as exc:
                    record_benchmark_failure(unit, exc)
                finally:
                    baseline_audit_s += time.perf_counter() - audit_start

        speculative_s = draft_s + verify_s + repair_s + fallback_s + overhead_s
        receipt_meta = dict(meta or {})
        receipt_meta["candidate_exact"] = candidate_exact
        receipt_meta["disposition"] = "candidate" if candidate_exact else "fallback"
        receipt_meta["baseline_reused_for_fallback_units"] = reused_baseline_fallback_units
        receipt_meta["reused_baseline_fallback_s"] = virtual_fallback_s
        receipt_meta["counterfactual_baseline_s"] = measured_baseline_s
        receipt_meta["baseline_audit_s"] = baseline_audit_s
        receipt_meta["baseline_comparable"] = baseline_comparable
        receipt_meta["benchmark_mode"] = measure_baseline
        if failure_events:
            receipt_meta["failure_events"] = failure_events
        if benchmark_events:
            receipt_meta["benchmark_events"] = benchmark_events

        reported_baseline_s = measured_baseline_s if baseline_comparable else 0.0
        reported_baseline_source = "measured" if baseline_comparable else "absent"

        receipt = SpecReceipt(
            branch_id=self.branch_id,
            modality=self.modality,
            units=len(unit_list),
            accepted_units=accepted,
            repaired_units=repaired,
            rejected_units=rejected,
            draft_s=draft_s,
            verify_s=verify_s,
            repair_s=repair_s,
            fallback_s=fallback_s,
            overhead_s=overhead_s,
            baseline_s=reported_baseline_s,
            speculative_s=speculative_s,
            speedup_x=(reported_baseline_s / speculative_s if speculative_s > 0 else 0.0),
            # Every escaped artifact is checked against the charged verifier
            # truth or replaced by the authoritative fallback. `candidate_exact`
            # records whether speculation itself succeeded; `exact` describes
            # final delivery under this engine's equality contract.
            exact=bool(unit_list),
            quality_gate=candidate_exact,
            artifact_verified=False,
            attempted_units=attempted,
            fallback_units=fallback_units,
            baseline_source=reported_baseline_source,
            evidence=self.evidence,
            meta=receipt_meta,
        )
        if self.quality_gate is not None:
            gate_start = time.perf_counter()
            gate_meta = receipt.meta
            try:
                gate_pass = self.quality_gate(receipt)
                if not isinstance(gate_pass, bool):
                    raise TypeError("quality_gate must return a bool")
            except Exception as exc:
                gate_pass = False
                gate_meta = dict(receipt.meta)
                existing_events = gate_meta.get("failure_events")
                events = list(existing_events) if isinstance(existing_events, list) else []
                events.append({
                    "unit_id": "<batch>", "phase": "quality_gate",
                    "error": _safe_error(exc),
                })
                gate_meta["failure_events"] = events
            gate_elapsed = time.perf_counter() - gate_start
            receipt = replace(
                receipt,
                overhead_s=receipt.overhead_s + gate_elapsed,
                speculative_s=receipt.speculative_s + gate_elapsed,
                speedup_x=(
                    receipt.baseline_s / (receipt.speculative_s + gate_elapsed)
                    if receipt.speculative_s + gate_elapsed > 0 else 0.0
                ),
                quality_gate=bool(receipt.quality_gate and gate_pass),
                meta=gate_meta,
            )
        if not receipt.quality_gate:
            # Only authoritative outputs may escape a failed batch gate.
            swapped_outputs = sum(not already_used for already_used in used_baseline_output)
            added_fallback_s = 0.0
            added_virtual_fallback_s = 0.0
            for index, (unit, already_charged) in enumerate(zip(
                unit_list, baseline_product_charged, strict=True
            )):
                if already_charged:
                    continue
                if baseline_outputs[index] is missing:
                    try:
                        baseline_outputs[index], elapsed = timed(
                            lambda unit=unit: self.baseline(unit)
                        )
                    except Exception as exc:
                        raise RuntimeError(
                            f"baseline fallback failed for unit {unit.unit_id!r}: "
                            f"{_safe_error(exc)}"
                        ) from exc
                    baseline_elapsed_by_unit[index] = elapsed
                    added_fallback_s += elapsed
                else:
                    elapsed = baseline_elapsed_by_unit[index]
                    added_fallback_s += elapsed
                    added_virtual_fallback_s += elapsed
            virtual_fallback_s += added_virtual_fallback_s
            final_meta = dict(receipt.meta)
            final_meta["disposition"] = "fallback"
            final_meta["final_gate_fallback_units"] = swapped_outputs
            final_meta["reused_baseline_fallback_s"] = virtual_fallback_s
            final_meta["baseline_comparable"] = measure_baseline
            receipt = replace(
                receipt,
                fallback_units=receipt.units,
                fallback_s=receipt.fallback_s + added_fallback_s,
                speculative_s=receipt.speculative_s + added_fallback_s,
                baseline_s=(measured_baseline_s if measure_baseline else 0.0),
                baseline_source=("measured" if measure_baseline else "absent"),
                speedup_x=(
                    measured_baseline_s / (receipt.speculative_s + added_fallback_s)
                    if measure_baseline and receipt.speculative_s + added_fallback_s > 0
                    else 0.0
                ),
                meta=final_meta,
            )
            outputs = baseline_outputs
        return outputs, receipt
