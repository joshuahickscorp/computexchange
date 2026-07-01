package main

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"math"

	"github.com/google/uuid"
)

// verification.go — honeypot + within-class redundancy coordinator.
//
// This wires the control flow from the action plan's verify_task_result. The
// comparison primitive is real and job-type-aware: mean per-vector cosine
// similarity (>= 0.999) for embeddings, exact byte match for deterministic
// outputs. Within-class redundancy is the trust spine.

// VerifyOutcome is the result of verifying one committed task.
type VerifyOutcome string

const (
	OutcomePass            VerifyOutcome = "pass"
	OutcomeFail            VerifyOutcome = "fail"
	OutcomePassWithPenalty VerifyOutcome = "pass_with_penalty"
)

// Verifier coordinates honeypot + redundancy checks against the store. storage is
// used to fetch the sibling result objects a 3-way tiebreak vote compares (it may
// be nil in unit contexts that only exercise the 2-result fast paths).
type Verifier struct {
	store   *Store
	storage *Storage
}

func NewVerifier(s *Store) *Verifier { return &Verifier{store: s} }

// WithStorage attaches the object store so the verifier can run the 3-way
// tiebreak vote (gather all committed results for a chunk and compare them).
func (v *Verifier) WithStorage(st *Storage) *Verifier { v.storage = st; return v }

// verifyTaskResult runs the layered verification for a freshly committed task
// and applies reputation/payout side effects. It mirrors the action plan:
//
//	Step 1  honeypot check     — compare to known answer; dock + requeue on fail
//	Step 2  redundancy compare — same hw_class; mismatch → majority vote + dock
//	                             (the tiebreak RE-DISPATCH to a third worker is the
//	                             one documented next step; the compare is real)
//	Step 3  schedule payout    — ledger entries with the hold window
//
// Verification V2 (reputation-weighted): the per-probe verification COST is made
// adaptive to the committing supplier's reputation. A high-reputation supplier
// (above the trusted floor, verifyTrustFloor) has its honeypot comparison and its
// costly third-worker tiebreak re-dispatch SAMPLED — performed only a fraction of
// the time (effectiveCheckProb), shrinking toward verifyCheckProbFloor but NEVER to
// zero, so even the most trusted worker is still audited regularly. A supplier at or
// below the floor is checked at the full rate (probability 1.0), so a fresh/low-rep
// worker is checked the most. Sampling only ever skips SPENDING a check; it never
// suppresses a failure a check actually found, and a skipped check is never reported
// as a pass (BLACKHOLE: never fabricate a pass). The within-hardware-class
// comparison rule is unchanged. Reputation is read with the same live matching view
// the peer selector uses (committingReputation); when it cannot be determined the
// safe default is full checking.
//
// commitBytes is the worker's committed result, fetched from object storage by
// the caller (api.go) using the canonical result key. redundancyBytes is a
// committed peer's result when one exists for the same input chunk, else nil.
func (v *Verifier) verifyTaskResult(ctx context.Context, info *CommitTaskInfo, commit TaskCommit, commitBytes, redundancyBytes []byte) (VerifyOutcome, error) {
	// Reputation-weighted check budget for this committing supplier (V2). checkProb
	// is the probability we SPEND a sampled probe (honeypot comparison / tiebreak
	// re-dispatch) on this task; checkSampled draws a deterministic per-task decision
	// from it. The reputation read behind it is LAZY (memoized) so a plain non-probe
	// primary — the common commit — never pays for the lookup; it is fetched at most
	// once, only when a sampled branch is actually reached. A supplier at or below
	// verifyTrustFloor (or whose reputation we cannot read) gets 1.0 — always checked.
	var checkProbCached float64
	var checkProbDone bool
	checkProb := func() float64 {
		if !checkProbDone {
			checkProbCached = v.effectiveCheckProb(ctx, info)
			checkProbDone = true
		}
		return checkProbCached
	}

	// Step 1: honeypot. The known answer is keyed by (job_type, input_ref) — the
	// honeypot task's input chunk, NOT its result key — and we compare it against
	// the worker's committed result bytes (commitBytes). The comparison is sampled
	// by reputation (V2): a trusted supplier's honeypot is verified only a fraction
	// of the time. When sampled OUT we do not look at the known answer at all — the
	// task falls through to the ordinary success path, exactly as a non-probe task
	// would. This is reduced audit FREQUENCY, never a fabricated honeypot pass: we
	// only skip checks for suppliers already above the trusted floor, and even they
	// keep a verifyCheckProbFloor chance of being checked every task.
	if info.IsHoneypot && v.checkSampled(info.TaskID, checkProb()) {
		known, answerClass, err := v.store.GetHoneypotAnswer(ctx, jobTypeOf(info), info.InputRef)
		if err != nil && !errors.Is(err, errNotFound) {
			return OutcomeFail, err
		}
		// Class gate for BYTE-EXACT job types: a byte honeypot is only valid evidence
		// of a bad worker when the known answer was produced in the SAME verification
		// class as the committing worker. Across the class boundary (e.g. a
		// Candle-seeded answer vs a correct vLLM/Hawking result) bytes legitimately
		// differ, so a mismatch is NOT fraud and must not auto-quarantine an honest
		// worker (the audit's hw_class-blind-honeypot hazard). Tolerant job types
		// compare SEMANTICS (label/JSON/cosine/order), are robust to cross-kernel FP
		// jitter, and so always check. When a byte-exact answer is class-blind ("") or
		// cross-class we SKIP the byte probe and fall through to the redundancy/normal
		// path — exactly as a sampled-out check would: reduced coverage, never a
		// fabricated pass and never a wrongful quarantine.
		byteExactComparable := byteHoneypotComparable(info.jobType, answerClass, info.engine, info.buildHash)
		if known != nil && byteExactComparable {
			if !resultsAgree(info.jobType, commitBytes, known) {
				// Confirmed bad result on a known-answer task: dock reputation, claw
				// back any credit already written for this task, AUTO-QUARANTINE the
				// supplier (a failed honeypot is hard evidence of a bad/fraudulent
				// worker), and requeue the task to a different worker.
				if err := v.store.DockReputation(ctx, info.SupplierID, EventHoneypotFail); err != nil {
					return OutcomeFail, err
				}
				_ = v.store.RecordVerificationEvent(ctx, info.JobID, info.TaskID, info.SupplierID, "honeypot_fail")
				if err := v.store.ClawbackTaskCredit(ctx, info.SupplierID, info.TaskID); err != nil {
					return OutcomeFail, err
				}
				if err := v.store.QuarantineSupplier(ctx, info.SupplierID); err != nil {
					return OutcomeFail, err
				}
				if err := v.store.RequeueTask(ctx, taskIDOf(commit)); err != nil {
					return OutcomeFail, err
				}
				return OutcomeFail, nil
			}
			if err := v.store.DockReputation(ctx, info.SupplierID, EventHoneypotPass); err != nil {
				return OutcomePass, err
			}
			_ = v.store.RecordVerificationEvent(ctx, info.JobID, info.TaskID, info.SupplierID, "honeypot_pass")
			return OutcomePass, nil
		}
		// No known answer on file, OR a byte-exact answer that is class-blind/
		// cross-class (not comparable): fall through to the normal redundancy path
		// rather than fake a pass or a wrongful fail.
	}

	// Step 2: redundancy comparison + 3-way tiebreak, within the same hardware
	// class (guaranteed by the matcher). The vote is REAL: once a third opinion
	// exists for a chunk, gather all committed results and majority-vote; with two
	// disagreeing results, dispatch a pinned third worker and resolve when it
	// commits.
	if redundancyBytes != nil {
		all, ferr := v.gatherChunkResults(ctx, info, commitBytes)
		if ferr != nil {
			return OutcomeFail, ferr
		}
		// Fallback when the store path yields nothing (no object store / unknown
		// chunk identity in a unit context): vote over exactly the two blobs the
		// caller fetched, preserving the original 2-result behavior. The committing
		// vote carries its real class; the peer's class comes from the commit handler
		// when known, else "" (unknown) — which makes a byte-exact pair non-comparable
		// and so provisionally trusted rather than wrongly docked.
		if len(all) == 0 {
			// The peer's supplier when the commit handler knew it; uuid.Nil when
			// unknown (e.g. a unit context with no object store). We keep it Nil rather
			// than defaulting to the committing supplier, so the supplier-distinctness
			// gate below treats an UNKNOWN peer as "not provably same-supplier" (still a
			// match) instead of misreading it as a same-supplier collusion.
			all = []chunkVote{
				{supplierID: info.SupplierID, taskID: info.TaskID, bytes: commitBytes, engine: info.engine, buildHash: info.buildHash},
				{supplierID: info.peerSupplierID, bytes: redundancyBytes, engine: info.peerEngine, buildHash: info.peerBuildHash},
			}
		}
		switch {
		case len(all) >= 3:
			// A third opinion has arrived: a real N-way majority vote. The
			// supplier behind each LOSING result is docked for a mismatch; each
			// WINNER is credited a redundancy match. Idempotent reputation events
			// are fine — they are tiny deltas applied once per commit.
			return v.resolveTiebreak(ctx, info, all)
		case len(all) == 2 && !resultsAgree(info.jobType, all[0].bytes, all[1].bytes):
			// Two results disagree. For a BYTE-EXACT job type this only counts as a
			// real mismatch when the two results are in the SAME verification class
			// (engine + build_hash) — across the class boundary the bytes legitimately
			// differ (different kernels), so a pure byte mismatch is NOT a defect and
			// we provisionally trust the primary (no credit, no dock), mirroring the
			// missing-third-worker pattern. Tolerant job types compare semantics and
			// are always a real disagreement. The redundancy matcher already pins the
			// peer to the primary's class, so same-class is the common path; this gate
			// is the safety net for a cross-class pairing (e.g. a peer that registered
			// before advertising a build hash, or the no-store fallback).
			if byteExactJobType(info.jobType) &&
				!sameVerificationClass(all[0].engine, all[0].buildHash, all[1].engine, all[1].buildHash) {
				// Cross-class byte difference — not comparable. Surface the
				// uncheckable coverage as pass_with_penalty (BLACKHOLE: never a
				// fabricated clean pass) but do NOT dock and do NOT re-dispatch a
				// tiebreak that would also be cross-class.
				_ = v.store.RecordVerificationEvent(ctx, info.JobID, info.TaskID, info.SupplierID, "redundancy_cross_class")
				return OutcomePassWithPenalty, nil
			}
			// Two same-class results disagree — a real, DETECTED mismatch
			// (pass_with_penalty regardless of what follows; the caller bumps the
			// mismatch metric). Breaking the tie costs a third worker re-running the
			// chunk, so that re-dispatch is the expensive part we sample by reputation
			// (V2): for a trusted supplier a lone disagreement is more likely benign
			// jitter, so we skip the third opinion a fraction of the time and trust the
			// primary provisionally — no credit either way. A supplier at or below the
			// trusted floor always gets the tiebreak. Detection is never suppressed:
			// the outcome stays pass_with_penalty whether or not we re-dispatch.
			_ = v.store.RecordVerificationEvent(ctx, info.JobID, info.TaskID, info.SupplierID, "redundancy_mismatch")
			if v.checkSampled(info.TaskID, checkProb()) {
				if err := v.dispatchTiebreak(ctx, info, all); err != nil {
					return OutcomePassWithPenalty, err
				}
			}
			return OutcomePassWithPenalty, nil
		default:
			// Two results that agree (or a single result). A clean within-class match
			// normally credits an independent redundancy match — BUT a 2-result agreement
			// from the SAME supplier is NOT an independent check (a multi-worker supplier
			// could submit two matching forged results), so it must never be marked
			// verified (do-not-mark-same-supplier-peers-independent, backlog P0 item 7).
			// In that case record the gap and fall through to task success WITHOUT an
			// independent redundancy-match credit.
			if independentRedundancyMatch(all) {
				if err := v.store.DockReputation(ctx, info.SupplierID, EventRedundancyMatch); err != nil {
					return OutcomePass, err
				}
				_ = v.store.RecordVerificationEvent(ctx, info.JobID, info.TaskID, info.SupplierID, "redundancy_match")
			} else {
				_ = v.store.RecordVerificationEvent(ctx, info.JobID, info.TaskID, info.SupplierID, "redundancy_same_supplier")
			}
		}
	} else if info.IsRedundancy {
		// Redundancy-coverage gap, made EXPLICIT (V2): this task was injected
		// specifically to give its chunk a redundant peer to compare against, yet no
		// committed peer result was found (the primary has not landed, or its object
		// could not be read). We must NOT silently emit a clean pass for a chunk that
		// was supposed to be cross-checked and was not. The work itself is provisionally
		// accepted and the worker — blameless for a missing sibling — is still credited
		// task success below, but the outcome is surfaced as pass_with_penalty so the
		// missing coverage is visible (the caller bumps the verification-mismatch metric)
		// rather than being lost (BLACKHOLE: surface every gap, never fabricate a pass).
		if err := v.store.DockReputation(ctx, info.SupplierID, EventTaskSuccess); err != nil {
			return OutcomePassWithPenalty, err
		}
		return OutcomePassWithPenalty, nil
	}

	// Step 3: success path — credit reputation. Payout scheduling (ledger
	// rows with the hold) is done by the caller in api.go once it knows the
	// per-task charge, keeping money math in payment.go's splitCharge.
	if err := v.store.DockReputation(ctx, info.SupplierID, EventTaskSuccess); err != nil {
		return OutcomePass, err
	}
	return OutcomePass, nil
}

// independentRedundancyMatch reports whether a within-class AGREEMENT among `all`
// qualifies as an INDEPENDENT redundancy match. A 2-result agreement from the SAME
// known supplier does NOT — a multi-worker supplier could submit two matching forged
// results — so it must never be credited or marked verified (backlog P0 item 7). A
// single result, or an agreement across DIFFERENT (or unknown/uuid.Nil) suppliers,
// qualifies. Pure, so the supplier-distinctness guarantee is unit-tested.
func independentRedundancyMatch(all []chunkVote) bool {
	if len(all) == 2 && all[0].supplierID == all[1].supplierID && all[0].supplierID != uuid.Nil {
		return false
	}
	return true
}

// Reputation-weighted verification tuning (V2).
//
//   - verifyTrustFloor: at or below this reputation, every probe is checked at the
//     full rate (probability 1.0). It is the action plan's trusted-tier reputation
//     gate (reputationTier tier-3 threshold): only suppliers ABOVE it have earned
//     the reduced-audit margin. Anchoring the band here keeps a 0.90-reputation
//     supplier fully checked.
//   - verifyCheckProbFloor: the LOWEST a sampled-probe probability ever drops to,
//     for a maxed-out 1.0-reputation supplier. It is strictly > 0 so checks are
//     never disabled — even the most trusted worker is audited ~1 task in 4.
const (
	verifyTrustFloor     = 0.90
	verifyCheckProbFloor = 0.25
)

// effectiveCheckProb is the probability that a sampled probe (honeypot comparison
// or tiebreak re-dispatch) is SPENT on this committing supplier's task, derived
// from its reputation. At or below verifyTrustFloor → 1.0 (always check, so low-rep
// suppliers are checked the most). Above the floor it ramps linearly down to
// verifyCheckProbFloor at reputation 1.0, so the more trusted a supplier the less
// often (but never zero) we pay to re-verify it. When reputation cannot be read
// (the worker is not in the live matching view) the safe default is 1.0 — unknown
// trust is treated as untrusted, never as a license to skip checks.
func (v *Verifier) effectiveCheckProb(ctx context.Context, info *CommitTaskInfo) float64 {
	rep, ok := v.committingReputation(ctx, info)
	if !ok || rep <= verifyTrustFloor {
		return 1.0
	}
	if rep > 1.0 {
		rep = 1.0
	}
	// Linear interpolation from 1.0 at verifyTrustFloor to verifyCheckProbFloor at 1.0.
	frac := float64(rep-verifyTrustFloor) / float64(1.0-verifyTrustFloor)
	prob := 1.0 - frac*(1.0-verifyCheckProbFloor)
	if prob < verifyCheckProbFloor {
		prob = verifyCheckProbFloor
	}
	return prob
}

// committingReputation reads the committing worker's supplier reputation from the
// SAME live matching view SelectRedundancyPeerExcluding uses (CandidateWorkers,
// keyed by the task's job type / model / memory). The worker just committed, so it
// is live and present in that set; we find its row by worker id. ok is false when
// the worker is not in the candidate set (e.g. it went stale, or the store read
// failed) — the caller then defaults to full checking. This uses only existing
// store reads; it adds no query of its own.
func (v *Verifier) committingReputation(ctx context.Context, info *CommitTaskInfo) (float32, bool) {
	if info.WorkerID == uuid.Nil {
		return 0, false
	}
	cands, err := v.store.CandidateWorkers(ctx, info.jobType, info.ModelRef, info.MinMemoryGB)
	if err != nil {
		return 0, false // a read failure must not silently lower the check rate
	}
	for _, c := range cands {
		if c.ID == info.WorkerID {
			return c.Reputation, true
		}
	}
	return 0, false
}

// checkSampled draws a DETERMINISTIC per-task decision for whether to spend a
// sampled probe, true meaning "run the check this time". prob >= 1.0 is always
// true (full checking), and a non-positive prob also returns true (defensive: the
// floor keeps prob > 0, but a degenerate value must never disable checks). Otherwise
// it compares a stable [0,1) value derived from the task id's own bytes against
// prob, so the decision is identical across commit retries (idempotent) and
// uniformly spread across tasks without any RNG state or new dependency.
func (v *Verifier) checkSampled(taskID uuid.UUID, prob float64) bool {
	if prob >= 1.0 || prob <= 0 {
		return true
	}
	return taskSample(taskID) < prob
}

// taskSample maps a task id to a stable, uniformly distributed value in [0,1) by
// reading the first 8 bytes of its 16-byte UUID as a big-endian unsigned integer
// and dividing by 2^64. Deterministic and dependency-free (encoding/binary is
// already imported for the binary embed artifact).
func taskSample(taskID uuid.UUID) float64 {
	hi := binary.BigEndian.Uint64(taskID[:8])
	return float64(hi) / float64(1<<64)
}

// chunkVote is one committed result for a chunk plus the bytes the vote compares.
// taskID identifies the committed task behind the result so the tiebreak receipt
// (verification_events) references the right task; it is uuid.Nil in the no-object-
// store fallback where only the two blobs the caller fetched are known. engine +
// buildHash carry the worker's finer verification class so a byte-exact vote can
// tell whether two results are even comparable before docking on a mismatch.
type chunkVote struct {
	supplierID uuid.UUID
	taskID     uuid.UUID
	bytes      []byte
	engine     string
	buildHash  string
}

// verificationClass is the byte-equality comparability key for a result's worker:
// (hw_class is already guaranteed equal by the same-class matcher, so the finer key
// here is engine + build_hash). Two results are BYTE-comparable only when their
// classes match. An UNKNOWN build ("") is treated as its own non-matching class: a
// worker that does not advertise a build hash is never byte-docked against a known
// build, because we cannot prove they share kernels. Mirrors Hawking's profile
// identity (device + shader_hash + tensor_layout_hash); see docs/DETERMINISM_CLASS.md.
func sameVerificationClass(aEngine, aBuild, bEngine, bBuild string) bool {
	// An empty build hash on either side is "unknown" — not provably the same
	// kernels, so not the same class (never an auto-dock across that boundary).
	if aBuild == "" || bBuild == "" {
		return false
	}
	return aEngine == bEngine && aBuild == bBuild
}

// classKey formats a worker's finer verification class as "engine|build_hash", the
// string an hw_class-aware honeypot records in answer_class. A blank build hash yields
// a blank key (unknown class), which never matches a known class — the safe default.
func classKey(engine, buildHash string) string {
	if buildHash == "" {
		return ""
	}
	return engine + "|" + buildHash
}

// byteExactJobType reports whether a job type's redundancy/honeypot comparison is
// BYTE-EXACT (bytes.Equal in resultsAgree). The tolerant job types — embed (cosine),
// batch_classification (top-1 label), json_extraction (canonical JSON), rerank (exact
// order array) — compare SEMANTIC content that is robust to cross-kernel FP jitter, so
// they are safe to compare across classes and are unaffected by the class gate. Only
// the byte-exact types (batch_infer, audio_transcribe, custom, …) need the class
// boundary, because their bytes legitimately differ across engines/builds.
func byteExactJobType(jobType string) bool {
	switch jobType {
	case "embed", "batch_classification", "json_extraction", "rerank":
		return false
	default:
		return true
	}
}

// byteHoneypotComparable reports whether a honeypot's KNOWN ANSWER may be byte-compared
// against the committing worker's result. Tolerant job types compare semantics and are
// always comparable. A byte-exact job type is comparable ONLY when the known answer was
// recorded in a non-blank verification class that MATCHES the worker's (engine|build_hash);
// a blank or cross-class byte answer is skipped (never a wrongful quarantine). This is the
// gate that ACTIVATES class-aware generation honeypots (item 10).
func byteHoneypotComparable(jobType, answerClass, engine, buildHash string) bool {
	if !byteExactJobType(jobType) {
		return true
	}
	return answerClass != "" && answerClass == classKey(engine, buildHash)
}

// errHoneypotBlankClass is returned when a byte-exact honeypot is seeded without an
// answer_class. Such a honeypot can never fire (it is never class-comparable) — dead
// coverage — and a hand-written answer paired with a class would wrongly quarantine real
// workers. Byte-exact honeypots MUST carry the class of the worker that produced their
// known answer (item 11).
var errHoneypotBlankClass = fmt.Errorf("byte-exact honeypot requires a non-blank answer_class")

// validateHoneypotSeed refuses a blank-class byte-exact honeypot write (item 11). Tolerant
// job types may be class-blind (their comparison is semantic). Pure — unit-tested.
func validateHoneypotSeed(jobType, answerClass string) error {
	if byteExactJobType(jobType) && answerClass == "" {
		return fmt.Errorf("%w: job_type=%s", errHoneypotBlankClass, jobType)
	}
	return nil
}

// gatherChunkResults loads every committed result for the committing task's chunk
// (the primary + redundancy/tiebreak siblings) and fetches each result's bytes,
// so the N-way vote compares the real objects. When the verifier has no object
// store (unit contexts) or the chunk identity is unknown, it falls back to the
// two results already in hand (commitBytes + the peer the caller passed) so the
// 2-result paths still work. The committing result is guaranteed present because
// CommitTask persisted it before verification runs.
func (v *Verifier) gatherChunkResults(ctx context.Context, info *CommitTaskInfo, commitBytes []byte) ([]chunkVote, error) {
	if v.storage == nil || info.JobID == uuid.Nil {
		// No store to fetch siblings: the caller's two-blob comparison stands.
		return nil, nil
	}
	rows, err := v.store.ChunkResults(ctx, info.JobID, info.ChunkIndex)
	if err != nil {
		return nil, err
	}
	out := make([]chunkVote, 0, len(rows))
	for _, cr := range rows {
		var b []byte
		if cr.TaskID == info.TaskID {
			b = commitBytes // already in hand; avoid a redundant fetch
		} else {
			fetched, gerr := v.storage.GetObject(ctx, cr.ResultRef)
			if gerr != nil {
				// A sibling object we cannot read is a hard problem for the vote —
				// surface it rather than silently voting on fewer results.
				return nil, gerr
			}
			b = fetched
		}
		out = append(out, chunkVote{
			supplierID: cr.SupplierID,
			taskID:     cr.TaskID,
			bytes:      b,
			engine:     cr.Engine,
			buildHash:  cr.BuildHash,
		})
	}
	return out, nil
}

// resolveTiebreak runs the real N-way majority vote over all committed results
// for a chunk: the winning bytes are the majority (job-type-aware equality), each
// supplier on the winning side is credited a redundancy match, and each supplier
// on a losing side is docked a mismatch (and the metric bumped). With no majority
// (a 3-way split) no one is docked — an inconclusive vote must not punish.
func (v *Verifier) resolveTiebreak(ctx context.Context, info *CommitTaskInfo, all []chunkVote) (VerifyOutcome, error) {
	blobs := make([][]byte, len(all))
	for i, c := range all {
		blobs[i] = c.bytes
	}
	winner := majorityVote(info.jobType, blobs)
	// Was there an actual majority (more than half agree with the winner)?
	agreeWinner := 0
	for _, c := range all {
		if resultsAgree(info.jobType, c.bytes, winner) {
			agreeWinner++
		}
	}
	if agreeWinner*2 <= len(all) {
		// No majority: inconclusive. Do not dock anyone; the work is provisionally
		// accepted. (majorityVote returned the first result as a fallback.)
		return OutcomePassWithPenalty, nil
	}
	// Reference class for the winning bytes (byte-exact gate): the class of a vote on
	// the winning side. A loser is only DOCKED for a byte-exact job when it shares this
	// class — a cross-class loser's bytes differ legitimately (different kernels), so
	// it is credited a match (provisionally trusted) instead of docked. byteExact is
	// false for tolerant job types, which compare semantics and dock every loser.
	byteExact := byteExactJobType(info.jobType)
	var winEngine, winBuild string
	for _, c := range all {
		if resultsAgree(info.jobType, c.bytes, winner) {
			winEngine, winBuild = c.engine, c.buildHash
			break
		}
	}
	mismatch := false
	for _, c := range all {
		switch {
		case resultsAgree(info.jobType, c.bytes, winner):
			if err := v.store.DockReputation(ctx, c.supplierID, EventRedundancyMatch); err != nil {
				return OutcomeFail, err
			}
			_ = v.store.RecordVerificationEvent(ctx, info.JobID, c.taskID, c.supplierID, "tiebreak_win")
		case byteExact && !sameVerificationClass(winEngine, winBuild, c.engine, c.buildHash):
			// Byte-exact loser in a DIFFERENT class than the winner: not a defect —
			// the bytes legitimately differ across the class boundary. Do not dock;
			// credit a (provisional) match and record the cross-class skip so the gap
			// is visible rather than silently a clean win.
			if err := v.store.DockReputation(ctx, c.supplierID, EventRedundancyMatch); err != nil {
				return OutcomeFail, err
			}
			_ = v.store.RecordVerificationEvent(ctx, info.JobID, c.taskID, c.supplierID, "tiebreak_cross_class")
		default:
			mismatch = true
			if err := v.store.DockReputation(ctx, c.supplierID, EventMismatch); err != nil {
				return OutcomeFail, err
			}
			_ = v.store.RecordVerificationEvent(ctx, info.JobID, c.taskID, c.supplierID, "tiebreak_loss")
		}
	}
	if mismatch {
		// A loser was docked — the chunk had a real disagreement the vote settled.
		return OutcomePassWithPenalty, nil
	}
	return OutcomePass, nil
}

// dispatchTiebreak selects a third, distinct same-class worker (excluding both
// workers whose results already disagree) and inserts a pinned tiebreak task for
// the chunk, unless one is already pending. A missing third worker is not an
// error: with no one to break the tie the primary is provisionally trusted (the
// caller returns pass_with_penalty), never a fabricated pass.
func (v *Verifier) dispatchTiebreak(ctx context.Context, info *CommitTaskInfo, all []chunkVote) error {
	if v.storage == nil {
		return nil // no full lifecycle available (unit context)
	}
	exists, err := v.store.TiebreakExists(ctx, info.JobID, info.ChunkIndex)
	if err != nil || exists {
		return err
	}
	// Exclude every worker that already ran this chunk so the third opinion is
	// genuinely independent.
	chunk, err := v.store.ChunkResults(ctx, info.JobID, info.ChunkIndex)
	if err != nil {
		return err
	}
	var also []uuid.UUID
	var alsoSuppliers []uuid.UUID
	for _, cr := range chunk {
		if cr.WorkerID != info.WorkerID {
			also = append(also, cr.WorkerID)
			alsoSuppliers = append(alsoSuppliers, cr.SupplierID)
		}
	}
	// The third opinion must be independent of BOTH disputants — exclude the other
	// disputants' SUPPLIERS too, not just their worker ids, so one operator can never
	// hold two of the three votes (backlog P0 item 8).
	peer, err := v.store.SelectRedundancyPeerExcluding(ctx, info.jobType, info.ModelRef, info.MinMemoryGB, info.WorkerID, also, alsoSuppliers)
	if errors.Is(err, ErrNoSupply) {
		return nil // no third same-class worker online — provisional trust, logged upstream
	}
	if err != nil {
		return err
	}
	if _, err := v.store.InsertTiebreakTask(ctx, info.JobID, info.TaskID, peer, info.InputRef, info.ChunkIndex); err != nil {
		return err
	}
	metrics.tiebreaks.Add(1)
	return nil
}

// embeddingCosineThreshold is the action plan's pass gate for embedding
// redundancy: two embeddings of the same input on same-class hardware must have
// mean per-vector cosine similarity at or above this to agree.
const embeddingCosineThreshold = 0.999

// resultsAgree is the comparison primitive, keyed on job type. Each branch parses
// the job type's documented result schema from BOTH blobs and compares the
// semantic content; a parse failure, a shape mismatch, or a count mismatch is a
// DISAGREEMENT, never a fabricated pass (BLACKHOLE: surface every failure).
//
//   - embed                → mean per-vector cosine >= 0.999 (continuous band)
//   - batch_classification → per-item top-1 LABEL equality
//   - json_extraction      → per-item canonical-JSON (sorted-key) equality
//   - rerank               → per-item exact order-array equality
//   - everything else      → exact byte match (deterministic outputs)
func resultsAgree(jobType string, a, b []byte) bool {
	switch jobType {
	case "embed":
		sim, ok := meanCosine(a, b)
		return ok && sim >= embeddingCosineThreshold
	case "batch_classification":
		return classificationAgree(a, b)
	case "json_extraction":
		return jsonExtractionAgree(a, b)
	case "rerank":
		return rerankAgree(a, b)
	default:
		return bytes.Equal(a, b)
	}
}

// classificationResult is the batch_classification result schema: a top-1 label
// per input item, keyed by its index.
type classificationResult struct {
	Labels []struct {
		Index int    `json:"index"`
		Label string `json:"label"`
	} `json:"labels"`
}

// classificationAgree passes when both results assign the SAME top-1 label to
// every index. A parse failure, a differing item count, or any single label
// disagreement fails. Order-independent: items are matched by index.
func classificationAgree(a, b []byte) bool {
	var ra, rb classificationResult
	if json.Unmarshal(a, &ra) != nil || json.Unmarshal(b, &rb) != nil {
		return false
	}
	if len(ra.Labels) == 0 || len(ra.Labels) != len(rb.Labels) {
		return false
	}
	mb := make(map[int]string, len(rb.Labels))
	for _, it := range rb.Labels {
		mb[it.Index] = it.Label
	}
	for _, it := range ra.Labels {
		other, ok := mb[it.Index]
		if !ok || other != it.Label {
			return false
		}
	}
	return true
}

// jsonExtractionResult is the json_extraction result schema: an extracted JSON
// object per input item, keyed by index.
type jsonExtractionResult struct {
	Items []struct {
		Index int             `json:"index"`
		JSON  json.RawMessage `json:"json"`
	} `json:"items"`
}

// jsonExtractionAgree passes when both results extract a canonically-equal JSON
// object for every index. Equality is by canonical JSON (sorted keys, whitespace
// normalized) so two semantically identical objects with different key order
// agree. A parse failure, count mismatch, or any item that fails to canonicalize
// fails.
func jsonExtractionAgree(a, b []byte) bool {
	var ra, rb jsonExtractionResult
	if json.Unmarshal(a, &ra) != nil || json.Unmarshal(b, &rb) != nil {
		return false
	}
	if len(ra.Items) == 0 || len(ra.Items) != len(rb.Items) {
		return false
	}
	mb := make(map[int][]byte, len(rb.Items))
	for _, it := range rb.Items {
		c, ok := canonicalJSON(it.JSON)
		if !ok {
			return false
		}
		mb[it.Index] = c
	}
	for _, it := range ra.Items {
		ca, ok := canonicalJSON(it.JSON)
		if !ok {
			return false
		}
		cb, ok := mb[it.Index]
		if !ok || !bytes.Equal(ca, cb) {
			return false
		}
	}
	return true
}

// rerankResult is the rerank result schema: a ranking (ordered index list) per
// input item, keyed by index.
type rerankResult struct {
	Rankings []struct {
		Index int   `json:"index"`
		Order []int `json:"order"`
	} `json:"rankings"`
}

// rerankAgree passes when both results produce the EXACT same order array for
// every index (reranking is deterministic given the same model + candidates). A
// parse failure, count mismatch, or any order that differs in length or element
// fails.
func rerankAgree(a, b []byte) bool {
	var ra, rb rerankResult
	if json.Unmarshal(a, &ra) != nil || json.Unmarshal(b, &rb) != nil {
		return false
	}
	if len(ra.Rankings) == 0 || len(ra.Rankings) != len(rb.Rankings) {
		return false
	}
	mb := make(map[int][]int, len(rb.Rankings))
	for _, it := range rb.Rankings {
		mb[it.Index] = it.Order
	}
	for _, it := range ra.Rankings {
		other, ok := mb[it.Index]
		if !ok || !intsEqual(it.Order, other) {
			return false
		}
	}
	return true
}

func intsEqual(a, b []int) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// canonicalJSON parses arbitrary JSON and re-marshals it with object keys sorted
// (Go's encoding/json sorts map keys), so two semantically identical objects with
// different key order produce identical bytes. ok is false on a parse failure (a
// malformed extracted object is a disagreement, never a pass).
func canonicalJSON(raw json.RawMessage) ([]byte, bool) {
	if len(raw) == 0 {
		return nil, false
	}
	var v any
	if err := json.Unmarshal(raw, &v); err != nil {
		return nil, false
	}
	b, err := json.Marshal(v) // map keys are emitted in sorted order
	if err != nil {
		return nil, false
	}
	return b, true
}

// embeddingResult is the embed job's JSON result blob: a list of vectors, one per
// input record.
type embeddingResult struct {
	Vectors [][]float64 `json:"vectors"`
}

// parseEmbeddingVectors decodes an embed result blob into rows of float64,
// accepting BOTH the JSON `{"vectors":[...]}` shape and the opt-in binary float32
// artifact (PLANE_D D5/D15, magic "CXEM"). Verification must agree on whichever
// shape the worker actually produced, so a binary embed job's redundancy/honeypot
// comparison stays correct (and a worker is never wrongly docked because the blob
// was binary). ok is false on any malformation — an unparseable blob is a
// disagreement, not a pass.
func parseEmbeddingVectors(obj []byte) (vectors [][]float64, ok bool) {
	if isEmbedBinary(obj) {
		if len(obj) < embedBinHeaderLen || binary.LittleEndian.Uint32(obj[4:8]) != embedBinVersion {
			return nil, false
		}
		dim := int(binary.LittleEndian.Uint32(obj[8:12]))
		count := int(binary.LittleEndian.Uint32(obj[12:16]))
		if dim < 0 || count < 0 || len(obj) != embedBinHeaderLen+dim*count*4 {
			return nil, false
		}
		rows := make([][]float64, count)
		off := embedBinHeaderLen
		for r := 0; r < count; r++ {
			row := make([]float64, dim)
			for c := 0; c < dim; c++ {
				row[c] = float64(math.Float32frombits(binary.LittleEndian.Uint32(obj[off : off+4])))
				off += 4
			}
			rows[r] = row
		}
		return rows, true
	}
	var r embeddingResult
	if err := json.Unmarshal(obj, &r); err != nil {
		return nil, false
	}
	return r.Vectors, true
}

// meanCosine parses two embedding blobs (JSON or binary) and returns the mean of
// the per-vector cosine similarities. ok is false when either blob fails to parse,
// the vector counts differ, or there are no vectors — all of which are
// disagreements, not passes.
func meanCosine(a, b []byte) (sim float64, ok bool) {
	va, okA := parseEmbeddingVectors(a)
	vb, okB := parseEmbeddingVectors(b)
	if !okA || !okB {
		return 0, false
	}
	if len(va) == 0 || len(va) != len(vb) {
		return 0, false
	}
	var total float64
	for i := range va {
		c := cosine(va[i], vb[i])
		if math.IsNaN(c) {
			return 0, false
		}
		total += c
	}
	return total / float64(len(va)), true
}

// cosine computes the cosine similarity of two equal-length, non-zero vectors.
// A length mismatch, an empty vector, or a zero-magnitude vector returns 0
// (treated as "does not pass" by the caller), never a panic and never 1.
func cosine(a, b []float64) float64 {
	if len(a) == 0 || len(a) != len(b) {
		return 0
	}
	var dot, na, nb float64
	for i := range a {
		dot += a[i] * b[i]
		na += a[i] * a[i]
		nb += b[i] * b[i]
	}
	if na == 0 || nb == 0 {
		return 0
	}
	return dot / (math.Sqrt(na) * math.Sqrt(nb))
}

// majorityVote picks the result agreed on by a majority of same-class peers,
// using the same job-type-aware equality as resultsAgree (cosine for embeddings,
// exact bytes otherwise) so an embedding tiebreak is not defeated by benign
// floating-point jitter. No majority → the first (primary) result.
func majorityVote(jobType string, results [][]byte) []byte {
	if len(results) == 0 {
		return nil
	}
	for i := range results {
		votes := 0
		for j := range results {
			if resultsAgree(jobType, results[i], results[j]) {
				votes++
			}
		}
		if votes*2 > len(results) {
			return results[i]
		}
	}
	return results[0] // no majority: fall back to the first (primary) result
}

// jobTypeOf / taskIDOf are tiny adapters so verifyTaskResult reads cleanly.
func jobTypeOf(info *CommitTaskInfo) string { return info.jobType }

func taskIDOf(c TaskCommit) uuid.UUID { return c.TaskID }
