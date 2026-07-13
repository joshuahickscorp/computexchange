//! Feature-gated, device-free integration seam between the resident scheduler
//! and ragged speculative transactions.
//!
//! This module is deliberately a *shadow* actor.  It never loads a model,
//! touches KV memory, or routes a buyer request.  Instead it turns the decode
//! members of a [`resident_engine::DispatchPlan`] into a short-lived
//! [`slot_speculation::SlotSpeculator`] transaction, verifies it with a
//! deterministic exact target, and feeds only the next visible token back to
//! the resident scheduler.  Extra exact tokens remain in a per-request shadow
//! buffer until the scheduler reaches them.  That is the important bridge:
//! scheduler lifecycle remains authoritative while ragged rows may advance at
//! independent widths without a batch-wide common-minimum clamp.
//!
//! The root module must register this behind the `resident-spec-shadow` Cargo
//! feature.  Keeping the feature boundary at registration makes this file
//! testable in isolation while guaranteeing that ordinary agent builds do not
//! create the shadow actor or alter routing.

// The feature is a compile/test seam, not a production route. Keep its full
// public integration surface type-checked without presenting every hook as used
// by the current binary.
#![allow(dead_code)]

use std::collections::{BTreeMap, BTreeSet, VecDeque};

use crate::resident_engine::{
    AdmissionError, ApplyReport, CompletionError, ControlOutcome, DispatchCompletion, DispatchPlan,
    EngineConfig, EngineTime, ItemOutput, RequestHandle, RequestId, RequestSpec, ResidentEngine,
    TerminalEvent, TerminalReason, WorkKind,
};
use crate::slot_speculation::{
    PackedTargetOutput, PackedVerifierDispatch, RowCommit, SlotProposal, SlotSpecConfig,
    SlotSpeculator, SpecError, SpecSlotHandle, TokenId,
};

/// All bounded knobs for the pure shadow actor.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ShadowConfig {
    pub scheduler: EngineConfig,
    /// Maximum ragged rows to put through one shadow verifier transaction.
    pub max_spec_rows: usize,
    /// Packed verifier input-token ceiling. Rows beyond it use exact target-only
    /// shadow decoding for that scheduler tick; they are never silently dropped.
    pub max_packed_tokens: usize,
    pub eos_token: TokenId,
}

impl Default for ShadowConfig {
    fn default() -> Self {
        Self {
            scheduler: EngineConfig::default(),
            max_spec_rows: 32,
            max_packed_tokens: 512,
            eos_token: 0,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ShadowConfigError {
    Scheduler(crate::resident_engine::ConfigError),
    ZeroSpecRows,
    ZeroPackedTokens,
}

/// Input kept by the shadow actor; the resident scheduler itself only needs
/// prompt length, whereas the exact mock verifier needs concrete token ids.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ShadowRequestSpec {
    pub request_id: RequestId,
    pub prompt: Vec<TokenId>,
    pub max_new_tokens: usize,
    pub deadline: Option<EngineTime>,
    /// Per-row speculative width. Zero is valid and means target-only.
    pub requested_k: usize,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ShadowAdmissionError {
    EmptyPrompt,
    ContextOverflow,
    Scheduler(AdmissionError),
    SlotIdExhausted,
}

/// The tiny model boundary used by this shadow lane.  A future hardware actor
/// can implement the same interface without changing the scheduler/transaction
/// contract.  Implementations must make `target_next` exact for the supplied
/// teacher-forced prefix; drafts may be imperfect.
pub trait ShadowModel {
    fn draft_tokens(
        &self,
        handle: RequestHandle,
        prompt_len: usize,
        visible_tokens: &[TokenId],
        width: usize,
    ) -> Vec<TokenId>;

    fn target_next(
        &self,
        handle: RequestHandle,
        prompt_len: usize,
        teacher_forced_prefix: &[TokenId],
    ) -> TokenId;
}

/// Deterministic exact target plus controllable draft quality for tests and
/// offline lifecycle fuzzing. It is not a language model.
#[cfg(test)]
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MockExactTarget {
    salt: u64,
    eos_token: TokenId,
    eos_after: BTreeMap<RequestId, usize>,
    draft_modes: BTreeMap<RequestId, MockDraftMode>,
    default_draft_mode: MockDraftMode,
}

#[cfg(test)]
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum MockDraftMode {
    Exact,
    /// Mismatch at these generated-token offsets.
    MismatchAt(BTreeSet<usize>),
    /// Mismatch whenever `(generated_offset + offset) % every == 0`.
    Periodic {
        every: usize,
        offset: usize,
    },
}

#[cfg(test)]
impl MockExactTarget {
    pub fn new(eos_token: TokenId) -> Self {
        Self {
            salt: 0x9e37_79b9_7f4a_7c15,
            eos_token,
            eos_after: BTreeMap::new(),
            draft_modes: BTreeMap::new(),
            default_draft_mode: MockDraftMode::Periodic {
                every: 5,
                offset: 1,
            },
        }
    }

    pub fn set_eos_after(&mut self, request_id: RequestId, generated_tokens: usize) {
        self.eos_after.insert(request_id, generated_tokens);
    }

    pub fn set_draft_mode(&mut self, request_id: RequestId, mode: MockDraftMode) {
        self.draft_modes.insert(request_id, mode);
    }

    pub fn eos_token(&self) -> TokenId {
        self.eos_token
    }

    fn should_mismatch(&self, handle: RequestHandle, generated: usize) -> bool {
        match self
            .draft_modes
            .get(&handle.request_id)
            .unwrap_or(&self.default_draft_mode)
        {
            MockDraftMode::Exact => false,
            MockDraftMode::MismatchAt(offsets) => offsets.contains(&generated),
            MockDraftMode::Periodic { every, offset } => {
                *every != 0 && generated.saturating_add(*offset) % *every == 0
            }
        }
    }

    fn exact(&self, handle: RequestHandle, prompt_len: usize, prefix: &[TokenId]) -> TokenId {
        let generated = prefix.len().saturating_sub(prompt_len);
        if self.eos_after.get(&handle.request_id) == Some(&generated) {
            return self.eos_token;
        }
        let mut state = self.salt ^ handle.request_id.0.rotate_left(17);
        state ^= handle.admission_epoch.0.rotate_left(31);
        for (index, token) in prefix.iter().enumerate() {
            state ^= (*token as u64).wrapping_add((index as u64).rotate_left(11));
            state = state
                .wrapping_mul(6_364_136_223_846_793_005)
                .wrapping_add(1_442_695_040_888_963_407);
        }
        let mut token = (state % 8_191) as TokenId + 1;
        if token == self.eos_token {
            token = token.wrapping_add(1).max(1);
        }
        token
    }

    fn perturb(&self, token: TokenId) -> TokenId {
        let mut changed = token.wrapping_add(1).max(1);
        if changed == self.eos_token {
            changed = changed.wrapping_add(1).max(1);
        }
        changed
    }
}

#[cfg(test)]
impl ShadowModel for MockExactTarget {
    fn draft_tokens(
        &self,
        handle: RequestHandle,
        prompt_len: usize,
        visible_tokens: &[TokenId],
        width: usize,
    ) -> Vec<TokenId> {
        let mut prefix = visible_tokens.to_vec();
        let mut draft = Vec::with_capacity(width);
        for _ in 0..width {
            let generated = prefix.len().saturating_sub(prompt_len);
            let exact = self.exact(handle, prompt_len, &prefix);
            let token = if exact != self.eos_token && self.should_mismatch(handle, generated) {
                self.perturb(exact)
            } else {
                exact
            };
            draft.push(token);
            prefix.push(token);
        }
        draft
    }

    fn target_next(
        &self,
        handle: RequestHandle,
        prompt_len: usize,
        teacher_forced_prefix: &[TokenId],
    ) -> TokenId {
        self.exact(handle, prompt_len, teacher_forced_prefix)
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct ShadowTelemetry {
    pub scheduler_dispatches: u64,
    pub packed_dispatches: u64,
    pub packed_rows: u64,
    pub packed_target_tokens: u64,
    pub target_only_rows: u64,
    pub buffered_rows: u64,
    pub buffered_exact_tokens: u64,
    pub scheduler_visible_tokens: u64,
    pub malformed_target_rejections: u64,
    pub aborted_transactions: u64,
    pub cancelled_staged_rows: u64,
    pub batch_shrinks: u64,
    /// Counts transactions whose rows accepted different draft-prefix lengths.
    /// A positive value is direct evidence that no common-minimum clamp occurred.
    pub independent_acceptance_transactions: u64,
}

/// Auditable output from one packed shadow transaction.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ShadowPackedDispatch {
    pub resident_dispatch_id: u64,
    pub resident_decode_handles: Vec<RequestHandle>,
    pub packed: PackedVerifierDispatch,
    pub commits: Vec<RowCommit>,
    pub buffered_handles: Vec<RequestHandle>,
    pub target_only_handles: Vec<RequestHandle>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ShadowFinishedRequest {
    pub handle: RequestHandle,
    pub output_tokens: Vec<TokenId>,
    pub terminal: TerminalReason,
}

#[derive(Clone, Debug, Eq, PartialEq)]
// This diagnostic outcome preserves the complete immutable dispatch evidence for
// tests. The shadow actor is never on the live hot path, so boxing it would add
// indirection without a runtime benefit.
#[allow(clippy::large_enum_variant)]
pub enum ShadowDriveOutcome {
    Idle,
    Applied {
        report: ApplyReport,
        speculation: Option<ShadowPackedDispatch>,
    },
}

/// Fault hooks are intentionally shadow-only. They make lifecycle races and
/// malformed target results reproducible without a device or an unsafe model.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ShadowFault {
    None,
    MalformedPackedTarget,
    CancelAfterStage(RequestHandle),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ShadowError {
    SchedulerPlan(CompletionError),
    SchedulerApply(CompletionError),
    SchedulerAbort(CompletionError),
    Slot(SpecError),
    MissingRequest(RequestHandle),
    DraftShape {
        handle: RequestHandle,
        expected: usize,
        observed: usize,
    },
    FaultRequiresPackedDispatch,
    FaultHandleNotInDispatch(RequestHandle),
    FaultCancellation(ControlOutcome),
    NonMonotonicCompletionTime {
        planned: EngineTime,
        completed: EngineTime,
    },
    Invariant(String),
}

#[derive(Clone, Debug)]
struct ShadowRequest {
    handle: RequestHandle,
    slot: SpecSlotHandle,
    prompt: Vec<TokenId>,
    /// Exact target-visible context, potentially ahead of scheduler-visible
    /// output by `pending` tokens.
    verified: Vec<TokenId>,
    published: Vec<TokenId>,
    pending: VecDeque<TokenId>,
    max_new_tokens: usize,
    requested_k: usize,
    deadline: Option<EngineTime>,
}

#[derive(Clone, Debug)]
struct DecodePreparation {
    completion: DispatchCompletion,
    additions: BTreeMap<RequestHandle, Vec<TokenId>>,
    speculation: Option<ShadowPackedDispatch>,
}

/// A pure integration actor.  Its `ResidentEngine` is the sole lifecycle
/// authority; the ragged slot transaction is deliberately scoped to the
/// scheduler's already-reserved decode batch, preserving scheduling order.
pub struct ResidentSpecShadow<M> {
    config: ShadowConfig,
    scheduler: ResidentEngine,
    model: M,
    requests: BTreeMap<RequestHandle, ShadowRequest>,
    finished: BTreeMap<RequestHandle, ShadowFinishedRequest>,
    free_slots: BTreeSet<u32>,
    next_slot_id: u64,
    telemetry: ShadowTelemetry,
}

impl<M: ShadowModel> ResidentSpecShadow<M> {
    pub fn new(config: ShadowConfig, model: M) -> Result<Self, ShadowConfigError> {
        if config.max_spec_rows == 0 {
            return Err(ShadowConfigError::ZeroSpecRows);
        }
        if config.max_packed_tokens == 0 {
            return Err(ShadowConfigError::ZeroPackedTokens);
        }
        let scheduler =
            ResidentEngine::new(config.scheduler.clone()).map_err(ShadowConfigError::Scheduler)?;
        Ok(Self {
            config,
            scheduler,
            model,
            requests: BTreeMap::new(),
            finished: BTreeMap::new(),
            free_slots: BTreeSet::new(),
            next_slot_id: 0,
            telemetry: ShadowTelemetry::default(),
        })
    }

    pub fn scheduler(&self) -> &ResidentEngine {
        &self.scheduler
    }

    pub fn telemetry(&self) -> &ShadowTelemetry {
        &self.telemetry
    }

    pub fn output(&self, handle: RequestHandle) -> Option<&[TokenId]> {
        self.requests
            .get(&handle)
            .map(|request| request.published.as_slice())
            .or_else(|| {
                self.finished
                    .get(&handle)
                    .map(|result| result.output_tokens.as_slice())
            })
    }

    pub fn finished(&self, handle: RequestHandle) -> Option<&ShadowFinishedRequest> {
        self.finished.get(&handle)
    }

    pub fn slot_for(&self, handle: RequestHandle) -> Option<SpecSlotHandle> {
        self.requests.get(&handle).map(|request| request.slot)
    }

    pub fn live_handles(&self) -> Vec<RequestHandle> {
        self.requests.keys().copied().collect()
    }

    pub fn admit(
        &mut self,
        spec: ShadowRequestSpec,
        now: EngineTime,
    ) -> Result<RequestHandle, ShadowAdmissionError> {
        if spec.prompt.is_empty() {
            return Err(ShadowAdmissionError::EmptyPrompt);
        }
        // The resident engine currently terminates only on EOS/max/cancel/
        // deadline.  Make the temporary slot ceiling exactly cover max output
        // so the shadow layer can never introduce a different terminal cause.
        spec.prompt
            .len()
            .checked_add(spec.max_new_tokens)
            .ok_or(ShadowAdmissionError::ContextOverflow)?;
        let slot_id = self.peek_slot_id()?;
        let handle = self
            .scheduler
            .admit(
                RequestSpec {
                    request_id: spec.request_id,
                    prompt_tokens: spec.prompt.len(),
                    max_new_tokens: spec.max_new_tokens,
                    deadline: spec.deadline,
                },
                now,
            )
            .map_err(ShadowAdmissionError::Scheduler)?;
        self.claim_slot_id(slot_id);
        let request = ShadowRequest {
            handle,
            slot: SpecSlotHandle {
                slot_id,
                admission_epoch: handle.admission_epoch.0,
            },
            verified: spec.prompt.clone(),
            prompt: spec.prompt,
            published: Vec::new(),
            pending: VecDeque::new(),
            max_new_tokens: spec.max_new_tokens,
            requested_k: spec.requested_k,
            deadline: spec.deadline,
        };
        self.requests.insert(handle, request);
        self.check_invariants()
            .map_err(ShadowError::Invariant)
            .expect("fresh admission must preserve shadow invariants");
        Ok(handle)
    }

    #[allow(dead_code)] // feature-gated integration API; production routing is intentionally absent
    pub fn cancel(
        &mut self,
        handle: RequestHandle,
        now: EngineTime,
    ) -> Result<ControlOutcome, ShadowError> {
        let outcome = self
            .scheduler
            .cancel(handle, now)
            .map_err(|_| ShadowError::MissingRequest(handle))?;
        if outcome == ControlOutcome::Applied {
            let event = self
                .scheduler
                .terminal_events()
                .iter()
                .rev()
                .find(|event| event.handle == handle)
                .cloned()
                .ok_or_else(|| {
                    ShadowError::Invariant("cancelled request lacked terminal event".into())
                })?;
            self.retire(event)?;
        }
        self.check_invariants().map_err(ShadowError::Invariant)?;
        Ok(outcome)
    }

    #[allow(dead_code)] // feature-gated integration API; production routing is intentionally absent
    pub fn poll(&mut self, now: EngineTime) -> Result<Vec<TerminalEvent>, ShadowError> {
        let events = self.scheduler.poll(now);
        for event in &events {
            self.retire(event.clone())?;
        }
        self.check_invariants().map_err(ShadowError::Invariant)?;
        Ok(events)
    }

    pub fn drive_once(&mut self, now: EngineTime) -> Result<ShadowDriveOutcome, ShadowError> {
        self.drive_once_with_fault(now, now, ShadowFault::None)
    }

    /// Plan at `planned_at` and complete at `completed_at`, allowing a pure
    /// deadline race to be tested without a background executor.
    pub fn drive_once_at(
        &mut self,
        planned_at: EngineTime,
        completed_at: EngineTime,
    ) -> Result<ShadowDriveOutcome, ShadowError> {
        self.drive_once_with_fault(planned_at, completed_at, ShadowFault::None)
    }

    pub fn drive_once_with_fault(
        &mut self,
        planned_at: EngineTime,
        completed_at: EngineTime,
        fault: ShadowFault,
    ) -> Result<ShadowDriveOutcome, ShadowError> {
        if completed_at < planned_at {
            return Err(ShadowError::NonMonotonicCompletionTime {
                planned: planned_at,
                completed: completed_at,
            });
        }
        let plan = self
            .scheduler
            .plan(planned_at)
            .map_err(ShadowError::SchedulerPlan)?;
        let Some(plan) = plan else {
            return Ok(ShadowDriveOutcome::Idle);
        };
        self.telemetry.scheduler_dispatches += 1;

        let prepared = match plan.lane() {
            crate::resident_engine::Lane::Prefill => self.prepare_prefill(&plan),
            crate::resident_engine::Lane::Decode => {
                self.prepare_decode(&plan, planned_at, completed_at, &fault)
            }
        };
        let prepared = match prepared {
            Ok(prepared) => prepared,
            Err(error) => {
                self.abort_scheduler(completed_at)?;
                return Err(error);
            }
        };

        let report = match self
            .scheduler
            .apply_completion(prepared.completion, completed_at)
        {
            Ok(report) => report,
            Err(error) => {
                self.abort_scheduler(completed_at)?;
                return Err(ShadowError::SchedulerApply(error));
            }
        };
        self.apply_shadow_progress(prepared.additions, &report)?;
        self.check_invariants().map_err(ShadowError::Invariant)?;
        Ok(ShadowDriveOutcome::Applied {
            report,
            speculation: prepared.speculation,
        })
    }

    /// Checks the cross-layer contract without relying on either module's
    /// private fields. This is intentionally useful to long churn tests.
    pub fn check_invariants(&self) -> Result<(), String> {
        let mut slots = BTreeSet::new();
        for request in self.requests.values() {
            if !slots.insert(request.slot.slot_id) {
                return Err("two live requests own one slot id".into());
            }
            if request.verified.len() < request.prompt.len()
                || request.verified[..request.prompt.len()] != request.prompt
            {
                return Err("verified context lost prompt prefix".into());
            }
            let generated = request.verified.len() - request.prompt.len();
            if generated != request.published.len() + request.pending.len() {
                return Err("verified/published/pending token accounting diverged".into());
            }
            if generated > request.max_new_tokens {
                return Err("shadow transaction exceeded resident max_new_tokens".into());
            }
            if request.verified[request.prompt.len()..].get(..request.published.len())
                != Some(request.published.as_slice())
            {
                return Err("published tokens are not target-visible prefix".into());
            }
            let snapshot = self
                .scheduler
                .request(request.handle)
                .map_err(|_| "live shadow request missing from scheduler".to_string())?;
            if snapshot.generated_tokens != request.published.len() {
                return Err("scheduler-visible generation count diverged".into());
            }
        }
        Ok(())
    }

    fn prepare_prefill(&self, plan: &DispatchPlan) -> Result<DecodePreparation, ShadowError> {
        let mut items = Vec::with_capacity(plan.items().len());
        for item in plan.items() {
            let handle = item.handle();
            if !self.requests.contains_key(&handle) {
                return Err(ShadowError::MissingRequest(handle));
            }
            let WorkKind::Prefill { tokens, .. } = item.kind() else {
                return Err(ShadowError::Invariant(
                    "prefill lane held decode work".into(),
                ));
            };
            items.push(ItemOutput::Prefill {
                handle,
                processed_tokens: *tokens,
            });
        }
        Ok(DecodePreparation {
            completion: DispatchCompletion {
                dispatch_id: plan.dispatch_id(),
                items,
            },
            additions: BTreeMap::new(),
            speculation: None,
        })
    }

    fn prepare_decode(
        &mut self,
        plan: &DispatchPlan,
        planned_at: EngineTime,
        completed_at: EngineTime,
        fault: &ShadowFault,
    ) -> Result<DecodePreparation, ShadowError> {
        let mut decode_handles = Vec::with_capacity(plan.items().len());
        let mut buffered = Vec::new();
        let mut candidates = Vec::new();
        for item in plan.items() {
            let handle = item.handle();
            let WorkKind::Decode { position } = item.kind() else {
                return Err(ShadowError::Invariant(
                    "decode lane held prefill work".into(),
                ));
            };
            let request = self
                .requests
                .get(&handle)
                .ok_or(ShadowError::MissingRequest(handle))?;
            if *position != request.prompt.len() + request.published.len() {
                return Err(ShadowError::Invariant(
                    "scheduler decode position does not match shadow-visible context".into(),
                ));
            }
            decode_handles.push(handle);
            if request.pending.is_empty() {
                candidates.push(handle);
            } else {
                buffered.push(handle);
            }
        }

        let mut suppressed = BTreeSet::new();
        let mut fault_cancelled = false;
        if let ShadowFault::CancelAfterStage(handle) = fault {
            if !decode_handles.contains(handle) {
                return Err(ShadowError::FaultHandleNotInDispatch(*handle));
            }
        }

        let mut additions = BTreeMap::<RequestHandle, Vec<TokenId>>::new();
        let mut speculation = None;
        let mut target_only = Vec::new();

        if !candidates.is_empty() {
            let mut transaction = SlotSpeculator::new();
            let mut handles_by_slot = BTreeMap::new();
            for handle in &candidates {
                let request = self
                    .requests
                    .get(handle)
                    .ok_or(ShadowError::MissingRequest(*handle))?;
                let max_context_len = request
                    .prompt
                    .len()
                    .checked_add(request.max_new_tokens)
                    .ok_or_else(|| ShadowError::Invariant("context ceiling overflow".into()))?;
                transaction
                    .admit(SlotSpecConfig {
                        handle: request.slot,
                        last_token: *request.verified.last().ok_or_else(|| {
                            ShadowError::Invariant("empty verified context".into())
                        })?,
                        context_len: request.verified.len(),
                        max_context_len,
                        generated_tokens: request.verified.len() - request.prompt.len(),
                        max_new_tokens: request.max_new_tokens,
                        eos_token: Some(self.config.eos_token),
                        requested_k: request.requested_k,
                    })
                    .map_err(ShadowError::Slot)?;
                handles_by_slot.insert(request.slot, *handle);
            }
            let draft_plan = transaction
                .draft_plan(
                    self.config.max_spec_rows.min(candidates.len()),
                    self.config.max_packed_tokens,
                )
                .map_err(ShadowError::Slot)?;
            let proposals = draft_plan
                .rows
                .iter()
                .map(|row| {
                    let handle = *handles_by_slot
                        .get(&row.handle)
                        .expect("every slot-plan row was admitted above");
                    let request = self
                        .requests
                        .get(&handle)
                        .expect("every admitted slot has shadow request");
                    let tokens = self.model.draft_tokens(
                        handle,
                        request.prompt.len(),
                        &request.verified,
                        row.k,
                    );
                    (handle, row.handle, row.k, tokens)
                })
                .collect::<Vec<_>>();
            for (handle, _, expected, tokens) in &proposals {
                if tokens.len() != *expected {
                    return Err(ShadowError::DraftShape {
                        handle: *handle,
                        expected: *expected,
                        observed: tokens.len(),
                    });
                }
            }
            let packed = transaction
                .stage(
                    &draft_plan,
                    proposals
                        .iter()
                        .map(|(_, slot, _, tokens)| SlotProposal {
                            handle: *slot,
                            tokens: tokens.clone(),
                        })
                        .collect(),
                )
                .map_err(ShadowError::Slot)?;

            if let ShadowFault::CancelAfterStage(handle) = fault {
                let request = self
                    .requests
                    .get(handle)
                    .ok_or(ShadowError::MissingRequest(*handle))?;
                if candidates.contains(handle) {
                    transaction
                        .cancel(request.slot)
                        .map_err(ShadowError::Slot)?;
                    self.telemetry.cancelled_staged_rows += 1;
                }
                let outcome = self
                    .scheduler
                    .cancel(*handle, planned_at)
                    .map_err(|_| ShadowError::MissingRequest(*handle))?;
                if outcome != ControlOutcome::PendingCheckpoint {
                    return Err(ShadowError::FaultCancellation(outcome));
                }
                suppressed.insert(*handle);
                fault_cancelled = true;
            }

            let mut target_tokens = self.target_tokens(&packed, &handles_by_slot)?;
            let mut target =
                PackedTargetOutput::for_dispatch(&packed, std::mem::take(&mut target_tokens));
            if *fault == ShadowFault::MalformedPackedTarget {
                target.output_offsets.pop();
            }
            let commits = match transaction.commit(&target) {
                Ok(commits) => commits,
                Err(error) => {
                    self.telemetry.malformed_target_rejections += 1;
                    transaction
                        .abort(packed.dispatch_id)
                        .map_err(ShadowError::Slot)?;
                    self.telemetry.aborted_transactions += 1;
                    return Err(ShadowError::Slot(error));
                }
            };
            let packed_slots: BTreeSet<_> = packed.rows.iter().map(|row| row.handle).collect();
            for candidate in &candidates {
                let request = self
                    .requests
                    .get(candidate)
                    .ok_or(ShadowError::MissingRequest(*candidate))?;
                if !packed_slots.contains(&request.slot) {
                    target_only.push(*candidate);
                }
            }
            if packed.rows.len() < candidates.len() {
                self.telemetry.batch_shrinks += 1;
            }
            if let (Some(min), Some(max)) = (
                commits
                    .rows
                    .iter()
                    .map(|row| row.accepted_draft_tokens)
                    .min(),
                commits
                    .rows
                    .iter()
                    .map(|row| row.accepted_draft_tokens)
                    .max(),
            ) {
                if min != max {
                    self.telemetry.independent_acceptance_transactions += 1;
                }
            }
            for row in &commits.rows {
                let handle = *handles_by_slot
                    .get(&row.handle)
                    .expect("committed row belongs to packed dispatch");
                if !self.discard_at_completion(handle, completed_at, &suppressed)? {
                    additions.insert(handle, row.emitted_tokens.clone());
                }
            }
            self.telemetry.packed_dispatches += 1;
            self.telemetry.packed_rows += packed.rows.len() as u64;
            self.telemetry.packed_target_tokens += packed.input_tokens.len() as u64;
            speculation = Some(ShadowPackedDispatch {
                resident_dispatch_id: plan.dispatch_id(),
                resident_decode_handles: decode_handles.clone(),
                packed,
                commits: commits.rows,
                buffered_handles: buffered.clone(),
                target_only_handles: Vec::new(),
            });
        }

        if let ShadowFault::CancelAfterStage(handle) = fault {
            if !fault_cancelled {
                let outcome = self
                    .scheduler
                    .cancel(*handle, planned_at)
                    .map_err(|_| ShadowError::MissingRequest(*handle))?;
                if outcome != ControlOutcome::PendingCheckpoint {
                    return Err(ShadowError::FaultCancellation(outcome));
                }
                suppressed.insert(*handle);
            }
        }

        // Rows omitted by the bounded packed verifier remain exact through a
        // one-token target path. This is explicit batch shrink, never a shared
        // acceptance clamp and never a dropped scheduler reservation.
        for handle in &target_only {
            if self.discard_at_completion(*handle, completed_at, &suppressed)? {
                continue;
            }
            let request = self
                .requests
                .get(handle)
                .ok_or(ShadowError::MissingRequest(*handle))?;
            additions.insert(
                *handle,
                vec![self
                    .model
                    .target_next(*handle, request.prompt.len(), &request.verified)],
            );
            self.telemetry.target_only_rows += 1;
        }
        self.telemetry.buffered_rows += buffered.len() as u64;

        if let Some(evidence) = speculation.as_mut() {
            evidence.target_only_handles = target_only.clone();
        } else if matches!(fault, ShadowFault::MalformedPackedTarget) {
            return Err(ShadowError::FaultRequiresPackedDispatch);
        }

        let mut items = Vec::with_capacity(plan.items().len());
        for handle in &decode_handles {
            let request = self
                .requests
                .get(handle)
                .ok_or(ShadowError::MissingRequest(*handle))?;
            let token = if self.discard_at_completion(*handle, completed_at, &suppressed)? {
                // Scheduler validates shape before it discards due to a
                // cancellation/deadline checkpoint. The token is never exposed.
                self.model
                    .target_next(*handle, request.prompt.len(), &request.verified)
            } else if let Some(token) = request.pending.front() {
                *token
            } else {
                *additions
                    .get(handle)
                    .and_then(|tokens| tokens.first())
                    .ok_or_else(|| {
                        ShadowError::Invariant("decode reservation received no exact token".into())
                    })?
            };
            items.push(ItemOutput::Decode {
                handle: *handle,
                token,
                eos: token == self.config.eos_token,
            });
        }
        Ok(DecodePreparation {
            completion: DispatchCompletion {
                dispatch_id: plan.dispatch_id(),
                items,
            },
            additions,
            speculation,
        })
    }

    fn target_tokens(
        &self,
        dispatch: &PackedVerifierDispatch,
        handles_by_slot: &BTreeMap<SpecSlotHandle, RequestHandle>,
    ) -> Result<Vec<TokenId>, ShadowError> {
        let mut tokens = Vec::with_capacity(dispatch.input_tokens.len());
        for row in &dispatch.rows {
            let handle = *handles_by_slot
                .get(&row.handle)
                .expect("packed row belongs to a supplied slot");
            let request = self
                .requests
                .get(&handle)
                .ok_or(ShadowError::MissingRequest(handle))?;
            let proposal_end = row.proposal_offset + row.proposal_len;
            let proposals = &dispatch.proposal_tokens[row.proposal_offset..proposal_end];
            let mut prefix = request.verified.clone();
            for index in 0..row.input_len {
                tokens.push(
                    self.model
                        .target_next(handle, request.prompt.len(), &prefix),
                );
                if let Some(proposal) = proposals.get(index) {
                    prefix.push(*proposal);
                }
            }
        }
        Ok(tokens)
    }

    fn discard_at_completion(
        &self,
        handle: RequestHandle,
        completed_at: EngineTime,
        suppressed: &BTreeSet<RequestHandle>,
    ) -> Result<bool, ShadowError> {
        let request = self
            .requests
            .get(&handle)
            .ok_or(ShadowError::MissingRequest(handle))?;
        let scheduler = self
            .scheduler
            .request(handle)
            .map_err(|_| ShadowError::MissingRequest(handle))?;
        Ok(suppressed.contains(&handle)
            || scheduler.cancel_pending
            || request
                .deadline
                .is_some_and(|deadline| completed_at >= deadline))
    }

    fn apply_shadow_progress(
        &mut self,
        additions: BTreeMap<RequestHandle, Vec<TokenId>>,
        report: &ApplyReport,
    ) -> Result<(), ShadowError> {
        for (handle, tokens) in additions {
            if tokens.is_empty() {
                continue;
            }
            let request = self
                .requests
                .get_mut(&handle)
                .ok_or(ShadowError::MissingRequest(handle))?;
            request.verified.extend_from_slice(&tokens);
            request.pending.extend(tokens);
            self.telemetry.buffered_exact_tokens += request.pending.len() as u64;
        }
        for decoded in &report.decoded {
            let request = self
                .requests
                .get_mut(&decoded.handle)
                .ok_or(ShadowError::MissingRequest(decoded.handle))?;
            if decoded.position != request.published.len() {
                return Err(ShadowError::Invariant(
                    "scheduler decoded position does not match published output".into(),
                ));
            }
            let expected = request.pending.pop_front().ok_or_else(|| {
                ShadowError::Invariant("scheduler exposed a token absent from shadow buffer".into())
            })?;
            if expected != decoded.token {
                return Err(ShadowError::Invariant(
                    "scheduler-visible token differs from exact shadow token".into(),
                ));
            }
            request.published.push(decoded.token);
            self.telemetry.scheduler_visible_tokens += 1;
        }
        for event in &report.terminal {
            self.retire(event.clone())?;
        }
        Ok(())
    }

    fn abort_scheduler(&mut self, now: EngineTime) -> Result<(), ShadowError> {
        self.scheduler
            .abort_dispatch(now)
            .map_err(ShadowError::SchedulerAbort)?;
        // `abort_dispatch` can turn an in-flight cancellation/deadline into a
        // terminal event. Reconcile it before returning the original error.
        let events: Vec<_> = self.scheduler.terminal_events().iter().cloned().collect();
        for event in events {
            if self.requests.contains_key(&event.handle) {
                self.retire(event)?;
            }
        }
        Ok(())
    }

    fn retire(&mut self, event: TerminalEvent) -> Result<(), ShadowError> {
        let request = self
            .requests
            .remove(&event.handle)
            .ok_or(ShadowError::MissingRequest(event.handle))?;
        if event.generated_tokens != request.published.len() {
            return Err(ShadowError::Invariant(
                "terminal generated count differs from published output".into(),
            ));
        }
        self.free_slots.insert(request.slot.slot_id);
        self.finished.insert(
            event.handle,
            ShadowFinishedRequest {
                handle: event.handle,
                output_tokens: request.published,
                terminal: event.reason,
            },
        );
        Ok(())
    }

    fn peek_slot_id(&self) -> Result<u32, ShadowAdmissionError> {
        self.free_slots
            .iter()
            .next()
            .copied()
            .or_else(|| u32::try_from(self.next_slot_id).ok())
            .ok_or(ShadowAdmissionError::SlotIdExhausted)
    }

    fn claim_slot_id(&mut self, slot_id: u32) {
        if !self.free_slots.remove(&slot_id) {
            self.next_slot_id += 1;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config() -> ShadowConfig {
        ShadowConfig {
            scheduler: EngineConfig {
                max_active_requests: 4,
                max_queued_requests: 16,
                token_budget_per_tick: 64,
                max_batch_items: 8,
                prefill_chunk_tokens: 64,
                starvation_ticks: 2,
                activation_quantum_tokens: 64,
                terminal_event_capacity: 128,
            },
            max_spec_rows: 8,
            max_packed_tokens: 64,
            eos_token: 0,
        }
    }

    fn request(
        id: u64,
        prompt: &[TokenId],
        max_new_tokens: usize,
        requested_k: usize,
    ) -> ShadowRequestSpec {
        ShadowRequestSpec {
            request_id: RequestId(id),
            prompt: prompt.to_vec(),
            max_new_tokens,
            deadline: None,
            requested_k,
        }
    }

    fn greedy(
        model: &MockExactTarget,
        handle: RequestHandle,
        prompt: &[TokenId],
        max_new_tokens: usize,
    ) -> Vec<TokenId> {
        let mut visible = prompt.to_vec();
        let mut output = Vec::new();
        for _ in 0..max_new_tokens {
            let token = model.target_next(handle, prompt.len(), &visible);
            output.push(token);
            visible.push(token);
            if token == model.eos_token() {
                break;
            }
        }
        output
    }

    fn drain(shadow: &mut ResidentSpecShadow<MockExactTarget>, mut tick: u64) {
        for _ in 0..128 {
            if shadow.live_handles().is_empty() {
                return;
            }
            let _ = shadow.drive_once(EngineTime(tick)).unwrap();
            tick += 1;
        }
        panic!("shadow scheduler did not drain");
    }

    #[test]
    fn ragged_rows_match_greedy_oracle_without_common_minimum_clamp() {
        let mut model = MockExactTarget::new(0);
        model.set_draft_mode(
            RequestId(1),
            MockDraftMode::MismatchAt([0usize].into_iter().collect()),
        );
        model.set_draft_mode(RequestId(2), MockDraftMode::Exact);
        let oracle = model.clone();
        let mut shadow = ResidentSpecShadow::new(config(), model).unwrap();
        let a = shadow
            .admit(request(1, &[11, 12], 4, 3), EngineTime(0))
            .unwrap();
        let b = shadow
            .admit(request(2, &[21, 22], 4, 3), EngineTime(0))
            .unwrap();

        // Prefill, then one two-row ragged verifier transaction.
        shadow.drive_once(EngineTime(0)).unwrap();
        let outcome = shadow.drive_once(EngineTime(1)).unwrap();
        let ShadowDriveOutcome::Applied {
            speculation: Some(evidence),
            ..
        } = outcome
        else {
            panic!("decode should produce packed shadow evidence");
        };
        let commits: BTreeMap<_, _> = evidence
            .commits
            .iter()
            .map(|row| (row.handle.slot_id, row.accepted_draft_tokens))
            .collect();
        assert_eq!(commits[&0], 0);
        assert_eq!(commits[&1], 3);
        assert_eq!(evidence.commits[1].emitted_tokens.len(), 4);
        assert_eq!(shadow.telemetry().independent_acceptance_transactions, 1);

        // The second row carries an exact buffer while the first row keeps
        // speculating, so the next transaction is genuinely smaller.
        let next = shadow.drive_once(EngineTime(2)).unwrap();
        let ShadowDriveOutcome::Applied {
            speculation: Some(next_evidence),
            ..
        } = next
        else {
            panic!("mixed buffered/speculative decode should stay observable");
        };
        assert_eq!(next_evidence.packed.rows.len(), 1);
        assert_eq!(next_evidence.buffered_handles, vec![b]);

        drain(&mut shadow, 3);
        assert_eq!(
            shadow.output(a),
            Some(greedy(&oracle, a, &[11, 12], 4).as_slice())
        );
        assert_eq!(
            shadow.output(b),
            Some(greedy(&oracle, b, &[21, 22], 4).as_slice())
        );
        shadow.check_invariants().unwrap();
    }

    #[test]
    fn malformed_target_is_atomic_and_scheduler_retries_cleanly() {
        let model = MockExactTarget::new(0);
        let oracle = model.clone();
        let mut shadow = ResidentSpecShadow::new(config(), model).unwrap();
        let handle = shadow.admit(request(7, &[7], 3, 2), EngineTime(0)).unwrap();
        shadow.drive_once(EngineTime(0)).unwrap();

        assert!(matches!(
            shadow.drive_once_with_fault(
                EngineTime(1),
                EngineTime(1),
                ShadowFault::MalformedPackedTarget,
            ),
            Err(ShadowError::Slot(SpecError::PackedShape))
        ));
        assert_eq!(shadow.output(handle), Some([].as_slice()));
        assert!(!shadow.scheduler().snapshot().dispatch_in_flight);
        shadow.check_invariants().unwrap();

        drain(&mut shadow, 2);
        assert_eq!(
            shadow.output(handle),
            Some(greedy(&oracle, handle, &[7], 3).as_slice())
        );
        assert_eq!(shadow.telemetry().malformed_target_rejections, 1);
    }

    #[test]
    fn cancellation_deadline_eos_and_slot_epoch_reuse_remain_coherent() {
        let mut model = MockExactTarget::new(0);
        model.set_draft_mode(RequestId(10), MockDraftMode::Exact);
        model.set_eos_after(RequestId(10), 2);
        let oracle = model.clone();
        let mut shadow = ResidentSpecShadow::new(config(), model).unwrap();
        let cancelled = shadow.admit(request(9, &[9], 5, 3), EngineTime(0)).unwrap();
        let old_slot = shadow.slot_for(cancelled).unwrap();
        let eos = shadow
            .admit(request(10, &[10], 5, 3), EngineTime(0))
            .unwrap();
        let mut deadline_spec = request(11, &[11], 5, 3);
        deadline_spec.deadline = Some(EngineTime(3));
        let deadline = shadow.admit(deadline_spec, EngineTime(0)).unwrap();

        shadow.drive_once(EngineTime(0)).unwrap();
        let first = shadow
            .drive_once_with_fault(
                EngineTime(1),
                EngineTime(1),
                ShadowFault::CancelAfterStage(cancelled),
            )
            .unwrap();
        assert!(matches!(first, ShadowDriveOutcome::Applied { .. }));
        assert_eq!(
            shadow.finished(cancelled).unwrap().terminal,
            TerminalReason::Cancelled
        );

        // This plans before the deadline but commits at it: target work is
        // shape-valid yet cannot leak an output into the terminal request.
        let published_before_deadline = shadow.output(deadline).unwrap().to_vec();
        let _ = shadow.drive_once_at(EngineTime(2), EngineTime(3)).unwrap();
        assert_eq!(
            shadow.finished(deadline).unwrap().terminal,
            TerminalReason::DeadlineExceeded
        );
        assert_eq!(
            shadow.output(deadline),
            Some(published_before_deadline.as_slice())
        );

        drain(&mut shadow, 3);
        assert_eq!(shadow.finished(eos).unwrap().terminal, TerminalReason::Eos);
        assert_eq!(
            shadow.output(eos),
            Some(greedy(&oracle, eos, &[10], 5).as_slice())
        );

        let replacement = shadow
            .admit(request(9, &[90], 2, 1), EngineTime(20))
            .unwrap();
        let replacement_slot = shadow.slot_for(replacement).unwrap();
        assert_eq!(replacement_slot.slot_id, old_slot.slot_id);
        assert!(replacement_slot.admission_epoch > old_slot.admission_epoch);
        drain(&mut shadow, 20);
        assert_eq!(
            shadow.output(replacement),
            Some(greedy(&oracle, replacement, &[90], 2).as_slice())
        );
        shadow.check_invariants().unwrap();
    }
}
