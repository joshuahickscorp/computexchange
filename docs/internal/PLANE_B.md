# Plane B — co-located Mac clusters as one high-memory worker

> **Design + seam only.** This is the architecture and the integration seam for
> Plane B. Real distributed execution across multiple physical Macs over
> Thunderbolt is **external / field work** — it cannot be built or proven on one
> machine. What *is* locally buildable is a single-host multi-process cluster
> simulator that exercises the control-plane seam; that boundary is drawn
> explicitly below. References the action plan's Plane B sections
> (`COMPUTE_EXCHANGE_HARDENED_ACTION_PLAN.md` §F decision log, §P, Phase 6 of §L,
> and the open-source map in §C).

## 0. Why Plane B exists, and why it is NOT Plane A

Plane A (today's marketplace) is **job-level parallelism**: a job splits into
independent tasks, each task runs whole on one worker, results merge. It is the
business. Plane B is the opposite axis — **model-level parallelism**: one model
too large for any single worker, sharded across several machines that must talk to
each other mid-forward-pass.

The action plan's decision log is unambiguous (§P): *"Plane A (job-level
parallelism) before Plane B (model-sharding) — Physics: WAN links cannot sustain
tensor-parallel all-to-all. Plane A is the business. Plane B is a later product."*
Tensor/pipeline parallelism exchanges activations on every layer; that traffic is
latency- and bandwidth-bound in a way home-internet RTTs (tens of ms, jittery)
physically cannot serve. So Plane B is **co-located only**: machines on the same
desk/rack, wired together, low single-digit-µs link latency. That single
constraint is what makes it tractable and what keeps internet-random sharding out
of V1 (see §4).

Plane B is Phase 6 in the roadmap (§L): "co-located Mac Studio cluster — Exo +
JACCL-powered large-model inference on owned/managed cluster, sold as a reserved
tier." It is gated on Plane A being profitable and on validated buyer demand for
large-model inference.

## 1. The new hardware class: `apple_silicon_cluster`

A co-located cluster registers as **one worker** of a new hardware class,
`apple_silicon_cluster`, whose advertised memory is the **sum** of its members'
unified memory. This is the whole trick: the rest of the system — the claim
filter, the matcher, verification, payout — treats the cluster as a single
high-memory worker and needs no model-sharding awareness.

Concretely, against the existing wire contract:

- **`hw_class`**: add `apple_silicon_cluster` to the closed set
  (`control/types.go` `validHWClasses`, `agent/src/types.rs` `HardwareClass`, and
  `proto/manifest.schema.json`). This is a horizon change — it must land in all
  three in lockstep.
- **`memory_gb`** (`WorkerCapability`): the **summed** usable unified memory of
  the members, minus a held-back margin for the OS + KV cache + activation buffers
  on each node. A 4× Mac Studio M3 Ultra (512 GB each) cluster advertises on the
  order of ~1.8–1.9 TB, not 2.048 TB — the margin is real and must not be faked.
- **`memory_bw_gbps`**: report the **bottleneck** bandwidth, which for a sharded
  forward pass is the *interconnect* (Thunderbolt), not each node's local
  ~819 GB/s. Reporting local bandwidth would mislead the scheduler's throughput
  scoring. The honest number is the link.
- **`supported_models`**: only the giant models the cluster is actually
  provisioned to shard (e.g. a 405B / 671B class), since those are the entire
  reason to pay the interconnect cost. Small models should never route here — they
  run faster whole on a single Plane-A worker.

The control plane already hard-filters on `min_memory_gb` and `hw_classes` in the
claim CTE (`control/scheduler.go` `ClaimTask`). A buyer who needs a model that
only fits in summed memory sets `constraints.min_memory_gb` above any single Mac's
capacity and (optionally) `hw_classes: ["apple_silicon_cluster"]`; the existing
filter then routes only to clusters. **No scheduler change is required for
routing** — the summed-memory abstraction does the work. That is the seam.

### What stays the same (the conserved horizon)
- The job/task split, the presigned S3 in/out, the commit, the verification
  comparison primitives (`resultsAgree`), the merge — all unchanged. The cluster
  commits a task result exactly like any worker.
- Within-class redundancy still applies: a cluster's output is compared only to
  another `apple_silicon_cluster`'s output (or re-run on the same cluster), never
  to a single-Mac result — cross-class nondeterminism would false-positive.

### What is genuinely new (inside the cluster, opaque to the control plane)
Everything sharding-related lives **inside** the cluster agent and is invisible to
the control plane: topology discovery, layer assignment, the activation exchange,
and fault handling. The control plane sees one register/poll/commit/heartbeat
worker.

## 2. Topology discovery over Thunderbolt

A cluster agent (a head node running the supplier binary, the others as workers)
must, before it advertises capacity, discover its own fabric:

1. **Enumerate links.** Walk the Thunderbolt/USB4 bus to find peer Macs and the
   point-to-point links between them (ring, mesh, or star depending on port
   count). Thunderbolt 5 gives ~80 Gbps symmetric (per the action plan's Exo/JACCL
   notes, §C: "RDMA over Thunderbolt 5, 50–60 Gbps").
2. **Measure, don't trust the spec.** Run a quick all-pairs bandwidth + latency
   probe. The advertised `memory_bw_gbps` is the *measured bottleneck* link, and a
   degraded link (a bad cable, a 40 Gbps TB4 hop in an otherwise TB5 ring) must
   lower the advertised number or pull the node, never be papered over.
3. **Assign shards to the measured topology.** Pipeline stages map to nodes so the
   heaviest activation hand-offs ride the fastest links; the slowest link caps the
   pipeline and therefore the throughput estimate.
4. **Re-probe on membership change.** A node dropping (sleep, unplug, thermal) is a
   topology event: the cluster either re-shards across the survivors (if summed
   memory still fits the model) or drops to `offline` and stops accepting work.
   Silently continuing with a missing shard is the one thing it must not do.

This mirrors exo-style topology discovery and Apple's JACCL (native distributed ML
over RDMA on Thunderbolt 5, macOS 26.2, M4 Pro/Max/M3 Ultra minimum — §C). Those
are the reference substrates; this project's contribution is the *seam*, not a new
collective-comms library.

## 3. Execution substrate (reference, not re-implemented)

Per §C/§F of the action plan, the realistic substrate options are:

- **MLX-distributed / JACCL** — Apple-native, RDMA over Thunderbolt 5, the
  game-changer but gated on high-end hardware + macOS 26.2.
- **Exo** (`exo-explore/exo`, Apache 2.0) — co-located distributed inference, the
  named **Plane B reference** (§C: "Plane B reference, not Plane A. Not
  production-grade for mission-critical").
- **JACCL** as the collective-comms layer once the fleet is on macOS 26.2 + TB5.

The design does **not** re-implement tensor/pipeline parallelism. The cluster
agent wraps one of these, presents a single `run(manifest, input)` to the rest of
the agent, and emits the same `JobOutput` (`runners.rs` `JobRunner`). A
`ClusterRunner` would sit alongside the existing runners with a `can_run` that
requires `hw_class == apple_silicon_cluster` and a model in the giant set.

## 4. Why internet-random sharding stays OUT of V1 (the physics)

Sharding a model across *random internet* nodes (the Petals model) is explicitly
rejected, and the reason is latency physics, not implementation difficulty:

- Tensor parallelism exchanges activations on **every layer**; pipeline
  parallelism passes a hidden-state tensor at **every stage boundary**. A
  70-layer model is dozens of synchronous round-trips per token.
- Home-internet RTT is tens of milliseconds, jittery, with asymmetric and
  capped uplinks. Multiply dozens of round-trips per token by that RTT and
  per-token latency collapses to unusable, *before* counting bandwidth for the
  activation tensors themselves.
- Co-location replaces tens-of-ms WAN hops with single-digit-µs Thunderbolt hops —
  a ~10,000× latency improvement on the exact operation that dominates. That is
  the entire reason Plane B is co-located-only and Plane A (independent whole
  tasks, no mid-computation chatter) is the one that works over the internet.

The action plan lists "model-sharding across random internet nodes (Plane B =
co-located only)" under **Explicit non-goals for V1** (§E) and "Live chatbot
streaming (latency physics)" beside it. Plane B does not relax this; it satisfies
the large-model use case *within* the co-located constraint.

## 5. What is LOCALLY SIMULATABLE vs genuinely EXTERNAL

This is the honest line (mirroring `RELEASE_CANDIDATE.md`'s discipline).

### Locally simulatable (one host, no special hardware) — buildable + provable
- **Multi-process single-host cluster sim.** Spawn N agent processes on one Mac,
  each pinned to a fraction of memory, talking over loopback/UDS to stand in for
  the Thunderbolt fabric. This exercises the *seam*: register as one
  `apple_silicon_cluster` worker with summed memory, claim a task through the
  existing filter, run a (small, stand-in) sharded forward pass across the
  processes, commit one result, get it verified and merged exactly like a
  Plane-A task.
- **Topology-discovery + re-shard logic** as a pure unit: feed it a synthetic
  link-graph (bandwidths/latencies), assert the shard assignment, summed-memory
  math, bottleneck-bandwidth selection, and the drop-a-node → re-shard-or-offline
  decision. No hardware needed.
- **Routing proof.** A job with `min_memory_gb` above any single worker's capacity
  must route to the cluster worker and *not* to any single Mac — provable against
  the existing `ClaimTask` filter with a seeded cluster row. This validates the
  "summed memory needs no scheduler change" claim.
- **The horizon change** (`apple_silicon_cluster` in all three contract files) and
  its schema/validation round-trip.

### Genuinely external / field (cannot be done on one machine) — design only
- **Real multi-Mac execution.** Two or more physical Macs, wired and sharding a
  genuinely oversized model. Needs the hardware.
- **Thunderbolt 5 fabric** at 50–80 Gbps and the measured all-pairs probe on it.
  Loopback is not a TB5 link; the bandwidth/latency numbers that gate the design
  only exist on real cable.
- **macOS 26.2 + JACCL** on M4 Pro/Max/M3 Ultra. Native RDMA collective comms is
  hardware- and OS-gated (§C).
- **Cross-cluster within-class redundancy at scale** — do two real clusters agree
  byte/cosine on the same giant-model task? This is the Plane-B analogue of the
  open cross-machine determinism question in `RELEASE_CANDIDATE.md`, and like it,
  it is unproven until run on real hardware.
- **Thermal + availability under sustained cluster load** over days.

## 6. Build order (when Plane A is profitable)

1. Land the `apple_silicon_cluster` horizon change (contract + validation) and the
   summed-memory routing **proof** via the single-host sim — this is the cheap,
   provable seam, and it unblocks everything else without any new hardware.
2. Build the topology-discovery + re-shard logic as testable pure units.
3. Wire a `ClusterRunner` around Exo (or MLX-distributed/JACCL) behind the same
   `JobRunner` interface, validated on the multi-process sim.
4. **External, field:** stand up a real co-located Mac Studio cluster on TB5 /
   macOS 26.2, run the cross-cluster determinism + thermal + availability
   experiments, and only then offer it as the reserved large-model tier.

Steps 1–3 are this codebase. Step 4 is the field — and this document is the design
that step 4 executes against, not a claim that step 4 is done.

## 7. Status — the buildable seam is LANDED (steps 1–3)

The cheap, provable, hardware-free seam now exists in the codebase. What is real:

- **Horizon (step 1).** `apple_silicon_cluster` is in the closed hw-class set across
  all three contract files in lockstep — `agent/src/types.rs` (`HardwareClass`),
  `control/types.go` (`validHWClasses`), `proto/manifest.schema.json` (`hw_class`) —
  with round-trip tests (`hardware_class_cluster_roundtrips`, `TestClusterHWClassValid`).
- **Summed-memory routing proof (step 1).** `TestClusterSummedMemoryRouting`
  (control integration matrix) seeds a cluster worker advertising ~1800 GB summed
  memory and proves a `min_memory_gb=200` job routes ONLY to it (never to a 64 GB
  single Mac), while a small job still routes to the single Mac — against the
  **unchanged** `ClaimTask` filter. The summed-memory abstraction needs no
  scheduler change, exactly as designed (§1).
- **Topology + re-shard logic (step 2).** `agent/src/cluster.rs` — pure, fully
  unit-tested: summed usable memory (minus real per-node margin), bottleneck-link
  bandwidth, contiguous proportional shard assignment, and the drop-a-node →
  re-shard-or-`Offline` decision (never a partial run). Runnable as a real operator
  diagnostic: `cx-agent cluster-plan --members-gb 512,512,512,512 --link-gbps 80
  --margin-gb 32 --model-layers 126 --model-gb 700`.
- **ClusterRunner seam (step 3).** `runners::ClusterRunner` accepts a giant model
  ONLY on an `apple_silicon_cluster` worker (`can_run` gated on class + the giant
  model set) and then **surfaces the external-substrate boundary**
  (`RunError::ExternalSubstrate`) instead of faking a distributed forward pass —
  the BLACKHOLE-honest equivalent of the payout stub.

One locally-buildable item from §5 is deliberately **not yet built**: the full
single-host **multi-process execution sim** (N processes running a small stand-in
sharded forward pass end-to-end and committing one verified result). The routing,
the advertisement, the shard plan, and the runner seam it would exercise are all
landed above; the remaining piece is the stand-in distributed *execution* harness,
which is the natural validation rig for wrapping a real substrate (step 3 proper).
It is left unbuilt rather than faked — a stand-in that ran one whole model and
called it "sharded" would violate the honesty rule.

What is genuinely **external / field** (unchanged from §5): real multi-Mac
execution over a Thunderbolt 5 fabric, the measured all-pairs probe, macOS 26.2 +
JACCL, cross-cluster determinism, and thermal/availability at scale. The
`ClusterRunner`'s `run()` is the seam those substrates plug into; this codebase
proves everything around them, not the distributed execution itself.
