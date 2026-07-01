//! continuous_batch.rs — Apple-Silicon continuous-batch lane SKELETON, ported from
//! the founder's Hawking engine (PERF_AND_CAPABILITY_AUDIT Wave 2; docs/HAWKING_PORT_PLAN.md).
//!
//! Hawking's `hawking-serve/src/batch/{scheduler,driver}.rs` interleaves prefill and
//! decode across slots so concurrent requests share one model forward pass per step —
//! measured 5.0x aggregate vs single-stream at B=8 on an M3 Pro. This module defines
//! the SHAPE of that lane behind the `hawking` engine tag (config::InferenceBackend::
//! Hawking). It is a compiling skeleton, INERT BY DEFAULT: `Scheduler::decode_plan`
//! returns the slots a step would batch, but nothing wires it to a GPU kernel yet, so
//! the agent keeps using the existing per-task batched decode (LlamaBackend::
//! generate_batch). Behavior today is therefore UNCHANGED.
//!
//! Why a skeleton and not a wired lane: the Hawking port is a ~4-6 week, Apple-hardware
//! -gated build (it needs a multi-seq slot-strided KV kernel on Metal + a cross-worker
//! determinism re-gate against the Apple verification class). We land the data
//! structures and the deterministic, GPU-free selection logic now — the parts that ARE
//! testable on any host — so the seam doesn't move when the kernel lands. The port
//! mapping (Hawking module -> cx module), the determinism plan, and the week breakdown
//! live in docs/HAWKING_PORT_PLAN.md.
//!
//! Determinism: this module emits NO model output. The selection functions are pure and
//! deterministic (ascending slot-id order, stable tie-breaks — mirroring Hawking's
//! group_by_prefix). When the kernel lands, the lane is re-gated cross-worker on a
//! pinned (device, shader_hash) Apple class (Hawking proved token-level determinism is
//! impossible across heterogeneous Mac generations — semantic replay, not bit-exact),
//! so the batched path is NEVER byte-compared against a solo Candle worker.
//!
//! Apple-Silicon ONLY. This lane never touches the CUDA cloud lane (that is vLLM,
//! runners::VllmRunner). The constructor records the host engine tag so a non-Apple host
//! that somehow selects `hawking` stays on the fallback path.

#![allow(dead_code)] // skeleton: the selection logic is exercised by tests; the GPU
                     // decode loop is documented-only until the Metal kernel lands.

/// Lifecycle of one in-flight request's slot. Mirrors Hawking's `batch::SlotState`
/// (hawking-serve/src/batch/mod.rs). A slot keeps its KV region for its whole life so
/// the ready set can churn around it (slot-strided KV — see the driver mapping below).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SlotState {
    /// No request assigned; the KV region is free.
    Idle,
    /// Prompt admitted, prefill not yet complete.
    Prefilling,
    /// Prefill done; ready to emit one token per decode step.
    Decoding,
    /// Hit EOS or max tokens; awaiting release.
    Finishing,
}

/// One in-flight request. Mirrors Hawking's `batch::Slot`, trimmed to the fields the
/// scheduler's selection logic needs (the GPU-side KV handle is added when the kernel
/// lands). `id` doubles as the STABLE KV region index (0..max_batch, reused on release),
/// exactly as Hawking's driver keys KV by `slot_id` not the compacted batch index.
#[derive(Debug, Clone)]
pub struct Slot {
    /// Stable slot id == stable KV region index.
    pub id: u32,
    pub state: SlotState,
    /// The prompt's token ids (for prefix grouping + the prefill pass).
    pub prompt_ids: Vec<u32>,
    /// Tokens generated so far.
    pub generated_ids: Vec<u32>,
    /// The most recent token (the input to the next decode step).
    pub last_token: Option<u32>,
    /// Absolute decode position (== prompt_ids.len() + generated_ids.len()).
    pub position: usize,
    /// Hard cap on generated tokens for this request.
    pub max_new_tokens: usize,
    /// True when this request asked for greedy/temp=0 decode with no repetition
    /// penalty. All-greedy steps route to Hawking's token-only lane (B*4-byte readback
    /// of GPU argmax) rather than the full B*vocab logits lane.
    pub greedy: bool,
}

impl Slot {
    /// An empty, free slot owning KV region `id`.
    pub fn idle(id: u32) -> Self {
        Self {
            id,
            state: SlotState::Idle,
            prompt_ids: Vec::new(),
            generated_ids: Vec::new(),
            last_token: None,
            position: 0,
            max_new_tokens: 0,
            greedy: true,
        }
    }

    /// Ready to contribute a decode step this tick.
    pub fn is_ready_to_decode(&self) -> bool {
        self.state == SlotState::Decoding && self.generated_ids.len() < self.max_new_tokens
    }
}

/// One decode step a slot would contribute to a batched forward pass. Mirrors Hawking's
/// `batch::DecodeStep` (slot_id + the single input token + its absolute position). The
/// `slot_id` (not the compacted index) is what the multi-seq KV kernel keys on.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DecodeStep {
    pub slot_id: u32,
    pub token: u32,
    pub position: usize,
}

/// Admission policy for `decode_plan` / `prefill_plan`. Mirrors Hawking's
/// `scheduler::BatchPolicy`. The skeleton ships `Default` (FIFO by slot id) and
/// `PrefixGrouped` (co-batch shared-prefix prompts so one prefill pass covers the
/// shared prefix once — Hawking's `group_by_prefix` with `min_shared = 8`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum BatchPolicy {
    /// Admit any ready slots up to max_batch, FIFO by slot id.
    #[default]
    Default,
    /// Co-batch slots that share a token prefix for amortised prefill.
    PrefixGrouped,
}

/// Length of the longest common prefix of two token slices. Ported verbatim from
/// Hawking's `scheduler::common_prefix_len`.
#[inline]
fn common_prefix_len(a: &[u32], b: &[u32]) -> usize {
    a.iter().zip(b.iter()).take_while(|(x, y)| x == y).count()
}

/// Prefix-affinity prefill cohort selection (PURE). Ported from Hawking's
/// `scheduler::group_by_prefix`: pick the set of `Prefilling` slots that share the
/// LONGEST common token prefix, capped at `max_batch`, so one prefill pass covers the
/// shared prefix once. Latency-safety: when no group of size >= 2 with shared_len >=
/// `min_shared` exists, fall back to FIFO admit by slot id (a unique request is never
/// starved waiting for a co-prefix partner).
///
/// Determinism: candidates are processed in ascending slot-id order; the winning group
/// maximizes (shared_prefix_len, group_size) with the smallest anchor slot-id as the
/// final tie-break — a pure deterministic function of the table.
pub fn group_by_prefix(slots: &[Slot], max_batch: usize, min_shared: usize) -> Vec<u32> {
    let mut cands: Vec<(u32, &[u32])> = slots
        .iter()
        .filter(|s| s.state == SlotState::Prefilling)
        .map(|s| (s.id, s.prompt_ids.as_slice()))
        .collect();
    cands.sort_by_key(|&(id, _)| id);
    if cands.is_empty() || max_batch == 0 {
        return Vec::new();
    }

    let mut best: Option<(usize, usize, u32, Vec<u32>)> = None; // (shared, size, anchor, ids)
    for ai in 0..cands.len() {
        let (anchor_id, anchor_ids) = cands[ai];
        let mut partners: Vec<(usize, u32)> = cands
            .iter()
            .enumerate()
            .filter(|&(i, _)| i != ai)
            .map(|(_, &(id, ids))| (common_prefix_len(anchor_ids, ids), id))
            .collect();
        // Descending by cpl; tie-break ascending slot-id for determinism.
        partners.sort_by(|x, y| y.0.cmp(&x.0).then(x.1.cmp(&y.1)));
        let cap_partners = max_batch.saturating_sub(1).min(partners.len());
        for k in 1..=cap_partners {
            let shared_len = partners[k - 1].0; // k-th largest cpl (1-indexed)
            if shared_len < min_shared {
                break; // further k only lowers shared_len (sorted desc)
            }
            let size = k + 1;
            let mut group: Vec<u32> = Vec::with_capacity(size);
            group.push(anchor_id);
            for &(_, pid) in &partners[..k] {
                group.push(pid);
            }
            group.sort_unstable();
            let better = match &best {
                None => true,
                Some((bs, bz, ba, _)) => {
                    (shared_len, size).cmp(&(*bs, *bz)) == std::cmp::Ordering::Greater
                        || ((shared_len, size) == (*bs, *bz) && anchor_id < *ba)
                }
            };
            if better {
                best = Some((shared_len, size, anchor_id, group));
            }
        }
    }

    match best {
        Some((_, _, _, ids)) => ids.into_iter().take(max_batch).collect(),
        None => cands
            .into_iter()
            .take(max_batch)
            .map(|(id, _)| id)
            .collect(),
    }
}

/// How a planned decode step should be read back from the GPU. Mirrors Hawking's
/// driver lane routing in `decode_ready_once`: an all-greedy batch reads back only the
/// B argmax token ids (B*4 bytes), otherwise the full B*vocab logits.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DecodeLane {
    /// All slots greedy/temp=0: GPU argmax, B*4-byte token-only readback.
    GreedyTokens,
    /// At least one sampling/logprob/penalty slot: full B*vocab logits readback.
    FullLogits,
}

/// The slot manager. Mirrors Hawking's `scheduler::Scheduler`: a fixed pool of
/// `max_batch_size` slots, each owning a stable KV region. The skeleton owns the slot
/// table and the deterministic, GPU-free PLANNING of each step; the actual forward pass
/// is the documented-only `forward_multiseq_*` seam (docs/HAWKING_PORT_PLAN.md), so this
/// type does nothing to model output today.
pub struct Scheduler {
    pub slots: Vec<Slot>,
    pub max_batch_size: usize,
    pub policy: BatchPolicy,
    /// The host's engine tag (config::InferenceBackend::engine_tag). The lane only ever
    /// activates on an Apple host advertising engine="hawking"; recorded so the fallback
    /// decision is explicit, not implicit.
    pub engine: &'static str,
}

impl Scheduler {
    /// A scheduler with `max_batch_size` idle slots, FIFO policy. `engine` is the host's
    /// advertised engine tag (so the verification class is correct).
    pub fn new(max_batch_size: usize, engine: &'static str) -> Self {
        let slots = (0..max_batch_size as u32).map(Slot::idle).collect();
        Self {
            slots,
            max_batch_size,
            policy: BatchPolicy::Default,
            engine,
        }
    }

    /// Count of non-idle slots.
    pub fn active_count(&self) -> usize {
        self.slots.iter().filter(|s| s.state != SlotState::Idle).count()
    }

    /// The Prefilling slots to cover in the next prefill pass. Under `PrefixGrouped`,
    /// the shared-prefix cohort (Hawking's `prefill_slots_prefix_grouped`, min_shared=8);
    /// otherwise FIFO by slot id. PURE.
    pub fn prefill_plan(&self, max: usize) -> Vec<u32> {
        let cap = max.min(self.max_batch_size);
        match self.policy {
            BatchPolicy::PrefixGrouped => group_by_prefix(&self.slots, cap, 8),
            BatchPolicy::Default => self
                .slots
                .iter()
                .filter(|s| s.state == SlotState::Prefilling)
                .take(cap)
                .map(|s| s.id)
                .collect(),
        }
    }

    /// The decode steps the next batched forward pass would cover, FIFO by slot id up to
    /// `max`. Mirrors Hawking's `scheduler::decode_batch`. PURE — selection only, no
    /// model output. The GPU forward pass that consumes this plan is the documented-only
    /// seam; until it lands the agent uses LlamaBackend::generate_batch instead, so this
    /// is inert.
    pub fn decode_plan(&self, max: usize) -> Vec<DecodeStep> {
        let cap = max.min(self.max_batch_size);
        self.slots
            .iter()
            .filter(|s| s.is_ready_to_decode())
            .take(cap)
            .filter_map(|s| {
                s.last_token.map(|token| DecodeStep {
                    slot_id: s.id,
                    token,
                    position: s.position,
                })
            })
            .collect()
    }

    /// Which readback lane a planned batch routes to. Mirrors Hawking's driver
    /// `all_greedy` check: token-only when EVERY step's slot is greedy, else full logits.
    /// PURE.
    pub fn decode_lane(&self, batch: &[DecodeStep]) -> DecodeLane {
        let all_greedy = batch.iter().all(|step| {
            self.slots
                .iter()
                .find(|s| s.id == step.slot_id)
                .map(|s| s.greedy)
                .unwrap_or(false)
        });
        if all_greedy {
            DecodeLane::GreedyTokens
        } else {
            DecodeLane::FullLogits
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn decoding(id: u32, last: u32, position: usize, max_new: usize, greedy: bool) -> Slot {
        Slot {
            id,
            state: SlotState::Decoding,
            prompt_ids: Vec::new(),
            generated_ids: Vec::new(),
            last_token: Some(last),
            position,
            max_new_tokens: max_new,
            greedy,
        }
    }

    fn prefilling(id: u32, prompt: Vec<u32>) -> Slot {
        Slot {
            id,
            state: SlotState::Prefilling,
            prompt_ids: prompt,
            generated_ids: Vec::new(),
            last_token: None,
            position: 0,
            max_new_tokens: 8,
            greedy: true,
        }
    }

    #[test]
    fn new_scheduler_starts_all_idle() {
        let s = Scheduler::new(3, "hawking");
        assert_eq!(s.slots.len(), 3);
        assert_eq!(s.active_count(), 0);
        assert!(s.slots.iter().all(|sl| sl.state == SlotState::Idle));
        assert_eq!(s.engine, "hawking");
    }

    #[test]
    fn decode_plan_is_fifo_and_capped() {
        let mut s = Scheduler::new(4, "hawking");
        s.slots = vec![
            decoding(0, 10, 5, 8, true),
            decoding(1, 11, 5, 8, true),
            decoding(2, 12, 5, 8, true),
            Slot::idle(3),
        ];
        let plan = s.decode_plan(2);
        assert_eq!(
            plan,
            vec![
                DecodeStep { slot_id: 0, token: 10, position: 5 },
                DecodeStep { slot_id: 1, token: 11, position: 5 },
            ]
        );
    }

    #[test]
    fn decode_plan_skips_finished_slots() {
        let mut s = Scheduler::new(2, "hawking");
        // slot 0 has hit its max_new_tokens -> not ready.
        let mut done = decoding(0, 9, 8, 1, true);
        done.generated_ids = vec![9];
        s.slots = vec![done, decoding(1, 11, 5, 8, true)];
        let plan = s.decode_plan(8);
        assert_eq!(plan.len(), 1);
        assert_eq!(plan[0].slot_id, 1);
    }

    #[test]
    fn decode_lane_routes_greedy_vs_sampled() {
        let mut s = Scheduler::new(2, "hawking");
        s.slots = vec![decoding(0, 10, 5, 8, true), decoding(1, 11, 5, 8, true)];
        let plan = s.decode_plan(8);
        assert_eq!(s.decode_lane(&plan), DecodeLane::GreedyTokens);
        // Flip one slot to sampling -> full-logits lane.
        s.slots[1].greedy = false;
        let plan = s.decode_plan(8);
        assert_eq!(s.decode_lane(&plan), DecodeLane::FullLogits);
    }

    #[test]
    fn prefill_plan_default_is_fifo() {
        let mut s = Scheduler::new(4, "hawking");
        s.slots = vec![
            prefilling(0, vec![1, 2, 3]),
            prefilling(1, vec![4, 5, 6]),
            Slot::idle(2),
            Slot::idle(3),
        ];
        assert_eq!(s.prefill_plan(8), vec![0, 1]);
    }

    #[test]
    fn prefill_plan_prefix_grouped_cobatches_shared_prefix() {
        let mut s = Scheduler::new(4, "hawking");
        s.policy = BatchPolicy::PrefixGrouped;
        let shared: Vec<u32> = (100..110).collect();
        let mut a = shared.clone();
        a.push(1);
        let mut b = shared.clone();
        b.push(2);
        let d: Vec<u32> = (900..912).collect();
        s.slots = vec![
            prefilling(0, a),
            prefilling(1, b),
            prefilling(2, d),
            Slot::idle(3),
        ];
        let chosen = s.prefill_plan(4);
        assert!(chosen.contains(&0) && chosen.contains(&1), "shared-prefix pair must co-batch: {chosen:?}");
        assert!(!chosen.contains(&2), "unrelated slot must not join: {chosen:?}");
    }

    /// Determinism guard: identical tables -> identical plans (the property the
    /// cross-worker verification class depends on).
    #[test]
    fn group_by_prefix_is_deterministic() {
        let mut s = Scheduler::new(4, "hawking");
        let p: Vec<u32> = (0..12).collect();
        let mut x0 = p.clone();
        x0.push(70);
        let mut x1 = p.clone();
        x1.push(71);
        s.slots = vec![
            prefilling(0, x0),
            prefilling(1, x1),
            prefilling(2, (200..208).collect()),
            prefilling(3, (300..308).collect()),
        ];
        let first = group_by_prefix(&s.slots, 2, 8);
        assert_eq!(first, vec![0, 1]);
        assert_eq!(first, group_by_prefix(&s.slots, 2, 8));
    }
}
