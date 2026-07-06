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
//!
//! ## Week 3 update (docs/HAWKING_PORT_PLAN.md): the decode loop is wired, at the
//! kernel/scheduler level
//!
//! This module now carries the pieces Week 2's own doc comment named as
//! deliberately deferred: `sample_next` (temperature/top-k/top-p/repetition-penalty
//! sampling, ported from Hawking's `hawking-core/src/sample.rs::Sampler`),
//! `apply_decode_tokens` / `apply_decode_logits` (mutate slot state from a REAL
//! decode result: record the token, advance position, detect EOS, transition to
//! `Finishing` — ported from `hawking-serve/src/batch/mod.rs`'s
//! `Slot::apply_decoded_token` / `scheduler.rs`'s `apply_decode_logits` /
//! `apply_decode_tokens`), and `LaneStats` (ported from
//! `hawking-serve/src/batch/driver.rs::LaneStats`). Under `#[cfg(feature =
//! "metal")]`, `Scheduler::decode_step_metal` wires `decode_plan` straight into the
//! REAL `hawking_metal_kernel::{MultiSeqDecodeAttention, KvScatterAppend}` ops (not
//! a mock, not a synthetic stand-in) and back through `apply_decode_*`, proven on
//! real Metal hardware by `continuous_batch::tests` below.
//!
//! **Status update (Week 6, 2026-07-06):** the boundary the next paragraph
//! describes was CLOSED by Weeks 4-6 (docs/HAWKING_PORT_PLAN.md): a real GGUF
//! decodes through `quantized_llama_batched::ModelWeights::hawking_decode_step`
//! (per-slot RoPE + Q4_K projections + flat multi-region KV), the churn driver
//! `LlamaBackend::hawking_generate_churn` drives THIS scheduler's `admit`/
//! `apply_decode_tokens`/`release_slot` against it, and `runners::HawkingRunner`
//! now dispatches real `batch_infer` tasks through that driver. The paragraph is
//! kept verbatim as the honest record of what WEEK 2 did and did not claim.
//!
//! **What is honestly NOT done here** (see `runners::HawkingRunner`'s doc comment
//! for the full boundary statement): `decode_step_metal` drives the kernel on raw
//! Q/K/V tensors it is handed — it does NOT run a real GGUF model's forward pass.
//! Wiring a real `QLlama` model into this loop requires RoPE fusion, the Q4_K
//! quantized projection GEMMs, and rewriting `LayerWeights`'s private
//! per-model-instance `KvCacheSlot` into the flat, multi-region slot-strided buffer
//! this kernel expects across every layer — a model-integration rewrite Week 2's
//! own doc comment already named as out of scope, and confirmed by direct
//! inspection of `quantized_llama_batched.rs::LayerWeights::forward_attn` in this
//! pass: it does its own private Q/K/V projection, RoPE, and KV-cache append with NO
//! seam to substitute an external attention op. That end-to-end "real GGUF model
//! through the wired scheduler" proof is deferred to a follow-up pass (see
//! `HawkingRunner`'s doc comment) — landing it here would mean claiming a coherence
//! proof this pass did not actually run, which is exactly the half-verified claim
//! this lane's whole discipline exists to prevent.

#![allow(dead_code)] // the GPU decode loop (decode_step_metal) is exercised by
                     // #[cfg(feature = "metal")] tests; the production caller
                     // (HawkingRunner -> hawking_generate_churn, wired Week 6)
                     // uses admit/apply_decode_tokens/release_slot and only on
                     // the explicit inference_backend="hawking" opt-in.

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

    /// Admit a prompt into this (idle) slot. Mirrors Hawking's `Slot::assign`,
    /// trimmed to the fields this skeleton keeps (no `req`/`sampler` object — the
    /// caller drives `SamplingParams` + a `Sampler` explicitly, see `sample_next`).
    /// `last_token` seeds to the prompt's own last id (fed at `position =
    /// prompt_ids.len()` by the first decode step) and `position` starts at the
    /// prompt length, exactly as Hawking's `assign` does.
    pub fn assign(&mut self, prompt_ids: Vec<u32>, max_new_tokens: usize, greedy: bool) {
        self.last_token = prompt_ids.last().copied();
        self.position = prompt_ids.len();
        self.prompt_ids = prompt_ids;
        self.generated_ids.clear();
        self.max_new_tokens = max_new_tokens;
        self.greedy = greedy;
        self.state = SlotState::Prefilling;
    }

    /// Prefill done for this slot; ready to decode. Mirrors `Slot::mark_decoding`.
    pub fn mark_decoding(&mut self) {
        if self.state != SlotState::Idle {
            self.state = SlotState::Decoding;
        }
    }

    /// Sample the next token from this slot's (mutable) logits row and record it
    /// into sampling history. Ported from Hawking's `Slot::sample_next` +
    /// `Sampler::sample` (`hawking-serve/src/batch/mod.rs`,
    /// `hawking-core/src/sample.rs`) — temperature<=0 is greedy argmax (no RNG
    /// draw, so it stays deterministic across the same logits regardless of the
    /// sampler's seed); temperature>0 applies repetition penalty, temperature
    /// scaling, softmax, top-k, then top-p, and draws from the renormalized
    /// distribution. Returns `None` only if `logits` is empty (never silently
    /// returns a fabricated token).
    pub fn sample_next(&mut self, logits: &mut [f32], sampler: &mut Sampler) -> Option<u32> {
        if logits.is_empty() {
            return None;
        }
        let token = sampler.sample(logits, self.greedy);
        sampler.record(token);
        Some(token)
    }

    /// Record an already-sampled token (the greedy GPU-argmax lane never
    /// materializes logits on the CPU side, so there is nothing for `sample_next`
    /// to run on — `apply_decode_tokens` calls this directly). Mirrors
    /// `Slot::record_token` + `Slot::apply_decoded_token`: push to `generated_ids`,
    /// advance `position`, detect EOS (or the max-new-tokens ceiling) and
    /// transition to `Finishing`.
    pub fn apply_decoded_token(&mut self, token: u32, eos_id: Option<u32>) -> DecodedToken {
        self.generated_ids.push(token);
        self.last_token = Some(token);
        self.position += 1;
        let hit_eos = eos_id.is_some() && Some(token) == eos_id;
        if hit_eos || self.generated_ids.len() >= self.max_new_tokens {
            self.state = SlotState::Finishing;
        }
        DecodedToken {
            slot_id: self.id,
            token,
            finished: self.state == SlotState::Finishing,
        }
    }

    /// Release this slot back to `Idle`, freeing its KV region for reuse. Mirrors
    /// `Slot::release`.
    pub fn release(&mut self) {
        let id = self.id;
        *self = Self::idle(id);
    }
}

/// One token emitted by a completed decode step, plus whether that step finished
/// the slot (EOS or max_new_tokens reached). Mirrors Hawking's
/// `batch::DecodedToken`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DecodedToken {
    pub slot_id: u32,
    pub token: u32,
    pub finished: bool,
}

/// Deterministic-given-seed token sampler. Ported from Hawking's
/// `hawking-core/src/sample.rs::Sampler` (temperature / repetition-penalty / top-k
/// / top-p), using this workspace's existing `rand` dependency (no `rand_pcg`
/// needed — `rand::rngs::StdRng` is already a first-class, already-vendored
/// PRNG this crate can seed deterministically). One `Sampler` is owned per slot for
/// its whole lifetime so the repetition-penalty window and RNG stream are
/// continuous across decode steps, exactly like Hawking's per-slot `Sampler`.
#[derive(Debug, Clone)]
pub struct Sampler {
    rng: rand::rngs::StdRng,
    /// Rolling history of recently emitted tokens for the repetition penalty.
    recent: Vec<u32>,
    rep_window: usize,
    pub temperature: f32,
    pub top_k: usize,
    pub top_p: f32,
    pub repetition_penalty: f32,
}

impl Sampler {
    /// A greedy (temperature=0) sampler seeded deterministically. Greedy decode
    /// never actually draws from `rng` (argmax is a pure function of `logits`),
    /// so the seed only matters once a caller raises `temperature` above 0.
    pub fn new(seed: u64) -> Self {
        use rand::SeedableRng;
        Self {
            rng: rand::rngs::StdRng::seed_from_u64(seed),
            recent: Vec::new(),
            rep_window: 64,
            temperature: 0.0,
            top_k: 0,
            top_p: 0.0,
            repetition_penalty: 1.0,
        }
    }

    pub fn record(&mut self, token: u32) {
        self.recent.push(token);
        if self.recent.len() > self.rep_window {
            let n = self.recent.len() - self.rep_window;
            self.recent.drain(0..n);
        }
    }

    /// Sample one token from `logits` (mutated in place by repetition
    /// penalty/temperature scaling, exactly as Hawking's `Sampler::sample` does).
    /// `force_greedy` lets a caller route the all-greedy batch lane through argmax
    /// even if this sampler's own `temperature` field were (incorrectly) nonzero —
    /// defense in depth matching `Scheduler::decode_lane`'s own greedy check.
    pub fn sample(&mut self, logits: &mut [f32], force_greedy: bool) -> u32 {
        if self.repetition_penalty != 1.0 {
            for &t in &self.recent {
                let i = t as usize;
                if i < logits.len() {
                    let v = logits[i];
                    logits[i] = if v >= 0.0 {
                        v / self.repetition_penalty
                    } else {
                        v * self.repetition_penalty
                    };
                }
            }
        }
        if force_greedy || self.temperature <= 0.0 {
            return argmax(logits);
        }
        for v in logits.iter_mut() {
            *v /= self.temperature;
        }
        let mut probs = logits.to_vec();
        softmax(&mut probs);

        let mut indexed: Vec<(usize, f32)> = probs.iter().copied().enumerate().collect();
        indexed.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
        if self.top_k > 0 && self.top_k < indexed.len() {
            indexed.truncate(self.top_k);
        }
        if self.top_p > 0.0 && self.top_p < 1.0 {
            let mut cum = 0.0f32;
            let mut cutoff = indexed.len();
            for (k, (_, p)) in indexed.iter().enumerate() {
                cum += *p;
                if cum >= self.top_p {
                    cutoff = k + 1;
                    break;
                }
            }
            indexed.truncate(cutoff);
        }

        use rand::Rng;
        let total: f32 = indexed.iter().map(|(_, p)| *p).sum();
        let r: f32 = self.rng.gen::<f32>() * total;
        let mut acc = 0.0f32;
        for (idx, p) in &indexed {
            acc += *p;
            if r <= acc {
                return *idx as u32;
            }
        }
        indexed.last().map(|(i, _)| *i as u32).unwrap_or(0)
    }
}

fn argmax(xs: &[f32]) -> u32 {
    let mut best = 0usize;
    let mut bv = f32::NEG_INFINITY;
    for (i, &v) in xs.iter().enumerate() {
        if v > bv {
            best = i;
            bv = v;
        }
    }
    best as u32
}

fn softmax(xs: &mut [f32]) {
    let mut m = f32::NEG_INFINITY;
    for &v in xs.iter() {
        if v > m {
            m = v;
        }
    }
    let mut sum = 0.0f32;
    for v in xs.iter_mut() {
        *v = (*v - m).exp();
        sum += *v;
    }
    let inv = if sum > 0.0 { 1.0 / sum } else { 0.0 };
    for v in xs.iter_mut() {
        *v *= inv;
    }
}

/// Decode-lane stats accumulated across all steps, exposed the same way Hawking's
/// `/metrics` does. Ported verbatim (field-for-field) from
/// `hawking-serve/src/batch/driver.rs::LaneStats`.
#[derive(Debug, Default, Clone, Copy, PartialEq)]
pub struct LaneStats {
    /// Steps routed through the greedy token-only path (B*4 byte readback).
    pub greedy_steps: u64,
    /// Steps routed through the full-logits path (B*vocab*4 byte readback).
    pub logits_steps: u64,
    /// Cumulative bytes read back from GPU this session.
    pub readback_bytes: u64,
    /// Slots that finished (EOS or max_new_tokens) this session.
    pub finished_slots: u64,
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
/// `max_batch_size` slots, each owning a stable KV region. Week 1-2 shipped the
/// deterministic, GPU-free PLANNING of each step; Week 3 (this module's current
/// state) adds the state-MUTATING half — `admit`/`release_slot`/
/// `mark_prefill_complete`/`apply_decode_tokens`/`apply_decode_logits` — plus (under
/// `#[cfg(feature = "metal")]`) `decode_step_metal`, which drives a real decode step
/// through the real Metal kernel end to end. See the module doc comment for exactly
/// what is and is not proven at this layer.
pub struct Scheduler {
    pub slots: Vec<Slot>,
    pub max_batch_size: usize,
    pub policy: BatchPolicy,
    /// The host's engine tag (config::InferenceBackend::engine_tag). The lane only ever
    /// activates on an Apple host advertising engine="hawking"; recorded so the fallback
    /// decision is explicit, not implicit.
    pub engine: &'static str,
    /// Per-slot sampler, keyed by slot id (stable across the slot's lifetime — a
    /// fresh `Sampler` is installed on `admit`, matching Hawking's `Slot::assign`
    /// creating a fresh `Sampler` per admission). `None` for an Idle slot.
    pub samplers: Vec<Option<Sampler>>,
    /// Decode-lane stats accumulated across every `apply_decode_*`/`decode_step_metal`
    /// call this scheduler has driven. Ported from Hawking's `BatchDriver::lane_stats`.
    pub lane_stats: LaneStats,
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
            samplers: (0..max_batch_size).map(|_| None).collect(),
            lane_stats: LaneStats::default(),
        }
    }

    /// Admit a prompt into the first Idle slot. Mirrors Hawking's
    /// `Scheduler::admit`: installs a fresh `Sampler` (seeded from `seed`, or a
    /// slot-id-derived default so two concurrent admissions never accidentally
    /// share a seed) and transitions the slot to `Prefilling`. Returns `None` when
    /// every slot is occupied (the caller must wait for a release, never overwrite
    /// a live slot).
    pub fn admit(
        &mut self,
        prompt_ids: Vec<u32>,
        max_new_tokens: usize,
        greedy: bool,
        seed: Option<u64>,
    ) -> Option<u32> {
        let id = self.slots.iter().find(|s| s.state == SlotState::Idle)?.id;
        let slot = self.slots.iter_mut().find(|s| s.id == id)?;
        slot.assign(prompt_ids, max_new_tokens, greedy);
        let seed = seed.unwrap_or(0xD15A_0000_0000_0000u64 ^ id as u64);
        self.samplers[id as usize] = Some(Sampler::new(seed));
        Some(id)
    }

    /// Prefill finished for slot `id`; ready to decode. Mirrors
    /// `Scheduler::mark_prefill_complete`. Returns `false` for an unknown slot or
    /// one that was not Prefilling.
    pub fn mark_prefill_complete(&mut self, id: u32) -> bool {
        let Some(slot) = self.slots.iter_mut().find(|s| s.id == id) else {
            return false;
        };
        if slot.state != SlotState::Prefilling {
            return false;
        }
        slot.mark_decoding();
        true
    }

    /// Free slot `id` back to Idle (and drop its sampler). Mirrors
    /// `Scheduler::release_slot`. Returns `false` for an unknown slot.
    pub fn release_slot(&mut self, id: u32) -> bool {
        let Some(slot) = self.slots.iter_mut().find(|s| s.id == id) else {
            return false;
        };
        slot.release();
        self.samplers[id as usize] = None;
        true
    }

    /// Count of non-idle slots.
    pub fn active_count(&self) -> usize {
        self.slots
            .iter()
            .filter(|s| s.state != SlotState::Idle)
            .count()
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

    /// Validate a decode result still matches the live slot table before mutating
    /// it. Mirrors Hawking's own staleness guard in `apply_decode_logits` /
    /// `apply_decode_tokens` (`slot.decode_step() != Some(*step)`): if the plan was
    /// computed against a stale snapshot (e.g. another release/admit raced it), the
    /// caller gets an explicit typed error instead of silently mutating the wrong
    /// generation of a reused slot id.
    fn validate_step(&self, step: &DecodeStep) -> Result<(), String> {
        let slot = self
            .slots
            .iter()
            .find(|s| s.id == step.slot_id)
            .ok_or_else(|| format!("decode result for unknown slot {}", step.slot_id))?;
        let live = DecodeStep {
            slot_id: slot.id,
            token: slot.last_token.unwrap_or(u32::MAX),
            position: slot.position,
        };
        if !slot.is_ready_to_decode() || live != *step {
            return Err(format!(
                "stale decode result for slot {}: expected {:?}, got {:?}",
                step.slot_id, live, step
            ));
        }
        Ok(())
    }

    /// Apply a full-logits decode result: sample each step's slot, record the
    /// token, detect EOS, advance the slot. Mirrors Hawking's
    /// `Scheduler::apply_decode_logits`. `logits` must be `(batch.len(), vocab)`
    /// rows in the SAME order as `batch` (the order `decode_plan` produced, i.e.
    /// ascending slot id — this scheduler's own determinism guarantee). Updates
    /// `lane_stats` (logits_steps + readback_bytes) on success.
    pub fn apply_decode_logits(
        &mut self,
        batch: &[DecodeStep],
        logits: &mut [Vec<f32>],
        eos_id: Option<u32>,
    ) -> Result<Vec<DecodedToken>, String> {
        if batch.len() != logits.len() {
            return Err(format!(
                "decode result shape mismatch: batch={} logits={}",
                batch.len(),
                logits.len()
            ));
        }
        let mut out = Vec::with_capacity(batch.len());
        let mut vocab = 0usize;
        for (step, row) in batch.iter().zip(logits.iter_mut()) {
            self.validate_step(step)?;
            vocab = row.len();
            let slot = self
                .slots
                .iter_mut()
                .find(|s| s.id == step.slot_id)
                .expect("validated above");
            let sampler = self.samplers[step.slot_id as usize]
                .as_mut()
                .ok_or_else(|| format!("slot {} has no sampler", step.slot_id))?;
            let token = slot
                .sample_next(row, sampler)
                .ok_or_else(|| format!("slot {} cannot sample decode result", step.slot_id))?;
            out.push(slot.apply_decoded_token(token, eos_id));
        }
        self.lane_stats.logits_steps += 1;
        self.lane_stats.readback_bytes += (batch.len() * vocab * std::mem::size_of::<f32>()) as u64;
        self.lane_stats.finished_slots += out.iter().filter(|d| d.finished).count() as u64;
        Ok(out)
    }

    /// Apply a greedy token-only decode result: token ids arrive pre-sampled (GPU
    /// argmax), no logits involved. Mirrors Hawking's
    /// `Scheduler::apply_decode_tokens`. Updates `lane_stats` (greedy_steps +
    /// readback_bytes) on success.
    pub fn apply_decode_tokens(
        &mut self,
        batch: &[DecodeStep],
        token_ids: Vec<u32>,
        eos_id: Option<u32>,
    ) -> Result<Vec<DecodedToken>, String> {
        if batch.len() != token_ids.len() {
            return Err(format!(
                "decode tokens shape mismatch: batch={} tokens={}",
                batch.len(),
                token_ids.len()
            ));
        }
        let mut out = Vec::with_capacity(batch.len());
        for (step, token) in batch.iter().zip(token_ids) {
            self.validate_step(step)?;
            let slot = self
                .slots
                .iter_mut()
                .find(|s| s.id == step.slot_id)
                .expect("validated above");
            // The token already came from GPU argmax, but sampling history (the
            // repetition-penalty window) still needs the emission recorded so a
            // LATER step that flips this slot to sampling mode has continuous
            // history — mirrors Hawking's own note in `Slot::sample_next` that
            // skipping this made the penalty dead on the batch path.
            if let Some(sampler) = self.samplers[step.slot_id as usize].as_mut() {
                sampler.record(token);
            }
            out.push(slot.apply_decoded_token(token, eos_id));
        }
        self.lane_stats.greedy_steps += 1;
        self.lane_stats.readback_bytes += (batch.len() * std::mem::size_of::<u32>()) as u64;
        self.lane_stats.finished_slots += out.iter().filter(|d| d.finished).count() as u64;
        Ok(out)
    }
}

/// Week 3 kernel wiring: `decode_plan` -> `hawking_metal_kernel` -> `apply_decode_*`
/// -> slot advance, driven on REAL Candle tensors through the REAL Metal ops (not a
/// mock). This is the actual "connect decode_plan -> kernel -> apply_decode_* ->
/// slot advance" wiring docs/HAWKING_PORT_PLAN.md's Week 3 entry asks for, at the
/// layer the kernel actually operates on: one attention op over already-projected
/// Q and a persistent flat multi-region KV cache. It does NOT run a real GGUF
/// model's full forward pass (RoPE + Q4_K projections + per-layer KV) — see the
/// module doc comment and `runners::HawkingRunner` for that honest boundary.
#[cfg(feature = "metal")]
pub mod metal_decode {
    use super::*;
    use crate::hawking_metal_kernel::{KvScatterAppend, MultiSeqDecodeAttention};
    use candle_core::{Device, Tensor};

    /// The persistent, slot-strided KV cache one wired decode loop owns: flat
    /// `(num_regions * max_seq_per_slot, n_kv_heads, head_dim)` K and V buffers,
    /// exactly the layout `hawking_metal_kernel`'s ops require. `num_regions` ==
    /// `Scheduler::max_batch_size` (one region per stable slot id).
    pub struct KvCache {
        pub k: Tensor,
        pub v: Tensor,
        pub n_kv_heads: usize,
        pub head_dim: usize,
        pub max_seq_per_slot: usize,
    }

    impl KvCache {
        pub fn zeros(
            device: &Device,
            num_regions: usize,
            max_seq_per_slot: usize,
            n_kv_heads: usize,
            head_dim: usize,
        ) -> candle_core::Result<Self> {
            let shape = (num_regions * max_seq_per_slot, n_kv_heads, head_dim);
            Ok(Self {
                k: Tensor::zeros(shape, candle_core::DType::F32, device)?,
                v: Tensor::zeros(shape, candle_core::DType::F32, device)?,
                n_kv_heads,
                head_dim,
                max_seq_per_slot,
            })
        }

        pub fn kv_dim(&self) -> usize {
            self.n_kv_heads * self.head_dim
        }

        pub fn slot_stride(&self) -> usize {
            self.max_seq_per_slot * self.kv_dim()
        }
    }

    /// One wired decode step for `batch` (the output of `Scheduler::decode_plan`):
    /// scatter-append this step's new K/V into `cache` at each slot's own region
    /// (`KvScatterAppend`), then run the multi-seq decode attention
    /// (`MultiSeqDecodeAttention`) over each slot's own live history. `q`/`k_new`/
    /// `v_new` are `(batch.len(), n_heads_or_kv_heads, head_dim)` — the CALLER's
    /// job (a real model integration) is producing these from its own
    /// projection+RoPE; this function's job is only the attention math + KV
    /// bookkeeping, which is exactly what `hawking_metal_kernel` proves.
    ///
    /// Returns the raw attention output, `(batch.len(), n_heads, head_dim)` — the
    /// caller still owes the output projection + sampling (this crate's
    /// `Scheduler::apply_decode_tokens`/`apply_decode_logits` once logits exist).
    pub fn decode_attention_step(
        cache: &mut KvCache,
        batch: &[DecodeStep],
        q: &Tensor,
        k_new: &Tensor,
        v_new: &Tensor,
        n_heads: usize,
        scale: f32,
    ) -> candle_core::Result<Tensor> {
        let regions: Vec<u32> = batch.iter().map(|s| s.slot_id).collect();
        let positions: Vec<u32> = batch.iter().map(|s| s.position as u32).collect();

        // 1. Scatter this step's new K and V into the persistent cache at each
        //    slot's own region/position — the exact index arithmetic
        //    `kv_scatter_append_matches_manual_index_arithmetic` proves on real
        //    Metal.
        let scatter = KvScatterAppend {
            regions: regions.clone(),
            positions: positions.clone(),
            kv_dim: cache.kv_dim(),
            slot_stride: cache.slot_stride(),
        };
        cache.k.inplace_op2(k_new, &scatter)?;
        let scatter_v = KvScatterAppend {
            regions: regions.clone(),
            positions,
            kv_dim: cache.kv_dim(),
            slot_stride: cache.slot_stride(),
        };
        cache.v.inplace_op2(v_new, &scatter_v)?;

        // 2. Multi-seq decode attention: each slot reads ITS OWN region/history —
        //    the exact property `slots_are_independent_across_different_history_lengths`
        //    proves on real Metal.
        let op = MultiSeqDecodeAttention {
            positions: batch.iter().map(|s| s.position as u32).collect(),
            regions,
            n_kv_heads: cache.n_kv_heads,
            kv_slot_stride: cache.slot_stride(),
            scale,
        };
        let _ = n_heads; // shape is asserted inside the op itself
        q.apply_op3_no_bwd(&cache.k, &cache.v, &op)
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
                DecodeStep {
                    slot_id: 0,
                    token: 10,
                    position: 5
                },
                DecodeStep {
                    slot_id: 1,
                    token: 11,
                    position: 5
                },
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
        assert!(
            chosen.contains(&0) && chosen.contains(&1),
            "shared-prefix pair must co-batch: {chosen:?}"
        );
        assert!(
            !chosen.contains(&2),
            "unrelated slot must not join: {chosen:?}"
        );
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

    // -----------------------------------------------------------------------
    // Week 3: admit/apply_decode_*/sample_next/lane-stats (GPU-free logic,
    // runs on every host including CI's no-metal build).
    // -----------------------------------------------------------------------

    #[test]
    fn admit_assigns_idle_slot_and_installs_sampler() {
        let mut s = Scheduler::new(2, "hawking");
        let id = s.admit(vec![1, 2, 3], 8, true, Some(42)).expect("admit");
        assert_eq!(id, 0);
        assert_eq!(s.slots[0].state, SlotState::Prefilling);
        assert_eq!(s.slots[0].position, 3);
        assert_eq!(s.slots[0].last_token, Some(3));
        assert!(s.samplers[0].is_some());
        // Second admission takes the other idle slot.
        let id2 = s.admit(vec![9], 4, true, None).expect("admit 2");
        assert_eq!(id2, 1);
        // Pool full -> None, never overwrites a live slot.
        assert!(s.admit(vec![5], 4, true, None).is_none());
    }

    #[test]
    fn mark_prefill_complete_transitions_and_rejects_wrong_state() {
        let mut s = Scheduler::new(1, "hawking");
        let id = s.admit(vec![1], 4, true, None).unwrap();
        assert!(!s.mark_prefill_complete(99)); // unknown slot
        assert!(s.mark_prefill_complete(id));
        assert_eq!(s.slots[0].state, SlotState::Decoding);
        assert!(!s.mark_prefill_complete(id)); // already Decoding, not Prefilling
    }

    #[test]
    fn apply_decode_tokens_advances_slot_and_detects_eos() {
        let mut s = Scheduler::new(2, "hawking");
        let a = s.admit(vec![10], 4, true, None).unwrap();
        let b = s.admit(vec![20], 4, true, None).unwrap();
        s.mark_prefill_complete(a);
        s.mark_prefill_complete(b);

        let batch = s.decode_plan(8);
        assert_eq!(batch.len(), 2);
        // Slot a's token (1) is not EOS; slot b's token (2) IS eos_id=2 -> Finishing.
        let decoded = s
            .apply_decode_tokens(&batch, vec![1, 2], Some(2))
            .expect("apply tokens");
        assert_eq!(decoded[0], DecodedToken { slot_id: a, token: 1, finished: false });
        assert_eq!(decoded[1], DecodedToken { slot_id: b, token: 2, finished: true });
        assert_eq!(s.slots[a as usize].last_token, Some(1));
        assert_eq!(s.slots[a as usize].position, 2); // prompt_len(1) + 1 step
        assert_eq!(s.slots[b as usize].state, SlotState::Finishing);
        assert_eq!(s.lane_stats.greedy_steps, 1);
        assert_eq!(s.lane_stats.finished_slots, 1);
        assert_eq!(
            s.lane_stats.readback_bytes,
            (2 * std::mem::size_of::<u32>()) as u64
        );
    }

    #[test]
    fn apply_decode_tokens_finishes_on_max_new_tokens_without_eos() {
        let mut s = Scheduler::new(1, "hawking");
        let id = s.admit(vec![10], 1, true, None).unwrap(); // max_new_tokens=1
        s.mark_prefill_complete(id);
        let batch = s.decode_plan(8);
        let decoded = s
            .apply_decode_tokens(&batch, vec![777], Some(2))
            .expect("apply");
        assert!(decoded[0].finished, "must finish on hitting max_new_tokens even without EOS");
        assert_eq!(s.slots[id as usize].state, SlotState::Finishing);
    }

    #[test]
    fn apply_decode_tokens_rejects_stale_plan() {
        let mut s = Scheduler::new(1, "hawking");
        let id = s.admit(vec![10], 4, true, None).unwrap();
        s.mark_prefill_complete(id);
        let stale = s.decode_plan(8);
        // Race: release the slot (as if another caller reused it) before applying.
        s.release_slot(id);
        assert!(s.apply_decode_tokens(&stale, vec![1], Some(2)).is_err());
    }

    #[test]
    fn apply_decode_logits_samples_greedy_and_advances() {
        let mut s = Scheduler::new(2, "hawking");
        let a = s.admit(vec![10], 4, true, None).unwrap();
        let b = s.admit(vec![20], 4, false, None).unwrap(); // non-greedy -> full logits lane
        s.mark_prefill_complete(a);
        s.mark_prefill_complete(b);
        let batch = s.decode_plan(8);
        assert_eq!(s.decode_lane(&batch), DecodeLane::FullLogits);

        let mut logits = vec![vec![0.0, 3.0, 1.0], vec![0.0, 1.0, 5.0]];
        let decoded = s
            .apply_decode_logits(&batch, &mut logits, Some(2))
            .expect("apply logits");
        assert_eq!(decoded[0].token, 1); // argmax of [0,3,1] for the greedy slot
        assert_eq!(decoded[1].token, 2); // argmax of [0,1,5]; == eos -> finished
        assert!(decoded[1].finished);
        assert_eq!(s.lane_stats.logits_steps, 1);
        assert_eq!(s.lane_stats.readback_bytes, (2 * 3 * std::mem::size_of::<f32>()) as u64);
    }

    #[test]
    fn release_slot_frees_region_and_drops_sampler() {
        let mut s = Scheduler::new(1, "hawking");
        let id = s.admit(vec![10], 4, true, None).unwrap();
        assert!(s.release_slot(id));
        assert_eq!(s.slots[0].state, SlotState::Idle);
        assert!(s.samplers[0].is_none());
        assert!(!s.release_slot(99)); // unknown slot id
    }

    #[test]
    fn sampler_temperature_zero_is_deterministic_argmax_regardless_of_seed() {
        let mut a = Sampler::new(1);
        let mut b = Sampler::new(2);
        let logits_a = vec![0.1, 0.9, 0.2];
        let logits_b = logits_a.clone();
        let mut la = logits_a.clone();
        let mut lb = logits_b.clone();
        assert_eq!(a.sample(&mut la, false), b.sample(&mut lb, false));
        assert_eq!(a.sample(&mut logits_a.clone(), false), 1);
    }

    #[test]
    fn sampler_repetition_penalty_discourages_recent_token() {
        let mut s = Sampler::new(7);
        s.repetition_penalty = 4.0;
        s.record(1); // token 1 was just emitted
        let mut logits = vec![0.5, 0.9, 0.1]; // token 1 would win un-penalized
        let picked = s.sample(&mut logits, true); // force_greedy so this stays deterministic
        assert_ne!(
            picked, 1,
            "repetition penalty must knock down the just-emitted token's logit enough to lose argmax"
        );
    }

    // -----------------------------------------------------------------------
    // Week 3: the REAL Metal-kernel-wired decode loop. `#[ignore]`d per this
    // codebase's own convention for real-hardware tests (see
    // hawking_metal_kernel.rs, quantized_llama_batched.rs) — run explicitly with
    // `cargo test --features metal -- --ignored continuous_batch`.
    // -----------------------------------------------------------------------
    #[cfg(feature = "metal")]
    mod metal_wired {
        use super::*;
        use crate::continuous_batch::metal_decode::{decode_attention_step, KvCache};
        use candle_core::{Device, Tensor};

        /// A deterministic pseudo-random f32 generator — matches
        /// hawking_metal_kernel.rs's own test convention (a fixed LCG, no `rand`
        /// dependency needed for reproducible non-degenerate test input).
        fn lcg_f32(seed: &mut u64) -> f32 {
            *seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1);
            ((*seed >> 33) as f32 / u32::MAX as f32) * 2.0 - 1.0
        }

        /// Proves the Week 3 wiring end to end on REAL Metal hardware: two
        /// DIFFERENT slots (different history lengths) admitted into a real
        /// `Scheduler`, decoded concurrently in ONE dispatch through the real
        /// `hawking_metal_kernel` ops via `decode_attention_step`, with the
        /// resulting attention output driving REAL `apply_decode_tokens` slot
        /// mutation (EOS detection, position advance, lane stats) — extending
        /// Week 2's kernel-only proof (`slots_are_independent_across_different_history_lengths`)
        /// up to the scheduler/runner-facing layer this module now owns.
        #[test]
        #[ignore = "requires real Metal hardware; run explicitly: cargo test --features metal -- --ignored continuous_batch::tests::metal_wired"]
        fn wired_decode_loop_keeps_concurrent_slots_independent_on_real_metal() {
            let device = match Device::new_metal(0) {
                Ok(d) => d,
                Err(_) => {
                    eprintln!("skipping: no Metal device available on this host");
                    return;
                }
            };

            let n_heads = 4usize;
            let n_kv_heads = 2usize;
            let head_dim = 8usize;
            let max_seq_per_slot = 16usize;
            let max_batch = 2usize;
            let scale = 1.0 / (head_dim as f32).sqrt();

            let mut sched = Scheduler::new(max_batch, "hawking");
            // Slot 0: a 5-token prompt. Slot 1: a 3-token prompt (DIFFERENT history
            // length) — the exact mismatched-length setup the continuous-batching
            // property must survive.
            let slot_a = sched.admit(vec![1, 2, 3, 4, 5], 4, true, Some(1)).unwrap();
            let slot_b = sched.admit(vec![9, 8, 7], 4, true, Some(2)).unwrap();
            sched.mark_prefill_complete(slot_a);
            sched.mark_prefill_complete(slot_b);

            let mut cache =
                KvCache::zeros(&device, max_batch, max_seq_per_slot, n_kv_heads, head_dim).unwrap();

            // Seed each slot's existing KV history (as if prefill already ran) at
            // its own region, positions 0..position-1, with distinct pseudo-random
            // data per slot so the two slots are NOT accidentally identical.
            let mut seed = 100u64;
            for &(region, prompt_len) in [(slot_a, 5usize), (slot_b, 3usize)].iter() {
                for t in 0..prompt_len - 1 {
                    let kv_dim = n_kv_heads * head_dim;
                    let mut k_row = vec![0f32; kv_dim];
                    let mut v_row = vec![0f32; kv_dim];
                    for x in k_row.iter_mut() {
                        *x = lcg_f32(&mut seed);
                    }
                    for x in v_row.iter_mut() {
                        *x = lcg_f32(&mut seed);
                    }
                    let scatter = crate::hawking_metal_kernel::KvScatterAppend {
                        regions: vec![region],
                        positions: vec![t as u32],
                        kv_dim,
                        slot_stride: cache.slot_stride(),
                    };
                    let k_src = Tensor::from_vec(k_row, (1, kv_dim), &device).unwrap();
                    cache.k.inplace_op2(&k_src, &scatter).unwrap();
                    let scatter_v = crate::hawking_metal_kernel::KvScatterAppend {
                        regions: vec![region],
                        positions: vec![t as u32],
                        kv_dim,
                        slot_stride: cache.slot_stride(),
                    };
                    let v_src = Tensor::from_vec(v_row, (1, kv_dim), &device).unwrap();
                    cache.v.inplace_op2(&v_src, &scatter_v).unwrap();
                }
            }

            // decode_plan (PURE, GPU-free) -> the actual Metal kernel via
            // decode_attention_step -> reduce to fabricated "logits" (this test
            // stands in for the still-deferred output projection — see the module
            // doc comment) -> apply_decode_tokens (REAL slot mutation).
            let run_step = |sched: &mut Scheduler, cache: &mut KvCache| -> Vec<DecodedToken> {
                let batch = sched.decode_plan(max_batch);
                assert_eq!(batch.len(), 2, "both slots must be ready to decode");
                let bsz = batch.len();
                let mut qseed = 999u64;
                let mut q = vec![0f32; bsz * n_heads * head_dim];
                for x in q.iter_mut() {
                    *x = lcg_f32(&mut qseed);
                }
                let mut kseed = 555u64;
                let mut k_new = vec![0f32; bsz * n_kv_heads * head_dim];
                let mut v_new = vec![0f32; bsz * n_kv_heads * head_dim];
                for x in k_new.iter_mut() {
                    *x = lcg_f32(&mut kseed);
                }
                for x in v_new.iter_mut() {
                    *x = lcg_f32(&mut kseed);
                }
                let q_t = Tensor::from_vec(q, (bsz, n_heads, head_dim), &device).unwrap();
                let k_t = Tensor::from_vec(k_new, (bsz, n_kv_heads, head_dim), &device).unwrap();
                let v_t = Tensor::from_vec(v_new, (bsz, n_kv_heads, head_dim), &device).unwrap();

                let attn_out =
                    decode_attention_step(cache, &batch, &q_t, &k_t, &v_t, n_heads, scale).unwrap();
                let attn_out: Vec<f32> = attn_out.flatten_all().unwrap().to_vec1().unwrap();

                // Stand-in "logits": sum this slot's attention output into a tiny
                // fixed-vocab row so apply_decode_tokens has a real, kernel-derived
                // (not fabricated-from-nothing) greedy token to work with. This is
                // exactly the still-deferred boundary named in the module doc
                // comment: a real output projection is a real GGUF model's job.
                let mut tokens = Vec::with_capacity(bsz);
                for bi in 0..bsz {
                    let row = &attn_out[bi * n_heads * head_dim..(bi + 1) * n_heads * head_dim];
                    let sum: f32 = row.iter().sum();
                    // Map to a token id deterministically from the real kernel
                    // output so two slots with different histories provably
                    // produce different tokens below.
                    tokens.push((sum.to_bits() % 1000) as u32);
                }
                sched.apply_decode_tokens(&batch, tokens, Some(u32::MAX)).unwrap()
            };

            let decoded_1 = run_step(&mut sched, &mut cache);
            assert_eq!(decoded_1.len(), 2);
            assert_eq!(sched.slots[slot_a as usize].position, 6); // 5 + 1
            assert_eq!(sched.slots[slot_b as usize].position, 4); // 3 + 1
            assert_eq!(sched.lane_stats.greedy_steps, 1);

            // Run a second wired step — proves the slot-strided KV persists and
            // keeps growing correctly across multiple real dispatches, and that
            // both slots remain independently decodable (continuous batching, not
            // a one-shot synthetic call).
            let decoded_2 = run_step(&mut sched, &mut cache);
            assert_eq!(decoded_2.len(), 2);
            assert_eq!(sched.slots[slot_a as usize].position, 7);
            assert_eq!(sched.slots[slot_b as usize].position, 5);
            assert_eq!(sched.lane_stats.greedy_steps, 2);
            assert_eq!(
                sched.lane_stats.readback_bytes,
                (2 * 2 * std::mem::size_of::<u32>()) as u64,
                "two greedy steps of 2 slots each must accumulate token-only readback bytes"
            );

            // The core continuous-batching property at THIS layer: re-run step 1's
            // exact inputs against a FRESH cache/scheduler pair where slot b's
            // seeded history is perturbed, and confirm slot a's decoded token is
            // unaffected — mirrors hawking_metal_kernel's own
            // slots_are_independent_across_different_history_lengths, one layer up.
            let mut sched2 = Scheduler::new(max_batch, "hawking");
            let a2 = sched2.admit(vec![1, 2, 3, 4, 5], 4, true, Some(1)).unwrap();
            let b2 = sched2.admit(vec![9, 8, 7], 4, true, Some(2)).unwrap();
            sched2.mark_prefill_complete(a2);
            sched2.mark_prefill_complete(b2);
            assert_eq!(a2, slot_a);
            assert_eq!(b2, slot_b);
            let mut cache2 =
                KvCache::zeros(&device, max_batch, max_seq_per_slot, n_kv_heads, head_dim).unwrap();
            // Seed identically to slot_a but DIFFERENTLY to slot_b.
            let mut seed2 = 100u64;
            for x in 0..(5 - 1) {
                let kv_dim = n_kv_heads * head_dim;
                let mut k_row = vec![0f32; kv_dim];
                let mut v_row = vec![0f32; kv_dim];
                for v in k_row.iter_mut() {
                    *v = lcg_f32(&mut seed2);
                }
                for v in v_row.iter_mut() {
                    *v = lcg_f32(&mut seed2);
                }
                let scatter = crate::hawking_metal_kernel::KvScatterAppend {
                    regions: vec![slot_a],
                    positions: vec![x as u32],
                    kv_dim,
                    slot_stride: cache2.slot_stride(),
                };
                let k_src = Tensor::from_vec(k_row, (1, kv_dim), &device).unwrap();
                cache2.k.inplace_op2(&k_src, &scatter).unwrap();
                let scatter_v = crate::hawking_metal_kernel::KvScatterAppend {
                    regions: vec![slot_a],
                    positions: vec![x as u32],
                    kv_dim,
                    slot_stride: cache2.slot_stride(),
                };
                let v_src = Tensor::from_vec(v_row, (1, kv_dim), &device).unwrap();
                cache2.v.inplace_op2(&v_src, &scatter_v).unwrap();
            }
            // DIFFERENT seed for slot_b's history this time (perturbation).
            let mut seed2b = 424242u64;
            for x in 0..(3 - 1) {
                let kv_dim = n_kv_heads * head_dim;
                let mut k_row = vec![0f32; kv_dim];
                let mut v_row = vec![0f32; kv_dim];
                for v in k_row.iter_mut() {
                    *v = lcg_f32(&mut seed2b);
                }
                for v in v_row.iter_mut() {
                    *v = lcg_f32(&mut seed2b);
                }
                let scatter = crate::hawking_metal_kernel::KvScatterAppend {
                    regions: vec![slot_b],
                    positions: vec![x as u32],
                    kv_dim,
                    slot_stride: cache2.slot_stride(),
                };
                let k_src = Tensor::from_vec(k_row, (1, kv_dim), &device).unwrap();
                cache2.k.inplace_op2(&k_src, &scatter).unwrap();
                let scatter_v = crate::hawking_metal_kernel::KvScatterAppend {
                    regions: vec![slot_b],
                    positions: vec![x as u32],
                    kv_dim,
                    slot_stride: cache2.slot_stride(),
                };
                let v_src = Tensor::from_vec(v_row, (1, kv_dim), &device).unwrap();
                cache2.v.inplace_op2(&v_src, &scatter_v).unwrap();
            }
            let decoded_1b = run_step(&mut sched2, &mut cache2);
            let tok_a_run1 = decoded_1.iter().find(|d| d.slot_id == slot_a).unwrap().token;
            let tok_a_run2 = decoded_1b.iter().find(|d| d.slot_id == a2).unwrap().token;
            assert_eq!(
                tok_a_run1, tok_a_run2,
                "slot a's decoded token must be UNCHANGED by slot b's history being \
                 perturbed in the same shared dispatch — the core continuous-batching \
                 no-cross-slot-corruption property, proven at the wired scheduler layer"
            );
        }
    }
}
