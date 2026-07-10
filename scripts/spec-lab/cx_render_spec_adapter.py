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

# The canonical SpecReceipt contract (Branch A). A render receipt MUST expose at least these.
CANONICAL_FIELDS = (
    "draft_cost",
    "verify_cost",
    "accepted_fraction",
    "repair_cost",
    "total_product_time",
    "quality_tier",
    "speedup_vs_baseline",
)


@dataclass(frozen=True)
class QualityTier:
    """SSIM acceptance thresholds. A unit is accepted iff it clears BOTH global and worst-tile
    (and p5 when supplied). `.label` is the stable string that goes in the receipt."""

    global_min: float = DELIVERY_GLOBAL
    worst_tile_min: float = DELIVERY_WORST_TILE
    p5_min: float | None = None

    @property
    def label(self) -> str:
        base = f"g>={self.global_min:g},wt>={self.worst_tile_min:g}"
        return base + (f",p5>={self.p5_min:g}" if self.p5_min is not None else "")

    def clears(self, *, global_ssim: float, worst_tile_ssim: float,
               p5_ssim: float | None = None) -> bool:
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
    evidence: str = MEASURED


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
    quality_tier: str
    quality_gate: bool
    evidence: str
    global_ssim: float | None = None
    worst_tile_ssim: float | None = None
    p5_ssim: float | None = None
    branch_id: str = "render"
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def total_product_time(self) -> float:
        return self.draft_cost + self.verify_cost + self.repair_cost

    @property
    def accepted_fraction(self) -> float:
        return self.accepted_units / self.units if self.units else 0.0

    @property
    def repaired_fraction(self) -> float:
        return self.repaired_units / self.units if self.units else 0.0

    @property
    def speedup_vs_baseline(self) -> float | None:
        t = self.total_product_time
        return (self.baseline_cost / t) if t > 0 else None

    @property
    def delivery_eligible(self) -> bool:
        """Delivery needs a passing quality gate AND no modeled cost — mirrors
        cx_integrated_speculation.RenderVerifier: a modeled receipt PARKS, never delivers."""
        return bool(self.quality_gate) and self.evidence != MODELED

    def to_dict(self) -> dict[str, Any]:
        speedup = self.speedup_vs_baseline
        # The SSIM gate-spec string (e.g. "g>=0.98,wt>=0.95") is NOT a Branch-A
        # quality_tier — that field is a closed enum (fail/preview/delivery). Emit
        # the enum the DELIVERED shot earned (a passing gate == every unit accepted
        # or repaired-to-reference => "delivery"; a failing gate => "fail") and stash
        # the descriptive tier-spec in meta so no information is lost.
        canonical_quality_tier = "delivery" if self.quality_gate else "fail"
        meta_out = {**self.meta, "quality_gate_spec": self.quality_tier}
        return {
            # ---- canonical Branch-A contract fields --------------------------- #
            "draft_cost": round(self.draft_cost, 6),
            "verify_cost": round(self.verify_cost, 6),
            "accepted_fraction": round(self.accepted_fraction, 6),
            "repair_cost": round(self.repair_cost, 6),
            "total_product_time": round(self.total_product_time, 6),
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
            "baseline_cost": round(self.baseline_cost, 6),
            "quality_gate": bool(self.quality_gate),
            "delivery_eligible": self.delivery_eligible,
            # lower-cased to match Branch-A's snake_case Evidence enum
            # (measured/modeled/synthetic/imported); the dataclass field stays the
            # canonical MEASURED/MODELED/SYNTHETIC label the render code speaks.
            "evidence": self.evidence.lower(),
            "global_ssim": self.global_ssim,
            "worst_tile_ssim": self.worst_tile_ssim,
            "p5_ssim": self.p5_ssim,
            "claim_scope": (
                "Measured single delivered unit ratio only; per-tile ratios are NOT "
                "multiplied. baseline_cost is a real reference-quality render of this unit."
            ),
            "meta": meta_out,
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
        )

    def accepts(self, unit: TileMeasurement) -> bool:
        return self.tier.clears(
            global_ssim=unit.global_ssim,
            worst_tile_ssim=unit.worst_tile_ssim,
            p5_ssim=unit.p5_ssim,
        )

    # ---- (B) honest accounting over MEASURED/recorded units ----------------- #
    def receipt_from_measurements(
        self, units: Iterable[TileMeasurement], meta: dict[str, Any] | None = None
    ) -> RenderSpecReceipt:
        """Turn per-unit measurements into a canonical receipt. A unit that clears the tier is
        ACCEPTED at draft+verify cost; a unit that fails is REPAIRED (its repair_s re-render is
        charged) and then counts as delivered at reference quality. accepted_fraction is over
        ALL units; the headline speedup is baseline_cost / total_product_time — one ratio."""
        unit_list = list(units)
        if not unit_list:
            raise ValueError("receipt_from_measurements needs at least one unit")
        draft_cost = sum(u.draft_s for u in unit_list)
        verify_cost = sum(u.verify_s for u in unit_list)
        baseline_cost = sum(u.baseline_s for u in unit_list)
        accepted = 0
        repaired = 0
        repair_cost = 0.0
        g_min = min(u.global_ssim for u in unit_list)
        wt_min = min(u.worst_tile_ssim for u in unit_list)
        p5_vals = [u.p5_ssim for u in unit_list if u.p5_ssim is not None]
        p5_min = min(p5_vals) if p5_vals else None
        for u in unit_list:
            if self.accepts(u):
                accepted += 1
            else:
                repaired += 1
                repair_cost += u.repair_s
        # After repair a failed unit is re-rendered to reference, so the DELIVERED shot clears
        # the tier iff every unit either accepted or was repaired. quality_gate reflects the
        # delivered result; the raw worst SSIM is reported for transparency.
        quality_gate = True  # repaired units are re-rendered to reference quality
        return RenderSpecReceipt(
            modality="render",
            units=len(unit_list),
            accepted_units=accepted,
            repaired_units=repaired,
            draft_cost=draft_cost,
            verify_cost=verify_cost,
            repair_cost=repair_cost,
            baseline_cost=baseline_cost,
            quality_tier=self.tier.label,
            quality_gate=quality_gate,
            evidence=_combine_evidence(unit_list),
            global_ssim=g_min,
            worst_tile_ssim=wt_min,
            p5_ssim=p5_min,
            branch_id=self.branch_id,
            meta=meta or {},
        )

    # ---- (C) bridge from a real exp_render_stack.py metrics dict ------------- #
    def from_stack_metrics(
        self, metrics: dict[str, Any], meta: dict[str, Any] | None = None
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
        for key in ("T_ref_s", "T_stack_s", "quality", "worst_tile_ssim", "modeled"):
            if key not in metrics:
                raise ValueError(f"stack metrics missing {key!r}")
        t_ref = float(metrics["T_ref_s"])
        t_stack = float(metrics["T_stack_s"])
        accept_frac = float(metrics.get("reproject_accept_frac", 0.0))
        disocc_frac = float(metrics.get("mean_disoccluded_frac", 0.0))
        global_ssim = float(metrics["quality"])
        worst_tile = float(metrics["worst_tile_ssim"])
        p5 = float(metrics["p5_tile_ssim"]) if "p5_tile_ssim" in metrics else None
        modeled = bool(metrics["modeled"])
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
            repair_total_s = min(float(metrics["repair_total_s"]), t_stack)
            draft_cost = max(t_stack - repair_total_s, 0.0)
            frames_n = int(metrics.get("frames", 0) or 0)
            grid_tiles = 64  # 8x8 grading grid (exp_render_stack.GRADING_TILE_GRID ** 2)
            units = max(1, frames_n * grid_tiles)
            repaired_units = min(int(metrics.get("repaired_tile_count", 0)), units)
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
                quality_tier=self.tier.label,
                quality_gate=quality_gate,
                evidence=MODELED if modeled else MEASURED,
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
        crop_repair_s = float(metrics.get("fixed_overhead_s", 0.0))
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
            quality_tier=self.tier.label,
            quality_gate=quality_gate,
            evidence=MODELED if modeled else MEASURED,
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
        worst_tile_ssim: float | None = None, evidence: str = MEASURED,
    ) -> RenderSpecReceipt:
        """Prove the generic engine's SpecReceipt IS the canonical render receipt: the token
        lane and render lane come out the same shape. quality_gate here is the engine's gate
        (correctness/quality) AND, when SSIMs are supplied, the tier check."""
        gate = bool(receipt.quality_gate)
        if global_ssim is not None and worst_tile_ssim is not None:
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
            baseline_cost=receipt.baseline_s,
            quality_tier=self.tier.label,
            quality_gate=gate,
            evidence=evidence,
            global_ssim=global_ssim,
            worst_tile_ssim=worst_tile_ssim,
            branch_id=receipt.branch_id,
            meta={"source": "cx_speculative_core.SpecReceipt",
                  "engine_speedup_x": round(receipt.speedup_x, 6)},
        )


def assert_canonical(receipt_dict: dict[str, Any]) -> None:
    """Guard used by tests + the integrated driver: a render receipt MUST expose every Branch A
    contract field so render and token receipts compose in the staged-multiplier table."""
    missing = [k for k in CANONICAL_FIELDS if k not in receipt_dict]
    if missing:
        raise AssertionError(f"receipt missing canonical contract fields: {missing}")


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
                        repair_s=8.0, evidence=SYNTHETIC)
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
