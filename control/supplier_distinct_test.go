package main

import (
	"context"
	"testing"

	"github.com/google/uuid"
)

// Proof for backlog P0 item 6: prunePeers excludes the anchor worker, every id in
// `also`, AND any worker from the anchor's OWN supplier. A same-supplier peer is not
// an independent cross-check (a multi-worker supplier could verify its own forged
// result), so it must never be eligible as a redundancy/tiebreak peer.
func TestPrunePeersExcludesSameSupplier(t *testing.T) {
	supA := uuid.New()
	supB := uuid.New()
	anchor := uuid.New()
	sibling := uuid.New() // supA, a DIFFERENT worker than the anchor (the collusion case)
	indep := uuid.New()   // supB, genuinely independent
	alsoOut := uuid.New() // supB but explicitly excluded via `also`
	cands := []MatchWorker{
		{ID: anchor, SupplierID: supA},
		{ID: sibling, SupplierID: supA},
		{ID: indep, SupplierID: supB},
		{ID: alsoOut, SupplierID: supB},
	}
	got := prunePeers(cands, anchor, supA, []uuid.UUID{alsoOut}, nil)
	if len(got) != 1 || got[0].ID != indep {
		t.Fatalf("expected only the independent supB worker %s; got %v", indep, mwIDs(got))
	}
	for _, w := range got {
		if w.SupplierID == supA {
			t.Fatalf("a same-supplier worker (%s) was wrongly kept as an independent peer", w.ID)
		}
		if w.ID == anchor || w.ID == alsoOut {
			t.Fatalf("an excluded worker (%s) was kept", w.ID)
		}
	}
}

// Proof for backlog P0 item 8: the tiebreak passes the OTHER disputants' suppliers as
// `alsoSuppliers`, so the third opinion is independent of BOTH sides — a worker from a
// disputant's supplier (a different worker id, same supplier) is rejected, preventing
// one operator from holding two of the three votes.
func TestPrunePeersExcludesDisputantSuppliers(t *testing.T) {
	supAnchor := uuid.New()
	supDisputant := uuid.New()
	supIndep := uuid.New()
	anchor := uuid.New()
	disputantSibling := uuid.New() // a different worker, but from the other disputant's supplier
	indep := uuid.New()
	cands := []MatchWorker{
		{ID: disputantSibling, SupplierID: supDisputant},
		{ID: indep, SupplierID: supIndep},
	}
	got := prunePeers(cands, anchor, supAnchor, nil, []uuid.UUID{supDisputant})
	if len(got) != 1 || got[0].ID != indep {
		t.Fatalf("expected only the third-supplier worker %s; got %v", indep, mwIDs(got))
	}
}

// An unknown (uuid.Nil) anchor supplier disables ONLY the supplier gate, never the
// worker gate — so it still excludes the anchor but does not over-exclude every
// candidate (preserves behavior for pre-supplier worker rows).
func TestPrunePeersNilAnchorSupplierKeepsWorkers(t *testing.T) {
	anchor := uuid.New()
	cands := []MatchWorker{
		{ID: anchor, SupplierID: uuid.New()},
		{ID: uuid.New(), SupplierID: uuid.New()},
		{ID: uuid.New(), SupplierID: uuid.New()},
	}
	got := prunePeers(cands, anchor, uuid.Nil, nil, nil)
	if len(got) != 2 {
		t.Fatalf("a nil anchor supplier must exclude only the anchor; got %d peers", len(got))
	}
	for _, w := range got {
		if w.ID == anchor {
			t.Fatal("the anchor worker must always be excluded")
		}
	}
}

// Proof for backlog P0 item 7: a 2-result agreement from the SAME known supplier is
// NOT an independent redundancy match; different/unknown suppliers (and a single
// result) are. This is the gate that stops a multi-worker supplier from verifying
// its own forged output.
func TestIndependentRedundancyMatch(t *testing.T) {
	supA := uuid.New()
	supB := uuid.New()
	if independentRedundancyMatch([]chunkVote{{supplierID: supA}, {supplierID: supA}}) {
		t.Fatal("a same-supplier 2-result agreement must NOT count as an independent match")
	}
	if independentRedundancyMatch([]chunkVote{{supplierID: supA}, {supplierID: supA}, {supplierID: supA}}) {
		t.Fatal("three task rows from one supplier must still count as one non-independent vote")
	}
	if !independentRedundancyMatch([]chunkVote{{supplierID: supA}, {supplierID: supB}}) {
		t.Fatal("a different-supplier agreement must count as an independent match")
	}
	if !independentRedundancyMatch([]chunkVote{{supplierID: supA}, {supplierID: uuid.Nil}}) {
		t.Fatal("an unknown-supplier peer must still count (not provably same-supplier)")
	}
	if !independentRedundancyMatch([]chunkVote{{supplierID: supA}}) {
		t.Fatal("a single result must not be classed as a same-supplier non-match")
	}
}

func TestIndependentSupplierVotesChooseOneDeterministicRepresentative(t *testing.T) {
	supA := uuid.MustParse("10000000-0000-0000-0000-000000000001")
	supB := uuid.MustParse("20000000-0000-0000-0000-000000000002")
	taskA := uuid.MustParse("30000000-0000-0000-0000-000000000003")
	taskBFirst := uuid.MustParse("40000000-0000-0000-0000-000000000004")
	taskBSecond := uuid.MustParse("50000000-0000-0000-0000-000000000005")
	votes := []chunkVote{
		{supplierID: supB, taskID: taskBSecond, bytes: []byte("b")},
		{supplierID: supA, taskID: taskA, bytes: []byte("a")},
		{supplierID: supB, taskID: taskBFirst, bytes: []byte("b")},
	}

	got := independentSupplierVotes(votes)
	if len(got) != 2 {
		t.Fatalf("independent supplier votes = %d, want 2: %#v", len(got), got)
	}
	bySupplier := make(map[uuid.UUID]chunkVote, len(got))
	for _, vote := range got {
		bySupplier[vote.supplierID] = vote
	}
	if bySupplier[supA].taskID != taskA {
		t.Fatalf("supplier A representative = %s, want %s", bySupplier[supA].taskID, taskA)
	}
	if bySupplier[supB].taskID != taskBFirst {
		t.Fatalf("supplier B representative = %s, want deterministic lowest task %s",
			bySupplier[supB].taskID, taskBFirst)
	}

	reversed := []chunkVote{votes[2], votes[1], votes[0]}
	again := independentSupplierVotes(reversed)
	if len(again) != len(got) {
		t.Fatalf("reordered representatives = %d, want %d", len(again), len(got))
	}
	for i := range got {
		if got[i].supplierID != again[i].supplierID || got[i].taskID != again[i].taskID ||
			string(got[i].bytes) != string(again[i].bytes) {
			t.Fatalf("representative selection depends on input order:\nfirst  %#v\nsecond %#v", got, again)
		}
	}
}

func TestResolveTiebreakDoesNotCountSameSupplierTwice(t *testing.T) {
	supA := uuid.MustParse("10000000-0000-0000-0000-000000000001")
	supB := uuid.MustParse("20000000-0000-0000-0000-000000000002")
	supC := uuid.MustParse("30000000-0000-0000-0000-000000000003")
	taskA := uuid.MustParse("40000000-0000-0000-0000-000000000004")
	taskBRepresentative := uuid.MustParse("50000000-0000-0000-0000-000000000005")
	taskBDuplicate := uuid.MustParse("60000000-0000-0000-0000-000000000006")
	taskC := uuid.MustParse("70000000-0000-0000-0000-000000000007")
	info := &CommitTaskInfo{
		TaskID: taskA, JobID: uuid.New(), SupplierID: supA,
		Attempt: 2, jobType: "batch_infer", engine: "candle", buildHash: "build-1",
	}
	twoSupplierVotes := []chunkVote{
		{supplierID: supA, taskID: taskA, bytes: []byte("answer-a"), engine: "candle", buildHash: "build-1"},
		{supplierID: supB, taskID: taskBRepresentative, bytes: []byte("answer-b"), engine: "candle", buildHash: "build-1"},
		{supplierID: supB, taskID: taskBDuplicate, bytes: []byte("answer-b"), engine: "candle", buildHash: "build-1"},
	}

	recorder := &recordingVerificationStore{taskID: taskA, attempt: info.Attempt}
	verifier := &Verifier{store: recorder}
	outcome, err := verifier.resolveTiebreak(context.Background(), info, twoSupplierVotes)
	if err != nil {
		t.Fatalf("resolve duplicate-supplier vote: %v", err)
	}
	if outcome != OutcomePassWithPenalty {
		t.Fatalf("A vs B+B outcome = %q, want inconclusive %q", outcome, OutcomePassWithPenalty)
	}
	if len(recorder.effects) != 0 {
		t.Fatalf("A vs B+B manufactured majority effects: %#v", recorder.effects)
	}
	if got := len(independentSupplierVotes(twoSupplierVotes)); got != 2 {
		t.Fatalf("A vs B+B independent vote count = %d, want 2 so normal planning dispatches a third supplier", got)
	}

	// A genuinely independent supplier C agreeing with B resolves the dispute.
	// Supplier B still receives exactly one vote/effect set despite its duplicate
	// hedge+redundancy rows, and only A's representative is clawed back.
	recorder = &recordingVerificationStore{taskID: taskA, attempt: info.Attempt}
	verifier = &Verifier{store: recorder}
	resolved := append(append([]chunkVote(nil), twoSupplierVotes...),
		chunkVote{supplierID: supC, taskID: taskC, bytes: []byte("answer-b"), engine: "candle", buildHash: "build-1"})
	outcome, err = verifier.resolveTiebreak(context.Background(), info, resolved)
	if err != nil {
		t.Fatalf("resolve independent third supplier: %v", err)
	}
	if outcome != OutcomeLossNoPayout {
		t.Fatalf("A vs B+B+C outcome = %q, want committing supplier loss %q", outcome, OutcomeLossNoPayout)
	}
	docks := map[uuid.UUID]int{}
	events := map[uuid.UUID]int{}
	clawbacks := map[uuid.UUID]int{}
	for _, effect := range recorder.effects {
		if effect.TaskID == taskBDuplicate {
			t.Fatalf("duplicate supplier task received an effect: %#v", effect)
		}
		switch effect.Kind {
		case VerificationEffectDockReputation:
			docks[effect.SupplierID]++
		case VerificationEffectRecordEvent:
			events[effect.SupplierID]++
		case VerificationEffectClawbackCredit:
			clawbacks[effect.SupplierID]++
		}
	}
	for _, supplierID := range []uuid.UUID{supA, supB, supC} {
		if docks[supplierID] != 1 || events[supplierID] != 1 {
			t.Fatalf("supplier %s effects: docks=%d events=%d, want one each; all=%#v",
				supplierID, docks[supplierID], events[supplierID], recorder.effects)
		}
	}
	if clawbacks[supA] != 1 || clawbacks[supB] != 0 || clawbacks[supC] != 0 {
		t.Fatalf("clawbacks = %#v, want exactly one for losing supplier A", clawbacks)
	}
}

func mwIDs(ws []MatchWorker) []uuid.UUID {
	out := make([]uuid.UUID, len(ws))
	for i, w := range ws {
		out[i] = w.ID
	}
	return out
}
