<!-- CLAIM-SCOPE: internal-engineering-non-authoritative -->
# cx-renderer — Track 3, Phase 0

Rust-native, reuse-first renderer scaffold for ComputeExchange. Standalone crate
(not merged into `agent/`, which is inference job-execution — different concern).
Plan of record: `docs/research/ORIGINAL_ENGINE_THREE_TRACKS_2026-07-07.md` (Track 3).

## What's here

| Milestone | File | What it proves |
|---|---|---|
| **M0** CPU furnace | `src/furnace.rs`, `src/vec3.rs`, `src/rng.rs` | Zero-dependency unidirectional path tracer; correctness gated by the white furnace test. |
| **M0 gate** | `tests/furnace_test.rs` | 100%-albedo sphere in a uniform field renders invisible to within MC noise; a second test proves the metric detects energy loss. |
| **M1** GPU plumbing | `examples/wgpu_smoke.rs` | wgpu + WGSL compute round-trip (device→pipeline→buffers→dispatch→readback), Metal requested explicitly on Apple Silicon / Vulkan on RunPod. |
| **Phase 1 design** | `DECOUPLED_SHADING_NOTES.md` | Path-structure cache + GPU re-shade pass schedule for N material variants. |

## Run it

```sh
# M0 correctness gate, with the measured deviation printed:
cargo test --release -- --nocapture

# M1 GPU smoke test (auto-picks Metal on macOS, Vulkan elsewhere):
cargo run --example wgpu_smoke --features gpu
# force a backend:
CX_WGPU_BACKEND=vulkan cargo run --example wgpu_smoke --features gpu
```

The furnace core is zero-dependency and builds/tests with nothing fetched; wgpu
is pulled only behind `--features gpu`. The smoke test emits exactly one final
JSON line (`{"ok":true,...}` / `{"ok":false,"error":...}`) — the same
one-JSON-line honesty contract as the `pod/*.py` runners.
