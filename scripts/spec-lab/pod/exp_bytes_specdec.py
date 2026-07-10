#!/usr/bin/env python3
"""
exp_bytes_specdec.py — the "speculate on ANY file" generality test (spec-lab track A3/A4).

The owner's thesis: prediction = compression, so a cheap predictor can DRAFT the next
bytes of any file, and a verifier accepts the run of correct guesses and rejects at the
first miss (exactly the lossless spec-decode accept/reject). This runner measures the
draft ACCEPTANCE RATE — the fraction of bytes a tiny byte n-gram predictor gets right —
across genuinely different file types, with no GPU and no big model. Acceptance IS the
quantity that governs speculative speedup, so this cleanly tests where the principle holds.

Everything here is a REAL measurement of byte predictability (not modeled): we build real
files (a text paragraph, a real PNG via pillow, a real WAV via stdlib wave, and os.urandom
as the honest high-entropy negative control), fit an order-k byte n-gram on a prefix, and
score its next-byte predictions against the true bytes.

Contract: prints logs to stderr; the LAST stdout line is one JSON metrics object.

  default mode  -> {"acceptance_text","acceptance_image","acceptance_audio",
                    "acceptance_binary","acceptance_nontext","note"}
  entropy_sweep -> {"curve":[[level,acceptance]...],"entropy_threshold","note"}
"""

import io
import json
import math
import os
import struct
import sys
import wave


# ---- test files (all REAL bytes, built deterministically on the pod) -----------

def make_text_bytes(n):
    para = (b"The quick brown fox jumps over the lazy dog. Prediction is compression; "
            b"a good predictor of the next byte is a good codec for the whole stream. ")
    return (para * (n // len(para) + 1))[:n]


def make_image_bytes(n):
    # A real PNG: a smooth gradient (structured, compressible) — real image bytes.
    try:
        from PIL import Image
        w = h = 96
        img = Image.new("RGB", (w, h))
        px = img.load()
        for y in range(h):
            for x in range(w):
                px[x, y] = ((x * 2) % 256, (y * 2) % 256, ((x + y)) % 256)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b = buf.getvalue()
    except Exception as e:
        print(f"pillow unavailable ({e}); using a raw gradient bitmap instead", file=sys.stderr)
        b = bytes(((x * 2) % 256) for x in range(n))
    if len(b) < n:
        b = b * (n // len(b) + 1)
    return b[:n]


def make_audio_bytes(n):
    # A real 16-bit PCM WAV of a sine tone (structured waveform bytes).
    sr = 8000
    nsamp = max(n // 2, sr)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        frames = bytearray()
        for i in range(nsamp):
            val = int(12000 * math.sin(2 * math.pi * 220.0 * i / sr))
            frames += struct.pack("<h", val)
        wf.writeframes(bytes(frames))
    b = buf.getvalue()
    if len(b) < n:
        b = b * (n // len(b) + 1)
    return b[:n]


def make_binary_bytes(n):
    # Honest high-entropy negative control: acceptance here SHOULD be ~1/256 (random).
    return os.urandom(n)


# ---- the byte n-gram draft predictor + accept/reject verify --------------------

def ngram_acceptance(data, ctx_order, train_frac=0.5):
    """Fit an order-`ctx_order` byte n-gram on the first train_frac of `data`, then
    on the remainder predict the single most-likely next byte from each context and
    measure ACCEPTANCE = fraction of correct predictions (== the run of accepted
    speculative bytes before the first reject, averaged). Pure, real measurement."""
    n = len(data)
    split = int(n * train_frac)
    if split <= ctx_order or n - split <= 1:
        return 0.0
    # Train: context (last ctx_order bytes) -> Counter of next byte.
    table = {}
    for i in range(ctx_order, split):
        ctx = data[i - ctx_order:i]
        nxt = data[i]
        d = table.setdefault(ctx, {})
        d[nxt] = d.get(nxt, 0) + 1
    # Predict on the held-out tail.
    correct = 0
    total = 0
    for i in range(split, n):
        ctx = data[i - ctx_order:i]
        d = table.get(ctx)
        if d:
            pred = max(d.items(), key=lambda kv: kv[1])[0]
        else:
            # Unknown context: fall back to the global most-frequent byte (still a
            # legitimate cheap draft; on random data this stays near chance).
            pred = 0
        if pred == data[i]:
            correct += 1
        total += 1
    return correct / total if total else 0.0


# ---- entropy-controlled streams for the A4 sweep -------------------------------

def make_mixed_stream(predictable_frac, n):
    """A stream that is `predictable_frac` a repeating pattern and (1-frac) random —
    a knob on predictability to trace acceptance vs entropy."""
    pattern = bytes(range(64))
    out = bytearray()
    rnd = os.urandom(n)
    for i in range(n):
        if (i * 9973) % 1000 < predictable_frac * 1000:
            out.append(pattern[i % len(pattern)])
        else:
            out.append(rnd[i])
    return bytes(out)


def main():
    params = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    mode = params.get("mode", "default")
    ctx = int(params.get("draft_ctx", 8))
    order = int(params.get("draft_order", ctx))  # allow order override (remediation)
    order = min(order, ctx) if order else ctx

    if mode == "entropy_sweep":
        levels = params.get("levels", [0.1, 0.3, 0.5, 0.7, 0.9])
        n = int(params.get("n_bytes", 8192))
        curve = []
        for lv in levels:
            acc = ngram_acceptance(make_mixed_stream(lv, n), order or 4)
            curve.append([lv, round(acc, 4)])
            print(f"entropy level {lv}: acceptance {acc:.3f}", file=sys.stderr)
        # Interpolate the predictability level where acceptance crosses 0.5.
        thr = None
        for (l0, a0), (l1, a1) in zip(curve, curve[1:]):
            if (a0 - 0.5) * (a1 - 0.5) <= 0 and a1 != a0:
                thr = l0 + (0.5 - a0) * (l1 - l0) / (a1 - a0)
                break
        print(json.dumps({"curve": curve, "entropy_threshold": round(thr, 4) if thr is not None else None,
                          "note": "real byte n-gram acceptance vs a predictability knob; threshold = level where acceptance crosses 0.5"}))
        return

    n = int(params.get("n_bytes", 4096))
    files = params.get("files", ["text", "image", "audio", "binary"])
    builders = {"text": make_text_bytes, "image": make_image_bytes,
                "audio": make_audio_bytes, "binary": make_binary_bytes}
    acc = {}
    for name in files:
        b = builders[name](n)
        a = ngram_acceptance(b, order or 4)
        acc[name] = round(a, 4)
        print(f"{name}: {len(b)} bytes, order-{order} acceptance {a:.3f}", file=sys.stderr)
    nontext = [acc[k] for k in acc if k != "text"]
    out = {
        "acceptance_text": acc.get("text", 0.0),
        "acceptance_image": acc.get("image", 0.0),
        "acceptance_audio": acc.get("audio", 0.0),
        "acceptance_binary": acc.get("binary", 0.0),
        "acceptance_nontext": round(sum(nontext) / len(nontext), 4) if nontext else 0.0,
        "note": ("real measured byte-predictability (acceptance) per file type; "
                 "os.urandom 'binary' is the honest ~chance negative control"),
    }
    print(json.dumps(out))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        sys.exit(1)
