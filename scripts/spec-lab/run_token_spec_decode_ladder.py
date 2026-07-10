#!/usr/bin/env python3
"""Local token speculative-decode protocol ladder.

This is the cheap, local receipt path for the token branch of the broader
speculative rendering/decode plan. It is deliberately NOT an LLM throughput
claim. It measures the exact same accept/reject/repair control flow on token
streams with a deterministic target verifier and a cheap n-gram draft predictor.

The useful output is branch shape:

    structured streams -> grow when acceptance and verifier-call reduction pay
    high-entropy streams -> prune when draft overhead beats acceptance

Model-backed receipts can come from a CX-native backend, or from vLLM/Hawking
when we are mining/comparing useful pieces. The protocol and branch receipts do
not depend on those libraries.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
DATE = "2026-07-09"

TOKEN_LEDGER = REPO / "docs/speed-lane-reports/spec-lab/token_spec_decode_ledger.jsonl"
BRANCH_LEDGER = REPO / "docs/speed-lane-reports/spec-lab/spec_render_token_branch_ledger.jsonl"
REPORT = REPO / f"docs/speed-lane-reports/spec-lab/SPEC_RENDER_AND_TOKEN_DECODE_ITERATION_{DATE}.md"
BRANCH_REPORT = REPO / f"docs/speed-lane-reports/spec-lab/SPEC_RENDER_TOKEN_DECODE_BRANCH_LEDGER_{DATE}.md"

FRIENDLY_RENDER_FLOOR_X = 14.3372
HARD_RENDER_FLOOR_X = 7.8721
TILE_REFINE_PREVIEW_X = 3.4897


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({"ts": now(), **record}, sort_keys=True) + "\n")


def burn_work(iterations: int, seed: int) -> int:
    """Small deterministic CPU loop used to model target-verifier call cost.

    A model-backed verifier pays substantial per-forward overhead. This local
    protocol runner has no model, so the caller can add a deterministic cost per
    target call to expose the same branch/prune economics. The report labels this
    as simulated verifier cost; it is never mixed with vLLM tok/s.
    """
    x = (seed ^ 0x9E3779B9) & 0xFFFFFFFF
    for _ in range(iterations):
        x = (x * 1664525 + 1013904223) & 0xFFFFFFFF
    return x


def text_to_tokens(text: str) -> list[int]:
    # Byte tokens keep this dependency-free and make arbitrary structured text
    # streams comparable with the byte-speculation branch.
    return list(text.encode("utf-8"))


def repeat_stream(n: int) -> list[int]:
    pattern = [11, 17, 23, 31, 23, 17, 11, 5]
    return [pattern[i % len(pattern)] for i in range(n)]


def json_stream(n: int) -> list[int]:
    rows = []
    for i in range(max(1, n // 48 + 8)):
        status = "ok" if i % 5 else "retry"
        rows.append(f'{{"id":{i:06d},"status":"{status}","value":{(i * 17) % 997}}}\n')
    toks = text_to_tokens("".join(rows))
    return (toks * (n // len(toks) + 1))[:n]


def code_stream(n: int) -> list[int]:
    blocks = []
    for i in range(max(1, n // 96 + 8)):
        name = f"stage_{i % 9}"
        blocks.append(
            "def {name}(x):\n"
            "    y = (x * 1664525 + 1013904223) & 0xffffffff\n"
            "    return y ^ {mask}\n\n".format(name=name, mask=(i * 8191) & 0xFFFF)
        )
    toks = text_to_tokens("".join(blocks))
    return (toks * (n // len(toks) + 1))[:n]


def prose_stream(n: int) -> list[int]:
    base = (
        "Speculative decoding is draft, verify, accept, repair. "
        "Rendering can use the same control loop over pixels, tiles, and samples. "
        "Tokens are only one branch of the broader protocol. "
    )
    variants = []
    for i in range(max(1, n // len(base) + 8)):
        variants.append(base + f"Branch {i % 7} grows when evidence improves.\n")
    toks = text_to_tokens("".join(variants))
    return (toks * (n // len(toks) + 1))[:n]


def random_stream(n: int) -> list[int]:
    rng = random.Random(20260709)
    return [rng.randrange(0, 8192) for _ in range(n)]


SCENARIOS = {
    "repeat": repeat_stream,
    "json": json_stream,
    "code": code_stream,
    "prose": prose_stream,
    "random": random_stream,
}


class NGramDraft:
    """Online prompt-lookup/ngram draft predictor over already-verified tokens."""

    def __init__(self, ctx: int):
        self.ctx = max(1, ctx)
        self.history: list[int] = []
        self.table: dict[tuple[int, ...], Counter[int]] = defaultdict(Counter)
        self.global_counts: Counter[int] = Counter()

    def observe(self, token: int) -> None:
        if len(self.history) >= self.ctx:
            key = tuple(self.history[-self.ctx :])
            self.table[key][token] += 1
        self.global_counts[token] += 1
        self.history.append(token)

    def prime(self, tokens: Iterable[int]) -> None:
        for tok in tokens:
            self.observe(tok)

    def predict_one_from(self, ctx_tokens: list[int]) -> int:
        if len(ctx_tokens) >= self.ctx:
            key = tuple(ctx_tokens[-self.ctx :])
            counts = self.table.get(key)
            if counts:
                return counts.most_common(1)[0][0]
        if self.global_counts:
            return self.global_counts.most_common(1)[0][0]
        return 0

    def predict_many(self, n: int) -> list[int]:
        # Only the last `ctx` verified tokens are needed to choose the next
        # prediction. Copying the full history makes the high-entropy reject path
        # artificially O(n^2), which hides the actual branch/prune signal.
        ctx_tokens = list(self.history[-self.ctx :])
        out: list[int] = []
        for _ in range(n):
            tok = self.predict_one_from(ctx_tokens)
            out.append(tok)
            ctx_tokens.append(tok)
        return out


@dataclass(frozen=True)
class Target:
    tokens: list[int]
    verifier_work: int

    def next_token(self, pos: int) -> int:
        burn_work(self.verifier_work, pos)
        return self.tokens[pos]

    def span(self, pos: int, n: int) -> list[int]:
        burn_work(self.verifier_work, pos * 1315423911 + n)
        return self.tokens[pos : pos + n]


def run_baseline(tokens: list[int], prefix_len: int, verifier_work: int) -> dict:
    target = Target(tokens, verifier_work)
    out: list[int] = []
    start = time.perf_counter()
    for pos in range(prefix_len, len(tokens)):
        out.append(target.next_token(pos))
    wall = time.perf_counter() - start
    n = len(out)
    return {
        "output": out,
        "wall_s": wall,
        "tokens_s": n / wall if wall > 0 else 0.0,
        "target_calls": n,
    }


def run_speculative(tokens: list[int], prefix_len: int, ctx: int, num_spec_tokens: int, verifier_work: int) -> dict:
    target = Target(tokens, verifier_work)
    draft = NGramDraft(ctx)
    draft.prime(tokens[:prefix_len])

    out: list[int] = []
    pos = prefix_len
    target_calls = 0
    draft_tokens = 0
    accepted_tokens = 0
    reject_events = 0

    start = time.perf_counter()
    while pos < len(tokens):
        k = min(num_spec_tokens, len(tokens) - pos)
        proposal = draft.predict_many(k)
        truth = target.span(pos, k)
        target_calls += 1
        draft_tokens += len(proposal)

        accepted = 0
        for cand, real in zip(proposal, truth):
            if cand != real:
                break
            accepted += 1

        if accepted == k:
            emitted = truth
        else:
            reject_events += 1
            emitted = truth[: accepted + 1]

        for tok in emitted:
            draft.observe(tok)
        out.extend(emitted)
        accepted_tokens += accepted
        pos += len(emitted)

    wall = time.perf_counter() - start
    n = len(out)
    return {
        "output": out,
        "wall_s": wall,
        "tokens_s": n / wall if wall > 0 else 0.0,
        "target_calls": target_calls,
        "draft_tokens": draft_tokens,
        "accepted_tokens": accepted_tokens,
        "acceptance": accepted_tokens / draft_tokens if draft_tokens else 0.0,
        "reject_events": reject_events,
    }


def branch_action(lossless: bool, speedup: float, acceptance: float) -> str:
    if not lossless:
        return "kill_correctness"
    if speedup >= 1.20 and acceptance >= 0.35:
        return "grow"
    if speedup < 1.0 or acceptance < 0.08:
        return "prune"
    return "park"


def scenario_row(scenario: str, num_spec_tokens: int, n_tokens: int, prefix_frac: float, ctx: int, verifier_work: int) -> dict:
    stream = SCENARIOS[scenario](n_tokens)
    prefix_len = max(ctx + 2, int(len(stream) * prefix_frac))
    prefix_len = min(prefix_len, len(stream) - 1)

    base = run_baseline(stream, prefix_len, verifier_work)
    spec = run_speculative(stream, prefix_len, ctx, num_spec_tokens, verifier_work)
    lossless = spec["output"] == base["output"]
    base_tokens_s = base["tokens_s"]
    spec_tokens_s = spec["tokens_s"]
    speedup = spec_tokens_s / base_tokens_s if base_tokens_s > 0 else 0.0
    reduction = base["target_calls"] / spec["target_calls"] if spec["target_calls"] else 0.0
    action = branch_action(lossless, speedup, spec["acceptance"])
    return {
        "backend": "local_ngram_token_protocol",
        "claim_scope": "local protocol receipt; not model-backed LLM throughput",
        "protocol": "draft->batched_verify->accept_prefix_or_repair_one->continue",
        "scenario": scenario,
        "n_tokens": len(stream),
        "prefix_tokens": prefix_len,
        "generated_tokens": len(stream) - prefix_len,
        "ctx": ctx,
        "num_spec_tokens": num_spec_tokens,
        "verifier_work": verifier_work,
        "lossless": lossless,
        "baseline_wall_s": round(base["wall_s"], 6),
        "spec_wall_s": round(spec["wall_s"], 6),
        "baseline_tokens_s": round(base_tokens_s, 2),
        "spec_tokens_s": round(spec_tokens_s, 2),
        "speedup_x": round(speedup, 4),
        "acceptance": round(spec["acceptance"], 4),
        "target_calls_baseline": base["target_calls"],
        "target_calls_spec": spec["target_calls"],
        "target_call_reduction_x": round(reduction, 4),
        "draft_tokens": spec["draft_tokens"],
        "accepted_tokens": spec["accepted_tokens"],
        "reject_events": spec["reject_events"],
        "branch_action": action,
    }


def summarize(rows: list[dict]) -> dict:
    valid = [r for r in rows if r.get("lossless")]
    grows = [r for r in valid if r.get("branch_action") == "grow"]
    prunes = [r for r in rows if r.get("branch_action") == "prune"]
    best = max(valid, key=lambda r: r.get("speedup_x", 0.0), default=None)
    by_scenario: dict[str, dict] = {}
    for scenario in sorted({r["scenario"] for r in rows}):
        rs = [r for r in rows if r["scenario"] == scenario and r.get("lossless")]
        if not rs:
            continue
        b = max(rs, key=lambda r: r.get("speedup_x", 0.0))
        by_scenario[scenario] = {
            "best_speedup_x": b["speedup_x"],
            "best_num_spec_tokens": b["num_spec_tokens"],
            "acceptance": b["acceptance"],
            "branch_action": b["branch_action"],
        }
    return {
        "rows": len(rows),
        "lossless_rows": len(valid),
        "grow_rows": len(grows),
        "prune_rows": len(prunes),
        "best": best,
        "by_scenario": by_scenario,
    }


def render_markdown(rows: list[dict], summary: dict) -> str:
    best = summary.get("best") or {}
    grow_rows = [r for r in rows if r.get("branch_action") == "grow"]
    prune_rows = [r for r in rows if r.get("branch_action") == "prune"]

    lines = [
        f"# Spec Render + Token Decode Iteration - {DATE}",
        "",
        "## Scope",
        "",
        "This report keeps the two speculation branches separate:",
        "",
        "- pixel/render speculation: quality-gated draft/verify/refine over render outputs;",
        "- token speculation: exact draft/verify/repair over token streams.",
        "",
        "The token rows below are local protocol receipts, not vLLM/Hawking model throughput claims.",
        "They exist to prove the branch/prune loop and receipt schema before paid GPU speculative decode runs.",
        "",
        "## Current Renderer Receipts",
        "",
        f"- Render-only friendly floor: `{FRIENDLY_RENDER_FLOOR_X}x`.",
        f"- Render-only hard-scene delivery floor: `{HARD_RENDER_FLOOR_X}x`.",
        f"- Batch crop tile-refine scaffold: `{TILE_REFINE_PREVIEW_X}x` preview, not a hard-scene winner.",
        "",
        "No renderer speedup in this report is multiplied by token speculative decode. Multipliers are only comparable",
        "after an end-to-end workload uses both branches in the same delivered path.",
        "",
        "## Local Token Spec Decode Receipt",
        "",
        f"- Backend: `{best.get('backend', 'local_ngram_token_protocol')}`.",
        "- Claim scope: local protocol receipt; not model-backed LLM throughput.",
        f"- Rows: `{summary['rows']}`; lossless rows: `{summary['lossless_rows']}`.",
        f"- Grow rows: `{summary['grow_rows']}`; prune rows: `{summary['prune_rows']}`.",
    ]
    if best:
        lines.extend(
            [
                f"- Best local protocol speedup: `{best['speedup_x']}x` on `{best['scenario']}`",
                f"  with `num_spec_tokens={best['num_spec_tokens']}`, acceptance `{best['acceptance']}`,",
                f"  target-call reduction `{best['target_call_reduction_x']}x`.",
            ]
        )
    lines.extend(["", "## Scenario Bests", ""])
    lines.append("| Scenario | Best speedup | k | Acceptance | Action |")
    lines.append("| --- | ---: | ---: | ---: | --- |")
    for scenario, data in summary["by_scenario"].items():
        lines.append(
            f"| `{scenario}` | `{data['best_speedup_x']}x` | `{data['best_num_spec_tokens']}` | "
            f"`{data['acceptance']}` | `{data['branch_action']}` |"
        )
    lines.extend(["", "## Branch Decisions", ""])
    if grow_rows:
        top = sorted(grow_rows, key=lambda r: r["speedup_x"], reverse=True)[:5]
        lines.append("Grow:")
        for r in top:
            lines.append(
                f"- `{r['scenario']}` k=`{r['num_spec_tokens']}`: `{r['speedup_x']}x`, "
                f"acceptance `{r['acceptance']}`, lossless `{r['lossless']}`."
            )
    else:
        lines.append("Grow: none yet.")
    lines.append("")
    if prune_rows:
        lines.append("Prune:")
        for r in sorted(prune_rows, key=lambda r: r["speedup_x"])[:5]:
            lines.append(
                f"- `{r['scenario']}` k=`{r['num_spec_tokens']}`: `{r['speedup_x']}x`, "
                f"acceptance `{r['acceptance']}`."
            )
    else:
        lines.append("Prune: none yet.")
    lines.extend(
        [
            "",
            "## Next Loop",
            "",
            "1. Run the byte/protocol smoke locally and attach it to the branch ledger.",
            "2. Grow the CX-native SpecUnit/DraftProducer/Verifier/RepairPolicy branch before re-entering library probes.",
            "3. Treat vLLM/Hawking as references to mine or compare, not as the architecture center.",
            "4. Re-enter renderer work through warm/resident tile speculation, not the cold batch-crop branch.",
            "",
            "## Continuation Goal Prompt",
            "",
            "```text",
            "/goal Continue docs/research/SPEC_RENDER_AND_TOKEN_SPEC_DECODE_DEEP_PLAN_2026-07-09.md.",
            f"Use the new receipts in {REPORT} and {BRANCH_REPORT}. Keep speculative rendering and",
            "token speculative decode as separate branches until the same end-to-end workload truly uses both.",
            "Renderer floors remain friendly 14.3372x and hard-scene 7.8721x; tile-refine is a 3.4897x",
            "preview scaffold. The local token protocol branch now has measured lossless accept/reject rows;",
            "grow structured-token branches, prune high-entropy branches, and keep building the CX-native",
            "speculative receipt path. Use vLLM/Hawking only when they provide a measured piece worth mining.",
            "Do not stop merely because one branch improves; keep iterating until remaining branches are pruned by",
            "measured acceptance, exactness, wall-clock, or reproducibility evidence.",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def render_branch_markdown(rows: list[dict], summary: dict) -> str:
    lines = [
        f"# Spec Render / Token Branch Ledger - {DATE}",
        "",
        "| Branch | Status | Receipt | Next action |",
        "| --- | --- | --- | --- |",
        f"| renderer_base | floor | friendly `{FRIENDLY_RENDER_FLOOR_X}x`, hard `{HARD_RENDER_FLOOR_X}x` | grow only branches that beat hard-scene delivery |",
        f"| render_tile_refine | park | `{TILE_REFINE_PREVIEW_X}x` preview | revisit only warm/resident, not cold crop fanout |",
    ]
    for scenario, data in summary["by_scenario"].items():
        lines.append(
            f"| token_local_{scenario} | {data['branch_action']} | best `{data['best_speedup_x']}x`, "
            f"acceptance `{data['acceptance']}` | "
            f"{'increase k / move to model-backed verifier' if data['branch_action'] == 'grow' else 'prune or keep as negative control'} |"
        )
    lines.extend(
        [
            "",
            "Receipts:",
            "",
            f"- Token JSONL: `{TOKEN_LEDGER}`",
            f"- Branch JSONL: `{BRANCH_LEDGER}`",
            f"- Iteration report: `{REPORT}`",
            "",
            "Guardrail: no token row here is a vLLM/Hawking LLM throughput claim.",
            "",
        ]
    )
    return "\n".join(lines)


def parse_csv_ints(value: str) -> list[int]:
    out = []
    for part in value.split(","):
        part = part.strip()
        if part:
            out.append(int(part))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scenarios", default="repeat,json,code,prose,random")
    ap.add_argument("--num-spec-tokens", default="4,8,16")
    ap.add_argument("--tokens", type=int, default=8192)
    ap.add_argument("--prefix-frac", type=float, default=0.25)
    ap.add_argument("--ctx", type=int, default=8)
    ap.add_argument("--verifier-work", type=int, default=1024)
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    unknown = [s for s in scenarios if s not in SCENARIOS]
    if unknown:
        raise SystemExit(f"unknown scenarios: {unknown}; known={sorted(SCENARIOS)}")
    ks = parse_csv_ints(args.num_spec_tokens)
    if not ks:
        raise SystemExit("--num-spec-tokens produced no values")

    rows: list[dict] = []
    for scenario in scenarios:
        for k in ks:
            row = scenario_row(
                scenario=scenario,
                num_spec_tokens=k,
                n_tokens=args.tokens,
                prefix_frac=args.prefix_frac,
                ctx=args.ctx,
                verifier_work=args.verifier_work,
            )
            rows.append(row)
            print(
                f"[token-spec] {scenario} k={k}: {row['speedup_x']}x "
                f"accept={row['acceptance']} action={row['branch_action']} "
                f"lossless={row['lossless']}"
            )

    summary = summarize(rows)
    result = {
        "ok": all(r["lossless"] for r in rows),
        "summary": summary,
        "rows": rows,
        "reports": {
            "token_ledger": str(TOKEN_LEDGER),
            "branch_ledger": str(BRANCH_LEDGER),
            "report": str(REPORT),
            "branch_report": str(BRANCH_REPORT),
        },
    }

    if not args.no_write:
        for row in rows:
            append_jsonl(TOKEN_LEDGER, {"event": "token_spec_decode_row", "row": row})
            append_jsonl(
                BRANCH_LEDGER,
                {
                    "event": "branch_receipt",
                    "branch": f"token_local_{row['scenario']}",
                    "status": row["branch_action"],
                    "speedup_x": row["speedup_x"],
                    "acceptance": row["acceptance"],
                    "lossless": row["lossless"],
                    "row": row,
                },
            )
        append_jsonl(TOKEN_LEDGER, {"event": "result", "result": result})
        append_jsonl(
            BRANCH_LEDGER,
            {
                "event": "branch_summary",
                "renderer_friendly_floor_x": FRIENDLY_RENDER_FLOOR_X,
                "renderer_hard_floor_x": HARD_RENDER_FLOOR_X,
                "tile_refine_preview_x": TILE_REFINE_PREVIEW_X,
                "token_summary": summary,
            },
        )
        REPORT.write_text(render_markdown(rows, summary))
        BRANCH_REPORT.write_text(render_branch_markdown(rows, summary))

    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
