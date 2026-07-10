#!/usr/bin/env python3
"""CX-native speculative execution primitives.

This is the ComputeExchange-owned spine for speculation across modalities. A
backend can mine vLLM, Hawking, Cycles, ffmpeg, or custom kernels later, but the
control loop and receipts here are ours:

    draft units -> verify -> accept or repair -> receipt -> grow/prune
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable


@dataclass(frozen=True)
class SpecUnit:
    unit_id: str
    modality: str
    payload: Any
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DraftProposal:
    unit: SpecUnit
    draft: Any
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Verification:
    accepted: bool
    truth: Any
    quality: float | None = None
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RepairResult:
    output: Any
    meta: dict[str, Any] = field(default_factory=dict)


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
    attempted_units: int | None = None
    fallback_units: int = 0
    fallback_s: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

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
        return {
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
            "baseline_s": round(self.baseline_s, 6),
            "speculative_s": round(self.speculative_s, 6),
            "speedup_x": round(self.speedup_x, 6),
            "exact": self.exact,
            "quality_gate": self.quality_gate,
            "meta": self.meta,
        }


def timed(fn: Callable[[], Any]) -> tuple[Any, float]:
    start = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - start


def decide_branch(receipt: SpecReceipt, min_speedup: float = 1.2, min_accept: float = 0.35) -> str:
    if not receipt.exact or not receipt.quality_gate:
        return "kill_correctness"
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
        quality_gate: Callable[[SpecReceipt], bool] | None = None,
    ):
        self.branch_id = branch_id
        self.modality = modality
        self.draft = draft
        self.verify = verify
        self.repair = repair
        self.baseline = baseline
        self.should_speculate = should_speculate
        self.fallback = fallback or baseline
        self.equal = equal or (lambda a, b: a == b)
        self.quality_gate = quality_gate

    def run(self, units: Iterable[SpecUnit], meta: dict[str, Any] | None = None) -> tuple[list[Any], SpecReceipt]:
        unit_list = list(units)
        baseline_outputs = []
        baseline_s = 0.0
        for unit in unit_list:
            out, elapsed = timed(lambda unit=unit: self.baseline(unit))
            baseline_outputs.append(out)
            baseline_s += elapsed

        outputs = []
        accepted = 0
        repaired = 0
        rejected = 0
        attempted = 0
        fallback_units = 0
        draft_s = 0.0
        verify_s = 0.0
        repair_s = 0.0
        fallback_s = 0.0

        for unit in unit_list:
            if self.should_speculate is not None and not self.should_speculate(unit):
                fallback_units += 1
                out, elapsed = timed(lambda unit=unit: self.fallback(unit))
                fallback_s += elapsed
                outputs.append(out)
                continue

            attempted += 1
            proposal, elapsed = timed(lambda unit=unit: self.draft(unit))
            draft_s += elapsed
            verification, elapsed = timed(lambda proposal=proposal: self.verify(proposal))
            verify_s += elapsed
            if verification.accepted:
                accepted += 1
                outputs.append(proposal.draft)
            else:
                rejected += 1
                repair_result, elapsed = timed(
                    lambda proposal=proposal, verification=verification: self.repair(proposal, verification)
                )
                repair_s += elapsed
                repaired += 1
                outputs.append(repair_result.output)

        speculative_s = draft_s + verify_s + repair_s + fallback_s
        exact = len(outputs) == len(baseline_outputs) and all(
            self.equal(out, ref) for out, ref in zip(outputs, baseline_outputs)
        )
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
            baseline_s=baseline_s,
            speculative_s=speculative_s,
            speedup_x=(baseline_s / speculative_s if speculative_s > 0 else 0.0),
            exact=exact,
            quality_gate=True,
            attempted_units=attempted,
            fallback_units=fallback_units,
            meta=meta or {},
        )
        if self.quality_gate is not None:
            receipt = replace(receipt, quality_gate=bool(self.quality_gate(receipt)))
        return outputs, receipt
