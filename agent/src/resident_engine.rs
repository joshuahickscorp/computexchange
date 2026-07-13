//! Deterministic control plane for a long-lived, per-model inference engine.
//!
//! This module deliberately knows nothing about Candle, Metal, CUDA, tokenizers, or
//! KV-cache layout.  It owns the harder lifecycle contract around those components:
//! bounded admission, stable admission epochs, chunked prefill, decode scheduling,
//! cancellation/deadline checkpoints, fair preemption, and atomic result commits.
//! A real model actor can implement [`ResidentExecutor`] and use [`ResidentEngine::drive_once`];
//! the pure state machine remains exhaustively testable without loading a model.

// This complete scheduler is intentionally compile-gated before it is attached to
// the live runner. Keep the whole state machine type-checked and tested without
// pretending that every public integration seam is already routed by the binary.
#![allow(dead_code)]

use std::collections::{BTreeMap, BTreeSet, VecDeque};

/// Caller-owned request identity. An id may be reused only after its live admission ends.
#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct RequestId(pub u64);

/// Process-local, monotonically increasing incarnation of a request admission.
#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct AdmissionEpoch(pub u64);

/// The only identity accepted by lifecycle and completion operations.
#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct RequestHandle {
    pub request_id: RequestId,
    pub admission_epoch: AdmissionEpoch,
}

/// Monotonic scheduler time supplied by the actor (normally milliseconds).
#[derive(Clone, Copy, Debug, Default, Eq, Ord, PartialEq, PartialOrd)]
pub struct EngineTime(pub u64);

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EngineConfig {
    pub max_active_requests: usize,
    pub max_queued_requests: usize,
    pub token_budget_per_tick: usize,
    pub max_batch_items: usize,
    pub prefill_chunk_tokens: usize,
    /// A ready lane at least this old overrides ordinary lane alternation.
    pub starvation_ticks: u64,
    /// Yield an active request to an admission waiter after this much model work.
    pub activation_quantum_tokens: usize,
    pub terminal_event_capacity: usize,
}

impl Default for EngineConfig {
    fn default() -> Self {
        Self {
            max_active_requests: 32,
            max_queued_requests: 256,
            token_budget_per_tick: 2048,
            max_batch_items: 32,
            prefill_chunk_tokens: 256,
            starvation_ticks: 4,
            activation_quantum_tokens: 512,
            terminal_event_capacity: 1024,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ConfigError {
    NoActiveSlots,
    NoTokenBudget,
    EmptyBatch,
    EmptyPrefillChunk,
    MissingActivationQuantum,
    NoTerminalBuffer,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RequestSpec {
    pub request_id: RequestId,
    pub prompt_tokens: usize,
    pub max_new_tokens: usize,
    /// The request is expired when scheduler time is greater than or equal to this value.
    pub deadline: Option<EngineTime>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum AdmissionError {
    EmptyPrompt,
    ZeroMaxNewTokens,
    DeadlineExpired,
    DuplicateLiveRequest(RequestHandle),
    Backpressure {
        active: usize,
        queued: usize,
    },
    EpochExhausted,
    NonMonotonicTime {
        previous: EngineTime,
        observed: EngineTime,
    },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum HandleError {
    Unknown(RequestId),
    Stale {
        handle: RequestHandle,
        latest_epoch: AdmissionEpoch,
    },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Lane {
    Prefill,
    Decode,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum WorkKind {
    Prefill { offset: usize, tokens: usize },
    Decode { position: usize },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WorkItem {
    handle: RequestHandle,
    kind: WorkKind,
}

impl WorkItem {
    pub fn handle(&self) -> RequestHandle {
        self.handle
    }

    pub fn kind(&self) -> &WorkKind {
        &self.kind
    }

    pub fn token_cost(&self) -> usize {
        match self.kind {
            WorkKind::Prefill { tokens, .. } => tokens,
            WorkKind::Decode { .. } => 1,
        }
    }
}

/// An immutable reservation of model work. Only the scheduler can construct one.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DispatchPlan {
    dispatch_id: u64,
    created_at: EngineTime,
    lane: Lane,
    items: Vec<WorkItem>,
    token_cost: usize,
}

impl DispatchPlan {
    pub fn dispatch_id(&self) -> u64 {
        self.dispatch_id
    }

    pub fn created_at(&self) -> EngineTime {
        self.created_at
    }

    pub fn lane(&self) -> Lane {
        self.lane
    }

    pub fn items(&self) -> &[WorkItem] {
        &self.items
    }

    pub fn token_cost(&self) -> usize {
        self.token_cost
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ItemOutput {
    Prefill {
        handle: RequestHandle,
        processed_tokens: usize,
    },
    Decode {
        handle: RequestHandle,
        token: u32,
        eos: bool,
    },
}

impl ItemOutput {
    pub fn handle(&self) -> RequestHandle {
        match *self {
            Self::Prefill { handle, .. } | Self::Decode { handle, .. } => handle,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DispatchCompletion {
    pub dispatch_id: u64,
    pub items: Vec<ItemOutput>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum CompletionError {
    NoDispatchInFlight,
    WrongDispatch {
        expected: u64,
        observed: u64,
    },
    WrongItemCount {
        expected: usize,
        observed: usize,
    },
    DuplicateHandle(RequestHandle),
    HandleMismatch {
        expected: RequestHandle,
        observed: RequestHandle,
    },
    StaleHandle {
        handle: RequestHandle,
        latest_epoch: AdmissionEpoch,
    },
    WrongOutputKind(RequestHandle),
    WrongPrefillCount {
        handle: RequestHandle,
        expected: usize,
        observed: usize,
    },
    ReservationLost(RequestHandle),
    NonMonotonicTime {
        previous: EngineTime,
        observed: EngineTime,
    },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TerminalReason {
    Eos,
    MaxTokens,
    Cancelled,
    DeadlineExceeded,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TerminalEvent {
    pub handle: RequestHandle,
    pub reason: TerminalReason,
    pub generated_tokens: usize,
    pub at: EngineTime,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DecodedToken {
    pub handle: RequestHandle,
    pub position: usize,
    pub token: u32,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct ApplyReport {
    pub decoded: Vec<DecodedToken>,
    pub terminal: Vec<TerminalEvent>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ControlOutcome {
    Applied,
    PendingCheckpoint,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RequestStatus {
    Waiting,
    ReadyPrefill,
    ReadyDecode,
    InFlightPrefill,
    InFlightDecode,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RequestSnapshot {
    pub handle: RequestHandle,
    pub status: RequestStatus,
    pub prompt_tokens: usize,
    pub prefilled_tokens: usize,
    pub generated_tokens: usize,
    pub max_new_tokens: usize,
    pub cancel_pending: bool,
    pub preempt_pending: bool,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct Telemetry {
    pub admitted: u64,
    pub backpressured: u64,
    pub dispatches: u64,
    pub prefill_tokens: u64,
    pub decode_tokens: u64,
    pub completed_eos: u64,
    pub completed_max_tokens: u64,
    pub cancelled: u64,
    pub deadline_expired: u64,
    pub preemptions: u64,
    pub executor_errors: u64,
    pub rejected_completions: u64,
    pub stale_completions: u64,
    pub max_ready_wait_ticks: u64,
    pub max_queue_depth: usize,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EngineSnapshot {
    pub active: usize,
    pub queued: usize,
    pub ready_prefill: usize,
    pub ready_decode: usize,
    pub dispatch_in_flight: bool,
    pub budget_tick: Option<EngineTime>,
    pub budget_used: usize,
    pub telemetry: Telemetry,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Phase {
    Prefill,
    Decode,
}

#[derive(Clone, Debug)]
struct Reservation {
    dispatch_id: u64,
    kind: WorkKind,
}

#[derive(Clone, Debug)]
struct RequestEntry {
    handle: RequestHandle,
    prompt_tokens: usize,
    prefilled_tokens: usize,
    generated_tokens: usize,
    max_new_tokens: usize,
    deadline: Option<EngineTime>,
    phase: Phase,
    active: bool,
    ready_since: EngineTime,
    activation_tokens: usize,
    reservation: Option<Reservation>,
    cancel_pending: bool,
    preempt_pending: bool,
}

/// Executor boundary used by a resident per-model actor.
pub trait ResidentExecutor {
    type Error;

    fn execute(&mut self, plan: &DispatchPlan) -> Result<DispatchCompletion, Self::Error>;
}

#[derive(Debug)]
pub enum DriveError<E> {
    Scheduler(CompletionError),
    Executor(E),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum DriveOutcome {
    Idle,
    Applied(ApplyReport),
}

pub struct ResidentEngine {
    config: EngineConfig,
    entries: BTreeMap<RequestId, RequestEntry>,
    latest_epochs: BTreeMap<RequestId, AdmissionEpoch>,
    waiting: VecDeque<RequestHandle>,
    prefill_ready: VecDeque<RequestHandle>,
    decode_ready: VecDeque<RequestHandle>,
    active_count: usize,
    next_epoch: u64,
    next_dispatch_id: u64,
    next_lane: Lane,
    in_flight: Option<DispatchPlan>,
    last_time: Option<EngineTime>,
    budget_tick: Option<EngineTime>,
    budget_used: usize,
    terminal_events: VecDeque<TerminalEvent>,
    telemetry: Telemetry,
}

impl ResidentEngine {
    pub fn new(config: EngineConfig) -> Result<Self, ConfigError> {
        if config.max_active_requests == 0 {
            return Err(ConfigError::NoActiveSlots);
        }
        if config.token_budget_per_tick == 0 {
            return Err(ConfigError::NoTokenBudget);
        }
        if config.max_batch_items == 0 {
            return Err(ConfigError::EmptyBatch);
        }
        if config.prefill_chunk_tokens == 0 {
            return Err(ConfigError::EmptyPrefillChunk);
        }
        if config.activation_quantum_tokens == 0 {
            return Err(ConfigError::MissingActivationQuantum);
        }
        if config.terminal_event_capacity == 0 {
            return Err(ConfigError::NoTerminalBuffer);
        }
        Ok(Self {
            config,
            entries: BTreeMap::new(),
            latest_epochs: BTreeMap::new(),
            waiting: VecDeque::new(),
            prefill_ready: VecDeque::new(),
            decode_ready: VecDeque::new(),
            active_count: 0,
            next_epoch: 1,
            next_dispatch_id: 1,
            next_lane: Lane::Decode,
            in_flight: None,
            last_time: None,
            budget_tick: None,
            budget_used: 0,
            terminal_events: VecDeque::new(),
            telemetry: Telemetry::default(),
        })
    }

    pub fn config(&self) -> &EngineConfig {
        &self.config
    }

    pub fn telemetry(&self) -> &Telemetry {
        &self.telemetry
    }

    pub fn snapshot(&self) -> EngineSnapshot {
        EngineSnapshot {
            active: self.active_count,
            queued: self.waiting.len(),
            ready_prefill: self.prefill_ready.len(),
            ready_decode: self.decode_ready.len(),
            dispatch_in_flight: self.in_flight.is_some(),
            budget_tick: self.budget_tick,
            budget_used: self.budget_used,
            telemetry: self.telemetry.clone(),
        }
    }

    pub fn terminal_events(&self) -> &VecDeque<TerminalEvent> {
        &self.terminal_events
    }

    pub fn request(&self, handle: RequestHandle) -> Result<RequestSnapshot, HandleError> {
        let entry = self.live_entry(handle)?;
        let status = match (&entry.reservation, entry.active, entry.phase) {
            (Some(_), _, Phase::Prefill) => RequestStatus::InFlightPrefill,
            (Some(_), _, Phase::Decode) => RequestStatus::InFlightDecode,
            (None, false, _) => RequestStatus::Waiting,
            (None, true, Phase::Prefill) => RequestStatus::ReadyPrefill,
            (None, true, Phase::Decode) => RequestStatus::ReadyDecode,
        };
        Ok(RequestSnapshot {
            handle,
            status,
            prompt_tokens: entry.prompt_tokens,
            prefilled_tokens: entry.prefilled_tokens,
            generated_tokens: entry.generated_tokens,
            max_new_tokens: entry.max_new_tokens,
            cancel_pending: entry.cancel_pending,
            preempt_pending: entry.preempt_pending,
        })
    }

    pub fn admit(
        &mut self,
        spec: RequestSpec,
        now: EngineTime,
    ) -> Result<RequestHandle, AdmissionError> {
        self.observe_time_admission(now)?;
        self.expire_non_inflight(now);
        self.activate_waiters(now);
        if spec.prompt_tokens == 0 {
            return Err(AdmissionError::EmptyPrompt);
        }
        if spec.max_new_tokens == 0 {
            return Err(AdmissionError::ZeroMaxNewTokens);
        }
        if spec.deadline.is_some_and(|deadline| now >= deadline) {
            return Err(AdmissionError::DeadlineExpired);
        }
        if let Some(entry) = self.entries.get(&spec.request_id) {
            return Err(AdmissionError::DuplicateLiveRequest(entry.handle));
        }
        if self.active_count >= self.config.max_active_requests
            && self.waiting.len() >= self.config.max_queued_requests
        {
            self.telemetry.backpressured += 1;
            return Err(AdmissionError::Backpressure {
                active: self.active_count,
                queued: self.waiting.len(),
            });
        }
        let epoch = self
            .next_epoch
            .checked_add(1)
            .ok_or(AdmissionError::EpochExhausted)?;
        let handle = RequestHandle {
            request_id: spec.request_id,
            admission_epoch: AdmissionEpoch(self.next_epoch),
        };
        self.next_epoch = epoch;
        let active = self.active_count < self.config.max_active_requests;
        let entry = RequestEntry {
            handle,
            prompt_tokens: spec.prompt_tokens,
            prefilled_tokens: 0,
            generated_tokens: 0,
            max_new_tokens: spec.max_new_tokens,
            deadline: spec.deadline,
            phase: Phase::Prefill,
            active,
            ready_since: now,
            activation_tokens: 0,
            reservation: None,
            cancel_pending: false,
            preempt_pending: false,
        };
        self.entries.insert(spec.request_id, entry);
        self.latest_epochs
            .insert(spec.request_id, handle.admission_epoch);
        if active {
            self.active_count += 1;
            self.prefill_ready.push_back(handle);
        } else {
            self.waiting.push_back(handle);
            self.telemetry.max_queue_depth = self.telemetry.max_queue_depth.max(self.waiting.len());
        }
        self.telemetry.admitted += 1;
        debug_assert!(self.check_invariants().is_ok());
        Ok(handle)
    }

    /// Mark a request cancelled. In-flight work is discarded at its completion checkpoint.
    pub fn cancel(
        &mut self,
        handle: RequestHandle,
        now: EngineTime,
    ) -> Result<ControlOutcome, HandleError> {
        let now = self.observe_time_lossy(now);
        self.expire_non_inflight(now);
        let in_flight = self.live_entry(handle)?.reservation.is_some();
        if in_flight {
            self.entries
                .get_mut(&handle.request_id)
                .unwrap()
                .cancel_pending = true;
            return Ok(ControlOutcome::PendingCheckpoint);
        }
        self.finish(handle, TerminalReason::Cancelled, now);
        self.activate_waiters(now);
        debug_assert!(self.check_invariants().is_ok());
        Ok(ControlOutcome::Applied)
    }

    /// Yield at the next safe boundary. Progress already produced by in-flight work is kept.
    pub fn preempt(
        &mut self,
        handle: RequestHandle,
        now: EngineTime,
    ) -> Result<ControlOutcome, HandleError> {
        let now = self.observe_time_lossy(now);
        self.expire_non_inflight(now);
        let in_flight = self.live_entry(handle)?.reservation.is_some();
        if in_flight {
            self.entries
                .get_mut(&handle.request_id)
                .unwrap()
                .preempt_pending = true;
            return Ok(ControlOutcome::PendingCheckpoint);
        }
        self.yield_to_waiting(handle, now);
        self.activate_waiters(now);
        debug_assert!(self.check_invariants().is_ok());
        Ok(ControlOutcome::Applied)
    }

    /// Expire deadlines and return terminal events created by this checkpoint.
    pub fn poll(&mut self, now: EngineTime) -> Vec<TerminalEvent> {
        let now = self.observe_time_lossy(now);
        let events = self.expire_non_inflight(now);
        self.activate_waiters(now);
        debug_assert!(self.check_invariants().is_ok());
        events
    }

    /// Reserve one deterministic batch. At most one batch may be in flight per model actor.
    pub fn plan(&mut self, now: EngineTime) -> Result<Option<DispatchPlan>, CompletionError> {
        self.observe_time(now)?;
        self.expire_non_inflight(now);
        self.activate_waiters(now);
        if self.in_flight.is_some() {
            return Ok(None);
        }
        if self.budget_tick != Some(now) {
            self.budget_tick = Some(now);
            self.budget_used = 0;
        }
        let remaining_budget = self
            .config
            .token_budget_per_tick
            .saturating_sub(self.budget_used);
        if remaining_budget == 0 {
            return Ok(None);
        }
        let lane = match self.choose_lane(now) {
            Some(lane) => lane,
            None => return Ok(None),
        };
        let queue = match lane {
            Lane::Prefill => &mut self.prefill_ready,
            Lane::Decode => &mut self.decode_ready,
        };
        let mut items = Vec::new();
        let mut cost = 0usize;
        while items.len() < self.config.max_batch_items && cost < remaining_budget {
            let Some(handle) = queue.pop_front() else {
                break;
            };
            let entry = self
                .entries
                .get(&handle.request_id)
                .expect("ready handle is live");
            let kind = match lane {
                Lane::Prefill => {
                    let tokens = (entry.prompt_tokens - entry.prefilled_tokens)
                        .min(self.config.prefill_chunk_tokens)
                        .min(remaining_budget - cost);
                    WorkKind::Prefill {
                        offset: entry.prefilled_tokens,
                        tokens,
                    }
                }
                Lane::Decode => WorkKind::Decode {
                    position: entry.prompt_tokens + entry.generated_tokens,
                },
            };
            cost += match kind {
                WorkKind::Prefill { tokens, .. } => tokens,
                WorkKind::Decode { .. } => 1,
            };
            items.push(WorkItem { handle, kind });
        }
        if items.is_empty() {
            return Ok(None);
        }
        let dispatch_id = self.next_dispatch_id;
        self.next_dispatch_id = self.next_dispatch_id.wrapping_add(1).max(1);
        for item in &items {
            let entry = self.entries.get_mut(&item.handle.request_id).unwrap();
            let waited = now.0.saturating_sub(entry.ready_since.0);
            self.telemetry.max_ready_wait_ticks = self.telemetry.max_ready_wait_ticks.max(waited);
            entry.reservation = Some(Reservation {
                dispatch_id,
                kind: item.kind.clone(),
            });
        }
        let plan = DispatchPlan {
            dispatch_id,
            created_at: now,
            lane,
            items,
            token_cost: cost,
        };
        self.budget_used += cost;
        self.telemetry.dispatches += 1;
        self.in_flight = Some(plan.clone());
        debug_assert!(self.check_invariants().is_ok());
        Ok(Some(plan))
    }

    /// Atomically validate and commit a complete model batch.
    pub fn apply_completion(
        &mut self,
        completion: DispatchCompletion,
        now: EngineTime,
    ) -> Result<ApplyReport, CompletionError> {
        self.observe_time(now)?;
        if let Err(error) = self.validate_completion(&completion) {
            self.telemetry.rejected_completions += 1;
            if matches!(error, CompletionError::StaleHandle { .. }) {
                self.telemetry.stale_completions += 1;
            }
            return Err(error);
        }
        let plan = self.in_flight.take().unwrap();
        let mut report = ApplyReport::default();
        for (item, output) in plan.items.into_iter().zip(completion.items) {
            let handle = item.handle;
            let expired;
            let cancelled;
            {
                let entry = self.entries.get(&handle.request_id).unwrap();
                expired = entry.deadline.is_some_and(|deadline| now >= deadline);
                cancelled = entry.cancel_pending;
            }
            if cancelled || expired {
                let reason = if cancelled {
                    TerminalReason::Cancelled
                } else {
                    TerminalReason::DeadlineExceeded
                };
                let event = self.finish(handle, reason, now);
                report.terminal.push(event);
                continue;
            }

            let mut terminal = None;
            let mut decoded = None;
            let (phase, should_yield) = {
                let entry = self.entries.get_mut(&handle.request_id).unwrap();
                entry.reservation = None;
                match output {
                    ItemOutput::Prefill {
                        processed_tokens, ..
                    } => {
                        entry.prefilled_tokens += processed_tokens;
                        entry.activation_tokens += processed_tokens;
                        self.telemetry.prefill_tokens += processed_tokens as u64;
                        if entry.prefilled_tokens == entry.prompt_tokens {
                            entry.phase = Phase::Decode;
                        }
                    }
                    ItemOutput::Decode { token, eos, .. } => {
                        let position = entry.generated_tokens;
                        entry.generated_tokens += 1;
                        entry.activation_tokens += 1;
                        self.telemetry.decode_tokens += 1;
                        decoded = Some(DecodedToken {
                            handle,
                            position,
                            token,
                        });
                        if eos {
                            terminal = Some(TerminalReason::Eos);
                        } else if entry.generated_tokens >= entry.max_new_tokens {
                            terminal = Some(TerminalReason::MaxTokens);
                        }
                    }
                }
                (
                    entry.phase,
                    entry.preempt_pending
                        || (entry.activation_tokens >= self.config.activation_quantum_tokens
                            && !self.waiting.is_empty()),
                )
            };
            if let Some(token) = decoded {
                report.decoded.push(token);
            }
            if let Some(reason) = terminal {
                let event = self.finish(handle, reason, now);
                report.terminal.push(event);
            } else if should_yield {
                self.yield_to_waiting(handle, now);
            } else {
                self.enqueue_ready(handle, phase, now);
            }
        }
        self.expire_non_inflight_into(now, &mut report.terminal);
        self.activate_waiters(now);
        debug_assert!(self.check_invariants().is_ok());
        Ok(report)
    }

    /// Restore a failed batch reservation without committing model-visible progress.
    pub fn abort_dispatch(&mut self, now: EngineTime) -> Result<(), CompletionError> {
        self.observe_time(now)?;
        let Some(plan) = self.in_flight.take() else {
            return Err(CompletionError::NoDispatchInFlight);
        };
        self.telemetry.executor_errors += 1;
        let mut requeue = Vec::new();
        for item in plan.items {
            let handle = item.handle;
            let (cancelled, expired, preempted, phase) = {
                let entry = self.entries.get_mut(&handle.request_id).unwrap();
                entry.reservation = None;
                (
                    entry.cancel_pending,
                    entry.deadline.is_some_and(|deadline| now >= deadline),
                    entry.preempt_pending,
                    entry.phase,
                )
            };
            if cancelled {
                self.finish(handle, TerminalReason::Cancelled, now);
            } else if expired {
                self.finish(handle, TerminalReason::DeadlineExceeded, now);
            } else if preempted {
                self.yield_to_waiting(handle, now);
            } else {
                requeue.push((handle, phase));
            }
        }
        // Failed work retains its lane position ahead of later arrivals.
        for (handle, phase) in requeue.into_iter().rev() {
            self.enqueue_ready_front(handle, phase, now);
        }
        self.activate_waiters(now);
        debug_assert!(self.check_invariants().is_ok());
        Ok(())
    }

    pub fn drive_once<E: ResidentExecutor>(
        &mut self,
        executor: &mut E,
        now: EngineTime,
    ) -> Result<DriveOutcome, DriveError<E::Error>> {
        let plan = self.plan(now).map_err(DriveError::Scheduler)?;
        let Some(plan) = plan else {
            return Ok(DriveOutcome::Idle);
        };
        match executor.execute(&plan) {
            Ok(completion) => match self.apply_completion(completion, now) {
                Ok(report) => Ok(DriveOutcome::Applied(report)),
                Err(error) => {
                    // A malformed executor response is as unsafe as an execution error.
                    // `apply_completion` prevalidated it without touching request progress;
                    // release the reservation so one bad adapter response cannot wedge the
                    // resident model actor forever.
                    self.abort_dispatch(now)
                        .expect("rejected completion retains its reservation");
                    Err(DriveError::Scheduler(error))
                }
            },
            Err(error) => {
                // The reservation is rolled back at the same checkpoint; a later tick retries it.
                self.abort_dispatch(now)
                    .expect("fresh reservation can be aborted");
                Err(DriveError::Executor(error))
            }
        }
    }

    fn observe_time(&mut self, now: EngineTime) -> Result<(), CompletionError> {
        if let Some(previous) = self.last_time {
            if now < previous {
                return Err(CompletionError::NonMonotonicTime {
                    previous,
                    observed: now,
                });
            }
        }
        self.last_time = Some(now);
        Ok(())
    }

    fn observe_time_admission(&mut self, now: EngineTime) -> Result<(), AdmissionError> {
        if let Some(previous) = self.last_time {
            if now < previous {
                return Err(AdmissionError::NonMonotonicTime {
                    previous,
                    observed: now,
                });
            }
        }
        self.last_time = Some(now);
        Ok(())
    }

    fn observe_time_lossy(&mut self, observed: EngineTime) -> EngineTime {
        // Control messages may race through different channels. Their API is intentionally
        // infallible with respect to time, so clamp an older timestamp instead of letting it
        // move terminal timestamps/ready ages backwards.
        let now = self
            .last_time
            .map(|previous| previous.max(observed))
            .unwrap_or(observed);
        self.last_time = Some(now);
        now
    }

    fn live_entry(&self, handle: RequestHandle) -> Result<&RequestEntry, HandleError> {
        match self.entries.get(&handle.request_id) {
            Some(entry) if entry.handle == handle => Ok(entry),
            Some(entry) => Err(HandleError::Stale {
                handle,
                latest_epoch: entry.handle.admission_epoch,
            }),
            None => match self.latest_epochs.get(&handle.request_id) {
                Some(latest_epoch) if *latest_epoch != handle.admission_epoch => {
                    Err(HandleError::Stale {
                        handle,
                        latest_epoch: *latest_epoch,
                    })
                }
                _ => Err(HandleError::Unknown(handle.request_id)),
            },
        }
    }

    fn choose_lane(&mut self, now: EngineTime) -> Option<Lane> {
        match (self.prefill_ready.front(), self.decode_ready.front()) {
            (None, None) => None,
            (Some(_), None) => Some(Lane::Prefill),
            (None, Some(_)) => Some(Lane::Decode),
            (Some(prefill), Some(decode)) => {
                let p_age = self.ready_age(*prefill, now);
                let d_age = self.ready_age(*decode, now);
                let chosen = if p_age >= self.config.starvation_ticks
                    || d_age >= self.config.starvation_ticks
                {
                    if p_age > d_age {
                        Lane::Prefill
                    } else if d_age > p_age {
                        Lane::Decode
                    } else {
                        self.next_lane
                    }
                } else {
                    self.next_lane
                };
                self.next_lane = match chosen {
                    Lane::Prefill => Lane::Decode,
                    Lane::Decode => Lane::Prefill,
                };
                Some(chosen)
            }
        }
    }

    fn ready_age(&self, handle: RequestHandle, now: EngineTime) -> u64 {
        self.entries
            .get(&handle.request_id)
            .map(|entry| now.0.saturating_sub(entry.ready_since.0))
            .unwrap_or(0)
    }

    fn enqueue_ready(&mut self, handle: RequestHandle, phase: Phase, now: EngineTime) {
        let entry = self.entries.get_mut(&handle.request_id).unwrap();
        entry.active = true;
        entry.ready_since = now;
        entry.phase = phase;
        entry.preempt_pending = false;
        match phase {
            Phase::Prefill => self.prefill_ready.push_back(handle),
            Phase::Decode => self.decode_ready.push_back(handle),
        }
    }

    fn enqueue_ready_front(&mut self, handle: RequestHandle, phase: Phase, now: EngineTime) {
        let entry = self.entries.get_mut(&handle.request_id).unwrap();
        entry.active = true;
        entry.ready_since = now;
        entry.phase = phase;
        entry.preempt_pending = false;
        match phase {
            Phase::Prefill => self.prefill_ready.push_front(handle),
            Phase::Decode => self.decode_ready.push_front(handle),
        }
    }

    fn yield_to_waiting(&mut self, handle: RequestHandle, now: EngineTime) {
        self.remove_from_ready(handle);
        let entry = self.entries.get_mut(&handle.request_id).unwrap();
        debug_assert!(entry.reservation.is_none());
        if entry.active {
            self.active_count -= 1;
        }
        entry.active = false;
        entry.preempt_pending = false;
        entry.activation_tokens = 0;
        entry.ready_since = now;
        self.waiting.push_back(handle);
        self.telemetry.preemptions += 1;
        self.telemetry.max_queue_depth = self.telemetry.max_queue_depth.max(self.waiting.len());
    }

    fn activate_waiters(&mut self, now: EngineTime) {
        while self.active_count < self.config.max_active_requests {
            let Some(handle) = self.waiting.pop_front() else {
                break;
            };
            let Some(entry) = self.entries.get_mut(&handle.request_id) else {
                continue;
            };
            if entry.handle != handle || entry.active || entry.reservation.is_some() {
                continue;
            }
            entry.active = true;
            entry.activation_tokens = 0;
            let phase = entry.phase;
            self.active_count += 1;
            self.enqueue_ready(handle, phase, now);
        }
    }

    fn remove_from_ready(&mut self, handle: RequestHandle) {
        self.prefill_ready.retain(|candidate| *candidate != handle);
        self.decode_ready.retain(|candidate| *candidate != handle);
        self.waiting.retain(|candidate| *candidate != handle);
    }

    fn finish(
        &mut self,
        handle: RequestHandle,
        reason: TerminalReason,
        now: EngineTime,
    ) -> TerminalEvent {
        self.remove_from_ready(handle);
        let entry = self
            .entries
            .remove(&handle.request_id)
            .expect("finishing live request");
        if entry.active {
            self.active_count -= 1;
        }
        match reason {
            TerminalReason::Eos => self.telemetry.completed_eos += 1,
            TerminalReason::MaxTokens => self.telemetry.completed_max_tokens += 1,
            TerminalReason::Cancelled => self.telemetry.cancelled += 1,
            TerminalReason::DeadlineExceeded => self.telemetry.deadline_expired += 1,
        }
        let event = TerminalEvent {
            handle,
            reason,
            generated_tokens: entry.generated_tokens,
            at: now,
        };
        if self.terminal_events.len() == self.config.terminal_event_capacity {
            self.terminal_events.pop_front();
        }
        self.terminal_events.push_back(event.clone());
        event
    }

    fn expire_non_inflight(&mut self, now: EngineTime) -> Vec<TerminalEvent> {
        let mut events = Vec::new();
        self.expire_non_inflight_into(now, &mut events);
        events
    }

    fn expire_non_inflight_into(&mut self, now: EngineTime, events: &mut Vec<TerminalEvent>) {
        let expired: Vec<_> = self
            .entries
            .values()
            .filter(|entry| {
                entry.reservation.is_none()
                    && entry.deadline.is_some_and(|deadline| now >= deadline)
            })
            .map(|entry| entry.handle)
            .collect();
        for handle in expired {
            events.push(self.finish(handle, TerminalReason::DeadlineExceeded, now));
        }
    }

    fn validate_completion(&self, completion: &DispatchCompletion) -> Result<(), CompletionError> {
        let Some(plan) = &self.in_flight else {
            return Err(CompletionError::NoDispatchInFlight);
        };
        if plan.dispatch_id != completion.dispatch_id {
            return Err(CompletionError::WrongDispatch {
                expected: plan.dispatch_id,
                observed: completion.dispatch_id,
            });
        }
        if plan.items.len() != completion.items.len() {
            return Err(CompletionError::WrongItemCount {
                expected: plan.items.len(),
                observed: completion.items.len(),
            });
        }
        let mut seen = BTreeSet::new();
        for (expected, observed) in plan.items.iter().zip(&completion.items) {
            let observed_handle = observed.handle();
            if !seen.insert(observed_handle) {
                return Err(CompletionError::DuplicateHandle(observed_handle));
            }
            if expected.handle != observed_handle {
                if expected.handle.request_id == observed_handle.request_id {
                    let latest_epoch = self
                        .latest_epochs
                        .get(&observed_handle.request_id)
                        .copied()
                        .unwrap_or(expected.handle.admission_epoch);
                    if observed_handle.admission_epoch != latest_epoch {
                        return Err(CompletionError::StaleHandle {
                            handle: observed_handle,
                            latest_epoch,
                        });
                    }
                }
                return Err(CompletionError::HandleMismatch {
                    expected: expected.handle,
                    observed: observed_handle,
                });
            }
            match (&expected.kind, observed) {
                (
                    WorkKind::Prefill { tokens, .. },
                    ItemOutput::Prefill {
                        processed_tokens, ..
                    },
                ) if tokens == processed_tokens => {}
                (
                    WorkKind::Prefill { tokens, .. },
                    ItemOutput::Prefill {
                        processed_tokens, ..
                    },
                ) => {
                    return Err(CompletionError::WrongPrefillCount {
                        handle: expected.handle,
                        expected: *tokens,
                        observed: *processed_tokens,
                    });
                }
                (WorkKind::Decode { .. }, ItemOutput::Decode { .. }) => {}
                _ => return Err(CompletionError::WrongOutputKind(expected.handle)),
            }
            let entry = self
                .entries
                .get(&expected.handle.request_id)
                .ok_or(CompletionError::ReservationLost(expected.handle))?;
            if entry.handle != expected.handle
                || !matches!(
                    &entry.reservation,
                    Some(reservation)
                        if reservation.dispatch_id == plan.dispatch_id
                            && reservation.kind == expected.kind
                )
            {
                return Err(CompletionError::ReservationLost(expected.handle));
            }
        }
        Ok(())
    }

    fn check_invariants(&self) -> Result<(), String> {
        let active = self.entries.values().filter(|entry| entry.active).count();
        if active != self.active_count || active > self.config.max_active_requests {
            return Err("active count/capacity mismatch".into());
        }
        if self.waiting.len() > self.config.max_queued_requests {
            return Err("waiting queue exceeds capacity".into());
        }
        let mut membership = BTreeMap::<RequestHandle, usize>::new();
        for handle in self
            .waiting
            .iter()
            .chain(&self.prefill_ready)
            .chain(&self.decode_ready)
        {
            *membership.entry(*handle).or_default() += 1;
        }
        for entry in self.entries.values() {
            let count = membership.get(&entry.handle).copied().unwrap_or(0);
            if entry.reservation.is_some() {
                if !entry.active || count != 0 {
                    return Err("reserved request is queued or inactive".into());
                }
            } else if count != 1 {
                return Err("non-reserved request does not have one queue membership".into());
            } else if entry.active == self.waiting.contains(&entry.handle) {
                return Err("active/waiting membership mismatch".into());
            }
        }
        if let Some(plan) = &self.in_flight {
            if plan.items.is_empty()
                || plan.token_cost == 0
                || plan.token_cost > self.config.token_budget_per_tick
            {
                return Err("invalid in-flight plan".into());
            }
        } else if self
            .entries
            .values()
            .any(|entry| entry.reservation.is_some())
        {
            return Err("orphan reservation".into());
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config() -> EngineConfig {
        EngineConfig {
            max_active_requests: 2,
            max_queued_requests: 8,
            token_budget_per_tick: 8,
            max_batch_items: 4,
            prefill_chunk_tokens: 3,
            starvation_ticks: 2,
            activation_quantum_tokens: 4,
            terminal_event_capacity: 10_000,
        }
    }

    fn spec(id: u64, prompt: usize, max_new: usize) -> RequestSpec {
        RequestSpec {
            request_id: RequestId(id),
            prompt_tokens: prompt,
            max_new_tokens: max_new,
            deadline: None,
        }
    }

    fn successful(plan: &DispatchPlan) -> DispatchCompletion {
        successful_with(plan, |_| false)
    }

    fn successful_with(
        plan: &DispatchPlan,
        mut eos: impl FnMut(RequestHandle) -> bool,
    ) -> DispatchCompletion {
        DispatchCompletion {
            dispatch_id: plan.dispatch_id(),
            items: plan
                .items()
                .iter()
                .map(|item| match *item.kind() {
                    WorkKind::Prefill { tokens, .. } => ItemOutput::Prefill {
                        handle: item.handle(),
                        processed_tokens: tokens,
                    },
                    WorkKind::Decode { position } => ItemOutput::Decode {
                        handle: item.handle(),
                        token: 10_000 + position as u32,
                        eos: eos(item.handle()),
                    },
                })
                .collect(),
        }
    }

    fn apply_one(engine: &mut ResidentEngine, now: u64) -> Option<ApplyReport> {
        let plan = engine.plan(EngineTime(now)).unwrap()?;
        Some(
            engine
                .apply_completion(successful(&plan), EngineTime(now))
                .unwrap(),
        )
    }

    fn drain(engine: &mut ResidentEngine, first_tick: u64, limit: usize) {
        for tick in (first_tick..).take(limit) {
            if engine.entries.is_empty() {
                return;
            }
            while let Some(plan) = engine.plan(EngineTime(tick)).unwrap() {
                let completion = successful(&plan);
                engine
                    .apply_completion(completion, EngineTime(tick))
                    .unwrap();
            }
            assert!(engine.check_invariants().is_ok());
        }
        panic!("engine did not drain; snapshot={:?}", engine.snapshot());
    }

    #[test]
    fn config_rejects_every_zero_safety_bound() {
        let mut c = config();
        c.max_active_requests = 0;
        assert!(matches!(
            ResidentEngine::new(c),
            Err(ConfigError::NoActiveSlots)
        ));
        let mut c = config();
        c.token_budget_per_tick = 0;
        assert!(matches!(
            ResidentEngine::new(c),
            Err(ConfigError::NoTokenBudget)
        ));
        let mut c = config();
        c.max_batch_items = 0;
        assert!(matches!(
            ResidentEngine::new(c),
            Err(ConfigError::EmptyBatch)
        ));
        let mut c = config();
        c.prefill_chunk_tokens = 0;
        assert!(matches!(
            ResidentEngine::new(c),
            Err(ConfigError::EmptyPrefillChunk)
        ));
        let mut c = config();
        c.activation_quantum_tokens = 0;
        assert!(matches!(
            ResidentEngine::new(c),
            Err(ConfigError::MissingActivationQuantum)
        ));
        let mut c = config();
        c.terminal_event_capacity = 0;
        assert!(matches!(
            ResidentEngine::new(c),
            Err(ConfigError::NoTerminalBuffer)
        ));
    }

    #[test]
    fn admission_capacity_backpressure_and_epoch_reuse_are_typed() {
        let mut c = config();
        c.max_active_requests = 1;
        c.max_queued_requests = 1;
        let mut engine = ResidentEngine::new(c).unwrap();
        let first = engine.admit(spec(1, 2, 2), EngineTime(0)).unwrap();
        let queued = engine.admit(spec(2, 2, 2), EngineTime(0)).unwrap();
        assert_eq!(
            engine.request(queued).unwrap().status,
            RequestStatus::Waiting
        );
        assert_eq!(
            engine.admit(spec(3, 1, 1), EngineTime(0)),
            Err(AdmissionError::Backpressure {
                active: 1,
                queued: 1
            })
        );
        assert_eq!(
            engine.cancel(first, EngineTime(0)),
            Ok(ControlOutcome::Applied)
        );
        assert_eq!(
            engine.request(queued).unwrap().status,
            RequestStatus::ReadyPrefill
        );

        let reused = engine.admit(spec(1, 1, 1), EngineTime(0)).unwrap();
        assert_ne!(first.admission_epoch, reused.admission_epoch);
        assert!(matches!(
            engine.cancel(first, EngineTime(0)),
            Err(HandleError::Stale { latest_epoch, .. }) if latest_epoch == reused.admission_epoch
        ));
        assert_eq!(engine.telemetry().backpressured, 1);
        assert!(engine.check_invariants().is_ok());
    }

    #[test]
    fn admission_validates_shape_deadline_and_clock_without_partial_insert() {
        let mut engine = ResidentEngine::new(config()).unwrap();
        assert_eq!(
            engine.admit(spec(1, 0, 1), EngineTime(2)),
            Err(AdmissionError::EmptyPrompt)
        );
        assert_eq!(
            engine.admit(spec(1, 1, 0), EngineTime(2)),
            Err(AdmissionError::ZeroMaxNewTokens)
        );
        let mut expired = spec(1, 1, 1);
        expired.deadline = Some(EngineTime(2));
        assert_eq!(
            engine.admit(expired, EngineTime(2)),
            Err(AdmissionError::DeadlineExpired)
        );
        assert_eq!(
            engine.admit(spec(1, 1, 1), EngineTime(1)),
            Err(AdmissionError::NonMonotonicTime {
                previous: EngineTime(2),
                observed: EngineTime(1)
            })
        );
        assert!(engine.entries.is_empty());
    }

    #[test]
    fn chunked_prefill_and_all_same_tick_dispatches_stay_inside_budget() {
        let mut c = config();
        c.token_budget_per_tick = 5;
        c.prefill_chunk_tokens = 3;
        c.max_batch_items = 8;
        let mut engine = ResidentEngine::new(c).unwrap();
        for id in 0..3 {
            engine.admit(spec(id, 9, 2), EngineTime(0)).unwrap();
        }
        let mut spent = 0;
        while let Some(plan) = engine.plan(EngineTime(1)).unwrap() {
            spent += plan.token_cost();
            assert_eq!(plan.created_at(), EngineTime(1));
            assert!(plan.token_cost() > 0);
            assert!(plan.items().len() <= 8);
            for item in plan.items() {
                assert!(item.token_cost() <= 3);
            }
            engine
                .apply_completion(successful(&plan), EngineTime(1))
                .unwrap();
        }
        assert_eq!(spent, 5);
        assert_eq!(engine.config().token_budget_per_tick, 5);
        assert_eq!(engine.snapshot().budget_used, 5);
        assert!(engine.plan(EngineTime(1)).unwrap().is_none());
        assert!(engine.plan(EngineTime(2)).unwrap().is_some());
    }

    #[test]
    fn lane_alternation_and_aging_prevent_prefill_decode_starvation() {
        let mut c = config();
        c.token_budget_per_tick = 2;
        c.prefill_chunk_tokens = 1;
        c.activation_quantum_tokens = 100;
        let mut engine = ResidentEngine::new(c).unwrap();
        engine.admit(spec(1, 1, 20), EngineTime(0)).unwrap();
        engine.admit(spec(2, 20, 1), EngineTime(0)).unwrap();
        apply_one(&mut engine, 0).unwrap();
        let mut lanes = Vec::new();
        for tick in 1..7 {
            let plan = engine.plan(EngineTime(tick)).unwrap().unwrap();
            lanes.push(plan.lane());
            engine
                .apply_completion(successful(&plan), EngineTime(tick))
                .unwrap();
        }
        assert_eq!(
            lanes,
            vec![
                Lane::Decode,
                Lane::Prefill,
                Lane::Decode,
                Lane::Prefill,
                Lane::Decode,
                Lane::Prefill
            ]
        );
        assert!(engine.telemetry().max_ready_wait_ticks >= 1);
    }

    #[test]
    fn cancellation_racing_an_inflight_batch_discards_only_cancelled_progress() {
        let mut engine = ResidentEngine::new(config()).unwrap();
        let a = engine.admit(spec(1, 3, 2), EngineTime(0)).unwrap();
        let b = engine.admit(spec(2, 3, 2), EngineTime(0)).unwrap();
        let plan = engine.plan(EngineTime(0)).unwrap().unwrap();
        assert_eq!(
            engine.cancel(a, EngineTime(0)),
            Ok(ControlOutcome::PendingCheckpoint)
        );
        let report = engine
            .apply_completion(successful(&plan), EngineTime(0))
            .unwrap();
        assert_eq!(report.terminal.len(), 1);
        assert_eq!(report.terminal[0].handle, a);
        assert_eq!(report.terminal[0].reason, TerminalReason::Cancelled);
        assert!(matches!(engine.request(a), Err(HandleError::Unknown(_))));
        assert_eq!(engine.request(b).unwrap().prefilled_tokens, 3);
        assert!(engine.check_invariants().is_ok());
    }

    #[test]
    fn malformed_completion_matrix_is_atomic_and_retriable() {
        let mut engine = ResidentEngine::new(config()).unwrap();
        let a = engine.admit(spec(1, 3, 2), EngineTime(0)).unwrap();
        let b = engine.admit(spec(2, 3, 2), EngineTime(0)).unwrap();
        let plan = engine.plan(EngineTime(0)).unwrap().unwrap();
        assert_eq!(plan.items().len(), 2);
        let before_a = engine.request(a).unwrap();
        let before_b = engine.request(b).unwrap();

        let mut cases = Vec::new();
        let mut wrong_dispatch = successful(&plan);
        wrong_dispatch.dispatch_id += 1;
        cases.push(wrong_dispatch);
        let mut missing = successful(&plan);
        missing.items.pop();
        cases.push(missing);
        let mut duplicate = successful(&plan);
        duplicate.items[1] = duplicate.items[0].clone();
        cases.push(duplicate);
        let mut wrong_count = successful(&plan);
        if let ItemOutput::Prefill {
            processed_tokens, ..
        } = &mut wrong_count.items[0]
        {
            *processed_tokens -= 1;
        }
        cases.push(wrong_count);
        let mut wrong_kind = successful(&plan);
        wrong_kind.items[0] = ItemOutput::Decode {
            handle: a,
            token: 1,
            eos: false,
        };
        cases.push(wrong_kind);
        let mut stale = successful(&plan);
        if let ItemOutput::Prefill { handle, .. } = &mut stale.items[0] {
            handle.admission_epoch = AdmissionEpoch(0);
        }
        cases.push(stale);

        for malformed in cases {
            assert!(engine.apply_completion(malformed, EngineTime(0)).is_err());
            assert_eq!(engine.request(a).unwrap(), before_a);
            assert_eq!(engine.request(b).unwrap(), before_b);
            assert_eq!(engine.in_flight.as_ref(), Some(&plan));
            assert!(engine.check_invariants().is_ok());
        }
        engine
            .apply_completion(successful(&plan), EngineTime(0))
            .unwrap();
        assert_eq!(engine.request(a).unwrap().prefilled_tokens, 3);
        assert_eq!(engine.telemetry().rejected_completions, 6);
        assert_eq!(engine.telemetry().stale_completions, 1);
    }

    #[test]
    fn deadline_at_inflight_checkpoint_discards_output_and_waiting_deadline_frees_capacity() {
        let mut c = config();
        c.max_active_requests = 1;
        let mut engine = ResidentEngine::new(c).unwrap();
        let mut first = spec(1, 2, 2);
        first.deadline = Some(EngineTime(2));
        let first = engine.admit(first, EngineTime(0)).unwrap();
        let mut second = spec(2, 2, 2);
        second.deadline = Some(EngineTime(1));
        let second = engine.admit(second, EngineTime(0)).unwrap();
        let plan = engine.plan(EngineTime(1)).unwrap().unwrap();
        assert_eq!(plan.items()[0].handle(), first);
        let report = engine
            .apply_completion(successful(&plan), EngineTime(2))
            .unwrap();
        assert_eq!(report.terminal.len(), 1);
        let reasons: BTreeMap<_, _> = engine
            .terminal_events()
            .iter()
            .map(|event| (event.handle, event.reason))
            .collect();
        assert_eq!(reasons.get(&first), Some(&TerminalReason::DeadlineExceeded));
        assert_eq!(
            reasons.get(&second),
            Some(&TerminalReason::DeadlineExceeded)
        );
        assert!(engine.entries.is_empty());
        assert_eq!(engine.telemetry().prefill_tokens, 0);
    }

    #[test]
    fn eos_and_max_token_completion_commit_the_final_visible_token() {
        let mut engine = ResidentEngine::new(config()).unwrap();
        let eos_handle = engine.admit(spec(1, 1, 9), EngineTime(0)).unwrap();
        let max_handle = engine.admit(spec(2, 1, 1), EngineTime(0)).unwrap();
        apply_one(&mut engine, 0).unwrap();
        let plan = engine.plan(EngineTime(1)).unwrap().unwrap();
        let completion = successful_with(&plan, |handle| handle == eos_handle);
        let report = engine.apply_completion(completion, EngineTime(1)).unwrap();
        assert_eq!(report.decoded.len(), 2);
        let reasons: BTreeMap<_, _> = report
            .terminal
            .into_iter()
            .map(|event| (event.handle, event.reason))
            .collect();
        assert_eq!(reasons[&eos_handle], TerminalReason::Eos);
        assert_eq!(reasons[&max_handle], TerminalReason::MaxTokens);
        assert_eq!(engine.telemetry().completed_eos, 1);
        assert_eq!(engine.telemetry().completed_max_tokens, 1);
    }

    #[test]
    fn automatic_and_explicit_preemption_rotate_capacity_only_at_checkpoints() {
        let mut c = config();
        c.max_active_requests = 1;
        c.prefill_chunk_tokens = 2;
        c.activation_quantum_tokens = 2;
        let mut engine = ResidentEngine::new(c).unwrap();
        let a = engine.admit(spec(1, 6, 2), EngineTime(0)).unwrap();
        let b = engine.admit(spec(2, 2, 2), EngineTime(0)).unwrap();
        let plan = engine.plan(EngineTime(0)).unwrap().unwrap();
        engine
            .apply_completion(successful(&plan), EngineTime(0))
            .unwrap();
        assert_eq!(engine.request(a).unwrap().status, RequestStatus::Waiting);
        assert_eq!(
            engine.request(b).unwrap().status,
            RequestStatus::ReadyPrefill
        );

        let plan = engine.plan(EngineTime(1)).unwrap().unwrap();
        assert_eq!(
            engine.preempt(b, EngineTime(1)),
            Ok(ControlOutcome::PendingCheckpoint)
        );
        engine
            .apply_completion(successful(&plan), EngineTime(1))
            .unwrap();
        assert_eq!(
            engine.request(a).unwrap().status,
            RequestStatus::ReadyPrefill
        );
        assert_eq!(engine.request(b).unwrap().status, RequestStatus::Waiting);
        assert!(engine.telemetry().preemptions >= 2);
    }

    #[derive(Default)]
    struct MockExecutor {
        calls: usize,
        fail_next: bool,
        malformed_next: bool,
    }

    impl ResidentExecutor for MockExecutor {
        type Error = &'static str;

        fn execute(&mut self, plan: &DispatchPlan) -> Result<DispatchCompletion, Self::Error> {
            self.calls += 1;
            if std::mem::take(&mut self.fail_next) {
                Err("injected")
            } else {
                let mut completion = successful(plan);
                if std::mem::take(&mut self.malformed_next) {
                    completion.items.clear();
                }
                Ok(completion)
            }
        }
    }

    #[test]
    fn actor_executor_failure_rolls_back_exactly_and_retries() {
        let mut engine = ResidentEngine::new(config()).unwrap();
        let handle = engine.admit(spec(1, 3, 1), EngineTime(0)).unwrap();
        let before = engine.request(handle).unwrap();
        let mut executor = MockExecutor {
            fail_next: true,
            ..MockExecutor::default()
        };
        assert!(matches!(
            engine.drive_once(&mut executor, EngineTime(0)),
            Err(DriveError::Executor("injected"))
        ));
        let after = engine.request(handle).unwrap();
        assert_eq!(before.prefilled_tokens, after.prefilled_tokens);
        assert_eq!(after.status, RequestStatus::ReadyPrefill);
        assert_eq!(engine.telemetry().executor_errors, 1);
        assert!(matches!(
            engine.drive_once(&mut executor, EngineTime(1)),
            Ok(DriveOutcome::Applied(_))
        ));
        assert_eq!(engine.request(handle).unwrap().prefilled_tokens, 3);
    }

    #[test]
    fn malformed_executor_output_cannot_wedge_the_actor() {
        let mut engine = ResidentEngine::new(config()).unwrap();
        let handle = engine.admit(spec(1, 3, 1), EngineTime(0)).unwrap();
        let mut executor = MockExecutor {
            malformed_next: true,
            ..MockExecutor::default()
        };
        let error = engine.drive_once(&mut executor, EngineTime(0)).unwrap_err();
        assert!(matches!(
            error,
            DriveError::Scheduler(CompletionError::WrongItemCount { observed: 0, .. })
        ));
        assert!(!engine.snapshot().dispatch_in_flight);
        assert_eq!(
            engine.request(handle).unwrap().status,
            RequestStatus::ReadyPrefill
        );
        assert_eq!(engine.telemetry().rejected_completions, 1);
        assert_eq!(engine.telemetry().executor_errors, 1);
        assert!(matches!(
            engine.drive_once(&mut executor, EngineTime(1)),
            Ok(DriveOutcome::Applied(_))
        ));
    }

    #[test]
    fn raced_control_timestamp_is_clamped_and_never_moves_state_time_backwards() {
        let mut engine = ResidentEngine::new(config()).unwrap();
        let handle = engine.admit(spec(1, 2, 1), EngineTime(10)).unwrap();
        assert_eq!(
            engine.preempt(handle, EngineTime(3)),
            Ok(ControlOutcome::Applied)
        );
        assert_eq!(engine.last_time, Some(EngineTime(10)));
        assert_eq!(
            engine.request(handle).unwrap().status,
            RequestStatus::ReadyPrefill
        );
        assert_eq!(engine.entries[&RequestId(1)].ready_since, EngineTime(10));
    }

    #[test]
    fn exhaustive_small_capacity_matrix_has_no_starvation_or_budget_violation() {
        for active in 1..=3 {
            for budget in 1..=4 {
                for chunk in 1..=4 {
                    let mut c = config();
                    c.max_active_requests = active;
                    c.max_queued_requests = 6;
                    c.token_budget_per_tick = budget;
                    c.max_batch_items = 3;
                    c.prefill_chunk_tokens = chunk;
                    c.activation_quantum_tokens = 1;
                    let mut engine = ResidentEngine::new(c).unwrap();
                    let mut handles = Vec::new();
                    for id in 0..(active + 6) {
                        handles.push(
                            engine
                                .admit(spec(id as u64, 1 + id % 5, 1 + id % 3), EngineTime(0))
                                .unwrap(),
                        );
                    }
                    let mut seen = BTreeSet::new();
                    for tick in 0..2_000 {
                        if engine.entries.is_empty() {
                            break;
                        }
                        let mut spent = 0;
                        while let Some(plan) = engine.plan(EngineTime(tick)).unwrap() {
                            spent += plan.token_cost();
                            for item in plan.items() {
                                seen.insert(item.handle());
                            }
                            assert!(spent <= budget);
                            engine
                                .apply_completion(successful(&plan), EngineTime(tick))
                                .unwrap();
                        }
                        assert!(engine.check_invariants().is_ok());
                    }
                    assert!(
                        engine.entries.is_empty(),
                        "active={active} budget={budget} chunk={chunk}"
                    );
                    assert_eq!(seen.len(), handles.len());
                }
            }
        }
    }

    #[derive(Clone)]
    struct Lcg(u64);

    impl Lcg {
        fn next(&mut self) -> u64 {
            self.0 = self
                .0
                .wrapping_mul(6_364_136_223_846_793_005)
                .wrapping_add(1_442_695_040_888_963_407);
            self.0
        }

        fn usize(&mut self, upper: usize) -> usize {
            (self.next() as usize) % upper
        }
    }

    #[test]
    fn randomized_admit_cancel_preempt_deadline_eos_queue_churn_preserves_invariants() {
        for seed in 0..16 {
            let mut c = config();
            c.max_active_requests = 3;
            c.max_queued_requests = 24;
            c.token_budget_per_tick = 7;
            c.prefill_chunk_tokens = 3;
            c.activation_quantum_tokens = 3;
            let mut engine = ResidentEngine::new(c).unwrap();
            let mut rng = Lcg(seed + 1);
            let mut next_id = 1u64;
            for tick in 0..500u64 {
                if rng.usize(3) != 0 {
                    let mut request = spec(next_id, 1 + rng.usize(12), 1 + rng.usize(8));
                    if rng.usize(7) == 0 {
                        request.deadline = Some(EngineTime(tick + 1 + rng.usize(12) as u64));
                    }
                    if engine.admit(request, EngineTime(tick)).is_ok() {
                        next_id += 1;
                    }
                }
                let live: Vec<_> = engine.entries.values().map(|entry| entry.handle).collect();
                if !live.is_empty() && rng.usize(8) == 0 {
                    let handle = live[rng.usize(live.len())];
                    let _ = engine.cancel(handle, EngineTime(tick));
                } else if !live.is_empty() && rng.usize(9) == 0 {
                    let handle = live[rng.usize(live.len())];
                    let _ = engine.preempt(handle, EngineTime(tick));
                }
                if let Some(plan) = engine.plan(EngineTime(tick)).unwrap() {
                    let completion = successful_with(&plan, |_| rng.usize(23) == 0);
                    engine
                        .apply_completion(completion, EngineTime(tick))
                        .unwrap();
                }
                engine.poll(EngineTime(tick));
                assert!(engine.check_invariants().is_ok(), "seed={seed} tick={tick}");
            }
            let admitted = engine.telemetry().admitted as usize;
            drain(&mut engine, 500, 10_000);
            assert_eq!(engine.terminal_events().len(), admitted);
            assert!(engine.check_invariants().is_ok());
        }
    }

    #[test]
    fn planning_is_deterministic_for_identical_event_streams() {
        let mut left = ResidentEngine::new(config()).unwrap();
        let mut right = ResidentEngine::new(config()).unwrap();
        for id in 0..7 {
            let request = spec(id, 2 + id as usize, 2 + id as usize % 3);
            assert_eq!(
                left.admit(request.clone(), EngineTime(0)),
                right.admit(request, EngineTime(0))
            );
        }
        for tick in 0..100 {
            let lp = left.plan(EngineTime(tick)).unwrap();
            let rp = right.plan(EngineTime(tick)).unwrap();
            assert_eq!(lp, rp);
            match (lp, rp) {
                (Some(lp), Some(rp)) => {
                    assert_eq!(successful(&lp), successful(&rp));
                    assert_eq!(
                        left.apply_completion(successful(&lp), EngineTime(tick)),
                        right.apply_completion(successful(&rp), EngineTime(tick))
                    );
                }
                (None, None) if left.entries.is_empty() => break,
                (None, None) => {}
                _ => unreachable!(),
            }
            assert_eq!(left.snapshot(), right.snapshot());
        }
        assert!(left.entries.is_empty());
        assert_eq!(left.terminal_events(), right.terminal_events());
    }
}
