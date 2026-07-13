//! Host-side paged KV ownership and prefix-cache foundation.
//!
//! This module deliberately owns no tensors.  It is the authoritative host
//! model for fixed-size physical blocks and emits ordered [`DeviceMutation`]s
//! that a Metal/CUDA bridge can apply before atomically committing the matching
//! host mutation.  Published blocks are immutable: appending to a partial tail
//! always copies it into a reserved block.  Consequently prefix sharing is
//! alias-safe and aborting a failed device operation cannot corrupt live KV.

#![allow(dead_code)] // Device integration is intentionally a later layer.

use std::collections::HashMap;
use std::fmt;

use sha2::{Digest, Sha256};

/// Identifies one occupancy of a reusable sequence slot (ABA safe).
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct SequenceHandle {
    slot: u32,
    epoch: u64,
}

impl SequenceHandle {
    pub fn slot(self) -> u32 {
        self.slot
    }

    pub fn epoch(self) -> u64 {
        self.epoch
    }
}

/// Identifies one occupancy of a reusable physical block (ABA safe).
#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct BlockHandle {
    id: u32,
    generation: u64,
}

impl BlockHandle {
    pub fn id(self) -> u32 {
        self.id
    }

    pub fn generation(self) -> u64 {
        self.generation
    }
}

/// All fields are digests of canonical, pinned identities.  Keeping the five
/// dimensions distinct prevents accidental cache reuse across a tokenizer,
/// RoPE scaling, quantization, or engine build change.
#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct PrefixNamespace {
    pub model: [u8; 32],
    pub tokenizer: [u8; 32],
    pub rope: [u8; 32],
    pub quantization: [u8; 32],
    pub engine_build: [u8; 32],
}

/// Callers must choose sharing explicitly.  Tenant identifiers are accepted
/// only as opaque digests, so the cache never retains a raw account name/key.
#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub enum TenantScope {
    /// Safe only for public prompts under an identical trust boundary.
    PublicShared,
    /// Prevents lookup or hash equality across tenants.
    Tenant([u8; 32]),
}

#[derive(Clone, Debug)]
pub struct PagedKvConfig {
    pub block_size: usize,
    pub physical_blocks: usize,
    pub sequence_slots: usize,
    pub max_cache_entries: usize,
}

impl PagedKvConfig {
    pub fn validate(&self) -> Result<(), KvError> {
        if self.block_size == 0 {
            return Err(KvError::InvalidConfig("block_size must be nonzero"));
        }
        if self.physical_blocks == 0 {
            return Err(KvError::InvalidConfig("physical_blocks must be nonzero"));
        }
        if self.physical_blocks > u32::MAX as usize || self.sequence_slots > u32::MAX as usize {
            return Err(KvError::InvalidConfig(
                "capacity exceeds typed handle range",
            ));
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum KvError {
    InvalidConfig(&'static str),
    SequenceCapacity,
    OutOfBlocks { needed: usize, available: usize },
    StaleSequence,
    StaleBlock,
    MutationPending,
    StaleMutation,
    GenerationExhausted,
    InvalidPrefix(&'static str),
    CacheCapacityPinned,
    BlockNotPinned,
    Invariant(String),
}

impl fmt::Display for KvError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidConfig(s) => write!(f, "invalid paged-KV config: {s}"),
            Self::SequenceCapacity => write!(f, "no free sequence slot"),
            Self::OutOfBlocks { needed, available } => {
                write!(
                    f,
                    "paged-KV capacity exhausted: need {needed}, have {available}"
                )
            }
            Self::StaleSequence => write!(f, "stale sequence handle"),
            Self::StaleBlock => write!(f, "stale block handle"),
            Self::MutationPending => write!(f, "sequence already has a prepared mutation"),
            Self::StaleMutation => write!(f, "stale prepared mutation"),
            Self::GenerationExhausted => write!(f, "handle generation exhausted"),
            Self::InvalidPrefix(s) => write!(f, "invalid cache prefix: {s}"),
            Self::CacheCapacityPinned => write!(f, "all cache entries are pinned"),
            Self::BlockNotPinned => write!(f, "block pin underflow"),
            Self::Invariant(s) => write!(f, "paged-KV invariant failed: {s}"),
        }
    }
}

impl std::error::Error for KvError {}

/// Ordered device work. Allocate/copy/write are performed while the plan is
/// reserved; `FreeAfterCommit` is delayed until [`PagedKv::commit_append`]
/// succeeds. On device failure the caller invokes `abort_append`, leaving every
/// live logical table untouched and releasing all reservations.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum DeviceMutation {
    Allocate {
        block: BlockHandle,
    },
    Copy {
        source: BlockHandle,
        destination: BlockHandle,
        token_count: usize,
    },
    Write {
        block: BlockHandle,
        block_offset: usize,
        logical_offset: usize,
        tokens: Vec<u32>,
    },
    FreeAfterCommit {
        block: BlockHandle,
    },
}

/// Linear capability for a prepared append. It is intentionally not `Clone`.
/// Every value must be consumed by commit, abort, or rollback.
#[must_use = "a prepared append must be committed or aborted"]
#[derive(Debug)]
pub struct PreparedAppend {
    id: u64,
    sequence: SequenceHandle,
    events: Vec<DeviceMutation>,
    new_token_len: usize,
}

impl PreparedAppend {
    pub fn sequence(&self) -> SequenceHandle {
        self.sequence
    }

    pub fn events(&self) -> &[DeviceMutation] {
        &self.events
    }

    pub fn new_token_len(&self) -> usize {
        self.new_token_len
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CommitReceipt {
    pub sequence: SequenceHandle,
    pub token_len: usize,
    /// Old physical allocations that became unreferenced at commit time.
    pub freed_blocks: Vec<BlockHandle>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PrefixHit {
    pub matched_tokens: usize,
    pub blocks: usize,
    pub digest: [u8; 32],
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SequenceView {
    pub handle: SequenceHandle,
    pub token_len: usize,
    pub blocks: Vec<BlockHandle>,
}

#[derive(Clone, Debug)]
enum BlockState {
    Free,
    Reserved(u64),
    Live,
}

#[derive(Clone, Debug)]
struct PhysicalBlock {
    generation: u64,
    state: BlockState,
    tokens: Vec<u32>,
    sequence_refs: usize,
    cache_refs: usize,
    device_pins: usize,
}

impl PhysicalBlock {
    fn free() -> Self {
        Self {
            generation: 0,
            state: BlockState::Free,
            tokens: Vec::new(),
            sequence_refs: 0,
            cache_refs: 0,
            device_pins: 0,
        }
    }

    fn pinned(&self) -> bool {
        self.sequence_refs != 0 || self.device_pins != 0
    }
}

#[derive(Clone, Debug)]
struct Sequence {
    blocks: Vec<BlockHandle>,
    token_len: usize,
    pending: Option<u64>,
}

#[derive(Clone, Debug)]
struct SequenceSlot {
    epoch: u64,
    value: Option<Sequence>,
}

#[derive(Clone, Debug)]
struct PendingAppend {
    sequence: SequenceHandle,
    old_blocks: Vec<BlockHandle>,
    old_token_len: usize,
    new_blocks: Vec<BlockHandle>,
    new_token_len: usize,
    reserved: Vec<BlockHandle>,
}

#[derive(Clone, Debug)]
struct CacheEntry {
    id: u64,
    digest: [u8; 32],
    namespace: PrefixNamespace,
    scope: TenantScope,
    exact_tokens: Vec<u32>,
    blocks: Vec<BlockHandle>,
    last_used: u64,
}

/// Fixed-capacity allocator. Mutation requires `&mut self`; wrapping it in a
/// `Mutex` therefore gives callers a simple concurrency boundary without any
/// internal lock-order or ABA hazards.
#[derive(Debug)]
pub struct PagedKv {
    cfg: PagedKvConfig,
    blocks: Vec<PhysicalBlock>,
    sequences: Vec<SequenceSlot>,
    pending: HashMap<u64, PendingAppend>,
    next_plan: u64,
    cache: HashMap<u64, CacheEntry>,
    cache_buckets: HashMap<[u8; 32], Vec<u64>>,
    next_cache_id: u64,
    lru_clock: u64,
    #[cfg(test)]
    forced_digest: Option<[u8; 32]>,
}

impl PagedKv {
    pub fn new(cfg: PagedKvConfig) -> Result<Self, KvError> {
        cfg.validate()?;
        Ok(Self {
            blocks: (0..cfg.physical_blocks)
                .map(|_| PhysicalBlock::free())
                .collect(),
            sequences: (0..cfg.sequence_slots)
                .map(|_| SequenceSlot {
                    epoch: 0,
                    value: None,
                })
                .collect(),
            cfg,
            pending: HashMap::new(),
            next_plan: 1,
            cache: HashMap::new(),
            cache_buckets: HashMap::new(),
            next_cache_id: 1,
            lru_clock: 1,
            #[cfg(test)]
            forced_digest: None,
        })
    }

    pub fn block_size(&self) -> usize {
        self.cfg.block_size
    }

    pub fn free_block_count(&self) -> usize {
        self.blocks
            .iter()
            .filter(|b| matches!(b.state, BlockState::Free))
            .count()
    }

    pub fn live_sequence_count(&self) -> usize {
        self.sequences.iter().filter(|s| s.value.is_some()).count()
    }

    /// Admits an empty logical sequence. Prefill uses the same two-phase append
    /// path as decode, so admission itself never needs a second ownership model.
    pub fn admit_empty(&mut self) -> Result<SequenceHandle, KvError> {
        let slot = self
            .sequences
            .iter()
            .position(|s| s.value.is_none())
            .ok_or(KvError::SequenceCapacity)?;
        let epoch = self.sequences[slot]
            .epoch
            .checked_add(1)
            .ok_or(KvError::GenerationExhausted)?;
        self.sequences[slot].epoch = epoch;
        self.sequences[slot].value = Some(Sequence {
            blocks: Vec::new(),
            token_len: 0,
            pending: None,
        });
        Ok(SequenceHandle {
            slot: slot as u32,
            epoch,
        })
    }

    /// Convenience admission for host-only callers. Device-backed callers
    /// should use `admit_empty` + `prepare_append` and commit after device success.
    pub fn admit_tokens(&mut self, tokens: &[u32]) -> Result<SequenceHandle, KvError> {
        let handle = self.admit_empty()?;
        let result = self
            .prepare_append(handle, tokens)
            .and_then(|p| self.commit_append(p).map(|_| handle));
        if result.is_err() {
            let _ = self.release(handle);
        }
        result
    }

    pub fn sequence_view(&self, handle: SequenceHandle) -> Result<SequenceView, KvError> {
        let seq = self.sequence(handle)?;
        Ok(SequenceView {
            handle,
            token_len: seq.token_len,
            blocks: seq.blocks.clone(),
        })
    }

    pub fn sequence_tokens(&self, handle: SequenceHandle) -> Result<Vec<u32>, KvError> {
        let seq = self.sequence(handle)?;
        let mut out = Vec::with_capacity(seq.token_len);
        for h in &seq.blocks {
            out.extend_from_slice(&self.block(*h)?.tokens);
        }
        out.truncate(seq.token_len);
        Ok(out)
    }

    /// Reserves every destination block before returning a plan. The sequence's
    /// current table/refcounts remain untouched. Capacity is checked (including
    /// a dry-run of LRU reclamation) before any cache entry is evicted, so OOM is
    /// failure-atomic for both the sequence and prefix cache.
    pub fn prepare_append(
        &mut self,
        handle: SequenceHandle,
        tokens: &[u32],
    ) -> Result<PreparedAppend, KvError> {
        let (old_blocks, old_len) = {
            let seq = self.sequence(handle)?;
            if seq.pending.is_some() {
                return Err(KvError::MutationPending);
            }
            (seq.blocks.clone(), seq.token_len)
        };
        // This must precede cache eviction and physical reservation: even the
        // theoretical usize overflow path is failure-atomic.
        let new_token_len = old_len
            .checked_add(tokens.len())
            .ok_or(KvError::InvalidPrefix("token length overflow"))?;
        let plan_id = self.next_plan;
        self.next_plan = self
            .next_plan
            .checked_add(1)
            .ok_or(KvError::GenerationExhausted)?;

        let tail_len = old_len % self.cfg.block_size;
        let first_capacity = if tail_len == 0 {
            0
        } else {
            self.cfg.block_size - tail_len
        };
        let after_tail = tokens.len().saturating_sub(first_capacity);
        let needed = usize::from(!tokens.is_empty() && tail_len != 0)
            + after_tail.div_ceil(self.cfg.block_size);
        self.ensure_free_blocks(needed)?;

        let mut new_blocks = old_blocks.clone();
        let mut reserved = Vec::with_capacity(needed);
        let mut events = Vec::new();
        let mut consumed = 0;
        let mut free_after_commit = None;

        if !tokens.is_empty() && tail_len != 0 {
            let source = *old_blocks.last().expect("nonzero tail has a block");
            let take = first_capacity.min(tokens.len());
            let destination = self.reserve_block(plan_id)?;
            let mut combined = self.block(source)?.tokens.clone();
            combined.extend_from_slice(&tokens[..take]);
            self.reserved_block_mut(destination, plan_id)?.tokens = combined;
            *new_blocks.last_mut().expect("tail exists") = destination;
            reserved.push(destination);
            events.push(DeviceMutation::Allocate { block: destination });
            events.push(DeviceMutation::Copy {
                source,
                destination,
                token_count: tail_len,
            });
            events.push(DeviceMutation::Write {
                block: destination,
                block_offset: tail_len,
                logical_offset: old_len,
                tokens: tokens[..take].to_vec(),
            });
            if self.will_free_after_sequence_detach(source)? {
                free_after_commit = Some(source);
            }
            consumed = take;
        }

        while consumed < tokens.len() {
            let take = (tokens.len() - consumed).min(self.cfg.block_size);
            let destination = self.reserve_block(plan_id)?;
            self.reserved_block_mut(destination, plan_id)?
                .tokens
                .extend_from_slice(&tokens[consumed..consumed + take]);
            new_blocks.push(destination);
            reserved.push(destination);
            events.push(DeviceMutation::Allocate { block: destination });
            events.push(DeviceMutation::Write {
                block: destination,
                block_offset: 0,
                logical_offset: old_len + consumed,
                tokens: tokens[consumed..consumed + take].to_vec(),
            });
            consumed += take;
        }
        if let Some(block) = free_after_commit {
            events.push(DeviceMutation::FreeAfterCommit { block });
        }

        self.sequence_mut(handle)?.pending = Some(plan_id);
        self.pending.insert(
            plan_id,
            PendingAppend {
                sequence: handle,
                old_blocks,
                old_token_len: old_len,
                new_blocks,
                new_token_len,
                reserved,
            },
        );
        Ok(PreparedAppend {
            id: plan_id,
            sequence: handle,
            events,
            new_token_len,
        })
    }

    /// Publishes a prepared table after device success. No fallible work occurs
    /// after validation, giving the bridge a clean device-success -> host-commit
    /// pairing without leaked reservations.
    pub fn commit_append(&mut self, plan: PreparedAppend) -> Result<CommitReceipt, KvError> {
        self.validate_plan(&plan)?;
        let pending = self
            .pending
            .remove(&plan.id)
            .ok_or(KvError::StaleMutation)?;

        let old_tail = (pending.old_token_len % self.cfg.block_size != 0)
            .then(|| pending.old_blocks.last().copied())
            .flatten()
            .filter(|old| pending.new_blocks.get(pending.old_blocks.len() - 1) != Some(old));
        for h in &pending.reserved {
            let b = self.reserved_block_mut(*h, plan.id)?;
            b.state = BlockState::Live;
            b.sequence_refs = 1;
        }
        {
            let seq = self.sequence_mut(plan.sequence)?;
            seq.blocks = pending.new_blocks;
            seq.token_len = pending.new_token_len;
            seq.pending = None;
        }
        let mut freed_blocks = Vec::new();
        if let Some(old) = old_tail {
            self.drop_sequence_ref(old)?;
            if self.is_same_generation_free(old) {
                freed_blocks.push(old);
            }
        }
        Ok(CommitReceipt {
            sequence: plan.sequence,
            token_len: plan.new_token_len,
            freed_blocks,
        })
    }

    /// Aborts device-failed work. All reserved generations become stale and the
    /// published logical table is byte-for-byte unchanged.
    pub fn abort_append(&mut self, plan: PreparedAppend) -> Result<(), KvError> {
        self.validate_plan(&plan)?;
        let pending = self
            .pending
            .remove(&plan.id)
            .ok_or(KvError::StaleMutation)?;
        for h in pending.reserved {
            self.free_reserved(h, plan.id)?;
        }
        self.sequence_mut(plan.sequence)?.pending = None;
        Ok(())
    }

    /// Transactional spelling for callers that treat device failure as rollback.
    pub fn rollback_append(&mut self, plan: PreparedAppend) -> Result<(), KvError> {
        self.abort_append(plan)
    }

    pub fn release(&mut self, handle: SequenceHandle) -> Result<Vec<BlockHandle>, KvError> {
        let slot = self.sequence_slot_index(handle)?;
        let seq = self.sequences[slot]
            .value
            .as_ref()
            .ok_or(KvError::StaleSequence)?;
        if seq.pending.is_some() {
            return Err(KvError::MutationPending);
        }
        let blocks = seq.blocks.clone();
        let mut required = HashMap::<BlockHandle, usize>::new();
        for h in &blocks {
            *required.entry(*h).or_default() += 1;
        }
        for (h, count) in required {
            if self.block(h)?.sequence_refs < count {
                return Err(KvError::Invariant("sequence ref underflow".into()));
            }
        }
        self.sequences[slot].value = None;
        let mut freed = Vec::new();
        for h in blocks {
            self.drop_sequence_ref(h)?;
            if self.is_same_generation_free(h) {
                freed.push(h);
            }
        }
        Ok(freed)
    }

    /// Pins a live generation while an asynchronous device command references it.
    pub fn pin_block(&mut self, handle: BlockHandle) -> Result<(), KvError> {
        let b = self.block_mut(handle)?;
        b.device_pins = b
            .device_pins
            .checked_add(1)
            .ok_or(KvError::GenerationExhausted)?;
        Ok(())
    }

    pub fn unpin_block(&mut self, handle: BlockHandle) -> Result<(), KvError> {
        let b = self.block_mut(handle)?;
        if b.device_pins == 0 {
            return Err(KvError::BlockNotPinned);
        }
        b.device_pins -= 1;
        self.maybe_free(handle)
    }

    /// Inserts an aligned full-block prefix. Cache ownership is an independent
    /// refcount; live sequences and device pins make the entry non-evictable.
    pub fn cache_prefix(
        &mut self,
        handle: SequenceHandle,
        prefix_tokens: usize,
        namespace: PrefixNamespace,
        scope: TenantScope,
    ) -> Result<[u8; 32], KvError> {
        let seq = self.sequence(handle)?;
        if prefix_tokens == 0 || prefix_tokens > seq.token_len {
            return Err(KvError::InvalidPrefix(
                "prefix must be nonempty and within sequence",
            ));
        }
        if !prefix_tokens.is_multiple_of(self.cfg.block_size) {
            return Err(KvError::InvalidPrefix(
                "only complete physical blocks are cacheable",
            ));
        }
        let block_count = prefix_tokens / self.cfg.block_size;
        let blocks = seq.blocks[..block_count].to_vec();
        let exact_tokens = self.sequence_tokens(handle)?[..prefix_tokens].to_vec();
        let digest = self.prefix_digest(&namespace, &scope, &exact_tokens);

        if let Some(existing) =
            self.find_exact_cache_entry(&namespace, &scope, &exact_tokens, digest)
        {
            self.touch_cache(existing)?;
            return Ok(digest);
        }
        let next_cache_id = self
            .next_cache_id
            .checked_add(1)
            .ok_or(KvError::GenerationExhausted)?;
        let next_lru = self
            .lru_clock
            .checked_add(1)
            .ok_or(KvError::GenerationExhausted)?;
        for h in &blocks {
            self.block(*h)?
                .cache_refs
                .checked_add(1)
                .ok_or(KvError::GenerationExhausted)?;
        }
        self.make_cache_entry_room()?;
        let id = self.next_cache_id;
        self.next_cache_id = next_cache_id;
        self.lru_clock = next_lru;
        for h in &blocks {
            self.block_mut(*h)?.cache_refs += 1;
        }
        self.cache.insert(
            id,
            CacheEntry {
                id,
                digest,
                namespace,
                scope,
                exact_tokens,
                blocks,
                last_used: self.lru_clock,
            },
        );
        self.cache_buckets.entry(digest).or_default().push(id);
        Ok(digest)
    }

    /// Admits from the longest exact-token-confirmed cached prefix, then appends
    /// the uncached suffix. Digest collisions can only add comparisons, never a hit.
    pub fn admit_cached(
        &mut self,
        tokens: &[u32],
        namespace: &PrefixNamespace,
        scope: &TenantScope,
    ) -> Result<(SequenceHandle, Option<PrefixHit>), KvError> {
        let entry_id = self.longest_cache_match(tokens, namespace, scope);
        let handle = self.admit_empty()?;
        let mut hit = None;
        let mut matched = 0;
        if let Some(id) = entry_id {
            if let Err(err) = self.touch_cache(id) {
                self.release(handle)?;
                return Err(err);
            }
            let entry = self
                .cache
                .get(&id)
                .ok_or_else(|| KvError::Invariant("cache lookup vanished".into()))?
                .clone();
            if entry.blocks.iter().any(|h| {
                self.block(*h)
                    .ok()
                    .and_then(|b| b.sequence_refs.checked_add(1))
                    .is_none()
            }) {
                self.release(handle)?;
                return Err(KvError::GenerationExhausted);
            }
            for h in &entry.blocks {
                self.block_mut(*h)?.sequence_refs += 1;
            }
            {
                let seq = self.sequence_mut(handle)?;
                seq.blocks = entry.blocks.clone();
                seq.token_len = entry.exact_tokens.len();
            }
            matched = entry.exact_tokens.len();
            hit = Some(PrefixHit {
                matched_tokens: matched,
                blocks: entry.blocks.len(),
                digest: entry.digest,
            });
        }
        let result = self
            .prepare_append(handle, &tokens[matched..])
            .and_then(|p| self.commit_append(p).map(|_| ()));
        if let Err(err) = result {
            let _ = self.release(handle);
            return Err(err);
        }
        Ok((handle, hit))
    }

    /// Evicts up to `count` oldest unpinned entries. Returns the actual count.
    pub fn evict_lru(&mut self, count: usize) -> Result<usize, KvError> {
        let ids = self.select_evictable_entries(count, None);
        let n = ids.len();
        for id in ids {
            self.remove_cache_entry(id)?;
        }
        Ok(n)
    }

    pub fn cache_entry_count(&self) -> usize {
        self.cache.len()
    }

    /// Full ownership audit, intended for debug gates and randomized tests.
    pub fn validate_invariants(&self) -> Result<(), KvError> {
        let mut expected_seq = vec![0usize; self.blocks.len()];
        let mut expected_cache = vec![0usize; self.blocks.len()];
        let mut expected_reserved = vec![None; self.blocks.len()];
        for (slot_id, slot) in self.sequences.iter().enumerate() {
            let Some(seq) = &slot.value else { continue };
            let expected_blocks = seq.token_len.div_ceil(self.cfg.block_size);
            if seq.blocks.len() != expected_blocks {
                return Err(KvError::Invariant(format!(
                    "sequence {slot_id} table length"
                )));
            }
            let mut local = HashMap::new();
            for (logical, h) in seq.blocks.iter().enumerate() {
                let b = self.block(*h)?;
                if local.insert(*h, ()).is_some() {
                    return Err(KvError::Invariant(format!(
                        "sequence {slot_id} aliases one block twice"
                    )));
                }
                if logical + 1 < seq.blocks.len() && b.tokens.len() != self.cfg.block_size {
                    return Err(KvError::Invariant(format!(
                        "sequence {slot_id} has partial interior block"
                    )));
                }
                expected_seq[h.id as usize] += 1;
            }
            if let Some(tail) = seq.blocks.last() {
                let remainder = seq.token_len % self.cfg.block_size;
                let expected_tail = if remainder == 0 {
                    self.cfg.block_size
                } else {
                    remainder
                };
                if self.block(*tail)?.tokens.len() != expected_tail {
                    return Err(KvError::Invariant(format!(
                        "sequence {slot_id} tail length"
                    )));
                }
            }
            if let Some(id) = seq.pending {
                let p = self
                    .pending
                    .get(&id)
                    .ok_or_else(|| KvError::Invariant("sequence references absent plan".into()))?;
                if p.sequence.slot as usize != slot_id || p.sequence.epoch != slot.epoch {
                    return Err(KvError::Invariant("pending plan/sequence mismatch".into()));
                }
            }
        }
        for entry in self.cache.values() {
            if entry.exact_tokens.len() % self.cfg.block_size != 0
                || entry.blocks.len() != entry.exact_tokens.len() / self.cfg.block_size
            {
                return Err(KvError::Invariant(format!(
                    "cache entry {} shape",
                    entry.id
                )));
            }
            if self.prefix_digest(&entry.namespace, &entry.scope, &entry.exact_tokens)
                != entry.digest
            {
                return Err(KvError::Invariant(format!(
                    "cache entry {} digest",
                    entry.id
                )));
            }
            let mut reconstructed = Vec::with_capacity(entry.exact_tokens.len());
            for h in &entry.blocks {
                let b = self.block(*h)?;
                if b.tokens.len() != self.cfg.block_size {
                    return Err(KvError::Invariant(format!(
                        "cache entry {} references partial block",
                        entry.id
                    )));
                }
                reconstructed.extend_from_slice(&b.tokens);
                expected_cache[h.id as usize] += 1;
            }
            if reconstructed != entry.exact_tokens {
                return Err(KvError::Invariant(format!(
                    "cache entry {} token/block mismatch",
                    entry.id
                )));
            }
            if !self
                .cache_buckets
                .get(&entry.digest)
                .is_some_and(|v| v.contains(&entry.id))
            {
                return Err(KvError::Invariant(format!(
                    "cache entry {} missing bucket",
                    entry.id
                )));
            }
        }
        for (plan_id, pending) in &self.pending {
            let seq = self.sequence(pending.sequence)?;
            if seq.pending != Some(*plan_id)
                || seq.blocks != pending.old_blocks
                || seq.token_len != pending.old_token_len
                || pending.new_blocks.len() != pending.new_token_len.div_ceil(self.cfg.block_size)
            {
                return Err(KvError::Invariant("pending plan snapshot mismatch".into()));
            }
            let mut seen = HashMap::new();
            let mut reserved_in_table = 0;
            for (logical, h) in pending.new_blocks.iter().enumerate() {
                if seen.insert(*h, ()).is_some() {
                    return Err(KvError::Invariant(
                        "pending table aliases one block twice".into(),
                    ));
                }
                let b = self.block_slot(*h)?;
                match b.state {
                    BlockState::Reserved(id) if id == *plan_id => reserved_in_table += 1,
                    BlockState::Live if pending.old_blocks.contains(h) => {}
                    _ => return Err(KvError::Invariant("pending table has foreign block".into())),
                }
                let expected_len = if logical + 1 == pending.new_blocks.len() {
                    let remainder = pending.new_token_len % self.cfg.block_size;
                    if remainder == 0 {
                        self.cfg.block_size
                    } else {
                        remainder
                    }
                } else {
                    self.cfg.block_size
                };
                if b.tokens.len() != expected_len {
                    return Err(KvError::Invariant("pending block shape mismatch".into()));
                }
            }
            if reserved_in_table != pending.reserved.len() {
                return Err(KvError::Invariant(
                    "pending reservation set mismatch".into(),
                ));
            }
            for h in &pending.reserved {
                let b = self.block_slot(*h)?;
                if !matches!(b.state, BlockState::Reserved(id) if id == *plan_id)
                    || !pending.new_blocks.contains(h)
                    || expected_reserved[h.id as usize].replace(*plan_id).is_some()
                {
                    return Err(KvError::Invariant("reservation owner mismatch".into()));
                }
            }
        }
        for (id, b) in self.blocks.iter().enumerate() {
            if b.tokens.len() > self.cfg.block_size {
                return Err(KvError::Invariant(format!("block {id} overflow")));
            }
            match b.state {
                BlockState::Free => {
                    if !b.tokens.is_empty()
                        || b.sequence_refs != 0
                        || b.cache_refs != 0
                        || b.device_pins != 0
                    {
                        return Err(KvError::Invariant(format!("dirty free block {id}")));
                    }
                }
                BlockState::Reserved(plan_id) => {
                    if b.sequence_refs != 0 || b.cache_refs != 0 || b.device_pins != 0 {
                        return Err(KvError::Invariant(format!(
                            "referenced reserved block {id}"
                        )));
                    }
                    if expected_reserved[id] != Some(plan_id) {
                        return Err(KvError::Invariant(format!("orphan reserved block {id}")));
                    }
                }
                BlockState::Live => {
                    if b.sequence_refs != expected_seq[id] || b.cache_refs != expected_cache[id] {
                        return Err(KvError::Invariant(format!("block {id} refcount mismatch")));
                    }
                    if b.sequence_refs + b.cache_refs + b.device_pins == 0 {
                        return Err(KvError::Invariant(format!("unowned live block {id}")));
                    }
                }
            }
        }
        Ok(())
    }

    fn sequence_slot_index(&self, h: SequenceHandle) -> Result<usize, KvError> {
        let slot = self
            .sequences
            .get(h.slot as usize)
            .ok_or(KvError::StaleSequence)?;
        if slot.epoch != h.epoch || slot.value.is_none() {
            return Err(KvError::StaleSequence);
        }
        Ok(h.slot as usize)
    }

    fn sequence(&self, h: SequenceHandle) -> Result<&Sequence, KvError> {
        let i = self.sequence_slot_index(h)?;
        self.sequences[i]
            .value
            .as_ref()
            .ok_or(KvError::StaleSequence)
    }

    fn sequence_mut(&mut self, h: SequenceHandle) -> Result<&mut Sequence, KvError> {
        let i = self.sequence_slot_index(h)?;
        self.sequences[i]
            .value
            .as_mut()
            .ok_or(KvError::StaleSequence)
    }

    fn block_slot(&self, h: BlockHandle) -> Result<&PhysicalBlock, KvError> {
        let b = self.blocks.get(h.id as usize).ok_or(KvError::StaleBlock)?;
        if b.generation != h.generation || matches!(b.state, BlockState::Free) {
            return Err(KvError::StaleBlock);
        }
        Ok(b)
    }

    fn block(&self, h: BlockHandle) -> Result<&PhysicalBlock, KvError> {
        let b = self.block_slot(h)?;
        if !matches!(b.state, BlockState::Live) {
            return Err(KvError::StaleBlock);
        }
        Ok(b)
    }

    fn block_mut(&mut self, h: BlockHandle) -> Result<&mut PhysicalBlock, KvError> {
        let b = self
            .blocks
            .get_mut(h.id as usize)
            .ok_or(KvError::StaleBlock)?;
        if b.generation != h.generation || !matches!(b.state, BlockState::Live) {
            return Err(KvError::StaleBlock);
        }
        Ok(b)
    }

    fn reserved_block_mut(
        &mut self,
        h: BlockHandle,
        plan: u64,
    ) -> Result<&mut PhysicalBlock, KvError> {
        let b = self
            .blocks
            .get_mut(h.id as usize)
            .ok_or(KvError::StaleBlock)?;
        if b.generation != h.generation
            || !matches!(b.state, BlockState::Reserved(id) if id == plan)
        {
            return Err(KvError::StaleMutation);
        }
        Ok(b)
    }

    fn reserve_block(&mut self, plan: u64) -> Result<BlockHandle, KvError> {
        let id = self
            .blocks
            .iter()
            .position(|b| matches!(b.state, BlockState::Free) && b.generation != u64::MAX)
            .ok_or(KvError::OutOfBlocks {
                needed: 1,
                available: 0,
            })?;
        let generation = self.blocks[id]
            .generation
            .checked_add(1)
            .ok_or(KvError::GenerationExhausted)?;
        self.blocks[id].generation = generation;
        self.blocks[id].state = BlockState::Reserved(plan);
        Ok(BlockHandle {
            id: id as u32,
            generation,
        })
    }

    fn free_reserved(&mut self, h: BlockHandle, plan: u64) -> Result<(), KvError> {
        let b = self.reserved_block_mut(h, plan)?;
        b.tokens.fill(0);
        b.tokens.clear();
        b.state = BlockState::Free;
        Ok(())
    }

    fn validate_plan(&self, plan: &PreparedAppend) -> Result<(), KvError> {
        let seq = self.sequence(plan.sequence)?;
        if seq.pending != Some(plan.id) {
            return Err(KvError::StaleMutation);
        }
        let p = self.pending.get(&plan.id).ok_or(KvError::StaleMutation)?;
        if p.sequence != plan.sequence || p.new_token_len != plan.new_token_len {
            return Err(KvError::StaleMutation);
        }
        if seq.blocks != p.old_blocks || seq.token_len != p.old_token_len {
            return Err(KvError::StaleMutation);
        }
        for h in &p.reserved {
            let b = self.block_slot(*h)?;
            if !matches!(b.state, BlockState::Reserved(id) if id == plan.id) {
                return Err(KvError::StaleMutation);
            }
        }
        Ok(())
    }

    fn will_free_after_sequence_detach(&self, h: BlockHandle) -> Result<bool, KvError> {
        let b = self.block(h)?;
        Ok(b.sequence_refs == 1 && b.cache_refs == 0 && b.device_pins == 0)
    }

    fn drop_sequence_ref(&mut self, h: BlockHandle) -> Result<(), KvError> {
        let b = self.block_mut(h)?;
        if b.sequence_refs == 0 {
            return Err(KvError::Invariant("sequence ref underflow".into()));
        }
        b.sequence_refs -= 1;
        self.maybe_free(h)
    }

    fn maybe_free(&mut self, h: BlockHandle) -> Result<(), KvError> {
        let b = self.block_mut(h)?;
        if b.sequence_refs == 0 && b.cache_refs == 0 && b.device_pins == 0 {
            b.tokens.fill(0);
            b.tokens.clear();
            b.state = BlockState::Free;
        }
        Ok(())
    }

    fn is_same_generation_free(&self, h: BlockHandle) -> bool {
        self.blocks
            .get(h.id as usize)
            .is_some_and(|b| b.generation == h.generation && matches!(b.state, BlockState::Free))
    }

    fn ensure_free_blocks(&mut self, needed: usize) -> Result<(), KvError> {
        let free = self
            .blocks
            .iter()
            .filter(|b| matches!(b.state, BlockState::Free) && b.generation != u64::MAX)
            .count();
        if free >= needed {
            return Ok(());
        }
        let deficit = needed - free;
        let mut virtual_refs: Vec<usize> = self.blocks.iter().map(|b| b.cache_refs).collect();
        let mut reclaim = 0;
        let mut selected = Vec::new();
        let mut candidates: Vec<_> = self.cache.values().map(|e| (e.last_used, e.id)).collect();
        candidates.sort_unstable();
        for (_, id) in candidates {
            let entry = &self.cache[&id];
            if entry
                .blocks
                .iter()
                .any(|h| self.blocks[h.id as usize].pinned())
            {
                continue;
            }
            selected.push(id);
            for h in &entry.blocks {
                let r = &mut virtual_refs[h.id as usize];
                *r -= 1;
                if *r == 0 && self.blocks[h.id as usize].generation != u64::MAX {
                    reclaim += 1;
                }
            }
            if reclaim >= deficit {
                break;
            }
        }
        if reclaim < deficit {
            return Err(KvError::OutOfBlocks {
                needed,
                available: free + reclaim,
            });
        }
        for id in selected {
            self.remove_cache_entry(id)?;
        }
        Ok(())
    }

    fn make_cache_entry_room(&mut self) -> Result<(), KvError> {
        if self.cfg.max_cache_entries == 0 {
            return Err(KvError::CacheCapacityPinned);
        }
        if self.cache.len() < self.cfg.max_cache_entries {
            return Ok(());
        }
        let id = self
            .select_evictable_entries(1, None)
            .into_iter()
            .next()
            .ok_or(KvError::CacheCapacityPinned)?;
        self.remove_cache_entry(id)
    }

    fn select_evictable_entries(&self, count: usize, exclude: Option<u64>) -> Vec<u64> {
        let mut candidates: Vec<_> = self
            .cache
            .values()
            .filter(|e| Some(e.id) != exclude)
            .filter(|e| !e.blocks.iter().any(|h| self.blocks[h.id as usize].pinned()))
            .map(|e| (e.last_used, e.id))
            .collect();
        candidates.sort_unstable();
        candidates
            .into_iter()
            .take(count)
            .map(|(_, id)| id)
            .collect()
    }

    fn remove_cache_entry(&mut self, id: u64) -> Result<(), KvError> {
        let mut entry = self
            .cache
            .remove(&id)
            .ok_or_else(|| KvError::Invariant("missing cache entry".into()))?;
        if entry
            .blocks
            .iter()
            .any(|h| self.block(*h).is_ok_and(PhysicalBlock::pinned))
        {
            self.cache.insert(id, entry);
            return Err(KvError::CacheCapacityPinned);
        }
        if let Some(bucket) = self.cache_buckets.get_mut(&entry.digest) {
            bucket.retain(|candidate| *candidate != id);
            if bucket.is_empty() {
                self.cache_buckets.remove(&entry.digest);
            }
        }
        for h in entry.blocks {
            let b = self.block_mut(h)?;
            if b.cache_refs == 0 {
                return Err(KvError::Invariant("cache ref underflow".into()));
            }
            b.cache_refs -= 1;
            self.maybe_free(h)?;
        }
        // Exact confirmation necessarily retains prompt token IDs. Scrub their
        // allocation before dropping a tenant-scoped cache entry.
        entry.exact_tokens.fill(0);
        Ok(())
    }

    fn longest_cache_match(
        &self,
        tokens: &[u32],
        namespace: &PrefixNamespace,
        scope: &TenantScope,
    ) -> Option<u64> {
        let mut best: Option<(usize, u64)> = None;
        let mut hasher = self.prefix_hasher(namespace, scope);
        for (block_index, chunk) in tokens.chunks_exact(self.cfg.block_size).enumerate() {
            for token in chunk {
                hasher.update(token.to_le_bytes());
            }
            let len = (block_index + 1) * self.cfg.block_size;
            let digest = self.finish_prefix_digest(hasher.clone(), len);
            let Some(bucket) = self.cache_buckets.get(&digest) else {
                continue;
            };
            for id in bucket {
                let e = &self.cache[id];
                if &e.namespace == namespace
                    && &e.scope == scope
                    && e.exact_tokens == tokens[..len]
                    && best.is_none_or(|(best_len, best_id)| {
                        len > best_len || (len == best_len && *id < best_id)
                    })
                {
                    best = Some((len, *id));
                }
            }
        }
        best.map(|(_, id)| id)
    }

    fn find_exact_cache_entry(
        &self,
        namespace: &PrefixNamespace,
        scope: &TenantScope,
        tokens: &[u32],
        digest: [u8; 32],
    ) -> Option<u64> {
        self.cache_buckets.get(&digest)?.iter().copied().find(|id| {
            let e = &self.cache[id];
            &e.namespace == namespace && &e.scope == scope && e.exact_tokens == tokens
        })
    }

    fn touch_cache(&mut self, id: u64) -> Result<(), KvError> {
        if !self.cache.contains_key(&id) {
            return Err(KvError::Invariant("missing cache entry".into()));
        }
        let next = self
            .lru_clock
            .checked_add(1)
            .ok_or(KvError::GenerationExhausted)?;
        self.lru_clock = next;
        self.cache
            .get_mut(&id)
            .expect("cache presence preflighted")
            .last_used = self.lru_clock;
        Ok(())
    }

    fn prefix_digest(&self, ns: &PrefixNamespace, scope: &TenantScope, tokens: &[u32]) -> [u8; 32] {
        // Cryptographic hashing prevents attacker-chosen collision buckets from
        // becoming a CPU/memory DoS primitive. Exact confirmation below remains
        // authoritative even in the event of a digest collision.
        let mut hash = self.prefix_hasher(ns, scope);
        for token in tokens {
            hash.update(token.to_le_bytes());
        }
        self.finish_prefix_digest(hash, tokens.len())
    }

    fn prefix_hasher(&self, ns: &PrefixNamespace, scope: &TenantScope) -> Sha256 {
        let mut hash = Sha256::new();
        hash.update(b"cx-paged-kv-prefix-v1\0");
        for part in [
            &ns.model,
            &ns.tokenizer,
            &ns.rope,
            &ns.quantization,
            &ns.engine_build,
        ] {
            hash.update(part);
        }
        match scope {
            TenantScope::PublicShared => hash.update([0]),
            TenantScope::Tenant(digest) => {
                hash.update([1]);
                hash.update(digest);
            }
        }
        hash
    }

    fn finish_prefix_digest(&self, mut hash: Sha256, token_len: usize) -> [u8; 32] {
        #[cfg(test)]
        if let Some(d) = self.forced_digest {
            return d;
        }
        // The length suffix lets lookup clone an incremental hasher at each
        // complete block, avoiding quadratic rehashing of long prompts.
        hash.update((token_len as u64).to_le_bytes());
        hash.finalize().into()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};

    fn cfg(blocks: usize, slots: usize, cache: usize) -> PagedKvConfig {
        PagedKvConfig {
            block_size: 4,
            physical_blocks: blocks,
            sequence_slots: slots,
            max_cache_entries: cache,
        }
    }

    fn ns(byte: u8) -> PrefixNamespace {
        PrefixNamespace {
            model: [byte; 32],
            tokenizer: [2; 32],
            rope: [3; 32],
            quantization: [4; 32],
            engine_build: [5; 32],
        }
    }

    #[test]
    fn two_phase_cow_is_invisible_until_commit_and_abort_is_exact() {
        let mut kv = PagedKv::new(cfg(4, 2, 0)).unwrap();
        let s = kv.admit_tokens(&[1, 2, 3]).unwrap();
        let old = kv.sequence_view(s).unwrap().blocks[0];
        let before_free = kv.free_block_count();
        let p = kv.prepare_append(s, &[4, 5]).unwrap();
        assert_eq!(kv.sequence_tokens(s).unwrap(), [1, 2, 3]);
        assert!(matches!(p.events()[0], DeviceMutation::Allocate { .. }));
        assert!(p.events().iter().any(
            |e| matches!(e, DeviceMutation::Copy { source, token_count: 3, .. } if *source == old)
        ));
        assert!(p
            .events()
            .iter()
            .any(|e| matches!(e, DeviceMutation::FreeAfterCommit { block } if *block == old)));
        assert_eq!(kv.release(s), Err(KvError::MutationPending));
        kv.rollback_append(p).unwrap();
        assert_eq!(kv.sequence_tokens(s).unwrap(), [1, 2, 3]);
        assert_eq!(kv.free_block_count(), before_free);

        let p = kv.prepare_append(s, &[4, 5]).unwrap();
        let reserved = p
            .events()
            .iter()
            .find_map(|e| match e {
                DeviceMutation::Allocate { block } => Some(*block),
                _ => None,
            })
            .unwrap();
        let receipt = kv.commit_append(p).unwrap();
        assert_eq!(receipt.freed_blocks, [old]);
        assert_eq!(kv.sequence_tokens(s).unwrap(), [1, 2, 3, 4, 5]);
        assert_eq!(kv.sequence_view(s).unwrap().blocks[0], reserved);
        assert!(matches!(kv.block(old), Err(KvError::StaleBlock)));
        kv.validate_invariants().unwrap();
    }

    #[test]
    fn oom_is_atomic_and_stale_sequence_epochs_are_rejected() {
        let mut kv = PagedKv::new(cfg(2, 1, 0)).unwrap();
        let old = kv.admit_tokens(&[1, 2, 3, 4]).unwrap();
        let before = kv.sequence_view(old).unwrap();
        assert!(matches!(
            kv.prepare_append(old, &[5, 6, 7, 8, 9]),
            Err(KvError::OutOfBlocks { .. })
        ));
        assert_eq!(kv.sequence_view(old).unwrap(), before);
        assert_eq!(kv.free_block_count(), 1);

        kv.next_plan = u64::MAX;
        assert!(matches!(
            kv.prepare_append(old, &[5]),
            Err(KvError::GenerationExhausted)
        ));
        assert_eq!(kv.sequence_view(old).unwrap(), before);
        assert_eq!(kv.free_block_count(), 1);
        kv.next_plan = 100;

        kv.sequence_mut(old).unwrap().token_len = usize::MAX;
        assert!(matches!(
            kv.prepare_append(old, &[5]),
            Err(KvError::InvalidPrefix("token length overflow"))
        ));
        assert!(kv.sequence(old).unwrap().pending.is_none());
        assert_eq!(kv.free_block_count(), 1);
        kv.sequence_mut(old).unwrap().token_len = 4;
        kv.release(old).unwrap();
        let fresh = kv.admit_empty().unwrap();
        assert_eq!(fresh.slot(), old.slot());
        assert!(fresh.epoch() > old.epoch());
        assert_eq!(kv.sequence_view(old), Err(KvError::StaleSequence));
        kv.validate_invariants().unwrap();
    }

    #[test]
    fn cache_clock_overflow_does_not_leak_an_admission_or_reference() {
        let mut kv = PagedKv::new(cfg(3, 1, 2)).unwrap();
        let namespace = ns(1);
        let scope = TenantScope::PublicShared;
        let source = kv.admit_tokens(&[1, 2, 3, 4]).unwrap();
        kv.cache_prefix(source, 4, namespace.clone(), scope.clone())
            .unwrap();
        let block = kv.sequence_view(source).unwrap().blocks[0];
        kv.release(source).unwrap();
        assert_eq!(kv.block(block).unwrap().sequence_refs, 0);
        kv.lru_clock = u64::MAX;
        assert!(matches!(
            kv.admit_cached(&[1, 2, 3, 4], &namespace, &scope),
            Err(KvError::GenerationExhausted)
        ));
        assert_eq!(kv.live_sequence_count(), 0);
        assert_eq!(kv.block(block).unwrap().sequence_refs, 0);
        assert_eq!(kv.block(block).unwrap().cache_refs, 1);
        kv.lru_clock = 10;
        kv.validate_invariants().unwrap();
    }

    #[test]
    fn exact_collision_confirmation_and_tenant_scope_prevent_false_hits() {
        let mut kv = PagedKv::new(cfg(8, 3, 8)).unwrap();
        kv.forced_digest = Some([7; 32]);
        let namespace = ns(1);
        let tenant_a = TenantScope::Tenant([10; 32]);
        let tenant_b = TenantScope::Tenant([11; 32]);
        for tokens in [[1, 2, 3, 4], [9, 8, 7, 6]] {
            let s = kv.admit_tokens(&tokens).unwrap();
            kv.cache_prefix(s, 4, namespace.clone(), tenant_a.clone())
                .unwrap();
            kv.release(s).unwrap();
        }
        let (a, hit) = kv
            .admit_cached(&[9, 8, 7, 6, 5], &namespace, &tenant_a)
            .unwrap();
        assert_eq!(hit.unwrap().matched_tokens, 4);
        assert_eq!(kv.sequence_tokens(a).unwrap(), [9, 8, 7, 6, 5]);
        kv.release(a).unwrap();
        let (isolated, miss) = kv
            .admit_cached(&[9, 8, 7, 6], &namespace, &tenant_b)
            .unwrap();
        assert!(miss.is_none());
        kv.release(isolated).unwrap();
        let (wrong_model, miss) = kv.admit_cached(&[9, 8, 7, 6], &ns(99), &tenant_a).unwrap();
        assert!(miss.is_none());
        kv.release(wrong_model).unwrap();
        kv.validate_invariants().unwrap();
    }

    #[test]
    fn shared_prefix_is_immutable_and_cow_tail_does_not_alias() {
        let mut kv = PagedKv::new(cfg(8, 3, 4)).unwrap();
        let namespace = ns(1);
        let scope = TenantScope::PublicShared;
        let original = kv.admit_tokens(&[1, 2, 3, 4, 10]).unwrap();
        kv.cache_prefix(original, 4, namespace.clone(), scope.clone())
            .unwrap();
        let shared_block = kv.sequence_view(original).unwrap().blocks[0];
        let (fork, hit) = kv
            .admit_cached(&[1, 2, 3, 4, 20], &namespace, &scope)
            .unwrap();
        assert_eq!(hit.unwrap().matched_tokens, 4);
        assert_eq!(kv.sequence_view(fork).unwrap().blocks[0], shared_block);
        let p = kv.prepare_append(fork, &[21, 22, 23]).unwrap();
        kv.commit_append(p).unwrap();
        assert_eq!(kv.sequence_tokens(original).unwrap(), [1, 2, 3, 4, 10]);
        assert_eq!(
            kv.sequence_tokens(fork).unwrap(),
            [1, 2, 3, 4, 20, 21, 22, 23]
        );
        assert_eq!(kv.sequence_view(fork).unwrap().blocks[0], shared_block);
        kv.validate_invariants().unwrap();
    }

    #[test]
    fn lru_never_evicts_sequence_or_device_pinned_blocks() {
        let mut kv = PagedKv::new(cfg(5, 3, 1)).unwrap();
        let namespace = ns(1);
        let scope = TenantScope::PublicShared;
        let pinned = kv.admit_tokens(&[1, 2, 3, 4]).unwrap();
        kv.cache_prefix(pinned, 4, namespace.clone(), scope.clone())
            .unwrap();
        let other = kv.admit_tokens(&[5, 6, 7, 8]).unwrap();
        assert_eq!(
            kv.cache_prefix(other, 4, namespace.clone(), scope.clone()),
            Err(KvError::CacheCapacityPinned)
        );
        let block = kv.sequence_view(pinned).unwrap().blocks[0];
        kv.release(pinned).unwrap();
        kv.pin_block(block).unwrap();
        assert_eq!(
            kv.cache_prefix(other, 4, namespace.clone(), scope.clone()),
            Err(KvError::CacheCapacityPinned)
        );
        kv.unpin_block(block).unwrap();
        kv.cache_prefix(other, 4, namespace, scope).unwrap();
        assert_eq!(kv.cache_entry_count(), 1);
        assert_eq!(kv.unpin_block(block), Err(KvError::StaleBlock));
        kv.validate_invariants().unwrap();
    }

    #[test]
    fn randomized_transactions_match_vector_model_and_audit_every_step() {
        let mut kv = PagedKv::new(cfg(31, 8, 8)).unwrap();
        let mut live: Vec<(SequenceHandle, Vec<u32>)> = Vec::new();
        let mut rng = 0x1234_5678_9abc_def0u64;
        for _ in 0..2_000 {
            rng ^= rng << 13;
            rng ^= rng >> 7;
            rng ^= rng << 17;
            let action = (rng % 5) as usize;
            if (action == 0 || live.is_empty()) && live.len() < 8 {
                if let Ok(h) = kv.admit_empty() {
                    live.push((h, Vec::new()));
                }
            } else if action <= 3 && !live.is_empty() {
                let i = (rng as usize) % live.len();
                let n = ((rng >> 8) % 7) as usize;
                let add: Vec<u32> = (0..n)
                    .map(|j| (rng as u32).wrapping_add(j as u32))
                    .collect();
                if let Ok(plan) = kv.prepare_append(live[i].0, &add) {
                    if rng & 0x40 == 0 {
                        kv.abort_append(plan).unwrap();
                    } else {
                        kv.commit_append(plan).unwrap();
                        live[i].1.extend(add);
                    }
                }
            } else if !live.is_empty() {
                let i = (rng as usize) % live.len();
                let (h, _) = live.swap_remove(i);
                kv.release(h).unwrap();
            }
            for (h, expected) in &live {
                assert_eq!(&kv.sequence_tokens(*h).unwrap(), expected);
            }
            kv.validate_invariants().unwrap();
        }
    }

    #[test]
    fn mutex_shared_allocator_survives_concurrent_churn() {
        let kv = Arc::new(Mutex::new(PagedKv::new(cfg(16, 4, 0)).unwrap()));
        let threads: Vec<_> = (0..4)
            .map(|thread| {
                let kv = Arc::clone(&kv);
                std::thread::spawn(move || {
                    for round in 0..100 {
                        let mut guard = kv.lock().unwrap();
                        let h = guard.admit_empty().unwrap();
                        let tokens = [thread, round, thread ^ round];
                        let p = guard.prepare_append(h, &tokens).unwrap();
                        guard.commit_append(p).unwrap();
                        assert_eq!(guard.sequence_tokens(h).unwrap(), tokens);
                        guard.release(h).unwrap();
                        guard.validate_invariants().unwrap();
                    }
                })
            })
            .collect();
        for thread in threads {
            thread.join().unwrap();
        }
        let guard = kv.lock().unwrap();
        assert_eq!(guard.live_sequence_count(), 0);
        assert_eq!(guard.free_block_count(), 16);
    }
}
