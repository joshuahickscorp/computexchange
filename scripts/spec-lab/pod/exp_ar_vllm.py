#!/usr/bin/env python3
"""
exp_ar_vllm.py — Track A anchor: measure vLLM's NATIVE speculative decoding
speedup vs a no-spec baseline, on identical prompts, on the pod's GPU.

What it proves (the lossless anchor of the whole spec-lab ladder):
  Speculative decoding is LOSSLESS by construction — the target model verifies
  every drafted token, so greedy spec output must be byte-identical to greedy
  baseline output. We assert that here. If they diverge, that's a real finding
  (a vLLM bug or a non-greedy config leak), so we DON'T hide it: lossless=false
  plus a note, rather than a fabricated pass.

How it measures (honest, real numbers only):
  Build vLLM's offline LLM() twice on the SAME model —
    1. BASELINE: no speculative_config at all.
    2. SPEC:     speculative_config set for the requested method:
                   method="ngram"       → prompt-lookup n-gram drafter (no 2nd model)
                   method="draft_model" → a smaller draft model verifies against target
  Generate the SAME N distinct prompts greedily (temperature=0, seed=0, fixed
  max_tokens) both ways. Decode throughput = total_generated_tokens / decode_wall.
  speedup = spec_tok_s / base_tok_s. All wall-clock, all on-GPU, no modeling.

Acceptance: vLLM's V1 engine tracks accepted/drafted speculative tokens. We try
  to read them from llm.get_metrics() (and a couple of fallbacks) and report
  acceptance = accepted / drafted if exposed; otherwise we omit the key rather
  than guess.

vLLM 0.11 speculative_config is an evolving API. We DON'T hardcode assumptions:
  we pass the documented dict, and if a key is rejected by this exact vllm build
  we catch the real error and emit {"error": <the real message>} so the operator
  sees the true cause instead of a fabricated result.

Contract (spec-lab runner):
  invoked as:  python3 pod/exp_ar_vllm.py '<json params>'
  params (argv[1], a JSON object), examples:
    {"method":"ngram","model":"Qwen/Qwen2.5-1.5B-Instruct",
     "num_spec_tokens":5,"prompts":64,"max_tokens":128}
    {"method":"draft_model","model":"Qwen/Qwen2.5-1.5B-Instruct",
     "draft":"Qwen/Qwen2.5-0.5B-Instruct","num_spec_tokens":5,...}
  output: human logs to STDERR; the LAST stdout line is exactly ONE JSON object.
  metrics: {"speedup","acceptance"(opt),"lossless","base_tok_s","spec_tok_s",
            "method","note", and passthrough "num_spec_tokens"/"n_prompts"}.
  any failure (OOM, download error, bad API key) → last stdout line {"error":...}.
"""

import json
import os
import subprocess
import sys
import time
import traceback

# Deterministic + quiet-ish. HF_HOME / HF_HUB_ENABLE_HF_TRANSFER are set by the
# orchestrator's ssh env; we set safe fallbacks so a manual run still behaves.
os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("VLLM_LOGGING_LEVEL", "WARNING")
# vLLM's V1 engine is required for native spec-decode metrics; it's the default
# on 0.11, but pin it explicitly so a stray env var can't silently drop us to V0.
os.environ.setdefault("VLLM_USE_V1", "1")

SEED = 0


def log(*a):
    """Human logs go to STDERR — STDOUT is reserved for the single metrics line."""
    print(*a, file=sys.stderr, flush=True)


def emit(obj):
    """Print the ONE-and-only metrics JSON as the last stdout line, then return."""
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


# --------------------------------------------------------------------------- #
# Prompts: N distinct, deterministic, decode-heavy prompts. We want each to
# actually generate ~max_tokens of continuation so the decode phase dominates
# (spec-decode only helps the decode phase, not prefill). Instruct-style "keep
# writing" prompts give long, self-consistent greedy continuations.
# --------------------------------------------------------------------------- #
_TOPICS = [
    "the history of the printing press", "how photosynthesis works",
    "the rules of chess", "the water cycle", "how a CPU executes instructions",
    "the causes of the French Revolution", "how vaccines train the immune system",
    "the theory of plate tectonics", "how compilers turn code into machine code",
    "the life cycle of a star", "how the internet routes packets",
    "the basics of double-entry bookkeeping", "how neurons transmit signals",
    "the geography of the Amazon basin", "how refrigeration works",
    "the structure of the United Nations", "how bees communicate direction",
    "the development of written language", "how a jet engine produces thrust",
    "the process of natural selection", "how encryption keeps data private",
    "the formation of the Grand Canyon", "how a violin produces sound",
    "the branches of the Roman government", "how photolithography makes chips",
    "the migration patterns of monarch butterflies", "how tides are caused",
    "the invention of the steam engine", "how DNA replication works",
    "the layers of the Earth's atmosphere",
]


def build_prompts(n):
    """N *distinct*, deterministic instruction prompts that produce long greedy text.

    Uniqueness matters: the lossless check compares outputs per-prompt, and equal
    prompts would silently collapse in the by-prompt re-keying. We guarantee every
    prompt is distinct by appending a unique, index-derived tag (deterministic, no
    randomness) so N can exceed len(_TOPICS) without collisions.
    """
    depths = ["briefly", "in detail", "step by step", "for a beginner"]
    prompts = []
    for i in range(n):
        topic = _TOPICS[i % len(_TOPICS)]
        depth = depths[i % len(depths)]
        # #i tag makes prompt i strictly distinct from every other, even once the
        # (topic, depth) pairs start to repeat past len(_TOPICS)*len(depths).
        prompts.append(
            f"[#{i}] Explain {topic} {depth}. Write a clear, complete, "
            f"multi-paragraph explanation and do not stop early."
        )
    return prompts


# --------------------------------------------------------------------------- #
# Acceptance metrics. vLLM's V1 engine records speculative accepted/drafted
# token counters. The exact accessor has shifted across 0.1x releases, so we try
# a few known shapes and give up gracefully (omit the key) rather than fabricate.
# --------------------------------------------------------------------------- #
def read_acceptance(llm):
    """Best-effort accepted/drafted → acceptance in [0,1]. Return None if unexposed."""
    # Path 1: the public LLM.get_metrics() (vLLM >= ~0.9 V1) returns a list of
    # metric objects; spec-decode ones carry accepted/draft counts.
    try:
        metrics = llm.get_metrics()
        accepted = drafted = None
        for m in metrics:
            name = getattr(m, "name", "") or ""
            val = getattr(m, "value", None)
            if val is None:
                # Some builds expose Counter.value under .count/.sum
                val = getattr(m, "count", None) or getattr(m, "sum", None)
            if val is None:
                continue
            if "spec_decode_num_accepted_tokens" in name:
                accepted = (accepted or 0) + float(val)
            elif "spec_decode_num_draft_tokens" in name:
                drafted = (drafted or 0) + float(val)
        if accepted is not None and drafted and drafted > 0:
            return max(0.0, min(1.0, accepted / drafted))
    except Exception as e:
        log(f"[acceptance] get_metrics() path unavailable: {e}")

    # Path 2: dig into the engine's stat loggers for the same counters.
    try:
        engine = getattr(llm, "llm_engine", None)
        loggers = getattr(engine, "stat_loggers", None)
        if loggers:
            # stat_loggers can be a dict or a list depending on version.
            candidates = loggers.values() if hasattr(loggers, "values") else loggers
            for logger in candidates:
                spec = getattr(logger, "spec_decode_metrics", None) or \
                       getattr(logger, "last_spec_decode_metrics", None)
                if spec is not None:
                    acc = getattr(spec, "draft_acceptance_rate", None)
                    if acc is not None:
                        return max(0.0, min(1.0, float(acc)))
                    a = getattr(spec, "accepted_tokens", None)
                    d = getattr(spec, "draft_tokens", None)
                    if a is not None and d:
                        return max(0.0, min(1.0, float(a) / float(d)))
    except Exception as e:
        log(f"[acceptance] stat_loggers path unavailable: {e}")

    return None


# --------------------------------------------------------------------------- #
# Generation. One LLM instance, greedy, timed. We time the whole generate() call
# (prefill+decode); because every runner does the SAME prompts+max_tokens, the
# prefill component is identical between baseline and spec, so the RATIO cleanly
# reflects the decode-phase speedup that spec-decode delivers.
# --------------------------------------------------------------------------- #
def run_generation(llm, prompts, max_tokens):
    """Greedy generate; return (list_of_texts, total_generated_tokens, wall_seconds)."""
    from vllm import SamplingParams

    sp = SamplingParams(
        temperature=0.0,   # greedy — required for the lossless comparison
        top_p=1.0,
        top_k=-1,
        seed=SEED,
        max_tokens=max_tokens,
        ignore_eos=False,  # let it stop naturally; identical stop point both ways
    )
    t0 = time.perf_counter()
    outputs = llm.generate(prompts, sp)
    wall = time.perf_counter() - t0

    # vLLM may reorder outputs internally; each RequestOutput carries its prompt,
    # so re-key by prompt to guarantee we compare like-for-like across the two runs.
    texts_by_prompt = {}
    total_tokens = 0
    for o in outputs:
        comp = o.outputs[0]
        texts_by_prompt[o.prompt] = comp.text
        total_tokens += len(comp.token_ids)
    ordered_texts = [texts_by_prompt.get(p, "") for p in prompts]
    return ordered_texts, total_tokens, wall


def build_llm(model, speculative_config=None):
    """Construct an offline vLLM engine. speculative_config=None ⇒ plain baseline."""
    from vllm import LLM

    kwargs = dict(
        model=model,
        seed=SEED,
        trust_remote_code=True,
        gpu_memory_utilization=0.80,
        max_model_len=2048,        # plenty for our short prompts + max_tokens
        # enforce_eager=True disables CUDA-graph capture. Spec-decode + CUDA graphs
        # is the classic cause of "EngineCore initialization failed" on many GPUs
        # (the graph capture chokes on the drafter path). Eager is a hair slower but
        # makes BOTH builds start reliably — and since baseline AND spec use the same
        # flag, the measured speedup stays a fair comparison.
        enforce_eager=True,
        disable_log_stats=False,   # keep stat loggers alive so acceptance is readable
    )
    if speculative_config is not None:
        kwargs["speculative_config"] = speculative_config
    return LLM(**kwargs)


def make_speculative_config(params):
    """Translate our params into vLLM 0.11's speculative_config dict.

    We build the documented shape. If THIS vllm build rejects a key, the caller's
    try/except surfaces the real error message (we don't silently pretend it worked).
    """
    method = params["method"]
    n_spec = int(params.get("num_spec_tokens", 5))

    if method == "ngram":
        # Prompt-lookup n-gram drafter: no second model, drafts by matching the
        # recent context against earlier text. Cheap, strong on repetitive output.
        return {
            "method": "ngram",
            "num_speculative_tokens": n_spec,
            # Window of n-gram sizes vLLM tries when looking up a draft. Sensible
            # defaults; kept explicit so behavior is deterministic across builds.
            "prompt_lookup_max": 4,
            "prompt_lookup_min": 2,
        }

    if method == "suffix":
        return {
            "method": "suffix",
            "num_speculative_tokens": n_spec,
            "suffix_decoding_max_tree_depth": int(params.get("suffix_decoding_max_tree_depth", 24)),
            "suffix_decoding_max_cached_requests": int(params.get("suffix_decoding_max_cached_requests", 10000)),
            "suffix_decoding_max_spec_factor": float(params.get("suffix_decoding_max_spec_factor", 1.0)),
            "suffix_decoding_min_token_prob": float(params.get("suffix_decoding_min_token_prob", 0.1)),
        }

    if method == "draft_model":
        draft = params.get("draft")
        if not draft:
            raise ValueError("method=draft_model requires a 'draft' model id")
        # A smaller model proposes tokens; the target verifies. vLLM keys the
        # draft under 'model' inside speculative_config on 0.11.
        return {
            "model": draft,
            "num_speculative_tokens": n_spec,
        }

    raise ValueError(f"unknown method '{method}' (expected 'ngram', 'suffix', or 'draft_model')")


def free_llm(llm):
    """Release GPU memory between the two engine builds so we don't OOM on build #2."""
    try:
        import gc
        import torch
        from vllm.distributed.parallel_state import (
            destroy_model_parallel, destroy_distributed_environment,
        )
        try:
            destroy_model_parallel()
            destroy_distributed_environment()
        except Exception:
            pass
        del llm
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.synchronize()
    except Exception as e:
            log(f"[cleanup] partial free (non-fatal): {e}")


def run_child_phase(params):
    """Run one vLLM engine in a fresh Python process.

    vLLM teardown can leave CUDA/distributed state behind inside a process even
    after best-effort cleanup. The real receipt we need is baseline vs spec on the
    same params, not a test of whether two offline engines can be constructed
    sequentially in one interpreter. Each phase therefore gets its own process.
    """
    phase = params.get("_phase")
    if phase not in ("baseline", "spec"):
        emit({"error": f"bad child phase: {phase!r}"})
        return
    method = params.get("method", "ngram")
    model = params.get("model", "Qwen/Qwen2.5-1.5B-Instruct")
    n_prompts = int(params.get("prompts", 64))
    max_tokens = int(params.get("max_tokens", 128))
    if n_prompts > 128:
        n_prompts = 128
    prompts = build_prompts(n_prompts)

    llm = None
    try:
        spec_cfg = None
        if phase == "spec":
            spec_cfg = make_speculative_config(params)
            log(f"[child:{phase}] speculative_config = {spec_cfg}")
        log(f"[child:{phase}] building vLLM engine…")
        llm = build_llm(model, speculative_config=spec_cfg)
        log(f"[child:{phase}] generating…")
        texts, tokens, wall = run_generation(llm, prompts, max_tokens)
        acceptance = read_acceptance(llm) if phase == "spec" else None
        emit({
            "phase": phase,
            "method": method,
            "texts": texts,
            "tokens": tokens,
            "wall_s": wall,
            "tok_s": tokens / wall if wall > 0 else 0.0,
            "acceptance": acceptance,
        })
    except Exception as e:
        tb = traceback.format_exc()
        log(tb)
        emit({
            "phase": phase,
            "method": method,
            "error": f"{phase} failed: {type(e).__name__}: {e}",
            "traceback_tail": tb[-4000:],
        })
    finally:
        if llm is not None:
            free_llm(llm)


def parse_last_json(stdout):
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            return json.loads(line)
    raise ValueError("child emitted no JSON metrics line")


def run_phase_subprocess(phase, params):
    child_params = dict(params)
    child_params["_phase"] = phase
    cmd = [sys.executable, os.path.abspath(__file__), json.dumps(child_params)]
    timeout_s = int(params.get("phase_timeout_s", 1800))
    log(f"[{phase}] launching isolated child process…")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    if proc.stderr:
        log(proc.stderr)
    try:
        metrics = parse_last_json(proc.stdout)
    except Exception as e:
        return {
            "error": f"{phase} child metrics parse failed: {type(e).__name__}: {e}",
            "stdout_tail": proc.stdout[-1000:],
            "stderr_tail": proc.stderr[-4000:],
            "returncode": proc.returncode,
        }
    if "error" in metrics:
        metrics.setdefault("stderr_tail", proc.stderr[-4000:])
        metrics.setdefault("returncode", proc.returncode)
    elif proc.returncode != 0:
        metrics["error"] = f"{phase} child exited rc={proc.returncode}"
        metrics["stderr_tail"] = proc.stderr[-2000:]
        metrics["returncode"] = proc.returncode
    return metrics


def main():
    # ---- parse params (argv[1] is a JSON object) --------------------------- #
    if len(sys.argv) < 2:
        emit({"error": "missing params argv[1] (expected a JSON object)"})
        return
    try:
        params = json.loads(sys.argv[1])
    except Exception as e:
        emit({"error": f"bad params json: {e}"})
        return
    if "_phase" in params:
        run_child_phase(params)
        return

    method = params.get("method", "ngram")
    model = params.get("model", "Qwen/Qwen2.5-1.5B-Instruct")
    n_prompts = int(params.get("prompts", 64))
    max_tokens = int(params.get("max_tokens", 128))
    n_spec = int(params.get("num_spec_tokens", 5))

    # Internal time-bound: cap total prompts so we stay well under ~20 min even
    # on a slower card. 64 short prompts × 128 tokens is a few minutes on a 4090;
    # we hard-cap at 128 prompts and sample down if the caller asks for more.
    if n_prompts > 128:
        log(f"[bound] clamping prompts {n_prompts} → 128 (time budget)")
        n_prompts = 128

    prompts = build_prompts(n_prompts)
    log(f"[cfg] method={method} model={model} draft={params.get('draft')} "
        f"n_spec={n_spec} prompts={n_prompts} max_tokens={max_tokens}")

    # ---- 1) BASELINE: no speculative config -------------------------------- #
    base = run_phase_subprocess("baseline", params)
    if "error" in base:
        emit(base)
        return
    base_texts = base["texts"]
    base_tokens = int(base["tokens"])
    base_wall = float(base["wall_s"])

    base_tok_s = base_tokens / base_wall if base_wall > 0 else 0.0
    log(f"[baseline] {base_tokens} tok in {base_wall:.2f}s → {base_tok_s:.1f} tok/s")
    if base_tokens == 0 or base_tok_s == 0.0:
        emit({"error": "baseline generated 0 tokens (nothing to compare)"})
        return

    # ---- 2) SPEC: same model + speculative_config -------------------------- #
    spec = run_phase_subprocess("spec", params)
    if "error" in spec:
        emit(spec)
        return
    spec_texts = spec["texts"]
    spec_tokens = int(spec["tokens"])
    spec_wall = float(spec["wall_s"])
    acceptance = spec.get("acceptance")

    spec_tok_s = spec_tokens / spec_wall if spec_wall > 0 else 0.0
    log(f"[spec] {spec_tokens} tok in {spec_wall:.2f}s → {spec_tok_s:.1f} tok/s")
    if spec_tok_s == 0.0:
        emit({"error": "spec generated 0 tokens (nothing to compare)"})
        return

    # ---- 3) LOSSLESSNESS: greedy spec MUST equal greedy baseline ----------- #
    # Compare per-prompt. Spec-decode verifies every token against the target, so
    # with temperature=0 both paths must produce byte-identical continuations.
    mismatches = 0
    first_bad = None
    for i, (b, s) in enumerate(zip(base_texts, spec_texts)):
        if b != s:
            mismatches += 1
            if first_bad is None:
                first_bad = i
    lossless = (mismatches == 0)

    speedup = spec_tok_s / base_tok_s

    # ---- note: exactly what was measured vs what (if anything) is off ------ #
    note_parts = [
        f"real on-GPU wall-clock; {n_prompts} identical greedy prompts, "
        f"max_tokens={max_tokens}; both engines same model={model}.",
    ]
    if method == "draft_model":
        note_parts.append(f"draft={params.get('draft')}.")
    if acceptance is None:
        note_parts.append("acceptance metric not exposed by this vLLM build (omitted, not guessed).")
    if not lossless:
        note_parts.append(
            f"LOSSLESS VIOLATION: {mismatches}/{n_prompts} prompts differ "
            f"(first at idx {first_bad}) — greedy spec should be byte-identical; "
            f"this is a real anomaly, reported not hidden."
        )
    note = " ".join(note_parts)

    metrics = {
        "speedup": round(speedup, 4),
        "lossless": lossless,
        "base_tok_s": round(base_tok_s, 2),
        "spec_tok_s": round(spec_tok_s, 2),
        "method": method,
        "num_spec_tokens": n_spec,
        "n_prompts": n_prompts,
        "note": note,
    }
    if acceptance is not None:
        metrics["acceptance"] = round(acceptance, 4)

    log(f"[result] speedup={speedup:.3f}x lossless={lossless} "
        f"acceptance={acceptance if acceptance is not None else 'n/a'}")
    emit(metrics)


if __name__ == "__main__":
    # Top-level guard: ANY unhandled path must still emit exactly one final JSON
    # line so the orchestrator never sees a crash without a metrics tail.
    try:
        main()
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001 — last-resort honesty net
        log(traceback.format_exc())
        emit({"error": f"unhandled: {type(e).__name__}: {e}"})
