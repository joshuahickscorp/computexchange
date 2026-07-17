# ComputExchange — Condensation Status

_Black-hole condensation: the most verified, sellable compute delivered through the
smallest physical, operational, and cognitive implementation._

- **Base:** `origin/main` @ `96a4890` (local `main` d9bff10 is 6 commits behind — origin/main is authoritative)
- **Branch / worktree:** `condense/black-hole` in `computexchange-condense/` (isolated; the dirty `codex/spec-decode-inference` checkout with 139 WIP files is NOT touched)
- **No-touch boundary:** [`census/LIVE_BOUND_NO_TOUCH.json`](census/LIVE_BOUND_NO_TOUCH.json)
- **Headline measurement:** `make audit` → `cx audit codebase` (retires the old `make loc`)

## Phase A — census + no-touch boundary — DONE

Authoritative census, deterministic per commit, produced by the new Go subcommand
`cx audit codebase` (in `cli/`). Regenerate anytime with `make audit`.

### Where the mass is (tracked, owned, at `96a4890`)

| measure | LOC | vs target |
|---|--:|---|
| total tracked (text) | 445,524 | — |
| **kernel** (owned prod runtime) | **89,657** | target 20–25k → **~3.6× over** |
| active product | 135,937 | target 30–40k → ~3.4× over |
| active repository | 184,345 | target 50–65k → ~3× over |
| hydrated owned (excl vendored) | 360,362 | — |
| vendored (candle + three.js) | 85,162 | account separately |
| historical / pack-eligible | 181,504 | relocate to packs |
| **python** | **123,741** | target production=0 |

### Python reclamation ([`census/PYTHON_RECLAMATION.json`](census/PYTHON_RECLAMATION.json)) — 0 unknown

| plan | files | LOC | destination |
|---|--:|--:|---|
| archive_research | 118 | 53,478 | render-lab / docs-archive packs |
| retain_blender_pack | 46 | 38,024 | `computexchange-render-lab` (bpy only) |
| rewrite_rust | 57 | 23,580 | spec-engine (speculative core) |
| rewrite_go | 22 | 7,902 | `cx prove` / `cx audit` (evidence authority) |
| retain_sdk | 3 | 757 | public Python SDK (first-class, kept) |

**No production/proof/speculative Python remains after reclamation.** Only the
dependency-free SDK (757 LOC) and Blender `bpy` scripts (in a pack) survive.

### Biggest physical targets (safe, non-runtime)

- `scripts/spec-lab/` — 177 Python files, ~106k LOC of render/spec research → `render-lab` pack
- `render/` + `renderer/` — 402 files, 56.4 MB (PDF rack manuals, `.onnx`/`.pt` denoiser, `*@3x.png` iteration renders) → `render-lab` pack
- `docs/` archive-class — 114 files, 21.9 MB → `docs-archive` pack
- `web/assets/site/vendor/three.module.js` — 53k LOC vendored (account separately)

## Phase B — non-runtime physical reduction — IN PROGRESS

Safe reductions only (no hot-path changes; Go build stays green; live-bound hashes
unchanged). See [`CONDENSATION_LEDGER.jsonl`](CONDENSATION_LEDGER.jsonl) for per-commit metrics.

## Remaining (C–H)

- **C** Go proof/evidence authority: rewrite the 22 `rewrite_go` scripts into `cx prove` / `cx audit`; delete replaced Python.
- **D** Rust speculation authority: fold the 57 `rewrite_rust` spec-lab files into `spec-engine`; one receipt type; delete Python core.
- **E** Go product collapse: merge `cli/` + `control/` into one `cx` module/binary; one lifecycle engine; one worker supervisor.
- **F** Rust agent collapse: one Cargo workspace (`agent` + `spec-engine` + `token-spec-poc` merged); kill shadow impls.
- **G** interface/product split; thin SDK; pack Swift/render.
- **H** 25k-kernel attempt (only after E/F green).

## Commands

```
make audit          # regenerate census/ (cx audit codebase)
make build test     # Go + Rust build & test
make prove-local    # source-bound local proof matrix (needs docker/native pg+minio)
```

## Rollback

Every checkpoint is an independent commit on `condense/black-hole`. Base release =
`origin/main` @ `96a4890`. To restore: `git reset --hard 96a4890` in the condense worktree.
Nothing is merged to `main` until the previous release is restorable per the above.
