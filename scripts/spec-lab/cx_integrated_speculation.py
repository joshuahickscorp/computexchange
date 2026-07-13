#!/usr/bin/env python3
"""CX-native receipt primitives for one speculative-render job.

The token lane accelerates the structured job/control stream.  The render lane
accelerates pixels.  They are accounted in one job receipt, but are never
multiplied: the only headline is the measured end-to-end wall-clock ratio.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any


DELIVERY_GLOBAL = 0.98
DELIVERY_WORST_TILE = 0.95
MEASURED_SAME_JOB_EVIDENCE = "same_gpu_production_cycles_stack"


@dataclass(frozen=True)
class RenderSpecJob:
    job_id: str
    workload: str
    scene: str
    resolution: str
    frames: int
    render_policy: str
    token_policy: str

    def __post_init__(self) -> None:
        for name in ("job_id", "workload", "scene", "resolution", "render_policy", "token_policy"):
            value = getattr(self, name)
            if (not isinstance(value, str) or not value.strip()
                    or len(value.encode("utf-8")) > 1024):
                raise ValueError(f"{name} must be a non-empty string <= 1024 UTF-8 bytes")
        if not isinstance(self.frames, int) or isinstance(self.frames, bool) or not 1 <= self.frames <= 1_000_000:
            raise ValueError("frames must be an integer in [1,1000000]")


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
    # The current JSON-template loop is an exact protocol laboratory, not a
    # model-backed one-forward-pass target decode. Keep it explicitly modeled
    # until the target-model transactional KV fork lands.
    token_modeled: bool = True
    # Explicit final-artifact binding from the modality verifier. Passing SSIM
    # numbers and a provenance label is not by itself proof that those numbers
    # describe the artifact a customer would receive.
    artifact_verified: bool = False
    # Derived from the validated facts below. Callers cannot inject a decision
    # that contradicts exactness, quality, or modeled provenance.
    decision: RenderBranchDecision = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.job, RenderSpecJob):
            raise TypeError("job must be a RenderSpecJob")
        times = {
            "token_baseline_s": self.token_baseline_s,
            "token_spec_s": self.token_spec_s,
            "render_baseline_s": self.render_baseline_s,
            "render_spec_s": self.render_spec_s,
        }
        for name, value in times.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise TypeError(f"{name} must be a real number")
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and > 0, got {value!r}")
        for name, value in (
            ("global_ssim", self.global_ssim),
            ("worst_tile_ssim", self.worst_tile_ssim),
        ):
            if value is not None and (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or not 0.0 <= value <= 1.0
            ):
                raise ValueError(f"{name} must be null or finite in [0,1], got {value!r}")
        if (not isinstance(self.token_exact, bool)
                or not isinstance(self.render_modeled, bool)
                or not isinstance(self.token_modeled, bool)
                or not isinstance(self.artifact_verified, bool)):
            raise TypeError(
                "token_exact, token_modeled, render_modeled and artifact_verified "
                "must be bools"
            )
        if (not isinstance(self.evidence_type, str) or not self.evidence_type.strip()
                or len(self.evidence_type.encode("utf-8")) > 128):
            raise ValueError("evidence_type must be a non-empty string <= 128 UTF-8 bytes")
        object.__setattr__(
            self,
            "decision",
            RenderVerifier.decide(
                token_exact=self.token_exact,
                global_ssim=self.global_ssim,
                worst_tile_ssim=self.worst_tile_ssim,
                render_modeled=self.render_modeled,
                evidence_type=self.evidence_type,
                token_modeled=self.token_modeled,
                artifact_verified=self.artifact_verified,
            ),
        )

    @property
    def baseline_total_s(self) -> float:
        return self.token_baseline_s + self.render_baseline_s

    @property
    def spec_total_s(self) -> float:
        return self.token_spec_s + self.render_spec_s

    @property
    def end_to_end_speedup_x(self) -> float | None:
        if self.spec_total_s <= 0 or self.baseline_total_s <= 0:
            return None
        return self.baseline_total_s / self.spec_total_s

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["baseline_total_s"] = round(self.baseline_total_s, 6)
        data["spec_total_s"] = round(self.spec_total_s, 6)
        speedup = self.end_to_end_speedup_x
        data["end_to_end_speedup_x"] = round(speedup, 6) if speedup is not None else None
        if self.render_modeled or self.token_modeled:
            label = "MODELED"
        elif self.evidence_type == MEASURED_SAME_JOB_EVIDENCE:
            label = "MEASURED"
        else:
            label = "UNATTESTED"
        data["claim_scope"] = (
            f"{label} end-to-end job ratio only; component speedups are not multiplied."
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
        evidence_type: str,
        token_modeled: bool,
        artifact_verified: bool = False,
    ) -> RenderBranchDecision:
        if (not isinstance(token_exact, bool)
                or not isinstance(render_modeled, bool)
                or not isinstance(token_modeled, bool)
                or not isinstance(artifact_verified, bool)):
            return RenderBranchDecision(
                "kill_correctness", "verifier policy flags must be strict booleans"
            )
        if not isinstance(evidence_type, str) or not evidence_type.strip():
            return RenderBranchDecision(
                "park", "quality evidence provenance is missing or malformed"
            )
        if not token_exact:
            return RenderBranchDecision("kill_correctness", "manifest decode diverged from baseline")
        if global_ssim is None or worst_tile_ssim is None:
            return RenderBranchDecision(
                "park",
                "quality evidence is incomplete; no delivery or combined performance claim",
            )
        if (
            not isinstance(global_ssim, (int, float))
            or isinstance(global_ssim, bool)
            or not isinstance(worst_tile_ssim, (int, float))
            or isinstance(worst_tile_ssim, bool)
            or not math.isfinite(global_ssim)
            or not math.isfinite(worst_tile_ssim)
            or not 0.0 <= global_ssim <= 1.0
            or not 0.0 <= worst_tile_ssim <= 1.0
        ):
            return RenderBranchDecision(
                "kill_correctness",
                "quality evidence is non-finite or outside [0,1]",
            )
        if global_ssim < DELIVERY_GLOBAL or worst_tile_ssim < DELIVERY_WORST_TILE:
            return RenderBranchDecision("prune", "render output failed the delivery quality gate")
        if render_modeled:
            return RenderBranchDecision(
                "park",
                "quality passed but render accounting includes a modeled component",
            )
        if token_modeled:
            return RenderBranchDecision(
                "park",
                "quality passed but token timing is not a model-backed target decode",
            )
        if evidence_type != MEASURED_SAME_JOB_EVIDENCE:
            return RenderBranchDecision(
                "park",
                "quality passed but evidence is not a same-GPU production measurement",
            )
        if not artifact_verified:
            return RenderBranchDecision(
                "park",
                "quality passed but the evidence is not bound to the delivered artifact",
            )
        return RenderBranchDecision("grow", "exact control stream and measured delivery-quality render")
