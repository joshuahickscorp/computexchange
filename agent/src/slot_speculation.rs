//! Pure, model-agnostic speculative transactions for a stable-region decoder.
//!
//! A row owns a stable `(slot_id, admission_epoch)` handle and chooses its own
//! draft width.  Rows are packed only for the verifier call: acceptance,
//! rollback, stopping, cancellation, and the next width remain per-row.  In
//! particular there is no batch-wide "common minimum" acceptance clamp.
//!
//! The verifier contract is conventional teacher forcing.  For draft tokens
//! `[d0, .., dK-1]`, the packed input is `[last, d0, .., dK-1]` and the target
//! returns `K + 1` next-token predictions.  The transaction emits the longest
//! exact draft prefix, followed by the target correction on a mismatch or the
//! target bonus when every draft token matched.  Only the accepted draft prefix
//! remains in staged KV; the correction/bonus becomes `last_token` for the next
//! tick.  This is exactly greedy target decoding, just with fewer target calls.

#![allow(dead_code)] // Wired into the Hawking driver in a later, hardware-gated pass.

use std::collections::BTreeMap;

pub type TokenId = u32;

/// One concrete occupancy of a reusable KV-cache region.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct SpecSlotHandle {
    pub slot_id: u32,
    pub admission_epoch: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FinishReason {
    Eos,
    MaxNewTokens,
    ContextCeiling,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SpecSlotState {
    Active,
    Verifying,
    Finished(FinishReason),
    Cancelled,
}

/// State supplied when a scheduler admits a slot to this transaction layer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SlotSpecConfig {
    pub handle: SpecSlotHandle,
    /// Last visible token.  It is the first verifier input for this tick.
    pub last_token: TokenId,
    /// Visible prompt + generated length, including `last_token`.
    pub context_len: usize,
    pub max_context_len: usize,
    pub generated_tokens: usize,
    pub max_new_tokens: usize,
    pub eos_token: Option<TokenId>,
    /// Desired draft width.  The planner independently clamps it to this row's
    /// remaining output/context budget and the dispatch token budget.
    pub requested_k: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SlotSnapshot {
    pub handle: SpecSlotHandle,
    pub state: SpecSlotState,
    pub last_token: TokenId,
    pub context_len: usize,
    pub generated_tokens: usize,
    pub requested_k: usize,
}

#[derive(Debug, Clone)]
struct SlotRecord {
    cfg: SlotSpecConfig,
    state: SpecSlotState,
}

impl SlotRecord {
    fn remaining(&self) -> usize {
        (self.cfg.max_new_tokens - self.cfg.generated_tokens)
            .min(self.cfg.max_context_len - self.cfg.context_len)
    }

    fn snapshot(&self) -> SlotSnapshot {
        SlotSnapshot {
            handle: self.cfg.handle,
            state: self.state,
            last_token: self.cfg.last_token,
            context_len: self.cfg.context_len,
            generated_tokens: self.cfg.generated_tokens,
            requested_k: self.cfg.requested_k,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SpecError {
    ZeroEpoch,
    EmptyContext,
    ContextBeyondCeiling,
    GeneratedBeyondMaximum,
    AlreadyExhausted,
    SlotBusy,
    StaleHandle,
    UnknownSlot,
    NoRows,
    ZeroRowBudget,
    DispatchInFlight,
    NoDispatchInFlight,
    PlanChanged,
    ProposalShape,
    PackedShape,
    DispatchMismatch,
    CounterOverflow,
    InvalidState,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DraftRow {
    pub handle: SpecSlotHandle,
    pub k: usize,
}

/// Immutable proposal request.  `revision` prevents staging a plan after an
/// admission, cancellation, or commit changed the ready set.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DraftPlan {
    pub rows: Vec<DraftRow>,
    revision: u64,
    max_rows: usize,
    max_packed_tokens: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SlotProposal {
    pub handle: SpecSlotHandle,
    pub tokens: Vec<TokenId>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PackedRow {
    pub handle: SpecSlotHandle,
    pub row: usize,
    pub proposal_offset: usize,
    pub proposal_len: usize,
    pub input_offset: usize,
    pub input_len: usize,
    pub base_context_len: usize,
}

/// Packed verifier work.  `input_offsets` and `position_offsets` are
/// cu-seqlens-style arrays of length `rows + 1`; each input row is
/// `[last_token, proposals...]` and therefore has length `K + 1`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PackedVerifierDispatch {
    pub dispatch_id: u64,
    pub rows: Vec<PackedRow>,
    pub proposal_tokens: Vec<TokenId>,
    pub proposal_offsets: Vec<u32>,
    pub input_tokens: Vec<TokenId>,
    pub input_offsets: Vec<u32>,
    pub position_ids: Vec<u32>,
    pub max_input_len: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PackedTargetOutput {
    pub dispatch_id: u64,
    pub handles: Vec<SpecSlotHandle>,
    pub output_offsets: Vec<u32>,
    pub tokens: Vec<TokenId>,
}

impl PackedTargetOutput {
    /// Shape a verifier result using the dispatch's required row ordering.
    pub fn for_dispatch(dispatch: &PackedVerifierDispatch, tokens: Vec<TokenId>) -> Self {
        Self {
            dispatch_id: dispatch.dispatch_id,
            handles: dispatch.rows.iter().map(|row| row.handle).collect(),
            output_offsets: dispatch.input_offsets.clone(),
            tokens,
        }
    }
}

#[derive(Debug, Clone)]
struct PendingRow {
    handle: SpecSlotHandle,
    proposals: Vec<TokenId>,
    base_context_len: usize,
    base_generated_tokens: usize,
}

#[derive(Debug, Clone)]
struct Inflight {
    dispatch: PackedVerifierDispatch,
    rows: Vec<PendingRow>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TargetTail {
    /// All K proposals matched and the final target prediction was appended.
    Bonus,
    /// The first non-matching target prediction was appended.
    Correction,
    /// EOS/ceiling ended emission in the accepted prefix, or the row cancelled.
    None,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RowCommit {
    pub handle: SpecSlotHandle,
    pub accepted_draft_tokens: usize,
    pub emitted_tokens: Vec<TokenId>,
    pub target_tail: TargetTail,
    /// KV length to retain after independently discarding rejected draft input.
    pub rollback_to_context_len: usize,
    pub rolled_back_draft_tokens: usize,
    pub state: SpecSlotState,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommitBatch {
    pub dispatch_id: u64,
    pub rows: Vec<RowCommit>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CancelOutcome {
    pub handle: SpecSlotHandle,
    pub rollback_to_context_len: usize,
    pub staged_draft_tokens: usize,
}

/// Stable-region speculative state machine.  A `BTreeMap` deliberately makes
/// selection deterministic by slot id while keeping slot ids stable as the
/// packed batch shrinks and compacts.
#[derive(Debug, Default)]
pub struct SlotSpeculator {
    slots: BTreeMap<u32, SlotRecord>,
    inflight: Option<Inflight>,
    revision: u64,
    next_dispatch_id: u64,
}

impl SlotSpeculator {
    pub fn new() -> Self {
        Self {
            next_dispatch_id: 1,
            ..Self::default()
        }
    }

    pub fn admit(&mut self, cfg: SlotSpecConfig) -> Result<(), SpecError> {
        validate_config(&cfg)?;
        if let Some(old) = self.slots.get(&cfg.handle.slot_id) {
            if cfg.handle.admission_epoch <= old.cfg.handle.admission_epoch {
                return Err(SpecError::StaleHandle);
            }
            if matches!(old.state, SpecSlotState::Active | SpecSlotState::Verifying)
                || self.inflight_has_slot(cfg.handle.slot_id)
            {
                return Err(SpecError::SlotBusy);
            }
        }
        let next_revision = self.next_revision()?;
        self.slots.insert(
            cfg.handle.slot_id,
            SlotRecord {
                cfg,
                state: SpecSlotState::Active,
            },
        );
        self.revision = next_revision;
        Ok(())
    }

    pub fn snapshot(&self, handle: SpecSlotHandle) -> Result<SlotSnapshot, SpecError> {
        Ok(self.live_slot(handle)?.snapshot())
    }

    pub fn set_requested_k(
        &mut self,
        handle: SpecSlotHandle,
        requested_k: usize,
    ) -> Result<(), SpecError> {
        let next_revision = self.next_revision()?;
        let slot = self.live_slot_mut(handle)?;
        if slot.state != SpecSlotState::Active {
            return Err(SpecError::InvalidState);
        }
        slot.cfg.requested_k = requested_k;
        self.revision = next_revision;
        Ok(())
    }

    /// Select at most `max_rows`, never exceeding `max_packed_tokens`.  Every
    /// selected row consumes `K + 1` verifier tokens, so a budget below one
    /// cannot make progress.  Width is clamped independently per row.
    pub fn draft_plan(
        &self,
        max_rows: usize,
        max_packed_tokens: usize,
    ) -> Result<DraftPlan, SpecError> {
        if self.inflight.is_some() {
            return Err(SpecError::DispatchInFlight);
        }
        if max_rows == 0 || max_packed_tokens == 0 {
            return Err(SpecError::ZeroRowBudget);
        }
        let candidates: Vec<_> = self
            .slots
            .values()
            .filter(|slot| slot.state == SpecSlotState::Active && slot.remaining() > 0)
            .take(max_rows.min(max_packed_tokens))
            .collect();
        let mut budget = max_packed_tokens;
        let mut rows = Vec::new();
        for (index, slot) in candidates.iter().enumerate() {
            // Reserve the mandatory target-only token for every later row.
            let reserved_for_later_rows = candidates.len() - index - 1;
            let k = slot
                .cfg
                .requested_k
                .min(slot.remaining().saturating_sub(1))
                .min(budget.saturating_sub(reserved_for_later_rows + 1));
            rows.push(DraftRow {
                handle: slot.cfg.handle,
                k,
            });
            budget -= k + 1;
        }
        if rows.is_empty() {
            return Err(SpecError::NoRows);
        }
        Ok(DraftPlan {
            rows,
            revision: self.revision,
            max_rows,
            max_packed_tokens,
        })
    }

    /// Atomically validate and stage proposals, returning flat token/offset/
    /// position arrays directly consumable by a packed GPU verifier.
    pub fn stage(
        &mut self,
        plan: &DraftPlan,
        proposals: Vec<SlotProposal>,
    ) -> Result<PackedVerifierDispatch, SpecError> {
        if self.inflight.is_some() {
            return Err(SpecError::DispatchInFlight);
        }
        if plan.revision != self.revision {
            return Err(SpecError::PlanChanged);
        }
        if plan.rows.is_empty() || proposals.len() != plan.rows.len() {
            return Err(SpecError::ProposalShape);
        }
        let packed_tokens = plan.rows.iter().try_fold(0usize, |total, row| {
            total
                .checked_add(row.k)
                .and_then(|value| value.checked_add(1))
                .ok_or(SpecError::CounterOverflow)
        })?;
        if plan.rows.len() > plan.max_rows || packed_tokens > plan.max_packed_tokens {
            return Err(SpecError::PlanChanged);
        }

        // Construct everything before changing a slot.  Any handle, width,
        // position, or integer-shape failure therefore leaves all rows active.
        let mut packed_rows = Vec::with_capacity(plan.rows.len());
        let mut pending_rows = Vec::with_capacity(plan.rows.len());
        let mut proposal_tokens = Vec::new();
        let mut input_tokens = Vec::new();
        let mut position_ids = Vec::new();
        let mut proposal_offsets = vec![0u32];
        let mut input_offsets = vec![0u32];
        let mut max_input_len = 0;

        let mut prior_slot_id = None;
        for (row_index, (planned, proposed)) in plan.rows.iter().zip(proposals.iter()).enumerate() {
            if prior_slot_id.is_some_and(|prior| planned.handle.slot_id <= prior) {
                return Err(SpecError::PlanChanged);
            }
            prior_slot_id = Some(planned.handle.slot_id);
            if planned.handle != proposed.handle || proposed.tokens.len() != planned.k {
                return Err(SpecError::ProposalShape);
            }
            let slot = self.live_slot(planned.handle)?;
            if slot.state != SpecSlotState::Active
                || planned.k > slot.cfg.requested_k
                || planned.k > slot.remaining().saturating_sub(1)
            {
                return Err(SpecError::PlanChanged);
            }
            let proposal_offset = proposal_tokens.len();
            proposal_tokens.extend_from_slice(&proposed.tokens);
            push_offset(&mut proposal_offsets, proposal_tokens.len())?;

            let input_offset = input_tokens.len();
            input_tokens.push(slot.cfg.last_token);
            input_tokens.extend_from_slice(&proposed.tokens);
            let input_len = planned.k + 1;
            for delta in 0..input_len {
                let position = slot
                    .cfg
                    .context_len
                    .checked_sub(1)
                    .and_then(|base| base.checked_add(delta))
                    .ok_or(SpecError::CounterOverflow)?;
                position_ids.push(u32::try_from(position).map_err(|_| SpecError::CounterOverflow)?);
            }
            push_offset(&mut input_offsets, input_tokens.len())?;
            max_input_len = max_input_len.max(input_len);
            packed_rows.push(PackedRow {
                handle: planned.handle,
                row: row_index,
                proposal_offset,
                proposal_len: planned.k,
                input_offset,
                input_len,
                base_context_len: slot.cfg.context_len,
            });
            pending_rows.push(PendingRow {
                handle: planned.handle,
                proposals: proposed.tokens.clone(),
                base_context_len: slot.cfg.context_len,
                base_generated_tokens: slot.cfg.generated_tokens,
            });
        }

        let dispatch_id = self.next_dispatch_id;
        self.next_dispatch_id = self
            .next_dispatch_id
            .checked_add(1)
            .ok_or(SpecError::CounterOverflow)?;
        let dispatch = PackedVerifierDispatch {
            dispatch_id,
            rows: packed_rows,
            proposal_tokens,
            proposal_offsets,
            input_tokens,
            input_offsets,
            position_ids,
            max_input_len,
        };
        for row in &pending_rows {
            self.slots
                .get_mut(&row.handle.slot_id)
                .expect("all staged handles were validated")
                .state = SpecSlotState::Verifying;
        }
        self.inflight = Some(Inflight {
            dispatch: dispatch.clone(),
            rows: pending_rows,
        });
        Ok(dispatch)
    }

    /// Cancel one occupancy.  A verifying slot is quarantined until its packed
    /// dispatch completes or aborts, preventing stale GPU work from racing a
    /// newly admitted occupant of the same cache region.
    pub fn cancel(&mut self, handle: SpecSlotHandle) -> Result<CancelOutcome, SpecError> {
        let next_revision = self.next_revision()?;
        let (rollback_to_context_len, staged_draft_tokens) = self
            .inflight
            .as_ref()
            .and_then(|batch| batch.rows.iter().find(|row| row.handle == handle))
            .map(|row| (row.base_context_len, row.proposals.len()))
            .unwrap_or_else(|| {
                self.slots
                    .get(&handle.slot_id)
                    .map(|slot| (slot.cfg.context_len, 0))
                    .unwrap_or((0, 0))
            });
        let slot = self.live_slot_mut(handle)?;
        if matches!(
            slot.state,
            SpecSlotState::Finished(_) | SpecSlotState::Cancelled
        ) {
            return Err(SpecError::InvalidState);
        }
        slot.state = SpecSlotState::Cancelled;
        self.revision = next_revision;
        Ok(CancelOutcome {
            handle,
            rollback_to_context_len,
            staged_draft_tokens,
        })
    }

    /// Validate the entire packed result before mutating any row, then commit
    /// each row independently.  Shape errors leave the dispatch retryable.
    pub fn commit(&mut self, output: &PackedTargetOutput) -> Result<CommitBatch, SpecError> {
        let next_revision = self.next_revision()?;
        let inflight = self
            .inflight
            .as_ref()
            .ok_or(SpecError::NoDispatchInFlight)?;
        validate_output(inflight, output)?;

        let mut commits = Vec::with_capacity(inflight.rows.len());
        for (row_index, pending) in inflight.rows.iter().enumerate() {
            let slot = self.live_slot(pending.handle)?;
            if slot.cfg.context_len != pending.base_context_len
                || slot.cfg.generated_tokens != pending.base_generated_tokens
                || !matches!(
                    slot.state,
                    SpecSlotState::Verifying | SpecSlotState::Cancelled
                )
            {
                return Err(SpecError::InvalidState);
            }
            let start = output.output_offsets[row_index] as usize;
            let end = output.output_offsets[row_index + 1] as usize;
            commits.push(compute_commit(slot, pending, &output.tokens[start..end]));
        }

        // All fallible validation and calculations are complete.
        for commit in &commits {
            let slot = self
                .slots
                .get_mut(&commit.handle.slot_id)
                .expect("validated slot disappeared without an intervening mutation");
            if slot.state == SpecSlotState::Cancelled {
                continue;
            }
            slot.cfg.generated_tokens += commit.emitted_tokens.len();
            slot.cfg.context_len += commit.emitted_tokens.len();
            if let Some(last) = commit.emitted_tokens.last() {
                slot.cfg.last_token = *last;
            }
            slot.state = commit.state;
        }
        let dispatch_id = inflight.dispatch.dispatch_id;
        self.inflight = None;
        self.revision = next_revision;
        Ok(CommitBatch {
            dispatch_id,
            rows: commits,
        })
    }

    /// Drop a failed verifier dispatch without exposing any generated token.
    pub fn abort(&mut self, dispatch_id: u64) -> Result<Vec<CancelOutcome>, SpecError> {
        let next_revision = self.next_revision()?;
        let inflight = self
            .inflight
            .as_ref()
            .ok_or(SpecError::NoDispatchInFlight)?;
        if inflight.dispatch.dispatch_id != dispatch_id {
            return Err(SpecError::DispatchMismatch);
        }
        let rollbacks: Vec<_> = inflight
            .rows
            .iter()
            .map(|row| CancelOutcome {
                handle: row.handle,
                rollback_to_context_len: row.base_context_len,
                staged_draft_tokens: row.proposals.len(),
            })
            .collect();
        for rollback in &rollbacks {
            let slot = self.live_slot(rollback.handle)?;
            if !matches!(
                slot.state,
                SpecSlotState::Verifying | SpecSlotState::Cancelled
            ) {
                return Err(SpecError::InvalidState);
            }
        }
        for rollback in &rollbacks {
            let slot = self
                .slots
                .get_mut(&rollback.handle.slot_id)
                .expect("all aborted handles were validated");
            if slot.state == SpecSlotState::Verifying {
                slot.state = SpecSlotState::Active;
            }
        }
        self.inflight = None;
        self.revision = next_revision;
        Ok(rollbacks)
    }

    fn live_slot(&self, handle: SpecSlotHandle) -> Result<&SlotRecord, SpecError> {
        let slot = self
            .slots
            .get(&handle.slot_id)
            .ok_or(SpecError::UnknownSlot)?;
        if slot.cfg.handle != handle {
            return Err(SpecError::StaleHandle);
        }
        Ok(slot)
    }

    fn live_slot_mut(&mut self, handle: SpecSlotHandle) -> Result<&mut SlotRecord, SpecError> {
        let slot = self
            .slots
            .get_mut(&handle.slot_id)
            .ok_or(SpecError::UnknownSlot)?;
        if slot.cfg.handle != handle {
            return Err(SpecError::StaleHandle);
        }
        Ok(slot)
    }

    fn inflight_has_slot(&self, slot_id: u32) -> bool {
        self.inflight
            .as_ref()
            .is_some_and(|batch| batch.rows.iter().any(|row| row.handle.slot_id == slot_id))
    }

    fn next_revision(&self) -> Result<u64, SpecError> {
        self.revision
            .checked_add(1)
            .ok_or(SpecError::CounterOverflow)
    }
}

fn validate_config(cfg: &SlotSpecConfig) -> Result<(), SpecError> {
    if cfg.handle.admission_epoch == 0 {
        return Err(SpecError::ZeroEpoch);
    }
    if cfg.context_len == 0 {
        return Err(SpecError::EmptyContext);
    }
    if cfg.context_len > cfg.max_context_len {
        return Err(SpecError::ContextBeyondCeiling);
    }
    if cfg.generated_tokens > cfg.max_new_tokens {
        return Err(SpecError::GeneratedBeyondMaximum);
    }
    if cfg.context_len == cfg.max_context_len || cfg.generated_tokens == cfg.max_new_tokens {
        return Err(SpecError::AlreadyExhausted);
    }
    Ok(())
}

fn push_offset(offsets: &mut Vec<u32>, value: usize) -> Result<(), SpecError> {
    offsets.push(u32::try_from(value).map_err(|_| SpecError::CounterOverflow)?);
    Ok(())
}

fn validate_output(inflight: &Inflight, output: &PackedTargetOutput) -> Result<(), SpecError> {
    if output.dispatch_id != inflight.dispatch.dispatch_id {
        return Err(SpecError::DispatchMismatch);
    }
    let handles: Vec<_> = inflight.rows.iter().map(|row| row.handle).collect();
    if output.handles != handles
        || output.output_offsets != inflight.dispatch.input_offsets
        || output.tokens.len() != inflight.dispatch.input_tokens.len()
    {
        return Err(SpecError::PackedShape);
    }
    Ok(())
}

fn compute_commit(slot: &SlotRecord, pending: &PendingRow, target: &[TokenId]) -> RowCommit {
    if slot.state == SpecSlotState::Cancelled {
        return RowCommit {
            handle: pending.handle,
            accepted_draft_tokens: 0,
            emitted_tokens: Vec::new(),
            target_tail: TargetTail::None,
            rollback_to_context_len: pending.base_context_len,
            rolled_back_draft_tokens: pending.proposals.len(),
            state: SpecSlotState::Cancelled,
        };
    }

    let remaining = slot.remaining();
    let mut accepted = 0;
    let mut emitted = Vec::with_capacity(target.len().min(remaining));
    let mut tail = TargetTail::None;
    let mut eos = false;
    for (index, proposal) in pending.proposals.iter().enumerate() {
        if *proposal != target[index] {
            emitted.push(target[index]);
            tail = TargetTail::Correction;
            eos = slot.cfg.eos_token == Some(target[index]);
            break;
        }
        accepted += 1;
        emitted.push(*proposal);
        eos = slot.cfg.eos_token == Some(*proposal);
        if eos || emitted.len() == remaining {
            break;
        }
    }
    if accepted == pending.proposals.len() && !eos && emitted.len() < remaining {
        emitted.push(target[pending.proposals.len()]);
        tail = TargetTail::Bonus;
        eos = slot.cfg.eos_token == emitted.last().copied();
    }

    let generated = slot.cfg.generated_tokens + emitted.len();
    let context = slot.cfg.context_len + emitted.len();
    let state = if eos {
        SpecSlotState::Finished(FinishReason::Eos)
    } else if generated == slot.cfg.max_new_tokens {
        SpecSlotState::Finished(FinishReason::MaxNewTokens)
    } else if context == slot.cfg.max_context_len {
        SpecSlotState::Finished(FinishReason::ContextCeiling)
    } else {
        SpecSlotState::Active
    };
    RowCommit {
        handle: pending.handle,
        accepted_draft_tokens: accepted,
        emitted_tokens: emitted,
        target_tail: tail,
        rollback_to_context_len: pending.base_context_len + accepted,
        rolled_back_draft_tokens: pending.proposals.len() - accepted,
        state,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn handle(slot_id: u32) -> SpecSlotHandle {
        SpecSlotHandle {
            slot_id,
            admission_epoch: 1,
        }
    }

    fn config(slot_id: u32, k: usize) -> SlotSpecConfig {
        SlotSpecConfig {
            handle: handle(slot_id),
            last_token: 100 + slot_id,
            context_len: 8,
            max_context_len: 128,
            generated_tokens: 0,
            max_new_tokens: 64,
            eos_token: Some(0),
            requested_k: k,
        }
    }

    fn staged(rows: &[(u32, Vec<TokenId>)]) -> (SlotSpeculator, PackedVerifierDispatch) {
        let mut spec = SlotSpeculator::new();
        for (id, tokens) in rows {
            spec.admit(config(*id, tokens.len())).unwrap();
        }
        let plan = spec.draft_plan(rows.len(), usize::MAX).unwrap();
        let proposals = plan
            .rows
            .iter()
            .map(|row| SlotProposal {
                handle: row.handle,
                tokens: rows
                    .iter()
                    .find(|(id, _)| *id == row.handle.slot_id)
                    .unwrap()
                    .1
                    .clone(),
            })
            .collect();
        let dispatch = spec.stage(&plan, proposals).unwrap();
        (spec, dispatch)
    }

    #[test]
    fn ragged_rows_pack_inputs_offsets_and_positions() {
        let (spec, dispatch) = staged(&[(7, vec![1, 2, 3]), (2, vec![4]), (9, vec![])]);
        // Stable regions are compacted by slot id, independent of admission order.
        assert_eq!(
            dispatch
                .rows
                .iter()
                .map(|r| r.handle.slot_id)
                .collect::<Vec<_>>(),
            vec![2, 7, 9]
        );
        assert_eq!(dispatch.proposal_offsets, vec![0, 1, 4, 4]);
        assert_eq!(dispatch.input_offsets, vec![0, 2, 6, 7]);
        assert_eq!(dispatch.input_tokens, vec![102, 4, 107, 1, 2, 3, 109]);
        assert_eq!(dispatch.position_ids, vec![7, 8, 7, 8, 9, 10, 7]);
        assert_eq!(dispatch.max_input_len, 4);
        assert!(spec.inflight.is_some());
    }

    #[test]
    fn rows_accept_independently_without_common_minimum() {
        let (mut spec, dispatch) = staged(&[(0, vec![1, 2, 3]), (1, vec![4, 5, 6])]);
        // Row 0 mismatches immediately; row 1 accepts all three plus bonus.
        let output = PackedTargetOutput::for_dispatch(&dispatch, vec![9, 8, 7, 6, 4, 5, 6, 7]);
        let committed = spec.commit(&output).unwrap();
        assert_eq!(committed.rows[0].accepted_draft_tokens, 0);
        assert_eq!(committed.rows[0].emitted_tokens, vec![9]);
        assert_eq!(committed.rows[0].target_tail, TargetTail::Correction);
        assert_eq!(committed.rows[1].accepted_draft_tokens, 3);
        assert_eq!(committed.rows[1].emitted_tokens, vec![4, 5, 6, 7]);
        assert_eq!(committed.rows[1].target_tail, TargetTail::Bonus);
    }

    fn target_for_acceptance(proposal: &[TokenId], accepted: usize) -> Vec<TokenId> {
        let mut target = vec![900; proposal.len() + 1];
        target[..accepted].copy_from_slice(&proposal[..accepted]);
        if accepted == proposal.len() {
            target[accepted] = 777;
        } else {
            target[accepted] = proposal[accepted] + 100;
        }
        target
    }

    #[test]
    fn exhaustive_two_row_acceptance_patterns_are_independent() {
        let p0 = vec![1, 2, 3];
        let p1 = vec![4, 5, 6];
        for a0 in 0..=p0.len() {
            for a1 in 0..=p1.len() {
                let (mut spec, dispatch) = staged(&[(0, p0.clone()), (1, p1.clone())]);
                let mut target = target_for_acceptance(&p0, a0);
                target.extend(target_for_acceptance(&p1, a1));
                let rows = spec
                    .commit(&PackedTargetOutput::for_dispatch(&dispatch, target))
                    .unwrap()
                    .rows;
                assert_eq!(
                    (rows[0].accepted_draft_tokens, rows[1].accepted_draft_tokens),
                    (a0, a1)
                );
                assert_eq!(rows[0].rollback_to_context_len, 8 + a0);
                assert_eq!(rows[1].rollback_to_context_len, 8 + a1);
                assert_eq!(rows[0].rolled_back_draft_tokens, 3 - a0);
                assert_eq!(rows[1].rolled_back_draft_tokens, 3 - a1);
            }
        }
    }

    #[test]
    fn randomized_commit_matches_sequential_target_oracle() {
        let mut rng = 0x9e37_79b9_7f4a_7c15u64;
        let mut next = || {
            rng ^= rng << 7;
            rng ^= rng >> 9;
            rng
        };
        for case in 0..2_000u32 {
            let requested_k = (next() % 9) as usize;
            let remaining = 1 + (next() % 10) as usize;
            let mut cfg = config(case, requested_k);
            cfg.max_new_tokens = remaining;
            cfg.eos_token = Some(3);
            let mut spec = SlotSpeculator::new();
            spec.admit(cfg).unwrap();
            let plan = spec.draft_plan(1, 32).unwrap();
            let k = plan.rows[0].k;
            let proposal: Vec<_> = (0..k).map(|_| 1 + (next() % 7) as u32).collect();
            let target: Vec<_> = (0..=k)
                .map(|index| {
                    if index < k && next() & 1 == 0 {
                        proposal[index]
                    } else {
                        1 + (next() % 7) as u32
                    }
                })
                .collect();
            let dispatch = spec
                .stage(
                    &plan,
                    vec![SlotProposal {
                        handle: handle(case),
                        tokens: proposal.clone(),
                    }],
                )
                .unwrap();

            // A deliberately simple sequential greedy oracle: matched drafts
            // can be emitted; at the first mismatch the target token wins.  If
            // all match, one target bonus wins.  EOS stops immediately.
            let mut expected = Vec::new();
            let mut expected_accepted = 0;
            for index in 0..k {
                if proposal[index] != target[index] {
                    expected.push(target[index]);
                    break;
                }
                expected_accepted += 1;
                expected.push(target[index]);
                if target[index] == 3 {
                    break;
                }
            }
            if expected_accepted == k && expected.last().copied() != Some(3) {
                expected.push(target[k]);
            }
            let row = &spec
                .commit(&PackedTargetOutput::for_dispatch(&dispatch, target))
                .unwrap()
                .rows[0];
            assert_eq!(row.emitted_tokens, expected, "case {case}");
            assert_eq!(row.accepted_draft_tokens, expected_accepted, "case {case}");
            assert!(row.emitted_tokens.len() <= remaining, "case {case}");
        }
    }

    #[test]
    fn malformed_outputs_and_mutated_plans_are_atomic() {
        let (mut spec, dispatch) = staged(&[(0, vec![1, 2]), (1, vec![3])]);
        let before0 = spec.snapshot(handle(0)).unwrap();
        let before1 = spec.snapshot(handle(1)).unwrap();
        let mut bad = PackedTargetOutput::for_dispatch(&dispatch, vec![1, 2, 9, 3, 9]);
        bad.output_offsets.pop();
        assert_eq!(spec.commit(&bad), Err(SpecError::PackedShape));
        bad = PackedTargetOutput::for_dispatch(&dispatch, vec![1, 2, 9, 3]);
        assert_eq!(spec.commit(&bad), Err(SpecError::PackedShape));
        bad = PackedTargetOutput::for_dispatch(&dispatch, vec![1, 2, 9, 3, 9]);
        bad.handles.swap(0, 1);
        assert_eq!(spec.commit(&bad), Err(SpecError::PackedShape));
        assert_eq!(spec.snapshot(handle(0)).unwrap(), before0);
        assert_eq!(spec.snapshot(handle(1)).unwrap(), before1);
        assert_eq!(spec.abort(dispatch.dispatch_id).unwrap().len(), 2);
        assert_eq!(
            spec.snapshot(handle(0)).unwrap().state,
            SpecSlotState::Active
        );

        let plan = spec.draft_plan(2, 8).unwrap();
        let mut forged = plan.clone();
        forged.rows[1].handle = forged.rows[0].handle;
        let before = spec.snapshot(handle(0)).unwrap();
        assert_eq!(
            spec.stage(
                &forged,
                vec![
                    SlotProposal {
                        handle: forged.rows[0].handle,
                        tokens: vec![1, 2]
                    },
                    SlotProposal {
                        handle: forged.rows[1].handle,
                        tokens: vec![3]
                    },
                ],
            ),
            Err(SpecError::PlanChanged)
        );
        assert_eq!(spec.snapshot(handle(0)).unwrap(), before);
    }

    #[test]
    fn cancellation_quarantines_region_and_stale_epoch_is_rejected() {
        let (mut spec, dispatch) = staged(&[(0, vec![1, 2]), (1, vec![3, 4])]);
        let cancelled = spec.cancel(handle(0)).unwrap();
        assert_eq!(
            (
                cancelled.rollback_to_context_len,
                cancelled.staged_draft_tokens
            ),
            (8, 2)
        );
        let mut newer = config(0, 1);
        newer.handle.admission_epoch = 2;
        assert_eq!(spec.admit(newer.clone()), Err(SpecError::SlotBusy));
        let commit = spec
            .commit(&PackedTargetOutput::for_dispatch(
                &dispatch,
                vec![1, 2, 9, 3, 4, 9],
            ))
            .unwrap();
        assert!(commit.rows[0].emitted_tokens.is_empty());
        assert_eq!(commit.rows[0].rolled_back_draft_tokens, 2);
        spec.admit(newer.clone()).unwrap();
        assert_eq!(spec.snapshot(handle(0)), Err(SpecError::StaleHandle));
        assert_eq!(spec.cancel(handle(0)), Err(SpecError::StaleHandle));
        assert_eq!(
            spec.snapshot(newer.handle).unwrap().state,
            SpecSlotState::Active
        );
    }

    #[test]
    fn eos_limits_and_batch_compaction_are_per_slot() {
        let mut spec = SlotSpeculator::new();
        for id in 0..3 {
            let mut cfg = config(id, 8);
            if id == 2 {
                cfg.max_context_len = 10; // only two visible tokens remain => K=1.
            }
            spec.admit(cfg).unwrap();
        }
        let plan = spec.draft_plan(3, 32).unwrap();
        assert_eq!(
            plan.rows.iter().map(|row| row.k).collect::<Vec<_>>(),
            vec![8, 8, 1]
        );
        let proposals: Vec<_> = plan
            .rows
            .iter()
            .map(|row| SlotProposal {
                handle: row.handle,
                tokens: vec![5; row.k],
            })
            .collect();
        let dispatch = spec.stage(&plan, proposals).unwrap();
        spec.cancel(handle(1)).unwrap();
        let mut targets = vec![0; 9]; // slot 0 mismatches to EOS immediately.
        targets.extend(vec![8; 9]); // cancelled slot still preserves packed shape.
        targets.extend([5, 6]); // slot 2 accepts one + bonus and hits context cap.
        let committed = spec
            .commit(&PackedTargetOutput::for_dispatch(&dispatch, targets))
            .unwrap();
        assert_eq!(
            committed.rows[0].state,
            SpecSlotState::Finished(FinishReason::Eos)
        );
        assert_eq!(committed.rows[1].state, SpecSlotState::Cancelled);
        assert_eq!(
            committed.rows[2].state,
            SpecSlotState::Finished(FinishReason::ContextCeiling)
        );
        assert_eq!(spec.draft_plan(3, 32), Err(SpecError::NoRows));

        let mut replacement = config(1, 2);
        replacement.handle.admission_epoch = 2;
        spec.admit(replacement).unwrap();
        let compact = spec.draft_plan(3, 32).unwrap();
        assert_eq!(compact.rows.len(), 1);
        assert_eq!(compact.rows[0].handle.slot_id, 1);
    }

    #[test]
    fn final_token_budget_uses_exact_target_only_row() {
        let mut spec = SlotSpeculator::new();
        let mut cfg = config(4, 99);
        cfg.generated_tokens = 3;
        cfg.max_new_tokens = 4;
        spec.admit(cfg).unwrap();
        let plan = spec.draft_plan(1, 64).unwrap();
        assert_eq!(plan.rows[0].k, 0);
        let dispatch = spec
            .stage(
                &plan,
                vec![SlotProposal {
                    handle: handle(4),
                    tokens: vec![],
                }],
            )
            .unwrap();
        let row = spec
            .commit(&PackedTargetOutput::for_dispatch(&dispatch, vec![44]))
            .unwrap()
            .rows
            .remove(0);
        assert_eq!(row.emitted_tokens, vec![44]);
        assert_eq!(row.target_tail, TargetTail::Bonus);
        assert_eq!(
            row.state,
            SpecSlotState::Finished(FinishReason::MaxNewTokens)
        );
    }

    #[test]
    fn planner_work_is_strictly_bounded_and_keeps_rows_fair() {
        let mut spec = SlotSpeculator::new();
        for id in 0..100 {
            spec.admit(config(id, usize::MAX)).unwrap();
        }
        let plan = spec.draft_plan(7, 19).unwrap();
        assert_eq!(plan.rows.len(), 7);
        assert!(plan.rows.iter().map(|row| row.k + 1).sum::<usize>() <= 19);
        let proposals = plan
            .rows
            .iter()
            .map(|row| SlotProposal {
                handle: row.handle,
                tokens: vec![42; row.k],
            })
            .collect();
        let dispatch = spec.stage(&plan, proposals).unwrap();
        assert_eq!(dispatch.rows.len(), 7);
        assert!(dispatch.input_tokens.len() <= 19);
        assert_eq!(dispatch.input_offsets.len(), 8);
        assert_eq!(dispatch.position_ids.len(), dispatch.input_tokens.len());
    }
}
