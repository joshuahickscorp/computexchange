# CX adaptive resource agent

This lane makes the supplier agent fill an available host without crossing the
operator's resource limits. It is intentionally separate from speculative
decoding: it changes task triage and admission, not tokens, kernels, receipts, or
model output.

## Runtime contract

At startup CX derives two hard admission budgets:

- CPU units: `floor(logical CPUs × max_cpu_pct / 100)`, with a minimum of one.
- Memory units: allocatable RAM/VRAM after `memory_headroom_gb`, in 256 MiB units.

`max_cpu_pct` controls the host-local admission ledger. It does not currently
hard-throttle Candle's global worker threads or impose an OS-level CPU limit.
Runtime-specific thread-pool sizing remains a measured follow-up only if the
cross-device verification shows that admission control alone is insufficient.

Every dispatched task receives a resource plan before `start_task`:

- `parallel`: independent embedding, rerank, audio, or evaluation work can overlap.
- `stacked`: resident generative work shares the existing model runtime boundary.
- `exclusive`: custom, training, image, and render work reserves the full CPU budget.

The RAM estimate is the greater of the manifest's declared `min_memory_gb` and
the agent heuristic. Once a model has a real measured RSS residency entry, that
measurement replaces the cold-model heuristic. A warm model reserves only its
per-task working set because its weight residency is already present.

CPU and memory are acquired as weighted permits before the task is marked
running. The existing live pressure checks remain stricter: serious/critical
thermals, the memory percentage ceiling, operator headroom, and per-task fit can
still pause new claims. Active tasks keep checkpoint/preemption behavior.

`max_concurrent_tasks` is now a queue ceiling, not the resource model. When it is
omitted, CX derives a bounded greedy depth of `2 × CPU units`, capped at 64 and
also bounded by the memory-unit pool. This lets object I/O and cold loads overlap
without permitting an unbounded claimed-task backlog.

## Verification

Fast device-free gate:

```sh
cargo test --manifest-path agent/Cargo.toml --no-default-features resource_governor
```

The cross-device run must additionally prove on both Apple machines that:

1. the logged CPU budget reflects each machine and `max_cpu_pct`;
2. independent jobs overlap but never exceed weighted CPU/RAM reservations;
3. same-model generative jobs are tagged `stacked` and preserve output parity;
4. lowering headroom or inducing a serious thermal state stops new starts;
5. the original spec-decode checkout is byte-unchanged by this lane.
