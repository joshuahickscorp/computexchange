//! cluster.rs — Plane B topology + re-shard MATH, as pure, testable units.
//!
//! This is the locally-buildable, hardware-free core of docs/PLANE_B.md §2: given
//! a measured link graph of co-located Macs, compute the figures a cluster head
//! node advertises as ONE `apple_silicon_cluster` worker and the shard layout it
//! would run — plus the drop-a-node → re-shard-or-offline decision.
//!
//! It is deliberately pure: NO Thunderbolt, NO real collective-comms, NO model
//! execution. Real sharded inference rides an EXTERNAL substrate (Exo /
//! MLX-distributed / JACCL over Thunderbolt 5, macOS 26.2 — see PLANE_B.md §3,§5);
//! that is field work and is surfaced honestly by `runners::ClusterRunner`, never
//! faked here. What lives here is the seam math the action plan calls cheap and
//! provable without any new hardware (PLANE_B.md §5, §6 step 2).
//!
//! Honesty invariants (BLACKHOLE):
//! - Advertised summed memory is members' unified memory MINUS a real per-node
//!   margin (OS + KV cache + activation buffers); never the raw spec sum.
//! - Advertised bandwidth is the measured BOTTLENECK link (the interconnect),
//!   never a node's local memory bandwidth.
//! - A degraded link lowers the advertised bandwidth; a lost node either re-shards
//!   across survivors or goes offline — never silently runs with a missing shard.

/// One member Mac of a co-located cluster.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ClusterNode {
    /// Total unified memory of this Mac, GB (e.g. a Mac Studio M3 Ultra = 512).
    pub unified_memory_gb: f32,
}

/// A measured point-to-point link between two member nodes (indices into the
/// node list). `gbps` and `latency_us` come from a real all-pairs probe, not the
/// cable's spec — a bad cable or a slow TB4 hop shows up here.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Link {
    pub a: usize,
    pub b: usize,
    pub gbps: f32,
    pub latency_us: f32,
}

/// The discovered fabric: members + the measured links between them.
#[derive(Debug, Clone)]
pub struct ClusterTopology {
    pub nodes: Vec<ClusterNode>,
    pub links: Vec<Link>,
}

/// One pipeline stage: a contiguous range of model layers pinned to one node.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ShardAssignment {
    pub node: usize,
    pub first_layer: u32,
    pub layer_count: u32,
}

/// What a cluster head node advertises to the control plane as one worker. The
/// control plane treats this exactly like any worker (PLANE_B.md §1).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ClusterAdvert {
    /// Summed USABLE memory across members (after per-node margin), GB.
    pub memory_gb: f32,
    /// Bottleneck interconnect bandwidth (the slowest measured link), GB/s.
    pub memory_bw_gbps: f32,
    pub member_count: usize,
}

/// The decision after a membership change (a node sleeps / unplugs / overheats).
#[derive(Debug, Clone, PartialEq)]
pub enum ReshardDecision {
    /// Survivors still hold the model: re-shard across them.
    Reshard(Vec<ShardAssignment>),
    /// Survivors can't hold the model: stop accepting work (never run partial).
    Offline,
}

impl ClusterTopology {
    /// Summed USABLE memory: each member's unified memory minus `per_node_margin_gb`
    /// (held back for OS + KV cache + activation buffers), floored at 0 per node so
    /// a too-small member contributes nothing rather than going negative. This is
    /// the advertised `memory_gb` — the honest number, not the raw spec sum.
    pub fn summed_usable_memory_gb(&self, per_node_margin_gb: f32) -> f32 {
        let margin = per_node_margin_gb.max(0.0);
        self.nodes
            .iter()
            .map(|n| (n.unified_memory_gb - margin).max(0.0))
            .sum()
    }

    /// The BOTTLENECK link bandwidth (GB/s): the minimum over all measured links —
    /// for a sharded forward pass the interconnect, not local memory bandwidth, is
    /// the limit. Returns `None` when there is no fabric (0 or 1 node, or no links):
    /// a lone Mac is a normal Plane-A worker, not a cluster.
    pub fn bottleneck_bandwidth_gbps(&self) -> Option<f32> {
        self.links
            .iter()
            .map(|l| l.gbps)
            .fold(None, |acc, g| Some(acc.map_or(g, |m: f32| m.min(g))))
    }

    /// True iff the cluster (after margin) can hold a model of `model_memory_gb`.
    pub fn fits_model(&self, per_node_margin_gb: f32, model_memory_gb: f32) -> bool {
        self.summed_usable_memory_gb(per_node_margin_gb) >= model_memory_gb
    }

    /// Build the one-worker advertisement, or `None` when this is not a real
    /// cluster: fewer than 2 members, no measured fabric, or zero usable memory.
    /// `memory_bw_gbps` is the bottleneck link (never a node's local bandwidth).
    pub fn advertise(&self, per_node_margin_gb: f32) -> Option<ClusterAdvert> {
        if self.nodes.len() < 2 {
            return None; // a single Mac is a Plane-A worker, not a cluster
        }
        let bw = self.bottleneck_bandwidth_gbps()?; // no fabric → not a cluster
        let mem = self.summed_usable_memory_gb(per_node_margin_gb);
        if mem <= 0.0 {
            return None;
        }
        Some(ClusterAdvert {
            memory_gb: mem,
            memory_bw_gbps: bw,
            member_count: self.nodes.len(),
        })
    }

    /// Assign `model_layers` pipeline stages to the members, each a CONTIGUOUS
    /// range, proportional to each node's usable memory (a bigger Mac holds more
    /// layers — the heaviest hand-offs then ride between adjacent stages). Layers
    /// left by flooring are handed to the largest fractional remainders so the
    /// counts sum to exactly `model_layers`. Returns an empty plan when there are
    /// no layers or no usable-memory nodes.
    pub fn assign_shards(
        &self,
        model_layers: u32,
        per_node_margin_gb: f32,
    ) -> Vec<ShardAssignment> {
        if model_layers == 0 || self.nodes.is_empty() {
            return Vec::new();
        }
        let margin = per_node_margin_gb.max(0.0);
        let usable: Vec<f32> = self
            .nodes
            .iter()
            .map(|n| (n.unified_memory_gb - margin).max(0.0))
            .collect();
        let total: f32 = usable.iter().sum();
        if total <= 0.0 {
            return Vec::new();
        }

        // Proportional allotment with largest-remainder rounding (sums exactly).
        let mut counts: Vec<u32> = Vec::with_capacity(usable.len());
        let mut remainders: Vec<(usize, f32)> = Vec::with_capacity(usable.len());
        let mut assigned: u32 = 0;
        for (i, &u) in usable.iter().enumerate() {
            let ideal = model_layers as f32 * (u / total);
            let floor = ideal.floor();
            counts.push(floor as u32);
            assigned += floor as u32;
            remainders.push((i, ideal - floor));
        }
        // Distribute the leftover to the biggest remainders (a node with 0 usable
        // memory has remainder 0 and never gains a layer).
        let mut leftover = model_layers - assigned;
        remainders.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
        let mut ri = 0;
        while leftover > 0 && !remainders.is_empty() {
            let (idx, rem) = remainders[ri % remainders.len()];
            if usable[idx] > 0.0 || rem > 0.0 {
                counts[idx] += 1;
                leftover -= 1;
            }
            ri += 1;
            // Safety: if every remaining node has 0 usable memory, dump the rest on
            // the largest node so layers are never dropped.
            if ri > remainders.len() * 2 {
                if let Some((idx, _)) = remainders.first() {
                    counts[*idx] += leftover;
                }
                break;
            }
        }

        // Lay out contiguous ranges in node order; skip nodes that got 0 layers.
        let mut out = Vec::new();
        let mut cursor = 0u32;
        for (node, &c) in counts.iter().enumerate() {
            if c == 0 {
                continue;
            }
            out.push(ShardAssignment {
                node,
                first_layer: cursor,
                layer_count: c,
            });
            cursor += c;
        }
        out
    }

    /// Decide what to do after the surviving topology changes (a node left): if the
    /// survivors still hold `model_memory_gb`, re-shard across them; otherwise go
    /// `Offline`. NEVER returns a plan that leaves a layer unassigned — that is the
    /// one outcome PLANE_B.md §2.4 forbids (silently running with a missing shard).
    pub fn on_membership_change(
        &self,
        model_layers: u32,
        model_memory_gb: f32,
        per_node_margin_gb: f32,
    ) -> ReshardDecision {
        if !self.fits_model(per_node_margin_gb, model_memory_gb) {
            return ReshardDecision::Offline;
        }
        let plan = self.assign_shards(model_layers, per_node_margin_gb);
        // Defend the invariant: the plan must cover every layer with no gap.
        let covered: u32 = plan.iter().map(|s| s.layer_count).sum();
        if covered != model_layers {
            return ReshardDecision::Offline;
        }
        ReshardDecision::Reshard(plan)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn nodes(mems: &[f32]) -> Vec<ClusterNode> {
        mems.iter()
            .map(|&m| ClusterNode {
                unified_memory_gb: m,
            })
            .collect()
    }

    // A ring of equal links at `gbps`, plus one optionally-degraded link.
    fn ring(n: usize, gbps: f32) -> Vec<Link> {
        (0..n)
            .map(|i| Link {
                a: i,
                b: (i + 1) % n,
                gbps,
                latency_us: 3.0,
            })
            .collect()
    }

    #[test]
    fn summed_memory_subtracts_real_per_node_margin() {
        // 4× 512 GB Studios, 32 GB margin each → 4×480 = 1920 GB, NOT 2048.
        let topo = ClusterTopology {
            nodes: nodes(&[512.0, 512.0, 512.0, 512.0]),
            links: ring(4, 80.0),
        };
        assert!((topo.summed_usable_memory_gb(32.0) - 1920.0).abs() < 0.01);
        // A member smaller than the margin contributes 0, never negative.
        let small = ClusterTopology {
            nodes: nodes(&[512.0, 16.0]),
            links: ring(2, 80.0),
        };
        assert!((small.summed_usable_memory_gb(32.0) - 480.0).abs() < 0.01);
    }

    #[test]
    fn bottleneck_is_the_slowest_link_not_local_bandwidth() {
        // A degraded TB4 hop (40) in an otherwise TB5 (80) ring caps the cluster.
        let mut links = ring(3, 80.0);
        links[1].gbps = 40.0;
        let topo = ClusterTopology {
            nodes: nodes(&[512.0, 512.0, 512.0]),
            links,
        };
        assert_eq!(topo.bottleneck_bandwidth_gbps(), Some(40.0));
        // No fabric → no bottleneck (a lone Mac is not a cluster).
        let lone = ClusterTopology {
            nodes: nodes(&[512.0]),
            links: vec![],
        };
        assert_eq!(lone.bottleneck_bandwidth_gbps(), None);
    }

    #[test]
    fn advertise_requires_two_members_and_a_fabric() {
        let lone = ClusterTopology {
            nodes: nodes(&[512.0]),
            links: vec![],
        };
        assert_eq!(lone.advertise(32.0), None); // single Mac ⇒ Plane-A worker
        let nofabric = ClusterTopology {
            nodes: nodes(&[512.0, 512.0]),
            links: vec![],
        };
        assert_eq!(nofabric.advertise(32.0), None); // no measured links
        let cluster = ClusterTopology {
            nodes: nodes(&[512.0, 512.0]),
            links: ring(2, 80.0),
        };
        let a = cluster.advertise(32.0).unwrap();
        assert_eq!(a.member_count, 2);
        assert!((a.memory_gb - 960.0).abs() < 0.01); // 2×480
        assert_eq!(a.memory_bw_gbps, 80.0);
    }

    #[test]
    fn shards_are_contiguous_proportional_and_cover_every_layer() {
        // Unequal members: 512 + 256 + 256 (margin 0 for clean math) over 32 layers.
        let topo = ClusterTopology {
            nodes: nodes(&[512.0, 256.0, 256.0]),
            links: ring(3, 80.0),
        };
        let plan = topo.assign_shards(32, 0.0);
        // 50% / 25% / 25% → 16 / 8 / 8.
        assert_eq!(plan.len(), 3);
        assert_eq!(plan[0].layer_count, 16);
        assert_eq!(plan[1].layer_count, 8);
        assert_eq!(plan[2].layer_count, 8);
        // Contiguous, gapless, covering exactly [0,32).
        let mut next = 0u32;
        let mut covered = 0u32;
        for s in &plan {
            assert_eq!(s.first_layer, next);
            next += s.layer_count;
            covered += s.layer_count;
        }
        assert_eq!(covered, 32);
    }

    #[test]
    fn shard_remainders_sum_exactly() {
        // 3 equal nodes, 10 layers → 4/3/3 (largest-remainder), summing to 10.
        let topo = ClusterTopology {
            nodes: nodes(&[256.0, 256.0, 256.0]),
            links: ring(3, 80.0),
        };
        let plan = topo.assign_shards(10, 0.0);
        let total: u32 = plan.iter().map(|s| s.layer_count).sum();
        assert_eq!(total, 10);
        assert!(plan.iter().all(|s| s.layer_count >= 1));
    }

    #[test]
    fn dropped_node_reshards_when_survivors_still_fit() {
        // Model needs 700 GB. Start 3×512 (margin 32 → 1440 usable). Drop to 2×512
        // (960 usable ≥ 700) → re-shard across the two survivors, all layers covered.
        let survivors = ClusterTopology {
            nodes: nodes(&[512.0, 512.0]),
            links: ring(2, 80.0),
        };
        match survivors.on_membership_change(40, 700.0, 32.0) {
            ReshardDecision::Reshard(plan) => {
                assert_eq!(plan.iter().map(|s| s.layer_count).sum::<u32>(), 40);
            }
            ReshardDecision::Offline => panic!("survivors fit the model; should re-shard"),
        }
    }

    #[test]
    fn dropped_node_goes_offline_rather_than_run_partial() {
        // Same 700 GB model, but only ONE 512 survives (480 usable < 700) → Offline,
        // never a partial run with a missing shard (the forbidden outcome).
        let survivor = ClusterTopology {
            nodes: nodes(&[512.0]),
            links: vec![],
        };
        assert_eq!(
            survivor.on_membership_change(40, 700.0, 32.0),
            ReshardDecision::Offline
        );
    }
}
