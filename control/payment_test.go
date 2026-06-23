package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/google/uuid"
)

// TestManualExportPayout proves the alpha "manual export" payout adapter: it
// appends each owed payout to the export file (never overwriting), returns a
// "manual-export" ref (never a fabricated transfer id), and refuses a non-positive
// amount. This is the vendor-neutral, no-real-money-movement alpha rail.
func TestManualExportPayout(t *testing.T) {
	path := filepath.Join(t.TempDir(), "payouts.csv")
	p := newManualExportPayout(path)
	s1 := uuid.MustParse("00000000-0000-0000-0000-0000000000a1")
	s2 := uuid.MustParse("00000000-0000-0000-0000-0000000000a2")

	ref, err := p.Send(context.Background(), s1, 1.25)
	if err != nil {
		t.Fatalf("Send: %v", err)
	}
	if ref != "manual-export:"+path {
		t.Fatalf("ref = %q, want manual-export:%s", ref, path)
	}
	// A second payout APPENDS (the file is a running export, not a single payout).
	if _, err := p.Send(context.Background(), s2, 2.5); err != nil {
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
	if _, err := p.Send(context.Background(), uuid.New(), 0); err == nil {
		t.Fatal("expected error for non-positive amount")
	}
	if b2, _ := os.ReadFile(path); len(strings.Split(strings.TrimSpace(string(b2)), "\n")) != 2 {
		t.Fatal("rejected payout must not append a row")
	}
}
