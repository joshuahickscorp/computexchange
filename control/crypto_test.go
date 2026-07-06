package main

import (
	"sort"
	"testing"

	"github.com/google/uuid"
)

// With a key set, a sealed token must round-trip and must NOT appear in plaintext.
func TestSealOpenRoundTrip(t *testing.T) {
	t.Setenv("CX_TOKEN_KEY", "test-secret-key")
	sealed := sealToken("ghp_secrettoken123")
	if sealed == "ghp_secrettoken123" || len(sealed) < 5 || sealed[:4] != "enc:" {
		t.Fatalf("token not sealed: %q", sealed)
	}
	if got := openToken(sealed); got != "ghp_secrettoken123" {
		t.Fatalf("open = %q, want original", got)
	}
}

// With no key, sealing is honest plaintext (tagged) and still round-trips.
func TestSealNoKeyIsHonestPlaintext(t *testing.T) {
	t.Setenv("CX_TOKEN_KEY", "")
	sealed := sealToken("abc")
	if sealed != "plain:abc" {
		t.Fatalf("expected plain: marker, got %q", sealed)
	}
	if got := openToken(sealed); got != "abc" {
		t.Fatalf("open = %q", got)
	}
}

// Signed state must verify, and a tampered signature must be rejected.
func TestStateSignVerify(t *testing.T) {
	t.Setenv("CX_STATE_SECRET", "state-secret")
	id := uuid.New()
	st := signState(id)
	got, ok := verifyState(st)
	if !ok || got != id {
		t.Fatalf("verify failed for valid state")
	}
	if _, ok := verifyState(st + "x"); ok {
		t.Fatal("tampered state must not verify")
	}
	if _, ok := verifyState(id.String()); ok {
		t.Fatal("unsigned state must not verify when a secret is set")
	}
}

// redundancySelectionHash must be deterministic for a fixed (jobID, taskID) pair
// (so a replayed job assigns redundancy peers identically) but must NOT reduce to
// picking task IDs in their original ordinal position — the whole point of the
// fix (Verification Redundancy & Trust-Compute Overhead 6->7) is that "which
// primaries get a peer" no longer correlates with submission/chunk order.
func TestRedundancySelectionHashIsDeterministicNotOrdinal(t *testing.T) {
	jobID := uuid.New()
	tasks := make([]uuid.UUID, 20)
	for i := range tasks {
		tasks[i] = uuid.New()
	}

	// Determinism: hashing the same pair twice gives the same value.
	for _, id := range tasks {
		if redundancySelectionHash(jobID, id) != redundancySelectionHash(jobID, id) {
			t.Fatalf("redundancySelectionHash not deterministic for task %s", id)
		}
	}

	// Not ordinal: sort tasks by hash and confirm the resulting order is not
	// simply the original slice order — with 20 random UUIDs the odds of a real
	// hash function coincidentally preserving input order are negligible, so any
	// failure here is a real bug (e.g. accidentally hashing the index instead of
	// the UUID), not flakiness.
	sorted := append([]uuid.UUID(nil), tasks...)
	sort.Slice(sorted, func(i, j int) bool {
		return redundancySelectionHash(jobID, sorted[i]) < redundancySelectionHash(jobID, sorted[j])
	})
	same := true
	for i := range tasks {
		if tasks[i] != sorted[i] {
			same = false
			break
		}
	}
	if same {
		t.Fatal("hash-sorted order matched original ordinal order — selection is not actually keyed by the hash")
	}

	// A different job salts the same task ID to a different rank — two jobs
	// never share a predictable "always chunk 0..k" pattern.
	otherJob := uuid.New()
	differs := false
	for _, id := range tasks {
		if redundancySelectionHash(jobID, id) != redundancySelectionHash(otherJob, id) {
			differs = true
			break
		}
	}
	if !differs {
		t.Fatal("redundancySelectionHash did not vary with jobID")
	}
}
