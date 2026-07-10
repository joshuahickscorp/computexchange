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
import hashlib
import json
import os
import re
import shutil
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
    "branch_id",
    "modality",
    "draft_cost_s",
    "verify_cost_s",
    "repair_cost_s",
    "total_product_time_s",
    "baseline_total_time_s",
    "units",
    "accepted_fraction",
    "repaired_fraction",
    "exact",
    "speedup_vs_baseline",
)
CANONICAL_DEFAULTED_FIELDS = ("quality_tier", "evidence", "baseline_source", "details")

# The published render-lane 0.95 tier, reused as the default per-segment SSIM gate so the
# two media lanes gate on a comparable quality vocabulary.
DEFAULT_SSIM_GATE = 0.95


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
    draft_bytes: int = 0
    repair_bytes: int = 0
    evidence: str = MEASURED


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
    baseline_total_time_s: float  # ONE whole-video slow-preset encode (the honest denominator)
    baseline_source: str          # measured | modeled | absent
    exact: bool                   # True ONLY when decoded frames byte-match the source
    quality_tier: str             # fail | preview | delivery (receipt.rs enum)
    evidence: str                 # MEASURED | MODELED | SYNTHETIC (lower-cased on the wire)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def total_product_time_s(self) -> float:
        return self.draft_cost_s + self.verify_cost_s + self.repair_cost_s

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
        speedup = self.speedup_vs_baseline
        return {
            # ---- canonical receipt.rs spine (native names, no aliases) -------- #
            "branch_id": self.branch_id,
            "modality": self.modality,
            "draft_cost_s": round(self.draft_cost_s, 6),
            "verify_cost_s": round(self.verify_cost_s, 6),
            "repair_cost_s": round(self.repair_cost_s, 6),
            "total_product_time_s": round(self.total_product_time_s, 6),
            "baseline_total_time_s": round(self.baseline_total_time_s, 6),
            "baseline_source": self.baseline_source,
            "units": self.units,
            "accepted_fraction": round(self.accepted_fraction, 6),
            "repaired_fraction": round(self.repaired_fraction, 6),
            "exact": bool(self.exact),
            "quality_tier": self.quality_tier,
            "speedup_vs_baseline": round(speedup, 6) if speedup is not None else None,
            "evidence": self.evidence.lower(),
            # ---- transparency extras (serde ignores unknown keys) ------------- #
            "accepted_units": self.accepted_units,
            "repaired_units": self.repaired_units,
            "claim_scope": (
                "Measured single delivered-video ratio only; per-segment ratios are NOT "
                "multiplied. baseline_total_time_s is a real whole-video slow-preset encode "
                "of the SAME source. SSIM-gated delivery is NOT lossless (exact=false)."
            ),
            "details": self.details,
        }


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
    if duration_s <= 0 or segment_time_s <= 0:
        raise ValueError("duration_s and segment_time_s must be positive")
    return int(duration_s // segment_time_s) + (1 if duration_s % segment_time_s > 1e-9 else 0)


def build_receipt(
    segments: Iterable[SegmentResult],
    *,
    baseline_wall_s: float,
    segment_wall_s: float,
    concat_wall_s: float,
    exact: bool,
    repair_enabled: bool = True,
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
    seg_list = list(segments)
    if not seg_list:
        return TranscodeSpecReceipt(
            branch_id=branch_id, modality="transcode", units=0, accepted_units=0,
            repaired_units=0, draft_cost_s=0.0, verify_cost_s=0.0, repair_cost_s=0.0,
            baseline_total_time_s=baseline_wall_s, baseline_source=baseline_source,
            exact=False, quality_tier="fail", evidence=SYNTHETIC, details=details or {},
        )
    accepted = sum(1 for s in seg_list if s.accepted)
    rejected = [s for s in seg_list if not s.accepted]
    repaired = len(rejected) if repair_enabled else 0
    repair_cost = sum(s.repair_encode_s for s in rejected) if repair_enabled else 0.0
    draft_cost = segment_wall_s + sum(s.draft_encode_s for s in seg_list) + concat_wall_s
    verify_cost = sum(s.verify_s for s in seg_list)
    if repair_enabled or not rejected:
        tier = "delivery"
    else:
        tier = "preview"  # rejected drafts shipped as-is: below-gate content delivered
    d = dict(details or {})
    d.setdefault("cost_note", (
        "draft_cost_s = segmentation + per-segment fast-preset encodes + concat mux "
        f"(segment {segment_wall_s:.6f}s + drafts + concat {concat_wall_s:.6f}s); "
        "verify_cost_s is CHARGED (the source is the pipeline input, so the SSIM/MD5 gate "
        "is a real product step, unlike the render lane's measurement-only reference SSIM); "
        "total_product_time_s = draft+verify+repair covers the FULL delivered pipeline."
    ))
    d.setdefault("repair_note", (
        "repaired segments are re-encoded with the EXACT baseline recipe (same codec, CRF, "
        "preset), so delivered quality is gate-or-baseline everywhere; per-segment encoding "
        "lacks whole-video rate-control context (near-identical under CRF + keyframe-aligned "
        "segments, noted not hidden)."
        if repair_enabled else
        "repair DISABLED: rejected segments shipped as drafts — quality_tier is preview, "
        "never delivery."
    ))
    d.setdefault("per_segment", [
        {
            "seg_id": s.seg_id,
            "draft_ssim": s.draft_ssim,
            "accepted": s.accepted,
            "draft_encode_s": round(s.draft_encode_s, 6),
            "verify_s": round(s.verify_s, 6),
            "repair_encode_s": round(s.repair_encode_s, 6) if (not s.accepted and repair_enabled) else 0.0,
            "repaired_ssim": s.repaired_ssim,
        }
        for s in seg_list
    ])
    return TranscodeSpecReceipt(
        branch_id=branch_id,
        modality="transcode",
        units=len(seg_list),
        accepted_units=accepted,
        repaired_units=repaired,
        draft_cost_s=draft_cost,
        verify_cost_s=verify_cost,
        repair_cost_s=repair_cost,
        baseline_total_time_s=baseline_wall_s,
        baseline_source=baseline_source,
        exact=bool(exact),
        quality_tier=tier,
        evidence=_combine_evidence(s.evidence for s in seg_list),
        details=d,
    )


def assert_canonical(receipt_dict: dict[str, Any]) -> None:
    """Guard used by tests + the real runner: the emitted dict must satisfy the
    spec-engine/src/receipt.rs deserializer — required keys present with the right types,
    enum values inside the Rust enums' vocabularies. This is the Python-side mimic of
    `serde_json::from_str::<SpecReceipt>` strictness."""
    missing = [k for k in CANONICAL_REQUIRED_FIELDS if k not in receipt_dict]
    if missing:
        raise AssertionError(f"receipt missing canonical receipt.rs fields: {missing}")
    for k in ("draft_cost_s", "verify_cost_s", "repair_cost_s",
              "total_product_time_s", "baseline_total_time_s",
              "accepted_fraction", "repaired_fraction"):
        if not isinstance(receipt_dict[k], (int, float)) or isinstance(receipt_dict[k], bool):
            raise AssertionError(f"{k} must be a number, got {type(receipt_dict[k]).__name__}")
    if not isinstance(receipt_dict["units"], int) or receipt_dict["units"] < 0:
        raise AssertionError("units must be a non-negative integer (Rust u32)")
    if not isinstance(receipt_dict["exact"], bool):
        raise AssertionError("exact must be a bool")
    s = receipt_dict["speedup_vs_baseline"]
    if s is not None and (not isinstance(s, (int, float)) or isinstance(s, bool)):
        raise AssertionError("speedup_vs_baseline must be a number or null")
    if receipt_dict.get("quality_tier") not in QUALITY_TIERS:
        raise AssertionError(f"quality_tier must be one of {QUALITY_TIERS}")
    if receipt_dict.get("evidence") not in EVIDENCE_WIRE:
        raise AssertionError(f"evidence must be one of {EVIDENCE_WIRE}")
    if receipt_dict.get("baseline_source") not in BASELINE_SOURCES:
        raise AssertionError(f"baseline_source must be one of {BASELINE_SOURCES}")
    if not isinstance(receipt_dict.get("details"), dict):
        raise AssertionError("details must be an object")
    if not (0.0 <= receipt_dict["accepted_fraction"] <= 1.0):
        raise AssertionError("accepted_fraction must be in [0,1]")
    if not (0.0 <= receipt_dict["repaired_fraction"] <= 1.0):
        raise AssertionError("repaired_fraction must be in [0,1]")
    total = receipt_dict["total_product_time_s"]
    parts = (receipt_dict["draft_cost_s"] + receipt_dict["verify_cost_s"]
             + receipt_dict["repair_cost_s"])
    if abs(total - parts) > 1e-4:
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
    return shutil.which("ffmpeg") is not None


def ffmpeg_version() -> str:
    out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, check=True)
    return out.stdout.splitlines()[0].strip()


def _run(cmd: list[str]) -> tuple[subprocess.CompletedProcess, float]:
    """Run a command, return (proc, wall_s). Fail loud with stderr tail on error."""
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True)
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
        "slow": ["-c:v", "libx264", "-preset", "veryslow", "-crf", "23", "-pix_fmt", "yuv420p"],
        "fast": ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-pix_fmt", "yuv420p"],
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
        "slow": ["-c:v", "libx264", "-preset", "medium", "-qp", "0", "-pix_fmt", "yuv420p"],
        "fast": ["-c:v", "libx264", "-preset", "ultrafast", "-qp", "0", "-pix_fmt", "yuv420p"],
    },
}


def synthesize_source(path: str, *, duration_s: float, segment_time_s: float,
                      size: str = "1280x720", fps: int = 30, noise: int = 8) -> float:
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
    _, wall = _run([
        "ffmpeg", "-y", "-nostats",
        "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}",
        "-f", "lavfi", "-i", f"mandelbrot=size={size}:rate={fps}",
        "-filter_complex", fc, "-map", "[out]",
        "-c:v", "libx264", "-preset", "veryfast", "-qp", "0",
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
        "ffmpeg", "-y", "-nostats", "-i", source, "-map", "0:v:0", "-c", "copy",
        "-f", "segment", "-segment_time", f"{segment_time_s:g}",
        "-reset_timestamps", "1", pattern,
    ])
    segs = sorted(
        os.path.join(out_dir, f) for f in os.listdir(out_dir) if f.startswith("src_seg_")
    )
    if not segs:
        raise FfmpegError("segmentation produced no segments")
    return segs, wall


def encode(src: str, dst: str, codec_args: list[str]) -> float:
    _, wall = _run(["ffmpeg", "-y", "-nostats", "-i", src, *codec_args, dst])
    return wall


def ssim(distorted: str, reference: str) -> tuple[float, float]:
    """Global SSIM (All) of distorted vs reference. Returns (ssim, wall_s)."""
    proc, wall = _run([
        "ffmpeg", "-nostats", "-i", distorted, "-i", reference,
        "-lavfi", "[0:v][1:v]ssim", "-f", "null", "-",
    ])
    m = _SSIM_RE.search(proc.stderr or "")
    if not m:
        raise FfmpegError(f"could not parse SSIM from ffmpeg output for {distorted}")
    return float(m.group(1)), wall


def decoded_md5(path: str) -> tuple[str, float]:
    """MD5 of the DECODED yuv420p frames — the byte-check that proves losslessness."""
    proc, wall = _run([
        "ffmpeg", "-nostats", "-i", path, "-map", "0:v:0",
        "-pix_fmt", "yuv420p", "-f", "md5", "-",
    ])
    for line in (proc.stdout or "").splitlines():
        if line.startswith("MD5="):
            return line.strip(), wall
    raise FfmpegError(f"could not parse decoded MD5 for {path}")


def concat_segments(paths: list[str], dst: str, workdir: str) -> float:
    lst = os.path.join(workdir, "concat_list.txt")
    with open(lst, "w") as fh:
        for p in paths:
            fh.write(f"file '{os.path.abspath(p)}'\n")
    _, wall = _run(["ffmpeg", "-y", "-nostats", "-f", "concat", "-safe", "0",
                    "-i", lst, "-c", "copy", dst])
    return wall


def file_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_real(
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
) -> dict[str, Any]:
    """The REAL local measurement: synthesize content, run baseline (whole-video slow encode)
    and the speculative pipeline (segment -> fast draft -> verify -> repair -> concat), all
    wall-clock MEASURED, and emit the canonical receipt dict."""
    if codec not in CODECS:
        raise ValueError(f"codec must be one of {sorted(CODECS)}")
    if mode not in ("ssim", "lossless"):
        raise ValueError("mode must be 'ssim' or 'lossless'")
    cfg = CODECS[codec]
    os.makedirs(workdir, exist_ok=True)
    src = os.path.join(workdir, "source.mkv")
    synth_wall = synthesize_source(
        src, duration_s=duration_s, segment_time_s=segment_time_s,
        size=size, fps=fps, noise=noise,
    )

    # ---- BASELINE: one whole-video slow-preset encode (the honest denominator) ---- #
    baseline_out = os.path.join(workdir, f"baseline{cfg['ext']}")
    baseline_wall = encode(src, baseline_out, cfg["slow"])
    baseline_ssim, _ = ssim(baseline_out, src)  # measurement-only audit, NOT charged

    # ---- SPEC PIPELINE (every second below is charged) ---------------------------- #
    seg_dir = os.path.join(workdir, "segs")
    os.makedirs(seg_dir, exist_ok=True)
    src_segs, segment_wall = split_segments(src, seg_dir, segment_time_s)

    if mode == "lossless":
        src_md5s = {}  # verify prerequisite: source-segment decoded MD5s (charged to verify)
    results: list[SegmentResult] = []
    delivered_paths: list[str] = []
    for i, sseg in enumerate(src_segs):
        seg_id = f"seg_{i:03d}"
        draft_out = os.path.join(seg_dir, f"draft_{i:03d}{cfg['ext']}")
        draft_wall = encode(sseg, draft_out, cfg["fast"])
        if mode == "ssim":
            score, verify_wall = ssim(draft_out, sseg)
            accepted = score >= gate
        else:
            ref_md5, w1 = decoded_md5(sseg)
            got_md5, w2 = decoded_md5(draft_out)
            verify_wall = w1 + w2
            accepted = ref_md5 == got_md5
            score = 1.0 if accepted else 0.0
        repair_wall = 0.0
        repaired_ssim_val: float | None = None
        if accepted:
            delivered_paths.append(draft_out)
            repair_bytes = 0
        else:
            repair_out = os.path.join(seg_dir, f"repair_{i:03d}{cfg['ext']}")
            repair_wall = encode(sseg, repair_out, cfg["slow"])
            repaired_ssim_val, _ = ssim(repair_out, sseg)  # audit only, NOT charged
            delivered_paths.append(repair_out)
            repair_bytes = os.path.getsize(repair_out)
        results.append(SegmentResult(
            seg_id=seg_id,
            draft_encode_s=draft_wall,
            verify_s=verify_wall,
            accepted=accepted,
            draft_ssim=score,
            repair_encode_s=repair_wall,
            repaired_ssim=repaired_ssim_val,
            draft_bytes=os.path.getsize(draft_out),
            repair_bytes=repair_bytes,
            evidence=MEASURED,
        ))

    delivered = os.path.join(workdir, f"delivered{cfg['ext']}")
    concat_wall = concat_segments(delivered_paths, delivered, workdir)

    # ---- post-delivery audits (measurement-only, NEVER charged) ------------------- #
    delivered_ssim, _ = ssim(delivered, src)
    bitexact_vs_baseline = file_md5(delivered) == file_md5(baseline_out)  # expected False
    exact = False
    lossless_note = (
        "SSIM-gated delivery is NOT lossless: accepted segments are lossy fast-preset "
        "encodes that cleared the gate, exact=false by construction."
    )
    if mode == "lossless":
        src_full_md5, _ = decoded_md5(src)
        delivered_full_md5, _ = decoded_md5(delivered)
        exact = src_full_md5 == delivered_full_md5
        lossless_note = (
            f"lossless mode: decoded-frame MD5 of the delivered concat "
            f"{'MATCHES' if exact else 'DOES NOT MATCH'} the source ({src_full_md5})."
        )

    details: dict[str, Any] = {
        "run_label": "MEASURED/local",
        "host": "local (Apple Silicon, ffmpeg CPU encode)",
        "ffmpeg": ffmpeg_version(),
        "codec": codec,
        "mode": mode,
        "content": f"testsrc2+mandelbrot halves, noise=alls={noise}:allf=t, {size}@{fps}fps, "
                   f"{duration_s:g}s (synthesized locally, lossless x264 qp0 mezzanine; "
                   "synthesis is content creation, charged to NEITHER lane)",
        "source_synthesis_s_uncharged": round(synth_wall, 6),
        "segment_time_s": segment_time_s,
        "ssim_gate": gate if mode == "ssim" else None,
        "slow_args": " ".join(cfg["slow"]),
        "fast_args": " ".join(cfg["fast"]),
        "baseline_ssim_vs_source": round(baseline_ssim, 6),
        # Reference self-consistency (the render lane's hard-won lesson, applied here on
        # day one): the accept gate is only MEANINGFUL if the baseline recipe itself clears
        # it. When gate_achievable_by_baseline is False, rejecting a draft buys a repair
        # that may score WORSE than the draft (the repair is baseline-recipe by contract) —
        # the run is then structurally <=1x and self-prunes honestly.
        "gate_achievable_by_baseline": (baseline_ssim >= gate) if mode == "ssim" else None,
        "delivered_ssim_vs_source": round(delivered_ssim, 6),
        "delivered_bytes": os.path.getsize(delivered),
        "baseline_bytes": os.path.getsize(baseline_out),
        "bitexact_vs_baseline": bitexact_vs_baseline,
        "lossless_note": lossless_note,
        "audit_note": (
            "baseline_ssim/delivered_ssim/repaired_ssim and the byte-checks are post-delivery "
            "audits — measurement-only, never charged to total_product_time_s."
        ),
        "parallelism_note": (
            "segments are encoded SEQUENTIALLY — the wall-clock ratio contains no "
            "parallel-segment credit; segment-parallel fan-out is a real, unexercised lever."
        ),
    }
    receipt = build_receipt(
        results,
        baseline_wall_s=baseline_wall,
        segment_wall_s=segment_wall,
        concat_wall_s=concat_wall,
        exact=exact,
        repair_enabled=True,
        baseline_source="measured",
        details=details,
    )
    d = receipt.to_dict()
    assert_canonical(d)
    if not keep_artifacts:
        shutil.rmtree(seg_dir, ignore_errors=True)
        for f in (src, baseline_out, delivered, os.path.join(workdir, "concat_list.txt")):
            try:
                os.remove(f)
            except OSError:
                pass
    return d


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
        repair_encode_s=4.0, repaired_ssim=0.987, evidence=SYNTHETIC,
    ))
    receipt = build_receipt(
        segs, baseline_wall_s=25.0, segment_wall_s=0.2, concat_wall_s=0.1,
        exact=False, repair_enabled=True, baseline_source="modeled",
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
        keep_artifacts=args.keep_artifacts,
    )
    print(json.dumps(receipt, indent=2, sort_keys=True))
    if args.ledger:
        with open(args.ledger, "a") as fh:
            fh.write(json.dumps(receipt, sort_keys=True) + "\n")


if __name__ == "__main__":
    _main()
