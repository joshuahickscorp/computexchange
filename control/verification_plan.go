package main

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"

	"github.com/google/uuid"
)

// verificationStore is the complete database surface used by Verifier. Keeping
// reads and writes behind this narrow boundary lets PlanTaskResult execute the
// exact production decision logic while replacing every mutation with a typed,
// ordered effect.
type verificationStore interface {
	// Reads.
	GetHoneypotAnswer(context.Context, string, string) ([]byte, string, error)
	CandidateWorkers(context.Context, string, string, float32) ([]MatchWorker, error)
	ChunkResults(context.Context, uuid.UUID, int) ([]ChunkResult, error)
	TiebreakExists(context.Context, uuid.UUID, int) (bool, error)
	SelectRedundancyPeerExcluding(context.Context, string, string, float32, uuid.UUID, []uuid.UUID, []uuid.UUID) (uuid.UUID, error)

	// Writes.
	DockReputation(context.Context, uuid.UUID, ReputationEvent) error
	RecordVerificationEvent(context.Context, uuid.UUID, uuid.UUID, uuid.UUID, string) error
	ClawbackTaskCredit(context.Context, uuid.UUID, uuid.UUID) error
	QuarantineSupplier(context.Context, uuid.UUID) error
	RequeueTask(context.Context, uuid.UUID) error
	InsertTiebreakTask(context.Context, uuid.UUID, uuid.UUID, uuid.UUID, string, int) (uuid.UUID, error)
}

var _ verificationStore = (*Store)(nil)

// VerificationEffectKind is a closed description of one mutation requested by
// verification. Effects are returned in the exact order in which the existing
// write-through verifier would have issued them.
type VerificationEffectKind string

const (
	VerificationEffectDockReputation VerificationEffectKind = "dock_reputation"
	VerificationEffectRecordEvent    VerificationEffectKind = "record_verification_event"
	VerificationEffectClawbackCredit VerificationEffectKind = "clawback_task_credit"
	VerificationEffectQuarantine     VerificationEffectKind = "quarantine_supplier"
	VerificationEffectRequeue        VerificationEffectKind = "requeue_task"
	VerificationEffectInsertTiebreak VerificationEffectKind = "insert_tiebreak_task"
)

// VerificationEffect contains the union of arguments needed by the six
// verification mutation types. Fields irrelevant to Kind are zero-valued. ID is
// a stable decision-local id derived from (committing task, attempt, ordinal,
// kind); for InsertTiebreak it is also the task id returned to the decision
// algorithm. That makes repeated planning of one attempt byte-for-byte stable.
type VerificationEffect struct {
	ID              uuid.UUID              `json:"id"`
	Kind            VerificationEffectKind `json:"kind"`
	JobID           uuid.UUID              `json:"job_id,omitempty"`
	TaskID          uuid.UUID              `json:"task_id,omitempty"`
	SupplierID      uuid.UUID              `json:"supplier_id,omitempty"`
	ReputationEvent ReputationEvent        `json:"reputation_event,omitempty"`
	EventKind       string                 `json:"event_kind,omitempty"`
	PeerWorkerID    uuid.UUID              `json:"peer_worker_id,omitempty"`
	PrimaryTaskID   uuid.UUID              `json:"primary_task_id,omitempty"`
	InputRef        string                 `json:"input_ref,omitempty"`
	ChunkIndex      int                    `json:"chunk_index,omitempty"`
}

// VerificationDecision is the complete, write-free result of verification.
// Effects are safe to consume only when PlanTaskResult returns a nil error.
type VerificationDecision struct {
	Outcome VerifyOutcome        `json:"outcome"`
	Effects []VerificationEffect `json:"effects"`
	Failure *VerificationFailure `json:"failure,omitempty"`
}

// VerificationFailure is the typed, low-cardinality reason a non-payable
// decision was produced before semantic peer verification. Detail from an
// untrusted parser is deliberately excluded from the durable plan.
type VerificationFailure struct {
	Kind    string `json:"kind"`
	Code    string `json:"code"`
	JobType string `json:"job_type"`
}

// PlanTaskResult runs the production verification algorithm against live reads
// and object-store fetches, but records every requested database mutation instead
// of performing it. The receiver is not mutated. A read/fetch error invalidates
// the whole plan and returns no effects, preventing callers from applying a
// partially evaluated decision.
func (v *Verifier) PlanTaskResult(ctx context.Context, info *CommitTaskInfo, commit TaskCommit, commitBytes, redundancyBytes []byte) (VerificationDecision, error) {
	if info == nil {
		return VerificationDecision{}, fmt.Errorf("plan verification: nil commit task info")
	}
	recorder := &recordingVerificationStore{
		reads:   v.store,
		taskID:  info.TaskID,
		attempt: info.Attempt,
	}
	planner := *v
	planner.store = recorder
	planner.samplingDecisionObserver = nil
	planner.planning = true

	outcome, err := planner.verifyTaskResult(ctx, info, commit, commitBytes, redundancyBytes)
	if err != nil {
		return VerificationDecision{Outcome: outcome}, err
	}
	effects := append([]VerificationEffect(nil), recorder.effects...)
	return VerificationDecision{Outcome: outcome, Effects: effects}, nil
}

type recordingVerificationStore struct {
	reads   verificationStore
	taskID  uuid.UUID
	attempt int16
	effects []VerificationEffect
}

func (r *recordingVerificationStore) append(effect VerificationEffect) uuid.UUID {
	effect.ID = verificationEffectPayloadID(r.taskID, r.attempt, len(r.effects), effect)
	r.effects = append(r.effects, effect)
	return effect.ID
}

// verificationEffectID deliberately includes the attempt and ordinal. Replaying
// the same committed attempt produces identical ids, while a later retry or a
// differently ordered decision cannot alias an earlier effect.
func verificationEffectID(taskID uuid.UUID, attempt int16, ordinal int, kind VerificationEffectKind) uuid.UUID {
	return verificationEffectPayloadID(taskID, attempt, ordinal, VerificationEffect{Kind: kind})
}

// verificationEffectPayloadID includes the typed payload, not just the ordinal.
// A re-plan that selects a different peer or targets a different task therefore
// cannot masquerade behind the same effect identity. InsertTiebreak's TaskID is
// derived from this ID, so it is cleared before hashing to avoid a cycle.
func verificationEffectPayloadID(taskID uuid.UUID, attempt int16, ordinal int, effect VerificationEffect) uuid.UUID {
	data := make([]byte, 0, len(taskID)+2+8+len(effect.Kind)+128)
	data = append(data, taskID[:]...)
	var n [8]byte
	binary.BigEndian.PutUint16(n[:2], uint16(attempt))
	data = append(data, n[:2]...)
	binary.BigEndian.PutUint64(n[:], uint64(ordinal))
	data = append(data, n[:]...)
	effect.ID = uuid.Nil
	if effect.Kind == VerificationEffectInsertTiebreak {
		effect.TaskID = uuid.Nil
	}
	payload, err := json.Marshal(effect)
	if err != nil {
		panic(fmt.Sprintf("marshal verification effect identity: %v", err))
	}
	data = append(data, payload...)
	return uuid.NewSHA1(verificationEffectNamespace, data)
}

var verificationEffectNamespace = uuid.MustParse("d63f7f13-0178-5dc4-90fd-81caf1074a6f")

// Read methods delegate to the verifier's real store. The explicit nil errors
// make a no-database verifier usable in pure branches while failing clearly if a
// branch unexpectedly needs persistence.
func (r *recordingVerificationStore) GetHoneypotAnswer(ctx context.Context, jobType, inputRef string) ([]byte, string, error) {
	if r.reads == nil {
		return nil, "", fmt.Errorf("verification plan requires store read: GetHoneypotAnswer")
	}
	return r.reads.GetHoneypotAnswer(ctx, jobType, inputRef)
}

func (r *recordingVerificationStore) CandidateWorkers(ctx context.Context, jobType, modelRef string, minMemoryGB float32) ([]MatchWorker, error) {
	if r.reads == nil {
		return nil, fmt.Errorf("verification plan requires store read: CandidateWorkers")
	}
	return r.reads.CandidateWorkers(ctx, jobType, modelRef, minMemoryGB)
}

func (r *recordingVerificationStore) ChunkResults(ctx context.Context, jobID uuid.UUID, chunkIndex int) ([]ChunkResult, error) {
	if r.reads == nil {
		return nil, fmt.Errorf("verification plan requires store read: ChunkResults")
	}
	return r.reads.ChunkResults(ctx, jobID, chunkIndex)
}

func (r *recordingVerificationStore) TiebreakExists(ctx context.Context, jobID uuid.UUID, chunkIndex int) (bool, error) {
	if r.reads == nil {
		return false, fmt.Errorf("verification plan requires store read: TiebreakExists")
	}
	return r.reads.TiebreakExists(ctx, jobID, chunkIndex)
}

func (r *recordingVerificationStore) SelectRedundancyPeerExcluding(ctx context.Context, jobType, modelRef string, minMemoryGB float32, anchor uuid.UUID, also, alsoSuppliers []uuid.UUID) (uuid.UUID, error) {
	if r.reads == nil {
		return uuid.Nil, fmt.Errorf("verification plan requires store read: SelectRedundancyPeerExcluding")
	}
	return r.reads.SelectRedundancyPeerExcluding(ctx, jobType, modelRef, minMemoryGB, anchor, also, alsoSuppliers)
}

// Write methods only append typed effects. They never delegate.
func (r *recordingVerificationStore) DockReputation(_ context.Context, supplierID uuid.UUID, event ReputationEvent) error {
	r.append(VerificationEffect{Kind: VerificationEffectDockReputation, SupplierID: supplierID, ReputationEvent: event})
	return nil
}

func (r *recordingVerificationStore) RecordVerificationEvent(_ context.Context, jobID, taskID, supplierID uuid.UUID, kind string) error {
	r.append(VerificationEffect{Kind: VerificationEffectRecordEvent, JobID: jobID, TaskID: taskID, SupplierID: supplierID, EventKind: kind})
	return nil
}

func (r *recordingVerificationStore) ClawbackTaskCredit(_ context.Context, supplierID, taskID uuid.UUID) error {
	r.append(VerificationEffect{Kind: VerificationEffectClawbackCredit, SupplierID: supplierID, TaskID: taskID})
	return nil
}

func (r *recordingVerificationStore) QuarantineSupplier(_ context.Context, supplierID uuid.UUID) error {
	r.append(VerificationEffect{Kind: VerificationEffectQuarantine, SupplierID: supplierID})
	return nil
}

func (r *recordingVerificationStore) RequeueTask(_ context.Context, taskID uuid.UUID) error {
	r.append(VerificationEffect{Kind: VerificationEffectRequeue, TaskID: taskID})
	return nil
}

func (r *recordingVerificationStore) InsertTiebreakTask(_ context.Context, jobID, primaryTaskID, peerWorker uuid.UUID, inputRef string, chunkIndex int) (uuid.UUID, error) {
	id := r.append(VerificationEffect{
		Kind:          VerificationEffectInsertTiebreak,
		JobID:         jobID,
		PrimaryTaskID: primaryTaskID,
		PeerWorkerID:  peerWorker,
		InputRef:      inputRef,
		ChunkIndex:    chunkIndex,
	})
	r.effects[len(r.effects)-1].TaskID = id
	return id, nil
}
