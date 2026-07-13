#!/usr/bin/env python3
"""cx_render_spec_adapter.py — the render lane expressed as a SpecEngine instance.

Branch B render adapter for the owned SpecEngine

    SpecUnit -> DraftProducer -> Verifier -> AcceptancePolicy -> RepairPolicy -> SpecReceipt

mapped onto the PROVEN Cycles low-spp / SSIM-gate / re-render lane (the mechanism already
measured by pod/exp_render_stack.py):

  * SpecUnit         : a render UNIT — a tile (H x W pixel block) or a whole frame.
  * DraftProducer    : the cheap draft — a low-spp (+adaptive+OIDN+guides+light-tree) trace
                       or a reprojected/warped tile. Cost = draft_s (MEASURED wall-clock).
  * Verifier         : SSIM of the draft vs a high-spp / reference tile — global,
                       worst-8x8-tile and p5-tile. Cost = verify_s (measurement time).
  * AcceptancePolicy : the unit clears the quality tier (global >= g AND worst_tile >= wt).
  * RepairPolicy     : re-render the FAILED unit at reference quality. Cost = repair_s.
  * SpecReceipt      : the SAME canonical field set Branch A emits (by CONTRACT, not import):
                       draft_cost, verify_cost, accepted_fraction, repair_cost,
                       total_product_time, quality_tier, speedup_vs_baseline.

HONESTY (load-bearing): every second is labeled. baseline_cost is a REAL single-lane render
of the SAME delivered unit; speedup_vs_baseline = baseline_cost / total_product_time is the
ONLY headline — never a product of per-unit ratios. Numbers are tagged MEASURED (real Cycles
wall-clock), MODELED (e.g. the area-scaled disocclusion crop) or SYNTHETIC (a test fixture).
A receipt carrying any MODELED cost is NOT delivery-eligible on its own — it parks, exactly
like cx_integrated_speculation.RenderVerifier.

This wave the module does NOT import Branch A's `spec-engine/` crate; it matches the schema
by contract so render + token receipts compose in the plan's staged-multiplier table. The
build_engine() path is a live SpecEngine instance over cx_speculative_core; the
receipt_from_* paths are the honest accounting used to turn REAL exp_render_stack.py metrics
(or recorded tiles) into a canonical receipt without re-timing trivial Python.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import cx_speculative_core as core

# ---- evidence labels (honesty discipline) ----------------------------------- #
MEASURED = "MEASURED"    # real wall-clock of a real Cycles trace
MODELED = "MODELED"      # a derived cost (e.g. area-scaled crop re-render)
SYNTHETIC = "SYNTHETIC"  # a fixture / not-hardware number (tests, dry design runs)

# Delivery tier — kept in lockstep with cx_integrated_speculation so the render lane and the
# integrated receipt gate on the SAME thresholds.
DELIVERY_GLOBAL = 0.98
DELIVERY_WORST_TILE = 0.95
MAX_RENDER_RECEIPT_UNITS = 100_000

# The canonical SpecReceipt contract (Branch A). A render receipt MUST expose at least these.
CANONICAL_FIELDS = (
    "schema_version",
    "draft_cost_s",
    "verify_cost_s",
    "accepted_fraction",
    "repair_cost_s",
    "overhead_cost_s",
    "total_product_time_s",
    "baseline_total_time_s",
    "baseline_source",
    "artifact_verified",
    "quality_tier",
    "speedup_vs_baseline",
    "evidence",
    "details",
)


@dataclass(frozen=True)
class QualityTier:
    """SSIM acceptance thresholds. A unit is accepted iff it clears BOTH global and worst-tile
    (and p5 when supplied). `.label` is the stable string that goes in the receipt."""

    global_min: float = DELIVERY_GLOBAL
    worst_tile_min: float = DELIVERY_WORST_TILE
    p5_min: float | None = None
    canonical_tier: str = "delivery"

    def __post_init__(self) -> None:
        for name, value in (
            ("global_min", self.global_min),
            ("worst_tile_min", self.worst_tile_min),
            ("p5_min", self.p5_min),
        ):
            if value is not None and (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or not 0.0 <= value <= 1.0
            ):
                raise ValueError(f"{name} must be null or finite in [0,1], got {value!r}")
        if self.canonical_tier not in {"preview", "delivery"}:
            raise ValueError("canonical_tier must be preview or delivery")

    @property
    def label(self) -> str:
        base = f"g>={self.global_min:g},wt>={self.worst_tile_min:g}"
        return base + (f",p5>={self.p5_min:g}" if self.p5_min is not None else "")

    def clears(self, *, global_ssim: float, worst_tile_ssim: float,
               p5_ssim: float | None = None) -> bool:
        required = (global_ssim, worst_tile_ssim)
        if any(
            not isinstance(v, (int, float))
            or isinstance(v, bool)
            or not math.isfinite(v)
            or not 0.0 <= v <= 1.0
            for v in required
        ):
            return False
        if p5_ssim is not None and (
            not isinstance(p5_ssim, (int, float))
            or isinstance(p5_ssim, bool)
            or not math.isfinite(p5_ssim)
            or not 0.0 <= p5_ssim <= 1.0
        ):
            return False
        if global_ssim < self.global_min or worst_tile_ssim < self.worst_tile_min:
            return False
        if self.p5_min is not None and (p5_ssim is None or p5_ssim < self.p5_min):
            return False
        return True


DELIVERY_TIER = QualityTier()


@dataclass(frozen=True)
class TileMeasurement:
    """One render UNIT (tile or frame) with its MEASURED costs and quality.

    draft_s/verify_s are the cheap-path costs. baseline_s is the REAL single-lane
    (reference-quality) cost of the SAME unit — the honest denominator. repair_s is only
    charged if the unit fails the tier (a real re-render). evidence tags the unit's costs."""

    unit_id: str
    draft_s: float
    verify_s: float
    baseline_s: float
    global_ssim: float
    worst_tile_ssim: float
    p5_ssim: float | None = None
    repair_s: float = 0.0
    # A failed draft is counted as repaired/delivery-grade only when the caller
    # explicitly proves the repair cleared the same tier. Merely spending repair
    # time is not proof (the light-tree mismatch campaign made this distinction
    # load-bearing).
    repair_clears_tier: bool = False
    evidence: str = MEASURED

    def __post_init__(self) -> None:
        if (not isinstance(self.unit_id, str) or not self.unit_id.strip()
                or len(self.unit_id.encode("utf-8")) > 256):
            raise ValueError("unit_id must be non-empty and <= 256 UTF-8 bytes")
        for name, value in (
            ("draft_s", self.draft_s),
            ("verify_s", self.verify_s),
            ("baseline_s", self.baseline_s),
            ("repair_s", self.repair_s),
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{name} must be finite and >= 0, got {value!r}")
        for name, value in (
            ("global_ssim", self.global_ssim),
            ("worst_tile_ssim", self.worst_tile_ssim),
            ("p5_ssim", self.p5_ssim),
        ):
            if value is not None and (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or not 0.0 <= value <= 1.0
            ):
                raise ValueError(f"{name} must be null or finite in [0,1], got {value!r}")
        if not isinstance(self.repair_clears_tier, bool):
            raise TypeError("repair_clears_tier must be a bool")
        if self.evidence not in {MEASURED, MODELED, SYNTHETIC}:
            raise ValueError(f"unknown evidence label {self.evidence!r}")
        if self.baseline_s <= 0:
            raise ValueError("baseline_s must be > 0 for a measured/modeled render unit")


@dataclass(frozen=True)
class RenderSpecReceipt:
    """Canonical render receipt. Exposes the Branch A contract fields (draft_cost, verify_cost,
    accepted_fraction, repair_cost, total_product_time, quality_tier, speedup_vs_baseline) plus
    the honesty fields needed to keep the number defensible."""

    modality: str
    units: int
    accepted_units: int
    repaired_units: int
    draft_cost: float
    verify_cost: float
    repair_cost: float
    baseline_cost: float
    baseline_source: str
    quality_tier: str
    quality_gate: bool
    evidence: str
    # Explicit modality-contract proof. Never infer this from evidence/tier in
    # the wire serializer; constructors that actually ran the gate must set it.
    artifact_verified: bool = False
    global_ssim: float | None = None
    worst_tile_ssim: float | None = None
    p5_ssim: float | None = None
    branch_id: str = "render"
    meta: dict[str, Any] = field(default_factory=dict)
    overhead_cost: float = 0.0
    canonical_tier: str = "delivery"

    def __post_init__(self) -> None:
        if (not isinstance(self.branch_id, str) or not self.branch_id.strip()
                or len(self.branch_id.encode("utf-8")) > 256):
            raise ValueError("branch_id must be non-empty and <= 256 UTF-8 bytes")
        if self.modality != "render":
            raise ValueError(f"render receipt modality must be 'render', got {self.modality!r}")
        for name in ("units", "accepted_units", "repaired_units"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{name} must be an integer")
        if (not 1 <= self.units <= MAX_RENDER_RECEIPT_UNITS
                or not 0 <= self.accepted_units <= self.units):
            raise ValueError("accepted_units must be in [0, units]")
        if not 0 <= self.repaired_units <= self.units - self.accepted_units:
            raise ValueError("repaired_units must fit the unaccepted unit count")
        for name, value in (
            ("draft_cost", self.draft_cost),
            ("verify_cost", self.verify_cost),
            ("repair_cost", self.repair_cost),
            ("baseline_cost", self.baseline_cost),
            ("overhead_cost", self.overhead_cost),
        ):
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} must be finite and >= 0, got {value!r}")
        if self.baseline_source not in {"measured", "modeled", "absent"}:
            raise ValueError(f"unknown baseline_source {self.baseline_source!r}")
        if self.baseline_source == "absent" and self.baseline_cost != 0:
            raise ValueError("baseline_source=absent requires baseline_cost=0")
        if self.baseline_source != "absent" and self.baseline_cost <= 0:
            raise ValueError("measured/modeled baseline_source requires baseline_cost > 0")
        if self.evidence not in {MEASURED, MODELED, SYNTHETIC}:
            raise ValueError(f"unknown evidence label {self.evidence!r}")
        for name, value in (
            ("global_ssim", self.global_ssim),
            ("worst_tile_ssim", self.worst_tile_ssim),
            ("p5_ssim", self.p5_ssim),
        ):
            if value is not None and (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or not 0.0 <= value <= 1.0
            ):
                raise ValueError(f"{name} must be null or finite in [0,1]")
        if (not isinstance(self.quality_tier, str) or not self.quality_tier.strip()
                or len(self.quality_tier.encode("utf-8")) > 256):
            raise ValueError("quality_tier must be non-empty and <= 256 UTF-8 bytes")
        if not isinstance(self.quality_gate, bool):
            raise TypeError("quality_gate must be a bool")
        if not isinstance(self.artifact_verified, bool):
            raise TypeError("artifact_verified must be a bool")
        if self.artifact_verified and (not self.quality_gate or self.evidence != MEASURED):
            raise ValueError(
                "artifact_verified requires a passing gate and measured evidence"
            )
        if self.canonical_tier not in {"preview", "delivery"}:
            raise ValueError("canonical_tier must be preview or delivery")
        if self.total_product_time <= 0:
            raise ValueError("a non-empty render receipt must charge positive product time")
        core._canonical_json_object_bytes(self.meta, label="render receipt meta")

    @property
    def total_product_time(self) -> float:
        return self.draft_cost + self.verify_cost + self.repair_cost + self.overhead_cost

    @property
    def accepted_fraction(self) -> float:
        return self.accepted_units / self.units if self.units else 0.0

    @property
    def repaired_fraction(self) -> float:
        return self.repaired_units / self.units if self.units else 0.0

    @property
    def speedup_vs_baseline(self) -> float | None:
        t = self.total_product_time
        if self.baseline_source == "absent" or self.baseline_cost <= 0 or t <= 0:
            return None
        return self.baseline_cost / t

    @property
    def delivery_eligible(self) -> bool:
        """Delivery needs a passing quality gate AND no modeled cost — mirrors
        cx_integrated_speculation.RenderVerifier: a modeled receipt PARKS, never delivers."""
        return (self.artifact_verified and self.quality_gate
                and self.evidence == MEASURED)

    def to_dict(self) -> dict[str, Any]:
        meta_wire = json.loads(
            core._canonical_json_object_bytes(
                self.meta, label="render receipt meta"
            )
        )
        draft_wire = round(self.draft_cost, 6)
        verify_wire = round(self.verify_cost, 6)
        repair_wire = round(self.repair_cost, 6)
        overhead_wire = round(self.overhead_cost, 6)
        total_wire = round(self.total_product_time, 6)
        baseline_wire = round(self.baseline_cost, 6)
        if total_wire <= 0:
            raise ValueError("render product time is below six-decimal receipt resolution")
        speedup = (
            baseline_wire / total_wire
            if self.baseline_source != "absent" and baseline_wire > 0
            else None
        )
        if self.baseline_source == "measured":
            baseline_claim = "baseline_total_time_s is a measured reference-quality render"
        elif self.baseline_source == "modeled":
            baseline_claim = "baseline_total_time_s is modeled, not a product measurement"
        else:
            baseline_claim = "no baseline or speedup is claimed"
        # The SSIM gate-spec string (e.g. "g>=0.98,wt>=0.95") is NOT a Branch-A
        # quality_tier — that field is a closed enum (fail/preview/delivery). Emit
        # the enum the DELIVERED shot earned (a passing gate == every unit accepted
        # or repaired-to-reference => "delivery"; a failing gate => "fail") and stash
        # the descriptive tier-spec in meta so no information is lost.
        canonical_quality_tier = self.canonical_tier if self.quality_gate else "fail"
        meta_out = {**meta_wire, "quality_gate_spec": self.quality_tier}
        return {
            # ---- canonical Branch-A contract fields --------------------------- #
            "schema_version": 1,
            "draft_cost_s": draft_wire,
            "verify_cost_s": verify_wire,
            "accepted_fraction": round(self.accepted_fraction, 6),
            "repair_cost_s": repair_wire,
            "overhead_cost_s": overhead_wire,
            "total_product_time_s": total_wire,
            # quality_tier is a Branch-A enum value (fail/preview/delivery), never
            # the free SSIM gate-spec string (that lives in meta.quality_gate_spec).
            "quality_tier": canonical_quality_tier,
            "speedup_vs_baseline": round(speedup, 6) if speedup is not None else None,
            # render is NEVER bit-exact vs a full reference (Branch-A `exact` bool;
            # the render lane's fidelity is carried by quality_tier, not exactness).
            "exact": False,
            # ---- honesty / provenance ---------------------------------------- #
            "modality": self.modality,
            "branch_id": self.branch_id,
            "units": self.units,
            "accepted_units": self.accepted_units,
            "repaired_units": self.repaired_units,
            "repaired_fraction": round(self.repaired_fraction, 6),
            "baseline_total_time_s": baseline_wire,
            "baseline_source": self.baseline_source,
            "quality_gate": bool(self.quality_gate),
            # Canonical v1 proof bit. Product eligibility is derived again at
            # ingress from this fact, measured evidence and the outcome tier.
            "artifact_verified": self.artifact_verified,
            # lower-cased to match Branch-A's snake_case Evidence enum
            # (measured/modeled/synthetic/imported); the dataclass field stays the
            # canonical MEASURED/MODELED/SYNTHETIC label the render code speaks.
            "evidence": self.evidence.lower(),
            "global_ssim": self.global_ssim,
            "worst_tile_ssim": self.worst_tile_ssim,
            "p5_ssim": self.p5_ssim,
            "claim_scope": (
                f"{self.evidence} single delivered unit ratio only; per-tile ratios are NOT "
                f"multiplied. {baseline_claim} for this unit."
            ),
            "details": meta_out,
        }


def _combine_evidence(units: list[TileMeasurement]) -> str:
    """A shot is only as clean as its dirtiest unit: SYNTHETIC dominates, then MODELED."""
    labels = {u.evidence for u in units}
    if SYNTHETIC in labels:
        return SYNTHETIC
    if MODELED in labels:
        return MODELED
    return MEASURED


class RenderSpecAdapter:
    """The render lane as a SpecEngine instance + honest bridges into the canonical receipt."""

    def __init__(self, tier: QualityTier = DELIVERY_TIER, branch_id: str = "render"):
        if not isinstance(tier, QualityTier):
            raise TypeError("tier must be a QualityTier")
        if (not isinstance(branch_id, str) or not branch_id.strip()
                or len(branch_id.encode("utf-8")) > 256):
            raise ValueError("branch_id must be non-empty and <= 256 UTF-8 bytes")
        self.tier = tier
        self.branch_id = branch_id

    # ---- (A) live SpecEngine instance --------------------------------------- #
    def build_engine(
        self,
        *,
        draft: Callable[[core.SpecUnit], core.DraftProposal],
        verify: Callable[[core.DraftProposal], core.Verification],
        repair: Callable[[core.DraftProposal, core.Verification], core.RepairResult],
        baseline: Callable[[core.SpecUnit], Any],
        should_speculate: Callable[[core.SpecUnit], bool] | None = None,
        benchmark_equal: Callable[[Any, Any], bool] | None = None,
        evidence: str = "synthetic",
    ) -> core.SpeculativeEngine:
        """Wire the render draft/verify/accept-in-verify/repair callables into the shared
        cx_speculative_core engine. The callables ARE the Cycles/reproject backend; the engine
        emits a cx_speculative_core.SpecReceipt that from_speculative_receipt() maps onto the
        canonical render receipt. This is the modality-general trait shape (same as the token
        lane), instantiated for pixels."""
        return core.SpeculativeEngine(
            branch_id=self.branch_id,
            modality="render",
            draft=draft,
            verify=verify,
            repair=repair,
            baseline=baseline,
            should_speculate=should_speculate,
            benchmark_equal=benchmark_equal,
            evidence=evidence,
        )

    def accepts(self, unit: TileMeasurement) -> bool:
        return self.tier.clears(
            global_ssim=unit.global_ssim,
            worst_tile_ssim=unit.worst_tile_ssim,
            p5_ssim=unit.p5_ssim,
        )

    # ---- (B) honest accounting over MEASURED/recorded units ----------------- #
    def receipt_from_measurements(
        self, units: Iterable[TileMeasurement], meta: dict[str, Any] | None = None,
        *, artifact_verified: bool = False,
    ) -> RenderSpecReceipt:
        """Turn per-unit measurements into a canonical receipt. A unit that clears the tier is
        ACCEPTED at draft+verify cost; a unit that fails is REPAIRED (its repair_s re-render is
        charged) and then counts as delivered at reference quality. accepted_fraction is over
        ALL units; the headline speedup is baseline_cost / total_product_time — one ratio."""
        if meta is not None and not isinstance(meta, dict):
            raise TypeError("meta must be a dict or None")
        if meta is not None:
            core._canonical_json_object_bytes(meta, label="render receipt meta")
        if not isinstance(artifact_verified, bool):
            raise TypeError("artifact_verified must be a bool")
        iterator = iter(units)
        unit_list = []
        for _ in range(MAX_RENDER_RECEIPT_UNITS + 1):
            try:
                unit_list.append(next(iterator))
            except StopIteration:
                break
        if len(unit_list) > MAX_RENDER_RECEIPT_UNITS:
            raise ValueError(
                f"render receipt exceeds {MAX_RENDER_RECEIPT_UNITS} unit safety cap"
            )
        if not unit_list:
            raise ValueError("receipt_from_measurements needs at least one unit")
        if not all(isinstance(unit, TileMeasurement) for unit in unit_list):
            raise TypeError("render receipt units must be TileMeasurement values")
        unit_ids = [unit.unit_id for unit in unit_list]
        if len(set(unit_ids)) != len(unit_ids):
            raise ValueError("render receipt unit ids must be unique")
        draft_cost = sum(u.draft_s for u in unit_list)
        verify_cost = sum(u.verify_s for u in unit_list)
        baseline_cost = sum(u.baseline_s for u in unit_list)
        accepted = 0
        repaired = 0
        unresolved = 0
        repair_cost = 0.0
        g_min = min(u.global_ssim for u in unit_list)
        wt_min = min(u.worst_tile_ssim for u in unit_list)
        p5_vals = [u.p5_ssim for u in unit_list if u.p5_ssim is not None]
        p5_min = min(p5_vals) if p5_vals else None
        for u in unit_list:
            # Charge every attempted repair, including contradictory telemetry
            # that says the draft passed. Outcome classification must never hide
            # work already performed.
            repair_cost += u.repair_s
            if self.accepts(u):
                accepted += 1
            else:
                if u.repair_s > 0 and u.repair_clears_tier:
                    repaired += 1
                else:
                    unresolved += 1
        # After repair a failed unit is re-rendered to reference, so the DELIVERED shot clears
        # the tier iff every unit either accepted or was repaired. quality_gate reflects the
        # delivered result; the raw worst SSIM is reported for transparency.
        quality_gate = unresolved == 0
        receipt_meta = dict(meta or {})
        if unresolved:
            receipt_meta["unresolved_failed_units"] = unresolved
            receipt_meta["repair_validation"] = (
                "fail-closed: a failed draft needs positive repair cost and explicit "
                "repair_clears_tier=true before it counts as delivery-grade"
            )
        combined_evidence = _combine_evidence(unit_list)
        return RenderSpecReceipt(
            modality="render",
            units=len(unit_list),
            accepted_units=accepted,
            repaired_units=repaired,
            draft_cost=draft_cost,
            verify_cost=verify_cost,
            repair_cost=repair_cost,
            baseline_cost=baseline_cost,
            baseline_source=("measured" if combined_evidence == MEASURED else "modeled"),
            quality_tier=self.tier.label,
            quality_gate=quality_gate,
            evidence=combined_evidence,
            # Quality telemetry is not artifact binding. Only the caller that
            # actually ran and bound the modality verifier may assert this bit.
            artifact_verified=artifact_verified,
            canonical_tier=self.tier.canonical_tier,
            global_ssim=g_min,
            worst_tile_ssim=wt_min,
            p5_ssim=p5_min,
            branch_id=self.branch_id,
            meta=receipt_meta,
        )

    # ---- (C) bridge from a real exp_render_stack.py metrics dict ------------- #
    def from_stack_metrics(
        self, metrics: dict[str, Any], meta: dict[str, Any] | None = None,
        *, artifact_verified: bool = False,
    ) -> RenderSpecReceipt:
        """Map the KEYSTONE runner's final JSON onto the canonical receipt.

        The whole animated shot is ONE delivered unit:
          * baseline_cost      = T_ref_s  (every frame FULLY at ref_spp — real single-lane)
          * draft_cost         = T_stack_s (anchor keyframes + reproject; the cheap path)
          * repair_cost        = the disocclusion crop re-render — ALREADY inside T_stack_s,
                                  so it is reported (fixed_overhead + crop trace) but NOT added
                                  again; total_product_time stays == T_stack_s.
          * verify_cost        = 0 charged (SSIM/mask is measurement-only, never charged to
                                  T_stack) — reported in meta.
          * accepted_fraction  = reproject_accept_frac (tiles delivered WITHOUT a re-render)
          * speedup_vs_baseline= T_ref_s / T_stack_s == net_speedup (one measured wall-clock)
          * evidence           = MODELED iff metrics['modeled'] (a receipt with a modeled crop
                                 PARKS — not delivery-eligible), else MEASURED.
        """
        if not isinstance(metrics, dict):
            raise TypeError("metrics must be a dict")
        if meta is not None and not isinstance(meta, dict):
            raise TypeError("meta must be a dict or None")
        if meta is not None:
            core._canonical_json_object_bytes(meta, label="render receipt meta")
        if not isinstance(artifact_verified, bool):
            raise TypeError("artifact_verified must be a bool")
        for key in ("T_ref_s", "T_stack_s", "quality", "worst_tile_ssim", "modeled"):
            if key not in metrics:
                raise ValueError(f"stack metrics missing {key!r}")
        t_ref = _finite_metric(metrics, "T_ref_s", minimum=0.0, strict_min=True)
        t_stack = _finite_metric(metrics, "T_stack_s", minimum=0.0, strict_min=True)
        accept_frac = (
            _finite_metric(metrics, "reproject_accept_frac", minimum=0.0, maximum=1.0)
            if "reproject_accept_frac" in metrics else 0.0
        )
        disocc_frac = (
            _finite_metric(metrics, "mean_disoccluded_frac", minimum=0.0, maximum=1.0)
            if "mean_disoccluded_frac" in metrics else 0.0
        )
        global_ssim = _finite_metric(metrics, "quality", minimum=0.0, maximum=1.0)
        worst_tile = _finite_metric(metrics, "worst_tile_ssim", minimum=0.0, maximum=1.0)
        p5 = (_finite_metric(metrics, "p5_tile_ssim", minimum=0.0, maximum=1.0)
              if "p5_tile_ssim" in metrics else None)
        if not isinstance(metrics["modeled"], bool):
            raise TypeError("stack metrics 'modeled' must be a bool")
        modeled = metrics["modeled"]
        if metrics.get("net_speedup") is not None:
            reported = _finite_metric(metrics, "net_speedup", minimum=0.0, strict_min=True)
            expected = t_ref / t_stack
            if not math.isclose(reported, expected, rel_tol=1e-3, abs_tol=1e-6):
                raise ValueError(
                    f"net_speedup {reported} contradicts T_ref_s/T_stack_s {expected}"
                )
        quality_gate = self.tier.clears(
            global_ssim=global_ssim, worst_tile_ssim=worst_tile, p5_ssim=p5
        )
        m = dict(meta or {})
        m.update({
            "net_speedup_reported": metrics.get("net_speedup"),
            "device": metrics.get("device"),
            "scene": metrics.get("scene"),
            "resolution": metrics.get("resolution"),
            "frames": metrics.get("frames"),
            "keyframes": metrics.get("keyframes"),
            "ref_spp": metrics.get("ref_spp"),
            "draft_spp": metrics.get("draft_spp"),
            "verify_cost_note": "SSIM/mask is measurement-only; not charged to T_stack",
        })

        if "repair_total_s" in metrics:
            # ---- REPAIR-LOOP receipt (runner ran PASS 3.5) ------------------------ #
            # repair_total_s is REAL measured wall-clock (selection drafts + divergence
            # scoring + bordered repair renders + compositing) and is ALREADY inside
            # T_stack — so repair_cost is real here (no fixed_overhead_s stand-in) and
            # total_product_time stays == T_stack (never double-charged). Units are the
            # grading tiles (frames x 64 on the 8x8 grid): the SpecEngine accepted/
            # repaired fractions are REAL per-tile counts, not a whole-shot 0/1.
            repair_total_s = _finite_metric(metrics, "repair_total_s", minimum=0.0)
            if repair_total_s > t_stack:
                raise ValueError("repair_total_s cannot exceed T_stack_s")
            draft_cost = max(t_stack - repair_total_s, 0.0)
            frames_raw = metrics.get("frames")
            if (not isinstance(frames_raw, int) or isinstance(frames_raw, bool)
                    or frames_raw <= 0):
                raise ValueError("repair metrics require frames as a positive integer")
            frames_n = frames_raw
            grid_tiles = 64  # 8x8 grading grid (exp_render_stack.GRADING_TILE_GRID ** 2)
            if frames_n > MAX_RENDER_RECEIPT_UNITS // grid_tiles:
                raise ValueError("frames*64 exceeds the render receipt unit safety cap")
            units = frames_n * grid_tiles
            repaired_raw = metrics.get("repaired_tile_count", 0)
            if not isinstance(repaired_raw, int) or isinstance(repaired_raw, bool):
                raise TypeError("repaired_tile_count must be an integer")
            repaired_units = repaired_raw
            if repaired_units < 0 or repaired_units > units:
                raise ValueError("repaired_tile_count must be in [0, frames*64]")
            if repaired_units > 0 and repair_total_s <= 0:
                raise ValueError("repaired tiles require positive repair_total_s")
            m.update({
                "repair_note": (
                    "repair_cost is the runner's REAL measured repair_total_s (selection "
                    "drafts + divergence scoring + bordered tile re-renders + feathered "
                    "compositing), already INSIDE T_stack — total_product_time stays == "
                    "T_stack and is not double-charged. Units are grading tiles "
                    "(frames x 64); accepted/repaired fractions are real tile counts. "
                    "The tile selector is reference-free (two-independent-draft "
                    "divergence); SSIM-vs-reference remains measurement-only."
                ),
                "selection_cost_s": metrics.get("selection_cost_s"),
                "repair_cost_s": metrics.get("repair_cost_s"),
                "repaired_tile_indices": metrics.get("repaired_tile_indices"),
                "selector_recall": metrics.get("selector_recall"),
            })
            return RenderSpecReceipt(
                modality="render",
                units=units,
                accepted_units=units - repaired_units,
                repaired_units=repaired_units,
                draft_cost=draft_cost,
                verify_cost=0.0,
                repair_cost=repair_total_s,
                baseline_cost=t_ref,
                baseline_source="measured",
                quality_tier=self.tier.label,
                quality_gate=quality_gate,
                evidence=MODELED if modeled else MEASURED,
                artifact_verified=artifact_verified,
                canonical_tier=self.tier.canonical_tier,
                global_ssim=global_ssim,
                worst_tile_ssim=worst_tile,
                p5_ssim=p5,
                branch_id=self.branch_id,
                meta={"accepted_fraction_tiles": accept_frac,
                      "repaired_fraction_tiles": disocc_frac, **m},
            )

        # ---- legacy path (no repair keys): byte-identical to the pre-repair adapter --
        # repair cost reported (crop re-render) is the modeled fraction of T_stack; it is
        # NOT re-added — draft_cost already includes it, so total_product_time == T_stack.
        crop_repair_s = (
            _finite_metric(metrics, "fixed_overhead_s", minimum=0.0)
            if "fixed_overhead_s" in metrics else 0.0
        )
        if crop_repair_s > t_stack:
            raise ValueError("fixed_overhead_s must be finite in [0, T_stack_s]")
        draft_cost = max(t_stack - crop_repair_s, 0.0)
        m.update({
            "repair_note": (
                "the disocclusion crop re-render is already INSIDE T_stack; repair_cost here "
                "is the runner's directly-exposed fixed_overhead_s (a lower bound of the true "
                "crop cost) so total_product_time stays == T_stack and is not double-charged. "
                "The exact, load-bearing numbers are total_product_time and speedup_vs_baseline."
            ),
        })
        return RenderSpecReceipt(
            modality="render",
            units=1,
            accepted_units=1 if disocc_frac <= 0 else 0,
            repaired_units=1 if disocc_frac > 0 else 0,
            draft_cost=draft_cost,
            verify_cost=0.0,
            repair_cost=crop_repair_s,
            baseline_cost=t_ref,
            baseline_source="measured",
            quality_tier=self.tier.label,
            quality_gate=quality_gate,
            evidence=MODELED if modeled else MEASURED,
            artifact_verified=artifact_verified,
            canonical_tier=self.tier.canonical_tier,
            global_ssim=global_ssim,
            worst_tile_ssim=worst_tile,
            p5_ssim=p5,
            branch_id=self.branch_id,
            meta={"accepted_fraction_tiles": accept_frac,
                  "repaired_fraction_tiles": disocc_frac, **m},
        )

    # ---- (D) bridge from the generic cx_speculative_core receipt ------------- #
    def from_speculative_receipt(
        self, receipt: core.SpecReceipt, *, global_ssim: float | None = None,
        worst_tile_ssim: float | None = None, evidence: str | None = None,
    ) -> RenderSpecReceipt:
        """Prove the generic engine's SpecReceipt IS the canonical render receipt: the token
        lane and render lane come out the same shape. quality_gate here is the engine's gate
        (correctness/quality) AND, when SSIMs are supplied, the tier check."""
        if not isinstance(receipt, core.SpecReceipt):
            raise TypeError("receipt must be cx_speculative_core.SpecReceipt")
        derived_evidence = {
            "measured": MEASURED,
            "modeled": MODELED,
            "synthetic": SYNTHETIC,
        }.get(receipt.evidence)
        if derived_evidence is None:
            raise ValueError(f"unsupported core evidence label {receipt.evidence!r}")
        if evidence is None:
            evidence = derived_evidence
        if evidence not in {MEASURED, MODELED, SYNTHETIC}:
            raise ValueError(f"unknown evidence label {evidence!r}")
        if evidence != derived_evidence:
            raise ValueError(
                f"cannot relabel core evidence {receipt.evidence!r} as {evidence!r}"
            )
        gate = receipt.quality_gate
        if (global_ssim is None) != (worst_tile_ssim is None):
            raise ValueError("global_ssim and worst_tile_ssim must be supplied together")
        if global_ssim is not None:
            gate = gate and self.tier.clears(
                global_ssim=global_ssim, worst_tile_ssim=worst_tile_ssim
            )
        return RenderSpecReceipt(
            modality=receipt.modality,
            units=receipt.units,
            accepted_units=receipt.accepted_units,
            repaired_units=receipt.repaired_units,
            draft_cost=receipt.draft_s,
            verify_cost=receipt.verify_s,
            repair_cost=receipt.repair_s + receipt.fallback_s,
            overhead_cost=receipt.overhead_s,
            baseline_cost=receipt.baseline_s,
            baseline_source=receipt.baseline_source,
            quality_tier=self.tier.label,
            quality_gate=gate,
            evidence=evidence,
            # The generic core deliberately emits no product artifact proof.
            # Re-labeling its evidence must not manufacture one in this bridge.
            artifact_verified=False,
            canonical_tier=self.tier.canonical_tier,
            global_ssim=global_ssim,
            worst_tile_ssim=worst_tile_ssim,
            branch_id=receipt.branch_id,
            meta={
                "source": "cx_speculative_core.SpecReceipt",
                "engine_speedup_x": round(receipt.speedup_x, 6),
                "engine_benchmark": {
                    key: receipt.meta[key]
                    for key in (
                        "benchmark_mode",
                        "baseline_comparable",
                        "counterfactual_baseline_s",
                        "baseline_audit_s",
                        "candidate_exact",
                        "disposition",
                    )
                    if key in receipt.meta
                    and isinstance(receipt.meta[key], (bool, int, float, str, type(None)))
                },
            },
        )


def assert_canonical(receipt_dict: dict[str, Any]) -> None:
    """Guard used by tests + the integrated driver: a render receipt MUST expose every Branch A
    contract field so render and token receipts compose in the staged-multiplier table."""
    missing = [k for k in CANONICAL_FIELDS if k not in receipt_dict]
    if missing:
        raise AssertionError(f"receipt missing canonical contract fields: {missing}")
    numeric = (
        "draft_cost_s", "verify_cost_s", "repair_cost_s", "overhead_cost_s",
        "total_product_time_s",
        "baseline_total_time_s", "accepted_fraction", "repaired_fraction",
    )
    for key in numeric:
        value = receipt_dict[key]
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(value)):
            raise AssertionError(f"{key} must be a finite number")
    if any(receipt_dict[k] < 0 for k in numeric[:5]):
        raise AssertionError("receipt times must be >= 0")
    if not 0.0 <= receipt_dict["accepted_fraction"] <= 1.0:
        raise AssertionError("accepted_fraction must be in [0,1]")
    if not 0.0 <= receipt_dict["repaired_fraction"] <= 1.0:
        raise AssertionError("repaired_fraction must be in [0,1]")
    parts = sum(receipt_dict[k] for k in (
        "draft_cost_s", "verify_cost_s", "repair_cost_s", "overhead_cost_s",
    ))
    if not math.isclose(receipt_dict["total_product_time_s"], parts, rel_tol=1e-9, abs_tol=2e-6):
        raise AssertionError("total_product_time_s must equal the charged phase sum")
    if receipt_dict["baseline_source"] not in {"measured", "modeled", "absent"}:
        raise AssertionError("invalid baseline_source")
    if (receipt_dict["baseline_source"] != "absent"
            and receipt_dict["baseline_total_time_s"] <= 0):
        raise AssertionError("measured/modeled baseline requires positive baseline time")
    speedup = receipt_dict["speedup_vs_baseline"]
    if receipt_dict["baseline_source"] == "absent":
        if speedup is not None or receipt_dict["baseline_total_time_s"] != 0:
            raise AssertionError("absent baseline requires zero time and null speedup")
    else:
        expected_speedup = (
            receipt_dict["baseline_total_time_s"]
            / receipt_dict["total_product_time_s"]
        )
        if (not isinstance(speedup, (int, float)) or isinstance(speedup, bool)
                or not math.isfinite(speedup)
                or abs(speedup - expected_speedup) > 5e-6):
            raise AssertionError("speedup must equal rounded baseline/product times")
    if receipt_dict["evidence"] not in {"measured", "modeled", "synthetic", "imported"}:
        raise AssertionError("invalid evidence")
    if not isinstance(receipt_dict["artifact_verified"], bool):
        raise AssertionError("artifact_verified must be a bool")
    if receipt_dict["artifact_verified"] and (
        receipt_dict["evidence"] != "measured"
        or receipt_dict["quality_tier"] == "fail"
        or not receipt_dict.get("quality_gate")
    ):
        raise AssertionError(
            "artifact_verified requires measured evidence and a passing gate"
        )
    if not isinstance(receipt_dict["details"], dict):
        raise AssertionError("details must be an object")
    try:
        core._canonical_json_object_bytes(
            receipt_dict["details"], label="render receipt details"
        )
    except (TypeError, ValueError) as exc:
        raise AssertionError(str(exc)) from exc


def _finite_metric(
    metrics: dict[str, Any], key: str, *, minimum: float | None = None,
    maximum: float | None = None, strict_min: bool = False,
) -> float:
    """Read one numeric stack metric and reject bool/NaN/inf/out-of-range values."""
    raw = metrics[key]
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        raise TypeError(f"stack metric {key!r} must be a real number")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"stack metric {key!r} must be finite")
    if minimum is not None and (value <= minimum if strict_min else value < minimum):
        op = ">" if strict_min else ">="
        raise ValueError(f"stack metric {key!r} must be {op} {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"stack metric {key!r} must be <= {maximum}")
    return value


# --------------------------------------------------------------------------- #
# LOCAL dry-run entrypoint — NO cloud, NO Blender, NO money. Proves the receipt
# SHAPE end-to-end on SYNTHETIC inputs so the contract is exercisable before the
# sequenced GPU run produces the first real combined number. `python3
# cx_render_spec_adapter.py` prints the canonical receipts + a money-safety banner.
# --------------------------------------------------------------------------- #
def dry_run() -> list[dict[str, Any]]:
    """Exercise every adapter path on SYNTHETIC inputs and return the canonical receipt
    dicts. EVERY second here is a FIXTURE (SYNTHETIC) — nothing is a measurement. The real
    render numbers come only from the sequenced GPU run of
    run_integrated_production_benchmark.py (device bug fixed); until then the combined
    render number is NONE-YET, by design (see the plan's staged-multiplier table)."""
    adapter = RenderSpecAdapter()
    receipts: list[dict[str, Any]] = []

    # (1) per-tile hard-scene-shaped receipt: 3 tiles clear the tier, 1 fails worst-tile
    #     and is repaired. The seconds are ARBITRARY illustrative fixtures, not measured.
    tiles = [
        TileMeasurement(f"tile_{i}", draft_s=1.0, verify_s=0.1, baseline_s=8.0,
                        global_ssim=0.99, worst_tile_ssim=0.97, evidence=SYNTHETIC)
        for i in range(3)
    ]
    tiles.append(
        TileMeasurement("tile_3", draft_s=1.0, verify_s=0.1, baseline_s=8.0,
                        global_ssim=0.97, worst_tile_ssim=0.80,  # fails worst-tile -> repair
                        repair_s=8.0, repair_clears_tier=True, evidence=SYNTHETIC)
    )
    r1 = adapter.receipt_from_measurements(
        tiles, meta={"note": "SYNTHETIC per-tile fixture; seconds are illustrative, not measured"}
    )
    receipts.append(r1.to_dict())

    # (2) integrated animated-shot receipt shaped like the DEFAULT classroom job
    #     (3840x2160, 4 frames, 4096 ref / 512 draft, rerender hole-fill => modeled crop).
    #     A MODELED receipt PARKS: delivery_eligible is False by design. The seconds are an
    #     ARBITRARY fixture (T_ref/T_stack = 4.0x here is NOT any published headline) — the
    #     real classroom ratio is NONE-YET, pending the device-fixed cloud run.
    stack_fixture = {
        "T_ref_s": 120.0, "T_stack_s": 30.0, "quality": 0.991,
        "worst_tile_ssim": 0.962, "p5_tile_ssim": 0.972, "modeled": True,
        "reproject_accept_frac": 0.93, "mean_disoccluded_frac": 0.07,
        "fixed_overhead_s": 6.0, "net_speedup": 4.0,
        "device": "GPU/OPTIX (SYNTHETIC fixture)", "scene": "classroom",
        "resolution": "3840x2160", "frames": 4, "ref_spp": 4096, "draft_spp": 512,
    }
    r2 = adapter.from_stack_metrics(
        stack_fixture,
        meta={"note": "SYNTHETIC classroom-shaped fixture (arbitrary seconds); the real "
                      "combined number is NONE-YET pending the device-fixed cloud run of "
                      "run_integrated_production_benchmark.py"},
    )
    receipts.append(r2.to_dict())

    # (3) LIVE cx_speculative_core engine over trivial render-shaped callables — proves the
    #     SHARED engine emits the SAME canonical shape (render/token parity). The wall-clock
    #     is real Python but trivial; evidence is SYNTHETIC (this is not a render).
    def draft(u: core.SpecUnit) -> core.DraftProposal:
        return core.DraftProposal(u, draft=u.payload)

    def verify(p: core.DraftProposal) -> core.Verification:
        return core.Verification(accepted=True, truth=p.unit.payload, quality=1.0)

    def repair(p: core.DraftProposal, v: core.Verification) -> core.RepairResult:
        return core.RepairResult(output=p.unit.payload)

    def baseline(u: core.SpecUnit) -> Any:
        return u.payload

    engine = adapter.build_engine(draft=draft, verify=verify, repair=repair, baseline=baseline)
    units = [core.SpecUnit(f"u{i}", "render", payload=i) for i in range(5)]
    _outputs, core_receipt = engine.run(units)
    r3 = adapter.from_speculative_receipt(
        core_receipt, global_ssim=0.99, worst_tile_ssim=0.96, evidence=SYNTHETIC
    )
    receipts.append(r3.to_dict())

    for d in receipts:
        assert_canonical(d)  # every receipt exposes the Branch A contract fields
    return receipts


def _main() -> None:
    receipts = dry_run()
    banner = {
        "dry_run": "cx_render_spec_adapter",
        "money_safe": True,
        "cloud_touched": False,
        "blender_invoked": False,
        "evidence": "ALL SYNTHETIC — every second is a fixture, not a measurement",
        "real_number_status": (
            "combined render number is NONE-YET; it is produced only by the sequenced GPU "
            "run of run_integrated_production_benchmark.py (device bug fixed this wave)"
        ),
        "canonical_contract_fields": list(CANONICAL_FIELDS),
        "receipts": receipts,
    }
    print(json.dumps(banner, indent=2, sort_keys=True))


if __name__ == "__main__":
    _main()
