#!/usr/bin/env python3
"""CX-native receipt primitives for one speculative-render job.

The token lane accelerates the structured job/control stream.  The render lane
accelerates pixels.  They are accounted in one job receipt, but are never
multiplied: the only headline is the measured end-to-end wall-clock ratio.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


DELIVERY_GLOBAL = 0.98
DELIVERY_WORST_TILE = 0.95


@dataclass(frozen=True)
class RenderSpecJob:
    job_id: str
    workload: str
    scene: str
    resolution: str
    frames: int
    render_policy: str
    token_policy: str


@dataclass(frozen=True)
class RenderBranchDecision:
    action: str
    reason: str


@dataclass(frozen=True)
class RenderSpecReceipt:
    job: RenderSpecJob
    token_baseline_s: float
    token_spec_s: float
    render_baseline_s: float
    render_spec_s: float
    global_ssim: float | None
    worst_tile_ssim: float | None
    token_exact: bool
    render_modeled: bool
    evidence_type: str
    decision: RenderBranchDecision

    @property
    def baseline_total_s(self) -> float:
        return self.token_baseline_s + self.render_baseline_s

    @property
    def spec_total_s(self) -> float:
        return self.token_spec_s + self.render_spec_s

    @property
    def end_to_end_speedup_x(self) -> float | None:
        if self.spec_total_s <= 0:
            return None
        return self.baseline_total_s / self.spec_total_s

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["baseline_total_s"] = round(self.baseline_total_s, 6)
        data["spec_total_s"] = round(self.spec_total_s, 6)
        speedup = self.end_to_end_speedup_x
        data["end_to_end_speedup_x"] = round(speedup, 6) if speedup is not None else None
        data["claim_scope"] = (
            "Measured end-to-end job ratio only; component speedups are not multiplied."
        )
        return data


class RenderVerifier:
    """Applies the delivery gate with an explicit incomplete-quality state."""

    @staticmethod
    def decide(
        *,
        token_exact: bool,
        global_ssim: float | None,
        worst_tile_ssim: float | None,
        render_modeled: bool,
    ) -> RenderBranchDecision:
        if not token_exact:
            return RenderBranchDecision("kill_correctness", "manifest decode diverged from baseline")
        if global_ssim is None or worst_tile_ssim is None:
            return RenderBranchDecision(
                "park",
                "quality evidence is incomplete; no delivery or combined performance claim",
            )
        if global_ssim < DELIVERY_GLOBAL or worst_tile_ssim < DELIVERY_WORST_TILE:
            return RenderBranchDecision("prune", "render output failed the delivery quality gate")
        if render_modeled:
            return RenderBranchDecision(
                "park",
                "quality passed but render accounting includes a modeled component",
            )
        return RenderBranchDecision("grow", "exact control stream and measured delivery-quality render")
