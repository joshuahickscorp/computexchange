package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/google/uuid"
)

// TestMarkPayoutRefusesReleasedWithoutRef proves the money invariant: a credit can
// never be marked 'released' without a real rail reference (BLACKHOLE: never fake a
// transfer). The guard returns before any DB call, so a zero-value Store suffices.
func TestMarkPayoutRefusesReleasedWithoutRef(t *testing.T) {
	s := &Store{}
	if err := s.MarkPayout(context.Background(), uuid.New(), PayoutReleased, ""); err == nil {
		t.Fatal("MarkPayout(released, \"\") must error — releasing without a payout ref would fake a transfer")
	}
}

// TestManualExportPayout proves the alpha "manual export" payout adapter: it
// appends each owed payout to the export file (never overwriting), returns a
// "manual-export" ref (never a fabricated transfer id), and refuses a non-positive
// amount. This is the vendor-neutral, no-real-money-movement alpha rail.
func TestManualExportPayout(t *testing.T) {
	path := filepath.Join(t.TempDir(), "payouts.csv")
	p := newManualExportPayout(path)
	s1 := uuid.MustParse("00000000-0000-0000-0000-0000000000a1")
	s2 := uuid.MustParse("00000000-0000-0000-0000-0000000000a2")

	ref, err := p.Send(context.Background(), s1, 1.25, uuid.NewString())
	if err != nil {
		t.Fatalf("Send: %v", err)
	}
	if ref != "manual-export:"+path {
		t.Fatalf("ref = %q, want manual-export:%s", ref, path)
	}
	// A second payout APPENDS (the file is a running export, not a single payout).
	if _, err := p.Send(context.Background(), s2, 2.5, uuid.NewString()); err != nil {
		t.Fatalf("Send 2: %v", err)
	}

	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read export: %v", err)
	}
	lines := strings.Split(strings.TrimSpace(string(b)), "\n")
	if len(lines) != 2 {
		t.Fatalf("expected 2 exported rows, got %d: %q", len(lines), string(b))
	}
	if !strings.HasPrefix(lines[0], s1.String()+",1.250000,") {
		t.Fatalf("row 0 = %q, want %s,1.250000,<ts>", lines[0], s1)
	}
	if !strings.HasPrefix(lines[1], s2.String()+",2.500000,") {
		t.Fatalf("row 1 = %q", lines[1])
	}

	// Non-positive amounts are rejected and NOT exported (no fake zero payouts).
	if _, err := p.Send(context.Background(), uuid.New(), 0, uuid.NewString()); err == nil {
		t.Fatal("expected error for non-positive amount")
	}
	if b2, _ := os.ReadFile(path); len(strings.Split(strings.TrimSpace(string(b2)), "\n")) != 2 {
		t.Fatal("rejected payout must not append a row")
	}
}

// TestStripeIdempotencyKey pins the real-money fix: the Stripe payout idempotency
// key must be UNIQUE per distinct payout yet STABLE across a genuine retry of the
// same payout. The old key was "cx-{supplier}-{cents}", so two separate credits of
// identical cents in different release cycles produced the same key — Stripe
// replayed the first transfer and silently dropped the second payout. The released
// ledger-entry id is now folded in, so distinct entries can never collide while a
// retry of the same entry reuses its key.
func TestStripeIdempotencyKey(t *testing.T) {
	supplier := uuid.MustParse("00000000-0000-0000-0000-0000000000b1")
	const cents = int64(500) // identical amount in both cycles — the collision trigger

	// Two SEPARATE supplier credits (distinct ledger-entry ids), same supplier, same
	// cents: this is exactly the case that used to collide and drop a payout.
	entry1 := uuid.NewString()
	entry2 := uuid.NewString()
	k1 := stripeIdempotencyKey(supplier, cents, entry1)
	k2 := stripeIdempotencyKey(supplier, cents, entry2)
	if k1 == k2 {
		t.Fatalf("distinct payouts collided: both keyed %q — the second transfer would be silently dropped", k1)
	}

	// A genuine RETRY of the SAME payout (same entry id) must reuse the key so Stripe
	// treats it as a no-op rather than paying twice. Idempotency is preserved.
	if got := stripeIdempotencyKey(supplier, cents, entry1); got != k1 {
		t.Fatalf("retry of same payout changed key: %q != %q (would double-pay)", got, k1)
	}

	// The key stays bound to supplier + cents, so it can never be reused across a
	// different supplier or amount even for the same entry id.
	other := uuid.MustParse("00000000-0000-0000-0000-0000000000b2")
	if stripeIdempotencyKey(other, cents, entry1) == k1 {
		t.Fatal("key not bound to supplier: same key for two suppliers")
	}
	if stripeIdempotencyKey(supplier, cents+1, entry1) == k1 {
		t.Fatal("key not bound to cents: same key for two amounts")
	}

	// Defensive fallback: an empty payoutKey degrades to the legacy (supplier, cents)
	// scheme — never a blank discriminator, never worse than before.
	if got, want := stripeIdempotencyKey(supplier, cents, ""), "cx-"+supplier.String()+"-500"; got != want {
		t.Fatalf("empty-key fallback = %q, want %q", got, want)
	}
}
