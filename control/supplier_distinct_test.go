package main

import (
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

func mwIDs(ws []MatchWorker) []uuid.UUID {
	out := make([]uuid.UUID, len(ws))
	for i, w := range ws {
		out[i] = w.ID
	}
	return out
}
