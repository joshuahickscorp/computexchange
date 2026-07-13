#!/usr/bin/env python3
"""cx_transcode_spec_adapter.py — the TRANSCODE lane expressed as a SpecEngine instance.

Branch C (Generalization Plan 2026-07-10): the SECOND real media modality through the ONE
owned SpecEngine

    SpecUnit -> DraftProducer -> Verifier -> AcceptancePolicy -> RepairPolicy -> SpecReceipt

mapped onto the PROVEN speculative-transcode mechanism (segment-wise fast-preset draft ->
SSIM verify -> re-encode rejected segments; VP9 5.2x banked in prior standalone campaigns):

  * SpecUnit         : one VIDEO SEGMENT (a keyframe-aligned time slice of the source).
  * DraftProducer    : a FAST-preset encode of the segment (same codec+CRF as baseline,
                       cheaper preset). Cost = draft_encode_s (MEASURED wall-clock).
  * Verifier         : per-segment SSIM of the encoded draft vs the SOURCE segment
                       (or decoded-frame MD5 in lossless mode). Cost = verify_s. Unlike the
                       render lane (where the reference does not exist in production and
                       SSIM is measurement-only), here the source IS the pipeline input, so
                       the verify pass is a REAL product step and its wall-clock is CHARGED.
  * AcceptancePolicy : the segment clears the gate (SSIM >= ssim_min, or byte-exact frames
                       in lossless mode).
  * RepairPolicy     : re-encode the REJECTED segment at the SLOW (baseline) preset — the
                       delivered segment is then baseline-recipe quality by construction.
                       Cost = repair_encode_s (MEASURED).
  * SpecReceipt      : the CANONICAL spec-engine/src/receipt.rs wire shape, emitted NATIVELY
                       (no serde aliases needed): draft_cost_s / verify_cost_s /
                       repair_cost_s / total_product_time_s / baseline_total_time_s /
                       accepted_fraction / repaired_fraction / exact / quality_tier enum
                       (fail|preview|delivery) / evidence (measured|modeled|synthetic) /
                       speedup_vs_baseline / baseline_source / details.

BASELINE (the honest denominator): ONE whole-video encode of the SAME source at the SLOW
preset — a real single-lane run of the same delivered unit. speedup_vs_baseline =
baseline_total_time_s / total_product_time_s is the ONLY headline, never a product of
per-segment ratios.

HONESTY (load-bearing):
  * Every number is labeled MEASURED / MODELED / SYNTHETIC. The real-run path measures real
    ffmpeg wall-clock on real (synthesized-content) video; the simulate path is SYNTHETIC.
  * total_product_time_s = draft_cost_s + verify_cost_s + repair_cost_s, where draft_cost_s
    INCLUDES segmentation and final concat/mux wall-clock (broken out in details) — the FULL
    delivered pipeline is charged, nothing hides outside the ratio.
  * SSIM-gated transcode is NOT lossless: `exact` is False in ssim mode even at
    quality_tier=delivery, and the delivered file is byte-compared against the baseline file
    (details.bitexact_vs_baseline — expected False) so nobody mistakes the tier for
    losslessness. The lossless mode (x264 qp0) verifies decoded-frame MD5 vs the source and
    only then sets exact=True.
  * Same-CRF fast presets pay in BITRATE: details reports delivered_bytes vs baseline_bytes.
    A speedup with a 2x file is not free — the receipt says so.
  * Repaired segments are encoded with the EXACT baseline recipe, but per-segment encoding
    lacks the whole-video rate-control context; with CRF mode + keyframe-aligned segments
    this is near-identical, and the residual difference is noted, not hidden.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

# ---- evidence labels (honesty discipline; lower-cased on the wire like receipt.rs) ---- #
MEASURED = "MEASURED"    # real ffmpeg wall-clock on real local hardware
MODELED = "MODELED"      # a derived cost, not a live measurement
SYNTHETIC = "SYNTHETIC"  # a fixture (tests / simulate mode) — never a hardware number

# receipt.rs enum vocabularies (snake_case on the wire).
QUALITY_TIERS = ("fail", "preview", "delivery")
EVIDENCE_WIRE = ("measured", "modeled", "synthetic", "imported")
BASELINE_SOURCES = ("measured", "modeled", "absent")

# Fields spec-engine/src/receipt.rs REQUIRES on deserialize (no #[serde(default)], and the
# adapter emits the canonical names directly — no alias crutch). quality_tier / evidence /
# baseline_source / details are serde-defaulted in Rust but this adapter always emits them.
CANONICAL_REQUIRED_FIELDS = (
    "schema_version",
    "branch_id",
    "modality",
    "draft_cost_s",
    "verify_cost_s",
    "repair_cost_s",
    "overhead_cost_s",
    "total_product_time_s",
    "baseline_total_time_s",
    "units",
    "accepted_fraction",
    "repaired_fraction",
    "exact",
    "artifact_verified",
    "speedup_vs_baseline",
)
CANONICAL_DEFAULTED_FIELDS = ("quality_tier", "evidence", "baseline_source", "details")

# The published render-lane 0.95 tier, reused as the default per-segment SSIM gate so the
# two media lanes gate on a comparable quality vocabulary.
DEFAULT_SSIM_GATE = 0.95
MAX_PIXEL_FRAMES = 500_000_000
MAX_TRANSCODE_RECEIPT_UNITS = 10_000
MAX_RECEIPT_JSON_BYTES = 1 << 20
MAX_RECEIPT_JSON_DEPTH = 32
MAX_CALLER_DETAILS_JSON_BYTES = 512 << 10
MAX_PER_SEGMENT_DETAIL_SAMPLES = 128
# A retained-artifact measurement peaked at 4.478x decoded yuv420p source bytes.  Reserve
# 6x plus a fixed 512 MiB for muxer metadata, concat manifests, filesystem allocation
# granularity, and subprocess spill.  This intentionally has ~34% multiplicative headroom
# above the observed peak before the fixed reserve is counted.
MEASURED_RETAINED_PEAK_RAW_MULTIPLIER = 4.478
SCRATCH_RAW_MULTIPLIER = 6.0
SCRATCH_FIXED_HEADROOM_BYTES = 512 << 20

# One SSIM filtergraph has two simultaneously active decoders plus the filter itself.
# Its advertised thread budget is split across all three components rather than applied
# independently to each (which used to permit a 3x oversubscription).
SSIM_THREAD_COMPONENTS = 3
ENCODE_THREAD_COMPONENTS = 2  # one input decoder plus one output encoder
# Four-way stage fanout was the fastest verified point in the initial local sweep. The
# resource-normalized planner below caps all concurrent encoders to the same aggregate
# thread envelope as the baseline and automatically reduces fanout on smaller hosts.
DEFAULT_WORKERS = 4


def _canonical_workdir_path(workdir: Any) -> str:
    try:
        raw = os.fspath(workdir)
    except TypeError as exc:
        raise TypeError("workdir must be a string or path-like value") from exc
    if not isinstance(raw, str):
        raise TypeError("workdir must resolve to a text path, not bytes")
    if not raw or "\x00" in raw:
        raise ValueError("workdir must be a non-empty path without NUL bytes")
    absolute = os.path.abspath(raw)
    if os.path.lexists(absolute) and stat.S_ISLNK(os.lstat(absolute).st_mode):
        raise ValueError("workdir itself cannot be a symlink")
    # Resolve existing parent aliases once and use only this canonical spelling
    # for subprocess arguments, cleanup, and publication.
    return os.path.realpath(absolute)


def _private_directory_identity(path: str, *, require_empty: bool) -> tuple[int, int]:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("workdir must be a real directory, not a symlink or special file")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise ValueError("workdir must be owned by the current effective user")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError("workdir must not be group/world writable")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("workdir identity changed while it was opened")
        if require_empty and os.listdir(fd):
            raise ValueError("workdir must be absent or empty; refusing stale/mixed artifacts")
        return opened.st_dev, opened.st_ino
    finally:
        os.close(fd)


def _prepare_private_empty_workdir(path: str) -> tuple[bool, tuple[int, int]]:
    created = False
    if not os.path.lexists(path):
        os.makedirs(path, mode=0o700)
        os.chmod(path, 0o700)
        created = True
    try:
        return created, _private_directory_identity(path, require_empty=True)
    except BaseException:
        if created:
            shutil.rmtree(path, ignore_errors=True)
        raise


def _directory_identity_matches(path: str, identity: tuple[int, int]) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and (info.st_dev, info.st_ino) == identity
    )


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        text = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("receipt content must be finite, acyclic, and JSON serializable") from exc
    depth = _json_nesting_depth(value)
    if depth > MAX_RECEIPT_JSON_DEPTH:
        raise ValueError(
            f"canonical JSON nesting depth is {depth}; limit is "
            f"{MAX_RECEIPT_JSON_DEPTH}"
        )
    return text.encode("utf-8")


def _json_nesting_depth(value: Any) -> int:
    """Return object/array nesting depth after json.dumps has rejected cycles."""
    maximum = 0
    stack = [(value, 0)]
    while stack:
        current, parent_depth = stack.pop()
        if isinstance(current, dict):
            depth = parent_depth + 1
            maximum = max(maximum, depth)
            stack.extend((child, depth) for child in current.values())
        elif isinstance(current, (list, tuple)):
            depth = parent_depth + 1
            maximum = max(maximum, depth)
            stack.extend((child, depth) for child in current)
    return maximum


# --------------------------------------------------------------------------- #
# Accounting layer (pure Python — unit-testable without ffmpeg)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SegmentResult:
    """One SpecUnit (video segment) with its MEASURED costs and verify outcome.

    draft_ssim is the verify score vs the SOURCE segment (None only in lossless mode where
    the verifier is a decoded-frame MD5 equality instead). repair_encode_s is charged only
    when the segment failed the gate and was re-encoded at the slow preset. repaired_ssim
    is a MEASUREMENT-ONLY audit of the repaired segment (never charged)."""

    seg_id: str
    draft_encode_s: float
    verify_s: float
    accepted: bool
    draft_ssim: float | None = None
    repair_encode_s: float = 0.0
    repaired_ssim: float | None = None
    # Explicit post-repair proof. A rejected segment is never promoted to
    # delivery merely because an encode command ran; the repaired artifact must
    # clear the same verifier (SSIM or decoded-frame hash).
    repair_verified: bool | None = None
    # Production SSIM and lossless-MD5 acceptance additionally prove that both inputs
    # describe the same decoded timeline. None is retained for synthetic fixtures.
    draft_timeline_verified: bool | None = None
    repair_timeline_verified: bool | None = None
    draft_bytes: int = 0
    repair_bytes: int = 0
    evidence: str = MEASURED

    def __post_init__(self) -> None:
        if (
            not isinstance(self.seg_id, str)
            or not self.seg_id.strip()
            or len(self.seg_id.encode("utf-8")) > 256
        ):
            raise ValueError("seg_id must be non-empty and <= 256 UTF-8 bytes")
        for name, value in (
            ("draft_encode_s", self.draft_encode_s),
            ("verify_s", self.verify_s),
            ("repair_encode_s", self.repair_encode_s),
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{name} must be finite and >= 0, got {value!r}")
        for name, value in (("draft_ssim", self.draft_ssim), ("repaired_ssim", self.repaired_ssim)):
            if value is not None and (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or not 0.0 <= value <= 1.0
            ):
                raise ValueError(f"{name} must be null or finite in [0,1], got {value!r}")
        if not isinstance(self.accepted, bool):
            raise TypeError("accepted must be a bool")
        if self.repair_verified is not None and not isinstance(self.repair_verified, bool):
            raise TypeError("repair_verified must be a bool or None")
        for name, value in (
            ("draft_timeline_verified", self.draft_timeline_verified),
            ("repair_timeline_verified", self.repair_timeline_verified),
        ):
            if value is not None and not isinstance(value, bool):
                raise TypeError(f"{name} must be a bool or None")
        if self.accepted and (self.repair_encode_s != 0 or self.repair_verified is not None):
            raise ValueError("an accepted segment cannot also carry a repair outcome")
        if self.accepted and self.repair_timeline_verified is not None:
            raise ValueError("an accepted segment cannot carry a repair timeline outcome")
        for name, value in (("draft_bytes", self.draft_bytes), ("repair_bytes", self.repair_bytes)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.evidence not in {MEASURED, MODELED, SYNTHETIC}:
            raise ValueError(f"unknown evidence label {self.evidence!r}")


@dataclass(frozen=True)
class TranscodeSpecReceipt:
    """Canonical transcode receipt — the spec-engine/src/receipt.rs shape, natively."""

    branch_id: str
    modality: str
    units: int
    accepted_units: int
    repaired_units: int
    draft_cost_s: float           # segmentation + draft encodes + concat (full cheap path)
    verify_cost_s: float          # per-segment SSIM / frame-MD5 verification (charged)
    repair_cost_s: float          # slow-preset re-encodes of rejected segments
    overhead_cost_s: float        # bounded orchestration/assembly outside phase walls
    baseline_total_time_s: float  # ONE whole-video slow-preset encode (the honest denominator)
    baseline_source: str          # measured | modeled | absent
    exact: bool                   # True ONLY when decoded frames byte-match the source
    quality_tier: str             # fail | preview | delivery (receipt.rs enum)
    evidence: str                 # MEASURED | MODELED | SYNTHETIC (lower-cased on the wire)
    # Explicit proof bit from the final artifact verifier. Never inferred from tier/evidence.
    artifact_verified: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.branch_id, str)
            or not self.branch_id.strip()
            or len(self.branch_id.encode("utf-8")) > 256
        ):
            raise ValueError("branch_id must be non-empty and <= 256 UTF-8 bytes")
        if self.modality != "transcode":
            raise ValueError(f"transcode receipt modality must be 'transcode', got {self.modality!r}")
        for name, value in (
            ("units", self.units),
            ("accepted_units", self.accepted_units),
            ("repaired_units", self.repaired_units),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not 1 <= self.units <= MAX_TRANSCODE_RECEIPT_UNITS:
            raise ValueError(
                f"units must be in [1,{MAX_TRANSCODE_RECEIPT_UNITS}]"
            )
        if not 0 <= self.accepted_units <= self.units:
            raise ValueError("accepted_units must be in [0, units]")
        if not 0 <= self.repaired_units <= self.units - self.accepted_units:
            raise ValueError("repaired_units must fit the unaccepted unit count")
        for name, value in (
            ("draft_cost_s", self.draft_cost_s),
            ("verify_cost_s", self.verify_cost_s),
            ("repair_cost_s", self.repair_cost_s),
            ("overhead_cost_s", self.overhead_cost_s),
            ("baseline_total_time_s", self.baseline_total_time_s),
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{name} must be finite and >= 0, got {value!r}")
        if (
            self.draft_cost_s + self.verify_cost_s + self.repair_cost_s
            + self.overhead_cost_s
        ) <= 0:
            raise ValueError("receipt total product time must be positive")
        if self.baseline_source not in BASELINE_SOURCES:
            raise ValueError(f"baseline_source {self.baseline_source!r} not in {BASELINE_SOURCES}")
        if self.baseline_source == "absent" and self.baseline_total_time_s != 0:
            raise ValueError("baseline_source=absent requires baseline_total_time_s=0")
        if self.baseline_source != "absent" and self.baseline_total_time_s <= 0:
            raise ValueError(
                "measured/modeled baseline_source requires baseline_total_time_s > 0"
            )
        if self.quality_tier not in QUALITY_TIERS:
            raise ValueError(f"quality_tier {self.quality_tier!r} not in {QUALITY_TIERS}")
        if self.evidence not in {MEASURED, MODELED, SYNTHETIC}:
            raise ValueError(f"unknown evidence label {self.evidence!r}")
        if not isinstance(self.exact, bool):
            raise TypeError("exact must be a bool")
        if not isinstance(self.artifact_verified, bool):
            raise TypeError("artifact_verified must be a bool")
        if self.artifact_verified and (
            self.evidence != MEASURED or self.quality_tier == "fail"
        ):
            raise ValueError(
                "artifact_verified=true requires measured evidence and a non-fail tier"
            )
        if not isinstance(self.details, dict):
            raise TypeError("details must be a dict")

    @property
    def total_product_time_s(self) -> float:
        return (self.draft_cost_s + self.verify_cost_s + self.repair_cost_s
                + self.overhead_cost_s)

    @property
    def accepted_fraction(self) -> float:
        return self.accepted_units / self.units if self.units else 0.0

    @property
    def repaired_fraction(self) -> float:
        return self.repaired_units / self.units if self.units else 0.0

    @property
    def speedup_vs_baseline(self) -> float | None:
        """ONE ratio: baseline / total. None (null on the wire) when the baseline is absent
        or the spec time is non-positive — a speedup is never fabricated."""
        t = self.total_product_time_s
        if self.baseline_source == "absent" or t <= 0:
            return None
        return self.baseline_total_time_s / t

    def to_dict(self) -> dict[str, Any]:
        if self.quality_tier not in QUALITY_TIERS:
            raise ValueError(f"quality_tier {self.quality_tier!r} not in {QUALITY_TIERS}")
        if self.baseline_source not in BASELINE_SOURCES:
            raise ValueError(f"baseline_source {self.baseline_source!r} not in {BASELINE_SOURCES}")
        draft_wire = round(self.draft_cost_s, 6)
        verify_wire = round(self.verify_cost_s, 6)
        repair_wire = round(self.repair_cost_s, 6)
        overhead_wire = round(self.overhead_cost_s, 6)
        total_wire = round(self.total_product_time_s, 6)
        baseline_wire = round(self.baseline_total_time_s, 6)
        if total_wire <= 0:
            raise ValueError("transcode product time is below receipt resolution")
        speedup = (
            baseline_wire / total_wire
            if self.baseline_source != "absent" and baseline_wire > 0
            else None
        )
        if self.baseline_source == "measured":
            baseline_claim = "baseline_total_time_s is a measured whole-video slow-preset encode"
        elif self.baseline_source == "modeled":
            baseline_claim = "baseline_total_time_s is modeled, not a product measurement"
        else:
            baseline_claim = "no baseline or speedup is claimed"
        fidelity_claim = (
            "Decoded timeline and frame bytes were proven exact (exact=true)."
            if self.exact else
            "SSIM-gated delivery is NOT lossless (exact=false)."
        )
        wire = {
            # ---- canonical receipt.rs spine (native names, no aliases) -------- #
            "schema_version": 1,
            "branch_id": self.branch_id,
            "modality": self.modality,
            "draft_cost_s": draft_wire,
            "verify_cost_s": verify_wire,
            "repair_cost_s": repair_wire,
            "overhead_cost_s": overhead_wire,
            "total_product_time_s": total_wire,
            "baseline_total_time_s": baseline_wire,
            "baseline_source": self.baseline_source,
            "units": self.units,
            "accepted_fraction": round(self.accepted_fraction, 6),
            "repaired_fraction": round(self.repaired_fraction, 6),
            "exact": bool(self.exact),
            "artifact_verified": bool(self.artifact_verified),
            "quality_tier": self.quality_tier,
            "speedup_vs_baseline": round(speedup, 6) if speedup is not None else None,
            "evidence": self.evidence.lower(),
            # ---- transparency extras (serde ignores unknown keys) ------------- #
            "accepted_units": self.accepted_units,
            "repaired_units": self.repaired_units,
            "claim_scope": (
                f"{self.evidence} single delivered-video ratio only; per-segment ratios are NOT "
                f"multiplied. {baseline_claim} for the SAME source. "
                f"{fidelity_claim}"
            ),
            "details": self.details,
        }
        wire_size = len(_canonical_json_bytes(wire))
        if wire_size > MAX_RECEIPT_JSON_BYTES:
            raise ValueError(
                f"canonical receipt is {wire_size:,} UTF-8 bytes; limit is "
                f"{MAX_RECEIPT_JSON_BYTES:,}"
            )
        return wire


def _combine_evidence(labels: Iterable[str]) -> str:
    """A receipt is only as clean as its dirtiest unit: SYNTHETIC dominates, then MODELED."""
    label_set = set(labels)
    if SYNTHETIC in label_set:
        return SYNTHETIC
    if MODELED in label_set:
        return MODELED
    return MEASURED


def plan_segments(duration_s: float, segment_time_s: float) -> int:
    """Number of segments a duration splits into (last segment may be shorter)."""
    for name, value in (("duration_s", duration_s), ("segment_time_s", segment_time_s)):
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{name} must be a finite positive number")
    return int(duration_s // segment_time_s) + (1 if duration_s % segment_time_s > 1e-9 else 0)


def required_scratch_bytes(pixel_frames: int) -> int:
    """Conservative scratch preflight derived from the measured retained peak."""
    if not isinstance(pixel_frames, int) or isinstance(pixel_frames, bool) or pixel_frames < 0:
        raise ValueError("pixel_frames must be a non-negative integer")
    decoded_yuv420p_bytes = (pixel_frames * 3 + 1) // 2
    return (
        math.ceil(decoded_yuv420p_bytes * SCRATCH_RAW_MULTIPLIER)
        + SCRATCH_FIXED_HEADROOM_BYTES
    )


def build_receipt(
    segments: Iterable[SegmentResult],
    *,
    baseline_wall_s: float,
    segment_wall_s: float,
    concat_wall_s: float,
    exact: bool,
    repair_enabled: bool = True,
    delivery_verified: bool = False,
    final_verify_wall_s: float = 0.0,
    draft_encode_wall_s: float | None = None,
    verify_wall_s: float | None = None,
    repair_wall_s: float | None = None,
    overhead_wall_s: float = 0.0,
    baseline_source: str = "measured",
    branch_id: str = "transcode",
    details: dict[str, Any] | None = None,
) -> TranscodeSpecReceipt:
    """Fold per-segment results into the canonical receipt.

    Tier semantics (mirrors the render lane's repaired-to-reference discipline):
      * delivery : every segment either cleared the gate (accepted) or was REPAIRED at the
                   exact baseline recipe — the delivered video is gate-or-baseline quality
                   everywhere.
      * preview  : repair disabled and >=1 rejected segment was shipped as its draft — the
                   delivered video contains below-gate segments. Honest, cheaper, NOT delivery.
      * fail     : nothing was delivered (no segments).
    `exact` is the caller's PROOF (decoded-frame MD5 chain), never inferred from SSIM.
    """
    if not isinstance(exact, bool) or not isinstance(repair_enabled, bool):
        raise TypeError("exact and repair_enabled must be bools")
    if not isinstance(delivery_verified, bool):
        raise TypeError("delivery_verified must be a bool")
    for name, value in (
        ("baseline_wall_s", baseline_wall_s),
        ("segment_wall_s", segment_wall_s),
        ("concat_wall_s", concat_wall_s),
        ("final_verify_wall_s", final_verify_wall_s),
        ("overhead_wall_s", overhead_wall_s),
    ):
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"{name} must be finite and >= 0, got {value!r}")
    for name, value in (
        ("draft_encode_wall_s", draft_encode_wall_s),
        ("verify_wall_s", verify_wall_s),
        ("repair_wall_s", repair_wall_s),
    ):
        if value is not None and (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"{name} must be null or finite and >= 0, got {value!r}")
    if baseline_source not in BASELINE_SOURCES:
        raise ValueError(f"baseline_source {baseline_source!r} not in {BASELINE_SOURCES}")
    if baseline_source == "absent" and baseline_wall_s != 0:
        raise ValueError("baseline_source=absent requires baseline_wall_s=0")
    if baseline_source != "absent" and baseline_wall_s <= 0:
        raise ValueError("measured/modeled baseline_source requires baseline_wall_s > 0")
    iterator = iter(segments)
    seg_list = []
    for _ in range(MAX_TRANSCODE_RECEIPT_UNITS + 1):
        try:
            seg_list.append(next(iterator))
        except StopIteration:
            break
    if len(seg_list) > MAX_TRANSCODE_RECEIPT_UNITS:
        raise ValueError(
            f"transcode receipt exceeds {MAX_TRANSCODE_RECEIPT_UNITS} unit safety cap"
        )
    if not all(isinstance(segment, SegmentResult) for segment in seg_list):
        raise TypeError("transcode receipt units must be SegmentResult values")
    ids = [s.seg_id for s in seg_list]
    if len(set(ids)) != len(ids):
        raise ValueError("segment ids must be unique")
    if not seg_list:
        raise ValueError("transcode receipt requires at least one segment")
    if details is not None and not isinstance(details, dict):
        raise TypeError("details must be a dict or None")
    caller_details = details or {}
    caller_details_size = len(_canonical_json_bytes(caller_details))
    caller_details_depth = _json_nesting_depth(caller_details)
    if caller_details_depth > MAX_RECEIPT_JSON_DEPTH - 1:
        raise ValueError(
            f"caller details nesting depth is {caller_details_depth}; receipt limit is "
            f"{MAX_RECEIPT_JSON_DEPTH} including the receipt root"
        )
    if caller_details_size > MAX_CALLER_DETAILS_JSON_BYTES:
        raise ValueError(
            f"caller details are {caller_details_size:,} UTF-8 bytes; limit is "
            f"{MAX_CALLER_DETAILS_JSON_BYTES:,}"
        )
    accepted = sum(1 for s in seg_list if s.accepted)
    rejected = [s for s in seg_list if not s.accepted]
    invalid_accepted_proofs = [
        s for s in seg_list
        if s.accepted and s.evidence == MEASURED
        and s.draft_timeline_verified is not True
    ]
    successfully_repaired = [
        s for s in rejected
        if repair_enabled and s.repair_encode_s > 0 and s.repair_verified is True
        and (s.evidence != MEASURED or s.repair_timeline_verified is True)
    ]
    repaired = len(successfully_repaired)
    # Failed repair attempts still cost real time and must remain charged.
    repair_cost = (
        repair_wall_s if repair_wall_s is not None
        else (sum(s.repair_encode_s for s in rejected) if repair_enabled else 0.0)
    )
    draft_encode_cost = (
        draft_encode_wall_s if draft_encode_wall_s is not None
        else sum(s.draft_encode_s for s in seg_list)
    )
    draft_cost = segment_wall_s + draft_encode_cost + concat_wall_s
    verify_cost = (
        verify_wall_s if verify_wall_s is not None else sum(s.verify_s for s in seg_list)
    ) + final_verify_wall_s
    unresolved = (
        (len(rejected) - repaired if repair_enabled else len(rejected))
        + len(invalid_accepted_proofs)
    )
    effective_delivery_verified = delivery_verified and unresolved == 0
    if effective_delivery_verified:
        tier = "delivery"
    elif not repair_enabled and rejected:
        tier = "preview"  # rejected drafts shipped as-is: below-gate content delivered
    else:
        # The lane self-prunes. A caller may deliver the already-computed whole
        # baseline artifact, but the speculative attempt itself did not earn a tier.
        tier = "fail"
    d = dict(caller_details)
    d["cost_note"] = (
        "draft_cost_s = segmentation + per-segment fast-preset encodes + concat mux "
        f"(segment {segment_wall_s:.6f}s + drafts + concat {concat_wall_s:.6f}s); "
        "verify_cost_s is CHARGED (the source is the pipeline input, so the SSIM/MD5 gate "
        "is a real product step, unlike the render lane's measurement-only reference SSIM); "
        "total_product_time_s = draft+verify+repair covers the FULL delivered pipeline."
    )
    d["repair_note"] = (
        "repaired segments are re-encoded with the EXACT baseline recipe (same codec, CRF, "
        "preset), so delivered quality is gate-or-baseline everywhere; per-segment encoding "
        "lacks whole-video rate-control context (near-identical under CRF + keyframe-aligned "
        "segments, noted not hidden)."
        if repair_enabled else
        "repair DISABLED: rejected segments shipped as drafts — quality_tier is preview, "
        "never delivery."
    )
    d["delivery_verified"] = effective_delivery_verified
    d["unresolved_failed_segments"] = unresolved
    d["invalid_accepted_timeline_proofs"] = len(invalid_accepted_proofs)
    d["final_verify_wall_s"] = round(final_verify_wall_s, 6)
    per_segment_hasher = hashlib.sha256()
    sample_count = min(len(seg_list), MAX_PER_SEGMENT_DETAIL_SAMPLES)
    head_count = (sample_count + 1) // 2
    tail_count = sample_count - head_count
    sampled_indexes = set(range(head_count))
    if tail_count:
        sampled_indexes.update(range(len(seg_list) - tail_count, len(seg_list)))
    per_segment_sample = []
    for index, s in enumerate(seg_list):
        row = {
            "unit_index": index,
            "seg_id": s.seg_id,
            "draft_ssim": s.draft_ssim,
            "draft_timeline_verified": s.draft_timeline_verified,
            "accepted": s.accepted,
            "draft_encode_s": round(s.draft_encode_s, 6),
            "verify_s": round(s.verify_s, 6),
            "repair_encode_s": round(s.repair_encode_s, 6) if (not s.accepted and repair_enabled) else 0.0,
            "repaired_ssim": s.repaired_ssim,
            "repair_verified": s.repair_verified,
            "repair_timeline_verified": s.repair_timeline_verified,
        }
        per_segment_hasher.update(_canonical_json_bytes(row))
        per_segment_hasher.update(b"\n")
        if index in sampled_indexes:
            per_segment_sample.append(row)
    # Never let caller-supplied detail arrays bypass the ingress-size contract.
    d["per_segment"] = per_segment_sample
    d["per_segment_summary"] = {
        "count": len(seg_list),
        "sha256": per_segment_hasher.hexdigest(),
        "sample_count": len(per_segment_sample),
        "sample_strategy": "first+last" if len(seg_list) > sample_count else "all",
        "side_ledger_reference": d.get("per_segment_side_ledger_reference"),
    }
    combined_evidence = _combine_evidence(s.evidence for s in seg_list)
    return TranscodeSpecReceipt(
        branch_id=branch_id,
        modality="transcode",
        units=len(seg_list),
        accepted_units=accepted,
        repaired_units=repaired,
        draft_cost_s=draft_cost,
        verify_cost_s=verify_cost,
        repair_cost_s=repair_cost,
        overhead_cost_s=overhead_wall_s,
        baseline_total_time_s=baseline_wall_s,
        baseline_source=baseline_source,
        exact=bool(exact),
        quality_tier=tier,
        evidence=combined_evidence,
        artifact_verified=(
            effective_delivery_verified
            and tier != "fail"
            and combined_evidence == MEASURED
        ),
        details=d,
    )


def assert_canonical(receipt_dict: dict[str, Any]) -> None:
    """Guard used by tests + the real runner: the emitted dict must satisfy the
    spec-engine/src/receipt.rs deserializer — required keys present with the right types,
    enum values inside the Rust enums' vocabularies. This is the Python-side mimic of
    `serde_json::from_str::<SpecReceipt>` strictness."""
    wire_size = len(_canonical_json_bytes(receipt_dict))
    if wire_size > MAX_RECEIPT_JSON_BYTES:
        raise AssertionError(
            f"canonical receipt is {wire_size:,} UTF-8 bytes; limit is "
            f"{MAX_RECEIPT_JSON_BYTES:,}"
        )
    missing = [k for k in CANONICAL_REQUIRED_FIELDS if k not in receipt_dict]
    if missing:
        raise AssertionError(f"receipt missing canonical receipt.rs fields: {missing}")
    for k in ("draft_cost_s", "verify_cost_s", "repair_cost_s", "overhead_cost_s",
              "total_product_time_s", "baseline_total_time_s",
              "accepted_fraction", "repaired_fraction"):
        if (not isinstance(receipt_dict[k], (int, float)) or isinstance(receipt_dict[k], bool)
                or not math.isfinite(receipt_dict[k])):
            raise AssertionError(f"{k} must be a number, got {type(receipt_dict[k]).__name__}")
    if not isinstance(receipt_dict["units"], int) or receipt_dict["units"] < 0:
        raise AssertionError("units must be a non-negative integer (Rust u32)")
    if not isinstance(receipt_dict["exact"], bool):
        raise AssertionError("exact must be a bool")
    if not isinstance(receipt_dict["artifact_verified"], bool):
        raise AssertionError("artifact_verified must be a bool")
    s = receipt_dict["speedup_vs_baseline"]
    if s is not None and (
        not isinstance(s, (int, float)) or isinstance(s, bool)
        or not math.isfinite(s) or s <= 0
    ):
        raise AssertionError("speedup_vs_baseline must be a number or null")
    if receipt_dict.get("quality_tier") not in QUALITY_TIERS:
        raise AssertionError(f"quality_tier must be one of {QUALITY_TIERS}")
    if receipt_dict.get("evidence") not in EVIDENCE_WIRE:
        raise AssertionError(f"evidence must be one of {EVIDENCE_WIRE}")
    if receipt_dict["artifact_verified"] and (
        receipt_dict["evidence"] != "measured" or receipt_dict["quality_tier"] == "fail"
    ):
        raise AssertionError(
            "artifact_verified requires measured evidence and a non-fail quality tier"
        )
    if receipt_dict.get("baseline_source") not in BASELINE_SOURCES:
        raise AssertionError(f"baseline_source must be one of {BASELINE_SOURCES}")
    if not isinstance(receipt_dict.get("details"), dict):
        raise AssertionError("details must be an object")
    for key in ("draft_cost_s", "verify_cost_s", "repair_cost_s", "overhead_cost_s",
                "total_product_time_s",
                "baseline_total_time_s"):
        if receipt_dict[key] < 0:
            raise AssertionError(f"{key} must be >= 0")
    if receipt_dict["baseline_source"] == "absent":
        if receipt_dict["baseline_total_time_s"] != 0 or s is not None:
            raise AssertionError("an absent baseline requires zero baseline time and null speedup")
    elif receipt_dict["baseline_total_time_s"] <= 0:
        raise AssertionError("measured/modeled baseline requires positive baseline time")
    else:
        expected_speedup = (
            receipt_dict["baseline_total_time_s"]
            / receipt_dict["total_product_time_s"]
        )
        if abs(s - expected_speedup) > 5e-6:
            raise AssertionError("speedup must equal rounded baseline/product times")
    if not (0.0 <= receipt_dict["accepted_fraction"] <= 1.0):
        raise AssertionError("accepted_fraction must be in [0,1]")
    if not (0.0 <= receipt_dict["repaired_fraction"] <= 1.0):
        raise AssertionError("repaired_fraction must be in [0,1]")
    total = receipt_dict["total_product_time_s"]
    parts = (receipt_dict["draft_cost_s"] + receipt_dict["verify_cost_s"]
             + receipt_dict["repair_cost_s"] + receipt_dict["overhead_cost_s"])
    if abs(total - parts) > 5e-6:
        raise AssertionError(
            f"total_product_time_s {total} != draft+verify+repair {parts} — nothing may hide "
            "outside the charged pipeline")


# --------------------------------------------------------------------------- #
# ffmpeg runner (the REAL local measurement path)
# --------------------------------------------------------------------------- #
_SSIM_RE = re.compile(r"All:\s*([0-9]*\.?[0-9]+)")


class FfmpegError(RuntimeError):
    pass


def have_ffmpeg() -> bool:
    # SSIM delivery now depends on decoded-frame timeline probing as well as ffmpeg.
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def ffmpeg_version() -> str:
    out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, check=True)
    return out.stdout.splitlines()[0].strip()


def _run(cmd: list[str], *, timeout_s: float | None = None) -> tuple[subprocess.CompletedProcess, float]:
    """Run a command, return (proc, wall_s). Fail loud with stderr tail on error."""
    if timeout_s is None:
        timeout_s = float(os.environ.get("CX_FFMPEG_TIMEOUT_S", "1800"))
    if not math.isfinite(timeout_s) or not 1 <= timeout_s <= 86_400:
        raise ValueError("ffmpeg timeout must be finite and in [1,86400] seconds")
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        wall = time.perf_counter() - t0
        tail = ((exc.stderr or "") if isinstance(exc.stderr, str) else "")[-2000:]
        raise FfmpegError(
            f"command timed out after {wall:.1f}s (limit {timeout_s:.1f}s): "
            f"{' '.join(cmd)}\n{tail}"
        ) from exc
    wall = time.perf_counter() - t0
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-2000:]
        raise FfmpegError(f"command failed rc={proc.returncode}: {' '.join(cmd)}\n{tail}")
    return proc, wall


# Codec configs: same codec + same rate-control target for draft/slow — ONLY the preset
# (speed knob) differs, so the SSIM gate isolates the speed/quality trade honestly.
CODECS: dict[str, dict[str, Any]] = {
    "x264": {
        "ext": ".mkv",
        # Independently encoded fast/repair segments are stream-copy concatenated.
        # Keep profile/SPS-critical settings aligned, repeat parameter sets at each
        # independently encoded boundary, and disable B-frames so preset changes cannot
        # introduce reordered/duplicate PTS in the assembled delivery.
        "slow": ["-c:v", "libx264", "-preset", "veryslow", "-crf", "23",
                 "-pix_fmt", "yuv420p", "-profile:v", "high", "-bf", "0",
                 "-refs", "1", "-coder", "ac", "-x264-params",
                 "8x8dct=1:weightp=0:repeat-headers=1"],
        "fast": ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                 "-pix_fmt", "yuv420p", "-profile:v", "high", "-bf", "0",
                 "-refs", "1", "-coder", "ac", "-x264-params",
                 "8x8dct=1:weightp=0:repeat-headers=1"],
    },
    "vp9": {
        "ext": ".mkv",  # mkv holds vp9 fine and concat-copies cleanly
        "slow": ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32", "-deadline", "good",
                 "-cpu-used", "1", "-row-mt", "1", "-pix_fmt", "yuv420p"],
        "fast": ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32", "-deadline", "realtime",
                 "-cpu-used", "8", "-row-mt", "1", "-pix_fmt", "yuv420p"],
    },
    "x264_lossless": {
        "ext": ".mkv",
        "slow": ["-c:v", "libx264", "-preset", "medium", "-qp", "0",
                 "-pix_fmt", "yuv420p", "-profile:v", "high444", "-bf", "0",
                 "-refs", "1", "-coder", "ac", "-x264-params",
                 "8x8dct=1:weightp=0:repeat-headers=1"],
        "fast": ["-c:v", "libx264", "-preset", "ultrafast", "-qp", "0",
                 "-pix_fmt", "yuv420p", "-profile:v", "high444", "-bf", "0",
                 "-refs", "1", "-coder", "ac", "-x264-params",
                 "8x8dct=1:weightp=0:repeat-headers=1"],
    },
}


def synthesize_source(path: str, *, duration_s: float, segment_time_s: float,
                      size: str = "1280x720", fps: int = 30, noise: int = 8,
                      threads: int | None = None) -> float:
    """Synthesize REAL test content locally (no external asset): first half testsrc2
    (moderate complexity), second half mandelbrot (encoder-hostile), temporal noise overlaid,
    stored LOSSLESS (x264 qp0) with keyframes forced on segment boundaries so `-c copy`
    splitting is exact. Content synthesis is NOT charged to either lane — both the baseline
    and the spec pipeline start from this same source file."""
    half = duration_s / 2.0
    fc = (
        f"[0:v]trim=duration={half},setpts=PTS-STARTPTS[a];"
        f"[1:v]trim=duration={half},setpts=PTS-STARTPTS[b];"
        f"[a][b]concat=n=2:v=1:a=0,noise=alls={noise}:allf=t,format=yuv420p[out]"
    )
    thread_args = ["-threads", str(threads)] if threads is not None else []
    filter_thread_args = (
        ["-filter_complex_threads", str(threads)] if threads is not None else []
    )
    _, wall = _run([
        "ffmpeg", "-y", "-nostdin", "-nostats",
        "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}",
        "-f", "lavfi", "-i", f"mandelbrot=size={size}:rate={fps}",
        *filter_thread_args, "-filter_complex", fc, "-map", "[out]",
        "-c:v", "libx264", "-preset", "veryfast", "-qp", "0",
        *thread_args,
        "-force_key_frames", f"expr:gte(t,n_forced*{segment_time_s:g})",
        "-x264-params", "scenecut=0",
        "-t", f"{duration_s:g}",
        path,
    ])
    return wall


def split_segments(source: str, out_dir: str, segment_time_s: float) -> tuple[list[str], float]:
    """Keyframe-aligned lossless split (stream copy). CHARGED to the spec pipeline."""
    pattern = os.path.join(out_dir, "src_seg_%03d.mkv")
    _, wall = _run([
        "ffmpeg", "-y", "-nostdin", "-nostats", "-i", source,
        "-map", "0:v:0", "-c", "copy",
        "-f", "segment", "-segment_time", f"{segment_time_s:g}",
        "-reset_timestamps", "1", pattern,
    ])
    segs = sorted(
        os.path.join(out_dir, f) for f in os.listdir(out_dir) if f.startswith("src_seg_")
    )
    if not segs:
        raise FfmpegError("segmentation produced no segments")
    return segs, wall


def encode(src: str, dst: str, codec_args: list[str], *, threads: int | None = None) -> float:
    total_threads = threads if threads is not None else ENCODE_THREAD_COMPONENTS
    decoder_threads, encoder_threads = _encode_thread_plan(total_threads)
    _, wall = _run([
        "ffmpeg", "-y", "-nostdin", "-nostats",
        "-threads", str(decoder_threads), "-i", src,
        *codec_args, "-threads", str(encoder_threads), dst,
    ])
    return wall


def _parallel_map(fn, items: list[Any], workers: int) -> list[Any]:
    """Bounded deterministic fan-out: results preserve input order."""
    if workers == 1:
        return [fn(item) for item in items]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, items))


def _bounded_worker_count(
    requested: int, units: int, aggregate_threads: int, threads_per_worker_floor: int,
) -> int:
    """Choose fanout without exceeding a shared aggregate thread envelope."""
    for name, value in (
        ("requested", requested),
        ("units", units),
        ("aggregate_threads", aggregate_threads),
        ("threads_per_worker_floor", threads_per_worker_floor),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    return min(
        requested,
        units,
        max(aggregate_threads // threads_per_worker_floor, 1),
    )


def _require_concat_compatible(
    streams: list["StreamCompatibility"],
) -> "StreamCompatibility":
    if not streams:
        raise FfmpegError("stream-copy concat requires at least one verified segment")
    expected = streams[0]
    for index, actual in enumerate(streams[1:], start=1):
        if actual != expected:
            raise FfmpegError(
                "stream-copy concat compatibility mismatch at segment "
                f"{index}: {actual.to_dict()} != {expected.to_dict()}"
            )
    return expected


def _encode_thread_plan(total_threads: int) -> tuple[int, int]:
    """Split a bounded encode process budget into input decoder and output encoder."""
    if (
        not isinstance(total_threads, int)
        or isinstance(total_threads, bool)
        or total_threads < ENCODE_THREAD_COMPONENTS
    ):
        raise ValueError(
            f"encode total thread budget must be an integer >= {ENCODE_THREAD_COMPONENTS}"
        )
    return 1, total_threads - 1


@dataclass(frozen=True)
class StreamCompatibility:
    """Container/decoder fields that must agree across stream-copy segments."""

    codec_name: str
    profile: str | None
    width: int
    height: int
    pix_fmt: str
    level: int | None
    time_base: str

    def __post_init__(self) -> None:
        for name, value, max_bytes in (
            ("codec_name", self.codec_name, 64),
            ("pix_fmt", self.pix_fmt, 64),
            ("time_base", self.time_base, 64),
        ):
            if not isinstance(value, str) or not value or len(value.encode("utf-8")) > max_bytes:
                raise ValueError(f"{name} must be a non-empty string <= {max_bytes} bytes")
        if self.profile is not None and (
            not isinstance(self.profile, str)
            or not self.profile
            or len(self.profile.encode("utf-8")) > 128
        ):
            raise ValueError("profile must be null or a non-empty string <= 128 bytes")
        for name, value in (("width", self.width), ("height", self.height)):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.level is not None and (
            not isinstance(self.level, int) or isinstance(self.level, bool) or self.level < 0
        ):
            raise ValueError("level must be null or a nonnegative integer")
        if re.fullmatch(r"[1-9][0-9]*/[1-9][0-9]*", self.time_base) is None:
            raise ValueError("time_base must be a positive rational")

    def to_dict(self) -> dict[str, Any]:
        return {
            "codec_name": self.codec_name,
            "profile": self.profile,
            "width": self.width,
            "height": self.height,
            "pix_fmt": self.pix_fmt,
            "level": self.level,
            "time_base": self.time_base,
        }


@dataclass(frozen=True)
class VideoTimeline:
    """Bounded decoded-stream identity facts used before any SSIM score can pass."""

    decoded_frames: int
    start_time_s: float | None
    duration_s: float | None
    fps: float | None
    first_frame_pts_s: float
    last_frame_pts_s: float
    normalized_pts_sha256: str
    stream_compatibility: StreamCompatibility

    def to_dict(self) -> dict[str, Any]:
        return {
            "decoded_frames": self.decoded_frames,
            "start_time_s": self.start_time_s,
            "duration_s": self.duration_s,
            "fps": self.fps,
            "first_frame_pts_s": self.first_frame_pts_s,
            "last_frame_pts_s": self.last_frame_pts_s,
            "normalized_pts_sha256": self.normalized_pts_sha256,
            "stream_compatibility": self.stream_compatibility.to_dict(),
        }


@dataclass(frozen=True)
class SsimVerification:
    score: float
    wall_s: float
    timeline_verified: bool
    timeline_reason: str
    timeline_tolerance_s: float
    distorted_timeline: VideoTimeline
    reference_timeline: VideoTimeline

    def timeline_dict(self) -> dict[str, Any]:
        return {
            "verified": self.timeline_verified,
            "reason": self.timeline_reason,
            "tolerance_s": self.timeline_tolerance_s,
            "distorted": self.distorted_timeline.to_dict(),
            "reference": self.reference_timeline.to_dict(),
        }


@dataclass(frozen=True)
class DecodedExactVerification:
    exact: bool
    wall_s: float
    timeline_verified: bool
    timeline_reason: str
    timeline_tolerance_s: float
    distorted_timeline: VideoTimeline
    reference_timeline: VideoTimeline
    distorted_md5: str | None
    reference_md5: str | None

    def timeline_dict(self) -> dict[str, Any]:
        return {
            "verified": self.timeline_verified,
            "reason": self.timeline_reason,
            "tolerance_s": self.timeline_tolerance_s,
            "distorted": self.distorted_timeline.to_dict(),
            "reference": self.reference_timeline.to_dict(),
        }


def _ssim_thread_plan(total_threads: int) -> tuple[int, int, int]:
    """Split one process budget across decoder A, decoder B, and the SSIM filter."""
    if (
        not isinstance(total_threads, int)
        or isinstance(total_threads, bool)
        or total_threads < SSIM_THREAD_COMPONENTS
    ):
        raise ValueError(
            f"SSIM total thread budget must be an integer >= {SSIM_THREAD_COMPONENTS}"
        )
    base, remainder = divmod(total_threads, SSIM_THREAD_COMPONENTS)
    return tuple(base + (1 if i < remainder else 0) for i in range(3))  # type: ignore[return-value]


def _optional_finite_float(value: Any) -> float | None:
    if value in (None, "N/A"):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _optional_rate(value: Any) -> float | None:
    if not isinstance(value, str) or value in ("", "0/0", "N/A"):
        return None
    try:
        numerator, denominator = value.split("/", 1)
        parsed = float(numerator) / float(denominator)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _probe_frame_pts_digest(
    path: str, *, decoder_threads: int,
) -> tuple[int, float, float, str, float]:
    """Stream normalized per-frame best-effort PTS into a bounded SHA-256 digest."""
    timeout_s = float(os.environ.get("CX_FFMPEG_TIMEOUT_S", "1800"))
    if not math.isfinite(timeout_s) or not 1 <= timeout_s <= 86_400:
        raise ValueError("ffmpeg timeout must be finite and in [1,86400] seconds")
    cmd = [
        "ffprobe", "-v", "error", "-threads", str(decoder_threads),
        "-select_streams", "v:0", "-show_frames",
        "-show_entries", "frame=best_effort_timestamp_time",
        "-of", "csv=p=0", path,
    ]
    started = time.perf_counter()
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
    )

    def consume_stdout() -> tuple[int, float | None, float | None, str, str | None]:
        count = 0
        first_pts_us = None
        last_pts_us = None
        digest = hashlib.sha256()
        parse_error = None
        assert proc.stdout is not None
        for line in proc.stdout:
            token = line.strip().split(",", 1)[0]
            if not token:
                continue
            try:
                pts_s = float(token)
                if not math.isfinite(pts_s):
                    raise ValueError("non-finite")
                pts_us = int(round(pts_s * 1_000_000))
            except ValueError:
                parse_error = parse_error or f"invalid frame PTS token {token!r}"
                continue
            if first_pts_us is None:
                first_pts_us = pts_us
            if last_pts_us is not None and pts_us <= last_pts_us:
                parse_error = parse_error or "frame PTS sequence is not strictly increasing"
            last_pts_us = pts_us
            normalized_pts_us = pts_us - first_pts_us
            digest.update(f"{normalized_pts_us}\n".encode("ascii"))
            count += 1
        return count, first_pts_us, last_pts_us, digest.hexdigest(), parse_error

    def consume_stderr_tail() -> str:
        """Drain continuously to prevent PIPE backpressure; retain only a bounded tail."""
        tail = ""
        assert proc.stderr is not None
        while True:
            chunk = proc.stderr.read(8192)
            if not chunk:
                break
            tail = (tail + chunk)[-2000:]
        return tail

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    stdout_future = executor.submit(consume_stdout)
    stderr_future = executor.submit(consume_stderr_tail)
    try:
        try:
            returncode = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.wait()
            raise FfmpegError(
                f"frame timeline probe timed out after {timeout_s:.1f}s for {path}"
            ) from exc
        count, first_pts_us, last_pts_us, digest, parse_error = stdout_future.result(
            timeout=5
        )
        stderr_tail = stderr_future.result(timeout=5)
    except BaseException:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()
    wall = time.perf_counter() - started
    if returncode != 0:
        raise FfmpegError(
            f"frame timeline probe failed rc={returncode} for {path}: {stderr_tail}"
        )
    if parse_error is not None:
        raise FfmpegError(f"frame timeline probe failed for {path}: {parse_error}")
    if count <= 0 or first_pts_us is None or last_pts_us is None:
        raise FfmpegError(f"frame timeline probe found no decoded frames for {path}")
    return count, first_pts_us / 1_000_000, last_pts_us / 1_000_000, digest, wall


def probe_video_timeline(
    path: str, *, decoder_threads: int,
) -> tuple[VideoTimeline, float]:
    """Decode-count one video stream and read bounded container PTS coverage metadata."""
    if not isinstance(decoder_threads, int) or isinstance(decoder_threads, bool) or decoder_threads < 1:
        raise ValueError("decoder_threads must be a positive integer")
    proc, wall = _run([
        "ffprobe", "-v", "error", "-threads", str(decoder_threads),
        "-select_streams", "v:0",
        "-show_entries",
        "stream=start_time,duration,avg_frame_rate,codec_name,profile,width,height,"
        "pix_fmt,level,time_base:format=start_time,duration",
        "-of", "json", path,
    ])
    try:
        payload = json.loads(proc.stdout or "")
        streams = payload["streams"]
        stream = streams[0]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise FfmpegError(f"could not parse video timeline for {path}") from exc
    decoded_frames, first_pts_s, last_pts_s, pts_digest, pts_wall = (
        _probe_frame_pts_digest(path, decoder_threads=decoder_threads)
    )
    fmt = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    start_time_s = _optional_finite_float(stream.get("start_time"))
    if start_time_s is None:
        start_time_s = _optional_finite_float(fmt.get("start_time"))
    duration_s = _optional_finite_float(stream.get("duration"))
    if duration_s is None:
        duration_s = _optional_finite_float(fmt.get("duration"))
    fps = _optional_rate(stream.get("avg_frame_rate"))
    try:
        stream_compatibility = StreamCompatibility(
            codec_name=stream["codec_name"],
            profile=stream.get("profile"),
            width=stream["width"],
            height=stream["height"],
            pix_fmt=stream["pix_fmt"],
            # FFprobe uses -99 for codecs such as FFV1 that do not expose a level.
            level=(None if stream.get("level") == -99 else stream.get("level")),
            time_base=stream["time_base"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise FfmpegError(f"invalid video stream compatibility fields for {path}") from exc
    return VideoTimeline(
        decoded_frames, start_time_s, duration_s, fps,
        first_pts_s, last_pts_s, pts_digest, stream_compatibility,
    ), wall + pts_wall


def _compare_timelines(
    distorted: VideoTimeline, reference: VideoTimeline,
) -> tuple[bool, str, float]:
    def finite_number(value: Any) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )

    def invalid_reason(timeline: VideoTimeline) -> str | None:
        if (
            not isinstance(timeline.decoded_frames, int)
            or isinstance(timeline.decoded_frames, bool)
            or timeline.decoded_frames <= 0
        ):
            return "decoded_frames must be a positive integer"
        if not finite_number(timeline.first_frame_pts_s):
            return "first_frame_pts_s must be finite"
        if not finite_number(timeline.last_frame_pts_s):
            return "last_frame_pts_s must be finite"
        if timeline.last_frame_pts_s < timeline.first_frame_pts_s:
            return "decoded PTS coverage is reversed"
        if timeline.start_time_s is not None and not finite_number(timeline.start_time_s):
            return "start_time_s must be finite when present"
        if timeline.duration_s is not None and (
            not finite_number(timeline.duration_s) or timeline.duration_s < 0
        ):
            return "duration_s must be finite and nonnegative when present"
        if timeline.fps is not None and (
            not finite_number(timeline.fps) or timeline.fps <= 0
        ):
            return "fps must be finite and positive when present"
        if (
            not isinstance(timeline.normalized_pts_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", timeline.normalized_pts_sha256) is None
        ):
            return "normalized_pts_sha256 must be a lowercase SHA-256 digest"
        if not isinstance(timeline.stream_compatibility, StreamCompatibility):
            return "stream_compatibility must be validated"
        return None

    for label, timeline in (("distorted", distorted), ("reference", reference)):
        reason = invalid_reason(timeline)
        if reason is not None:
            return False, f"invalid {label} timeline: {reason}", 0.002
    # ffprobe emits microsecond decimals while common containers quantize to 1 ms. A 2 ms
    # allowance covers that representation boundary without accepting a one-frame shift.
    tolerance_s = 0.002
    if distorted.decoded_frames != reference.decoded_frames:
        return (
            False,
            "decoded-frame count mismatch: "
            f"{distorted.decoded_frames} != {reference.decoded_frames}",
            tolerance_s,
        )
    if distorted.normalized_pts_sha256 != reference.normalized_pts_sha256:
        return False, "normalized per-frame cadence/PTS mismatch", tolerance_s
    if abs(distorted.first_frame_pts_s - reference.first_frame_pts_s) > tolerance_s:
        return False, "decoded first-frame PTS mismatch", tolerance_s
    if abs(distorted.last_frame_pts_s - reference.last_frame_pts_s) > tolerance_s:
        return False, "decoded last-frame PTS mismatch", tolerance_s
    if (distorted.start_time_s is None) != (reference.start_time_s is None):
        return False, "start-PTS metadata availability mismatch", tolerance_s
    if (distorted.duration_s is None) != (reference.duration_s is None):
        return False, "duration metadata availability mismatch", tolerance_s
    if distorted.start_time_s is not None and reference.start_time_s is not None:
        if abs(distorted.start_time_s - reference.start_time_s) > tolerance_s:
            return False, "start-PTS coverage mismatch", tolerance_s
    if distorted.duration_s is not None and reference.duration_s is not None:
        if abs(distorted.duration_s - reference.duration_s) > tolerance_s:
            return False, "duration/PTS coverage mismatch", tolerance_s
    checked = ["decoded-frame count", "decoded PTS coverage"]
    if distorted.start_time_s is not None and reference.start_time_s is not None:
        checked.append("start PTS")
    if distorted.duration_s is not None and reference.duration_s is not None:
        checked.append("duration")
    return True, "matched " + ", ".join(checked), tolerance_s


def verify_decoded_exact(
    distorted: str, reference: str, *, threads: int,
) -> DecodedExactVerification:
    """Require equal decoded timeline coverage before comparing decoded bytes."""
    if not isinstance(threads, int) or isinstance(threads, bool) or threads < 1:
        raise ValueError("decoded exact thread budget must be a positive integer")
    distorted_timeline, distorted_probe_wall = probe_video_timeline(
        distorted, decoder_threads=threads
    )
    reference_timeline, reference_probe_wall = probe_video_timeline(
        reference, decoder_threads=threads
    )
    timeline_verified, timeline_reason, tolerance_s = _compare_timelines(
        distorted_timeline, reference_timeline
    )
    wall = distorted_probe_wall + reference_probe_wall
    if not timeline_verified:
        return DecodedExactVerification(
            exact=False,
            wall_s=wall,
            timeline_verified=False,
            timeline_reason=timeline_reason,
            timeline_tolerance_s=tolerance_s,
            distorted_timeline=distorted_timeline,
            reference_timeline=reference_timeline,
            distorted_md5=None,
            reference_md5=None,
        )
    distorted_digest, distorted_hash_wall = decoded_md5(distorted, threads=threads)
    reference_digest, reference_hash_wall = decoded_md5(reference, threads=threads)
    return DecodedExactVerification(
        exact=distorted_digest == reference_digest,
        wall_s=wall + distorted_hash_wall + reference_hash_wall,
        timeline_verified=True,
        timeline_reason=timeline_reason,
        timeline_tolerance_s=tolerance_s,
        distorted_timeline=distorted_timeline,
        reference_timeline=reference_timeline,
        distorted_md5=distorted_digest,
        reference_md5=reference_digest,
    )


def verify_ssim(
    distorted: str, reference: str, *, threads: int,
) -> SsimVerification:
    """Prove equal decoded sequence coverage, then compute global SSIM.

    FFmpeg's SSIM filter stops at the shorter input and can report 1.0 for a truncated
    static clip.  Frame-count equality is therefore a mandatory precondition.  Available
    start/duration metadata is compared as an additional PTS-coverage check.
    """
    decoder_a_threads, decoder_b_threads, filter_threads = _ssim_thread_plan(threads)
    distorted_timeline, distorted_probe_wall = probe_video_timeline(
        distorted, decoder_threads=decoder_a_threads
    )
    reference_timeline, reference_probe_wall = probe_video_timeline(
        reference, decoder_threads=decoder_b_threads
    )
    timeline_verified, timeline_reason, tolerance_s = _compare_timelines(
        distorted_timeline, reference_timeline
    )
    probe_wall = distorted_probe_wall + reference_probe_wall
    if not timeline_verified:
        return SsimVerification(
            score=0.0,
            wall_s=probe_wall,
            timeline_verified=False,
            timeline_reason=timeline_reason,
            timeline_tolerance_s=tolerance_s,
            distorted_timeline=distorted_timeline,
            reference_timeline=reference_timeline,
        )
    proc, ssim_wall = _run([
        "ffmpeg", "-nostdin", "-nostats",
        "-threads", str(decoder_a_threads), "-i", distorted,
        "-threads", str(decoder_b_threads), "-i", reference,
        "-filter_threads", str(filter_threads),
        "-lavfi", "[0:v][1:v]ssim", "-f", "null", "-",
    ])
    m = _SSIM_RE.search(proc.stderr or "")
    if not m:
        raise FfmpegError(f"could not parse SSIM from ffmpeg output for {distorted}")
    return SsimVerification(
        score=float(m.group(1)),
        wall_s=probe_wall + ssim_wall,
        timeline_verified=True,
        timeline_reason=timeline_reason,
        timeline_tolerance_s=tolerance_s,
        distorted_timeline=distorted_timeline,
        reference_timeline=reference_timeline,
    )


def ssim(
    distorted: str, reference: str, *, threads: int = SSIM_THREAD_COMPONENTS,
) -> tuple[float, float]:
    """Compatibility wrapper returning (score, charged verification wall time)."""
    verified = verify_ssim(distorted, reference, threads=threads)
    return verified.score, verified.wall_s


def decoded_md5(path: str, *, threads: int | None = None) -> tuple[str, float]:
    """MD5 of the DECODED yuv420p frames — the byte-check that proves losslessness."""
    thread_args = ["-threads", str(threads)] if threads is not None else []
    proc, wall = _run([
        "ffmpeg", "-nostdin", "-nostats", *thread_args, "-i", path, "-map", "0:v:0",
        "-pix_fmt", "yuv420p", "-f", "md5", "-",
    ])
    for line in (proc.stdout or "").splitlines():
        if line.startswith("MD5="):
            return line.strip(), wall
    raise FfmpegError(f"could not parse decoded MD5 for {path}")


def concat_segments(paths: list[str], dst: str, workdir: str) -> float:
    if not isinstance(paths, list) or not paths:
        raise ValueError("concat paths must be a non-empty list")
    root = _canonical_workdir_path(workdir)
    _private_directory_identity(root, require_empty=False)
    dst_abs = os.path.abspath(dst)
    if os.path.realpath(os.path.dirname(dst_abs)) != root:
        raise ValueError("concat destination must be directly inside workdir")
    dst_abs = os.path.join(root, os.path.basename(dst_abs))
    if os.path.lexists(dst_abs):
        raise ValueError("concat destination already exists; refusing overwrite")
    manifest_rows = []
    for path in paths:
        path_abs = os.path.abspath(os.fspath(path))
        info = os.lstat(path_abs)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError("concat inputs must be regular non-symlink files")
        resolved = os.path.realpath(path_abs)
        try:
            contained = os.path.commonpath((root, resolved)) == root
        except ValueError:
            contained = False
        if not contained:
            raise ValueError("concat input escapes workdir")
        relative = os.path.relpath(resolved, root)
        if "\n" in relative or "\r" in relative:
            raise ValueError("concat paths cannot contain line breaks")
        # FFconcat uses libavutil token quoting. Close/reopen a single-quoted
        # token around an escaped quote, while retaining a relative path whose
        # containment was established above.
        escaped = relative.replace("'", "'\\''")
        manifest_rows.append(f"file '{escaped}'\n")
    lst = os.path.join(root, "concat_list.txt")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    manifest_fd = os.open(lst, flags, 0o600)
    try:
        payload = "".join(manifest_rows).encode("utf-8")
        offset = 0
        while offset < len(payload):
            offset += os.write(manifest_fd, payload[offset:])
        os.fsync(manifest_fd)
    finally:
        os.close(manifest_fd)
    directory_fd = os.open(
        root,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    _, wall = _run(["ffmpeg", "-y", "-nostdin", "-nostats", "-f", "concat", "-safe", "0",
                    "-i", lst, "-c", "copy", dst_abs])
    return wall


def _sha256_fd(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(fd, 1 << 20)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _publish_verified_artifact(candidate: str, destination: str) -> tuple[str, int]:
    """No-clobber, descriptor-retained publication of one completed artifact."""
    candidate_abs = os.path.abspath(candidate)
    destination_abs = os.path.abspath(destination)
    root = os.path.realpath(os.path.dirname(candidate_abs))
    if os.path.realpath(os.path.dirname(destination_abs)) != root:
        raise ValueError("candidate and destination must share one publication directory")
    candidate_name = os.path.basename(candidate_abs)
    destination_name = os.path.basename(destination_abs)
    if candidate_name in ("", ".", "..") or destination_name in ("", ".", ".."):
        raise ValueError("invalid publication filename")
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    dir_flags |= getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(root, dir_flags)
    candidate_fd = -1
    linked = False
    try:
        path_info = os.lstat(root)
        opened_info = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(path_info.st_mode)
            or stat.S_ISLNK(path_info.st_mode)
            or (path_info.st_dev, path_info.st_ino)
            != (opened_info.st_dev, opened_info.st_ino)
        ):
            raise RuntimeError("publication directory identity changed")
        candidate_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        candidate_flags |= getattr(os, "O_NOFOLLOW", 0)
        candidate_fd = os.open(candidate_name, candidate_flags, dir_fd=directory_fd)
        before = os.fstat(candidate_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RuntimeError("publication candidate must be a singly-linked regular file")
        first_sha256 = _sha256_fd(candidate_fd)
        os.fsync(candidate_fd)
        os.link(
            candidate_name,
            destination_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        linked = True
        os.fsync(directory_fd)
        published = os.stat(
            destination_name, dir_fd=directory_fd, follow_symlinks=False
        )
        if (
            (published.st_dev, published.st_ino, published.st_size)
            != (before.st_dev, before.st_ino, before.st_size)
        ):
            raise RuntimeError("published artifact identity differs from retained candidate")
        if _sha256_fd(candidate_fd) != first_sha256:
            raise RuntimeError("publication candidate changed across directory fsync")
        os.unlink(candidate_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
        published_after = os.stat(
            destination_name, dir_fd=directory_fd, follow_symlinks=False
        )
        retained_after = os.fstat(candidate_fd)
        if (
            (published_after.st_dev, published_after.st_ino, published_after.st_size)
            != (before.st_dev, before.st_ino, before.st_size)
            or retained_after.st_nlink != 1
            or _sha256_fd(candidate_fd) != first_sha256
        ):
            raise RuntimeError("published artifact changed during final commit fsync")
        return first_sha256, before.st_size
    except BaseException:
        if linked:
            try:
                os.unlink(destination_name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except OSError:
                pass
        raise
    finally:
        if candidate_fd >= 0:
            os.close(candidate_fd)
        os.close(directory_fd)


def file_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _run_real_impl(
    *,
    codec: str,
    mode: str,  # "ssim" | "lossless"
    workdir: str,
    duration_s: float = 12.0,
    segment_time_s: float = 2.0,
    gate: float = DEFAULT_SSIM_GATE,
    size: str = "1280x720",
    fps: int = 30,
    noise: int = 8,
    keep_artifacts: bool = False,
    workers: int = DEFAULT_WORKERS,
    ffmpeg_threads: int | None = None,
    measure_baseline: bool = True,
) -> dict[str, Any]:
    """Run the real verified product pipeline.

    Benchmark mode (the default) also measures a whole-video slow baseline and
    earns a speedup ratio. Production mode sets ``measure_baseline=False``:
    output verification is unchanged, the benchmark-only encode is skipped,
    and the receipt emits baseline_source=absent with a null speedup.
    """
    workdir = _canonical_workdir_path(workdir)
    _private_directory_identity(workdir, require_empty=True)
    if codec not in CODECS:
        raise ValueError(f"codec must be one of {sorted(CODECS)}")
    if mode not in ("ssim", "lossless"):
        raise ValueError("mode must be 'ssim' or 'lossless'")
    if mode == "lossless" and codec != "x264_lossless":
        raise ValueError("lossless mode requires codec='x264_lossless'")
    if not math.isfinite(duration_s) or not 0 < duration_s <= 3600:
        raise ValueError("duration_s must be finite and in (0,3600]")
    if not math.isfinite(segment_time_s) or not 0 < segment_time_s <= 600:
        raise ValueError("segment_time_s must be finite and in (0,600]")
    planned_segments = plan_segments(duration_s, segment_time_s)
    if planned_segments > 10_000:
        raise ValueError("segment plan exceeds the 10,000-segment safety cap")
    if not math.isfinite(gate) or not 0.0 <= gate <= 1.0:
        raise ValueError("gate must be finite in [0,1]")
    m = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)", size)
    if not m:
        raise ValueError("size must be WIDTHxHEIGHT with positive integers")
    width, height = map(int, m.groups())
    if width > 16_384 or height > 16_384 or width * height > 67_108_864:
        raise ValueError("size exceeds the 16K/64-megapixel safety envelope")
    if not isinstance(fps, int) or isinstance(fps, bool) or not 1 <= fps <= 240:
        raise ValueError("fps must be an integer in [1,240]")
    if not isinstance(noise, int) or isinstance(noise, bool) or not 0 <= noise <= 100:
        raise ValueError("noise must be an integer in [0,100]")
    if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= 32:
        raise ValueError("workers must be an integer in [1,32]")
    if ffmpeg_threads is not None and (
        not isinstance(ffmpeg_threads, int) or isinstance(ffmpeg_threads, bool)
        or not 1 <= ffmpeg_threads <= 256
    ):
        raise ValueError("ffmpeg_threads must be null or an integer in [1,256]")
    if not isinstance(measure_baseline, bool):
        raise TypeError("measure_baseline must be a bool")
    if not isinstance(keep_artifacts, bool):
        raise TypeError("keep_artifacts must be a bool")
    frame_count = math.ceil(duration_s * fps)
    pixel_frames = width * height * frame_count
    if pixel_frames > MAX_PIXEL_FRAMES:
        raise ValueError(
            f"workload has {pixel_frames:,} pixel-frames; safety cap is "
            f"{MAX_PIXEL_FRAMES:,}"
        )
    cfg = CODECS[codec]
    estimated_raw_bytes = (pixel_frames * 3 + 1) // 2
    required_free_bytes = required_scratch_bytes(pixel_frames)
    free_bytes = shutil.disk_usage(workdir).free
    if free_bytes < required_free_bytes:
        raise ValueError(
            f"insufficient scratch space: need at least {required_free_bytes:,} bytes "
            f"for bounded intermediates, have {free_bytes:,}"
        )
    cpu_count = max(os.cpu_count() or 1, 1)
    host_thread_budget = min(cpu_count, 256)
    requested_aggregate_threads = min(
        ffmpeg_threads if ffmpeg_threads is not None else host_thread_budget,
        host_thread_budget,
    )
    # Each phase gets one aggregate budget shared by baseline and speculation. Floors
    # reflect simultaneously live FFmpeg components, even on a 1-2 CPU host: an encode
    # has one decoder+encoder, and SSIM has two decoders+one filter.
    encode_aggregate_thread_budget = max(
        requested_aggregate_threads, ENCODE_THREAD_COMPONENTS
    )
    exact_aggregate_thread_budget = max(requested_aggregate_threads, 1)
    aggregate_thread_budget = max(
        requested_aggregate_threads, SSIM_THREAD_COMPONENTS
    )
    baseline_encode_threads = encode_aggregate_thread_budget
    baseline_decode_threads = exact_aggregate_thread_budget
    baseline_ssim_threads = aggregate_thread_budget
    src = os.path.join(workdir, "source.mkv")
    synth_wall = synthesize_source(
        src, duration_s=duration_s, segment_time_s=segment_time_s,
        size=size, fps=fps, noise=noise, threads=baseline_encode_threads,
    )

    # ---- BASELINE: one whole-video slow-preset encode (the honest denominator) ---- #
    baseline_out = os.path.join(workdir, f"baseline{cfg['ext']}")
    if measure_baseline:
        baseline_wall = encode(
            src, baseline_out, cfg["slow"], threads=baseline_encode_threads
        )
        baseline_ssim_check = verify_ssim(
            baseline_out, src, threads=baseline_ssim_threads
        )
        baseline_ssim = baseline_ssim_check.score
    else:
        baseline_wall = 0.0
        baseline_ssim = None
        baseline_ssim_check = None

    # ---- SPEC PIPELINE (every second below is charged) ---------------------------- #
    seg_dir = os.path.join(workdir, "segs")
    os.makedirs(seg_dir)
    product_wall_start = time.perf_counter()
    src_segs, segment_wall = split_segments(src, seg_dir, segment_time_s)
    worker_count = _bounded_worker_count(
        workers,
        len(src_segs),
        encode_aggregate_thread_budget,
        ENCODE_THREAD_COMPONENTS,
    )
    verify_worker_count = (
        min(
            worker_count,
            max(aggregate_thread_budget // SSIM_THREAD_COMPONENTS, 1),
        )
        if mode == "ssim" else worker_count
    )
    # The plan and real split should agree; recompute only to tighten the cap if
    # ffmpeg emitted fewer segments than the duration ceiling predicted.
    encode_threads_per_worker = (
        encode_aggregate_thread_budget // worker_count
    )
    exact_threads_per_worker = (
        exact_aggregate_thread_budget // verify_worker_count
    )
    ssim_threads_per_worker = (
        aggregate_thread_budget // verify_worker_count
    )
    indexed_segs = list(enumerate(src_segs))

    # Stage 1: bounded parallel fast-preset drafts.
    def draft_one(item):
        i, sseg = item
        out = os.path.join(seg_dir, f"draft_{i:03d}{cfg['ext']}")
        wall = encode(sseg, out, cfg["fast"], threads=encode_threads_per_worker)
        return {"i": i, "src": sseg, "draft": out, "draft_wall": wall}

    stage_start = time.perf_counter()
    states = _parallel_map(draft_one, indexed_segs, worker_count)
    draft_stage_wall = time.perf_counter() - stage_start

    # Stage 2: bounded parallel source-vs-draft verification.
    def verify_one(state):
        state = dict(state)
        if mode == "ssim":
            verification = verify_ssim(
                state["draft"], state["src"], threads=ssim_threads_per_worker
            )
            state.update(
                score=verification.score,
                verify_wall=verification.wall_s,
                accepted=verification.timeline_verified and verification.score >= gate,
                draft_timeline=verification.timeline_dict(),
                draft_stream_compatibility=(
                    verification.distorted_timeline.stream_compatibility
                ),
            )
        else:
            verification = verify_decoded_exact(
                state["draft"], state["src"], threads=exact_threads_per_worker
            )
            state.update(
                score=1.0 if verification.exact else 0.0,
                verify_wall=verification.wall_s,
                accepted=verification.exact,
                draft_timeline=verification.timeline_dict(),
                draft_stream_compatibility=(
                    verification.distorted_timeline.stream_compatibility
                ),
            )
        return state

    stage_start = time.perf_counter()
    states = _parallel_map(verify_one, states, verify_worker_count)
    verify_stage_wall = time.perf_counter() - stage_start

    # Stage 3: bounded parallel baseline-recipe repairs for rejected segments.
    rejected_states = [state for state in states if not state["accepted"]]

    def repair_one(state):
        state = dict(state)
        repair_out = os.path.join(seg_dir, f"repair_{state['i']:03d}{cfg['ext']}")
        state["repair"] = repair_out
        state["repair_wall"] = encode(
            state["src"], repair_out, cfg["slow"], threads=encode_threads_per_worker
        )
        return state

    stage_start = time.perf_counter()
    repaired_states = _parallel_map(repair_one, rejected_states, worker_count)
    repair_stage_wall = time.perf_counter() - stage_start if rejected_states else 0.0

    # Stage 4: reverify repaired artifacts. This proof is charged.
    def verify_repair_one(state):
        state = dict(state)
        if mode == "ssim":
            verification = verify_ssim(
                state["repair"], state["src"], threads=ssim_threads_per_worker
            )
            state.update(
                repaired_score=verification.score,
                repair_verify_wall=verification.wall_s,
                repair_verified=(
                    verification.timeline_verified and verification.score >= gate
                ),
                repair_timeline=verification.timeline_dict(),
                repair_stream_compatibility=(
                    verification.distorted_timeline.stream_compatibility
                ),
            )
        else:
            verification = verify_decoded_exact(
                state["repair"], state["src"], threads=exact_threads_per_worker
            )
            state.update(
                repaired_score=None,
                repair_verify_wall=verification.wall_s,
                repair_verified=verification.exact,
                repair_timeline=verification.timeline_dict(),
                repair_stream_compatibility=(
                    verification.distorted_timeline.stream_compatibility
                ),
            )
        return state

    stage_start = time.perf_counter()
    repaired_states = _parallel_map(
        verify_repair_one, repaired_states, verify_worker_count
    )
    repair_verify_stage_wall = time.perf_counter() - stage_start if repaired_states else 0.0
    repaired_by_index = {state["i"]: state for state in repaired_states}

    results: list[SegmentResult] = []
    delivered_paths: list[str] = []
    delivered_streams: list[StreamCompatibility] = []
    for state in states:
        repaired = repaired_by_index.get(state["i"])
        delivered_path = state["draft"] if state["accepted"] else repaired["repair"]
        delivered_paths.append(delivered_path)
        delivered_streams.append(
            state["draft_stream_compatibility"]
            if state["accepted"]
            else repaired["repair_stream_compatibility"]
        )
        results.append(SegmentResult(
            seg_id=f"seg_{state['i']:03d}",
            draft_encode_s=state["draft_wall"],
            verify_s=state["verify_wall"] + (repaired["repair_verify_wall"] if repaired else 0.0),
            accepted=state["accepted"],
            draft_ssim=state["score"],
            repair_encode_s=repaired["repair_wall"] if repaired else 0.0,
            repaired_ssim=repaired["repaired_score"] if repaired else None,
            repair_verified=repaired["repair_verified"] if repaired else None,
            draft_timeline_verified=state["draft_timeline"]["verified"],
            repair_timeline_verified=(
                repaired["repair_timeline"]["verified"] if repaired else None
            ),
            draft_bytes=os.path.getsize(state["draft"]),
            repair_bytes=os.path.getsize(repaired["repair"]) if repaired else 0,
            evidence=MEASURED,
        ))

    concat_stream_compatibility = _require_concat_compatible(delivered_streams)
    # The assembled file remains private under a non-final name until every
    # content/timeline gate passes. A customer-visible filename never denotes a
    # partial concat or an artifact still under verification.
    delivered = os.path.join(workdir, f".delivered.candidate{cfg['ext']}")
    concat_wall = concat_segments(delivered_paths, delivered, workdir)

    # ---- final product verification ----------------------------------------------- #
    # SSIM mode needs the full score+timeline gate. Lossless mode already performs
    # the stronger decoded MD5+timeline proof below, so a redundant final SSIM
    # decode would only add latency without strengthening its contract.
    delivered_ssim_check = None
    delivered_ssim = None
    delivered_verify_wall = 0.0
    exact = False
    final_verify_wall = 0.0
    delivery_verified = False
    if mode == "ssim":
        delivered_ssim_check = verify_ssim(
            delivered, src, threads=baseline_ssim_threads
        )
        delivered_ssim = delivered_ssim_check.score
        delivered_verify_wall = delivered_ssim_check.wall_s
        final_verify_wall = delivered_verify_wall
        delivery_verified = (
            delivered_ssim_check.timeline_verified and delivered_ssim >= gate
        )
    lossless_exact_check = None
    lossless_note = (
        "SSIM-gated delivery is NOT lossless: accepted segments are lossy fast-preset "
        "encodes that cleared the gate, exact=false by construction."
    )
    if mode == "lossless":
        lossless_exact_check = verify_decoded_exact(
            delivered, src, threads=baseline_decode_threads
        )
        final_verify_wall += lossless_exact_check.wall_s
        exact = lossless_exact_check.exact
        delivery_verified = exact
        src_full_md5 = lossless_exact_check.reference_md5
        lossless_note = (
            "lossless mode: decoded-frame timeline plus MD5 of the delivered concat "
            f"{'MATCHES' if exact else 'DOES NOT MATCH'} the source ({src_full_md5})."
        )
    final_delivery_verified = (
        delivery_verified
        and all(s.accepted or s.repair_verified is True for s in results)
        and (
            (baseline_ssim >= gate)
            if mode == "ssim" and baseline_ssim is not None
            else True
        )
    )
    # Byte-vs-baseline is a measurement-only audit and deliberately remains
    # outside product accounting. Run it before the private candidate may be
    # atomically renamed/published.
    bitexact_vs_baseline = (
        file_md5(delivered) == file_md5(baseline_out) if measure_baseline else None
    )

    publication_path = None
    if final_delivery_verified and (keep_artifacts or not measure_baseline):
        publication_path = os.path.join(workdir, f"delivered{cfg['ext']}")
    elif keep_artifacts and not final_delivery_verified:
        publication_path = os.path.join(workdir, f"candidate-rejected{cfg['ext']}")

    # Content binding and durable no-clobber publication are product work.
    # Charge the retained-descriptor hashes and parent-directory fsync to the
    # final verification phase.
    digest_start = time.perf_counter()
    source_artifact_sha256 = file_sha256(src)
    if publication_path is not None:
        delivered_artifact_sha256, delivered_bytes = _publish_verified_artifact(
            delivered, publication_path
        )
    else:
        delivered_artifact_sha256 = file_sha256(delivered)
        delivered_bytes = os.path.getsize(delivered)
    artifact_digest_wall = time.perf_counter() - digest_start
    final_verify_wall += artifact_digest_wall

    # Enclosing product wall: all charged stages plus Python scheduling/assembly.
    product_outer_wall = time.perf_counter() - product_wall_start
    phase_accounted = (
        segment_wall + draft_stage_wall + concat_wall
        + verify_stage_wall + repair_verify_stage_wall + final_verify_wall
        + repair_stage_wall
    )
    overhead_wall = max(product_outer_wall - phase_accounted, 0.0)

    details: dict[str, Any] = {
        "run_label": "MEASURED/local",
        "host": "local (Apple Silicon, ffmpeg CPU encode)",
        "ffmpeg": ffmpeg_version(),
        "codec": codec,
        "mode": mode,
        "concat_stream_compatibility": concat_stream_compatibility.to_dict(),
        "concat_contract": (
            "all independently encoded segments share codec/profile/level/pixel-format/"
            "geometry/time-base; x264 segments repeat parameter sets in-band"
        ),
        "content": f"testsrc2+mandelbrot halves, noise=alls={noise}:allf=t, {size}@{fps}fps, "
                   f"{duration_s:g}s (synthesized locally, lossless x264 qp0 mezzanine; "
                   "synthesis is content creation, charged to NEITHER lane)",
        "source_synthesis_s_uncharged": round(synth_wall, 6),
        "pixel_frames": pixel_frames,
        "pixel_frame_safety_cap": MAX_PIXEL_FRAMES,
        "scratch_preflight": {
            "decoded_yuv420p_bytes": estimated_raw_bytes,
            "measured_retained_peak_raw_multiplier": (
                MEASURED_RETAINED_PEAK_RAW_MULTIPLIER
            ),
            "reserved_raw_multiplier": SCRATCH_RAW_MULTIPLIER,
            "fixed_headroom_bytes": SCRATCH_FIXED_HEADROOM_BYTES,
            "required_free_bytes": required_free_bytes,
            "available_free_bytes_at_start": free_bytes,
        },
        "segment_time_s": segment_time_s,
        "ssim_gate": gate if mode == "ssim" else None,
        "slow_args": " ".join(cfg["slow"]),
        "fast_args": " ".join(cfg["fast"]),
        "baseline_ssim_vs_source": (
            round(baseline_ssim, 6) if baseline_ssim is not None else None
        ),
        "baseline_timeline_verification": (
            baseline_ssim_check.timeline_dict() if baseline_ssim_check else None
        ),
        # Reference self-consistency (the render lane's hard-won lesson, applied here on
        # day one): the accept gate is only MEANINGFUL if the baseline recipe itself clears
        # it. When gate_achievable_by_baseline is False, rejecting a draft buys a repair
        # that may score WORSE than the draft (the repair is baseline-recipe by contract) —
        # the run is then structurally <=1x and self-prunes honestly.
        "gate_achievable_by_baseline": (
            (baseline_ssim >= gate) if mode == "ssim" and baseline_ssim is not None else None
        ),
        "delivered_ssim_vs_source": (
            round(delivered_ssim, 6) if delivered_ssim is not None else None
        ),
        "delivered_timeline_verification": (
            delivered_ssim_check.timeline_dict()
            if delivered_ssim_check is not None
            else lossless_exact_check.timeline_dict()
            if lossless_exact_check is not None
            else None
        ),
        "lossless_timeline_verification": (
            lossless_exact_check.timeline_dict() if lossless_exact_check else None
        ),
        "delivered_bytes": delivered_bytes,
        "source_artifact_sha256": source_artifact_sha256,
        "delivered_artifact_sha256": delivered_artifact_sha256,
        "artifact_digest_wall_s": round(artifact_digest_wall, 6),
        "baseline_bytes": os.path.getsize(baseline_out) if measure_baseline else None,
        "bitexact_vs_baseline": bitexact_vs_baseline,
        "benchmark_baseline_measured": measure_baseline,
        "delivered_artifact_path": (
            os.path.abspath(publication_path)
            if final_delivery_verified and (keep_artifacts or not measure_baseline)
            else None
        ),
        "candidate_artifact_path": (
            os.path.abspath(publication_path)
            if keep_artifacts and not final_delivery_verified
            else None
        ),
        "artifact_publication": {
            "atomic_no_clobber": publication_path is not None,
            "retained_descriptor_through_directory_fsync": publication_path is not None,
            "post_fsync_full_sha256_replay": publication_path is not None,
            "private_candidate_name": os.path.basename(delivered),
            "published_name": (
                os.path.basename(publication_path)
                if publication_path is not None else None
            ),
        },
        "artifact_disposition": (
            "delivery" if final_delivery_verified else "candidate_rejected"
        ),
        "lossless_note": lossless_note,
        "audit_note": (
            "Every SSIM and decoded-MD5 gate first proves equal decoded-frame count, an "
            "equal normalized per-frame best-effort-PTS digest, equal absolute decoded "
            "first/last PTS within 2 ms, and equal available container start/duration "
            "coverage within 2 ms; scores "
            "or hashes alone cannot bless truncated, shifted, retimed, or variable-cadence "
            "input. Baseline SSIM and byte-vs-baseline are measurement-only. Repair and "
            "final-delivery verification are product gates and ARE charged to verify_cost_s."
        ),
        "parallelism": {
            "workers": worker_count,
            "requested_workers": workers,
            "requested_ffmpeg_threads": ffmpeg_threads,
            "ffmpeg_threads_semantics": "aggregate phase budget override, capped to host CPUs",
            "verify_workers": verify_worker_count,
            # Kept for consumers of the previous detail key; it now explicitly means
            # encoder threads, not threads repeated independently across SSIM components.
            "ffmpeg_threads_per_worker": encode_threads_per_worker,
            "ffmpeg_encode_threads_per_worker": encode_threads_per_worker,
            "encode_component_thread_plan": list(
                _encode_thread_plan(encode_threads_per_worker)
            ),
            "encode_aggregate_thread_budget": encode_aggregate_thread_budget,
            "max_concurrent_encode_threads": (
                worker_count * encode_threads_per_worker
            ),
            "exact_decode_threads_per_worker": exact_threads_per_worker,
            "exact_aggregate_thread_budget": exact_aggregate_thread_budget,
            "max_concurrent_exact_decode_threads": (
                verify_worker_count * exact_threads_per_worker
                if mode == "lossless" else None
            ),
            "ssim_total_threads_per_worker": (
                ssim_threads_per_worker if mode == "ssim" else None
            ),
            "ssim_component_thread_plan": (
                list(_ssim_thread_plan(ssim_threads_per_worker))
                if mode == "ssim" else None
            ),
            "max_concurrent_ssim_threads": (
                verify_worker_count * ssim_threads_per_worker
                if mode == "ssim" else None
            ),
            "final_ssim_component_thread_plan": list(
                _ssim_thread_plan(baseline_ssim_threads)
            ),
            "cpu_count": cpu_count,
            "host_thread_budget": host_thread_budget,
            "minimum_process_thread_floors": {
                "encode_decoder_plus_encoder": ENCODE_THREAD_COMPONENTS,
                "ssim_two_decoders_plus_filter": SSIM_THREAD_COMPONENTS,
                "explicit_request_below_floor_is_raised_to_floor": True,
            },
            "aggregate_thread_budget": aggregate_thread_budget,
            "baseline_encode_threads": baseline_encode_threads,
            "baseline_exact_decode_threads": baseline_decode_threads,
            "baseline_ssim_threads": baseline_ssim_threads,
            "resource_normalized_encode_budget": True,
            "draft_stage_wall_s": round(draft_stage_wall, 6),
            "verify_stage_wall_s": round(verify_stage_wall, 6),
            "repair_stage_wall_s": round(repair_stage_wall, 6),
            "repair_verify_stage_wall_s": round(repair_verify_stage_wall, 6),
            "product_outer_wall_s": round(product_outer_wall, 6),
            "phase_accounted_wall_s": round(phase_accounted, 6),
            "overhead_wall_s": round(overhead_wall, 6),
            "note": (
                "bounded stage fan-out; results are reassembled in source order. Encode "
                "process budgets explicitly cover one input decoder plus one output encoder "
                "and collectively stay within the same aggregate thread envelope as the "
                "baseline encoder. SSIM fan-out is capped so "
                "each process splits its caller-capped budget across two decoders plus the "
                "filter without exceeding aggregate_thread_budget. "
                "Receipt phase costs are critical-path stage walls, not the sum of "
                "overlapping subprocess durations."
            ),
        },
    }
    receipt = build_receipt(
        results,
        baseline_wall_s=baseline_wall,
        segment_wall_s=segment_wall,
        concat_wall_s=concat_wall,
        exact=exact,
        repair_enabled=True,
        delivery_verified=final_delivery_verified,
        final_verify_wall_s=final_verify_wall,
        draft_encode_wall_s=draft_stage_wall,
        verify_wall_s=verify_stage_wall + repair_verify_stage_wall,
        repair_wall_s=repair_stage_wall,
        overhead_wall_s=overhead_wall,
        baseline_source="measured" if measure_baseline else "absent",
        details=details,
    )
    d = receipt.to_dict()
    assert_canonical(d)
    if not keep_artifacts:
        shutil.rmtree(seg_dir, ignore_errors=True)
        cleanup_files = [
            src, baseline_out, delivered,
            os.path.join(workdir, "concat_list.txt"),
        ]
        for f in cleanup_files:
            try:
                os.remove(f)
            except OSError:
                pass
    return d


def run_real(
    *,
    codec: str,
    mode: str,
    workdir: str,
    duration_s: float = 12.0,
    segment_time_s: float = 2.0,
    gate: float = DEFAULT_SSIM_GATE,
    size: str = "1280x720",
    fps: int = 30,
    noise: int = 8,
    keep_artifacts: bool = False,
    workers: int = DEFAULT_WORKERS,
    ffmpeg_threads: int | None = None,
    measure_baseline: bool = True,
) -> dict[str, Any]:
    """Exception-safe public wrapper around the real runner."""
    if not isinstance(keep_artifacts, bool):
        raise TypeError("keep_artifacts must be a bool")
    workdir = _canonical_workdir_path(workdir)
    workspace_existed = os.path.lexists(workdir)
    workspace_created, workspace_identity = _prepare_private_empty_workdir(workdir)
    try:
        return _run_real_impl(
            codec=codec,
            mode=mode,
            workdir=workdir,
            duration_s=duration_s,
            segment_time_s=segment_time_s,
            gate=gate,
            size=size,
            fps=fps,
            noise=noise,
            keep_artifacts=keep_artifacts,
            workers=workers,
            ffmpeg_threads=ffmpeg_threads,
            measure_baseline=measure_baseline,
        )
    except BaseException:
        if not keep_artifacts and _directory_identity_matches(
            workdir, workspace_identity
        ):
            if workspace_created and not workspace_existed:
                shutil.rmtree(workdir, ignore_errors=True)
            else:
                shutil.rmtree(os.path.join(workdir, "segs"), ignore_errors=True)
                for name in (
                    "source.mkv", "baseline.mkv", "delivered.mkv",
                    "candidate-rejected.mkv", ".delivered.candidate.mkv",
                    "concat_list.txt",
                ):
                    try:
                        os.remove(os.path.join(workdir, name))
                    except OSError:
                        pass
        raise


# --------------------------------------------------------------------------- #
# SYNTHETIC simulate mode — receipt-shape proof without ffmpeg (mirrors the render
# adapter's dry_run: every second is a fixture, clearly labeled, never a measurement)
# --------------------------------------------------------------------------- #
def simulate() -> dict[str, Any]:
    segs = [
        SegmentResult(f"seg_{i:03d}", draft_encode_s=0.5, verify_s=0.1, accepted=True,
                      draft_ssim=0.985, evidence=SYNTHETIC)
        for i in range(4)
    ]
    segs.append(SegmentResult(
        "seg_004", draft_encode_s=0.5, verify_s=0.1, accepted=False, draft_ssim=0.91,
        repair_encode_s=4.0, repaired_ssim=0.987, repair_verified=True,
        evidence=SYNTHETIC,
    ))
    receipt = build_receipt(
        segs, baseline_wall_s=25.0, segment_wall_s=0.2, concat_wall_s=0.1,
        exact=False, repair_enabled=True, delivery_verified=True, baseline_source="modeled",
        details={"note": "SYNTHETIC fixture seconds — shape proof only, not a measurement"},
    )
    d = receipt.to_dict()
    assert_canonical(d)
    return d


def _main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--codec", choices=sorted(CODECS), default="x264")
    ap.add_argument("--mode", choices=["ssim", "lossless"], default="ssim")
    ap.add_argument("--duration", type=float, default=12.0)
    ap.add_argument("--segment-time", type=float, default=2.0)
    ap.add_argument("--gate", type=float, default=DEFAULT_SSIM_GATE)
    ap.add_argument("--size", default="1280x720")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--noise", type=int, default=8)
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help=("bounded parallel segment workers "
                          f"(1-32; default {DEFAULT_WORKERS})"))
    ap.add_argument("--ffmpeg-threads", type=int, default=None,
                    help="aggregate per-phase FFmpeg thread budget override, capped to host "
                         "CPUs and divided across workers; irreducible floors are 2 for "
                         "encode (decoder+encoder) and 3 for SSIM (two decoders+filter)")
    ap.add_argument("--skip-baseline", action="store_true",
                    help="production mode: skip benchmark baseline; emit null speedup")
    ap.add_argument("--workdir", default=None,
                    help="scratch dir for artifacts (default: a fresh temp dir)")
    ap.add_argument("--ledger", default=None,
                    help="append the receipt as one JSONL line to this path")
    ap.add_argument("--keep-artifacts", action="store_true")
    ap.add_argument("--simulate", action="store_true",
                    help="SYNTHETIC shape proof only — no ffmpeg, no measurement")
    args = ap.parse_args()

    if args.simulate:
        print(json.dumps(simulate(), indent=2, sort_keys=True))
        return
    if not have_ffmpeg():
        print(json.dumps({"status": "PENDING-LOCAL-FFMPEG",
                          "note": "ffmpeg not on PATH; the real local measurement was NOT "
                                  "run — no number is invented in its place"}))
        sys.exit(2)
    import tempfile
    workdir = args.workdir or tempfile.mkdtemp(prefix="cx_transcode_spec_")
    receipt = run_real(
        codec=args.codec, mode=args.mode, workdir=workdir,
        duration_s=args.duration, segment_time_s=args.segment_time,
        gate=args.gate, size=args.size, fps=args.fps, noise=args.noise,
        keep_artifacts=args.keep_artifacts, workers=args.workers,
        ffmpeg_threads=args.ffmpeg_threads,
        measure_baseline=not args.skip_baseline,
    )
    print(json.dumps(receipt, indent=2, sort_keys=True))
    if args.ledger:
        with open(args.ledger, "a") as fh:
            fh.write(json.dumps(receipt, sort_keys=True) + "\n")
    if args.skip_baseline and not receipt["artifact_verified"]:
        # The receipt remains available for audit, but no below-contract file is
        # published as delivery and production callers receive a hard failure.
        sys.exit(3)


if __name__ == "__main__":
    _main()
