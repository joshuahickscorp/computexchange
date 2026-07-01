package main

import (
	"errors"
	"testing"
)

// Item 10: class-aware generation honeypots. A byte-exact honeypot (batch_infer) is
// comparable ONLY with a non-blank answer_class matching the committing worker's class;
// tolerant types are always comparable; blank or cross-class byte answers are skipped
// (never a wrongful quarantine).
func TestByteHoneypotComparable(t *testing.T) {
	if !byteHoneypotComparable("embed", "", "candle", "h1") {
		t.Fatal("tolerant job type must always be comparable")
	}
	if !byteHoneypotComparable("batch_infer", "candle|h1", "candle", "h1") {
		t.Fatal("byte-exact honeypot with matching non-blank class must be comparable (the activation)")
	}
	if byteHoneypotComparable("batch_infer", "", "candle", "h1") {
		t.Fatal("byte-exact honeypot with blank class must NOT be comparable")
	}
	if byteHoneypotComparable("batch_infer", "candle|h1", "candle", "h2") {
		t.Fatal("byte-exact honeypot in a DIFFERENT class must NOT be comparable")
	}
}

// Item 11: the seed/admin path refuses a blank-class byte-exact honeypot write.
func TestValidateHoneypotSeed(t *testing.T) {
	if err := validateHoneypotSeed("batch_infer", ""); !errors.Is(err, errHoneypotBlankClass) {
		t.Fatalf("a blank-class byte-exact honeypot must be refused; got %v", err)
	}
	if err := validateHoneypotSeed("batch_infer", "candle|h1"); err != nil {
		t.Fatalf("a byte-exact honeypot WITH a class must be allowed; got %v", err)
	}
	if err := validateHoneypotSeed("embed", ""); err != nil {
		t.Fatalf("a tolerant honeypot may be class-blind; got %v", err)
	}
}
