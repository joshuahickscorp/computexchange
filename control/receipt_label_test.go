package main

import "testing"

// Items 9 + 12: the verification receipt label differentiates verified / honeypot-checked
// / no-independent-peer / cross-class-skip / unverified, surfacing the same-supplier and
// cross-class coverage gaps that the supplier-distinct + class machinery now record. An
// independent cross-check (redundancy match or tiebreak) always wins.
func TestDeriveVerificationLabel(t *testing.T) {
	cases := []struct {
		name string
		v    Verification
		want string
	}{
		{"independent redundancy", Verification{RedundancyMatched: 1, Checked: 1}, "verified"},
		{"tiebreak", Verification{Tiebreaks: 1, Checked: 1}, "verified"},
		{"honeypot only", Verification{HoneypotsPassed: 2, Checked: 2}, "honeypot-checked"},
		{"same-supplier only", Verification{SameSupplier: 1}, "no-independent-peer"},
		{"cross-class only", Verification{CrossClassSkipped: 1}, "cross-class-skip"},
		{"nothing", Verification{}, "unverified"},
		{"verified beats gaps", Verification{RedundancyMatched: 1, SameSupplier: 1, CrossClassSkipped: 1, Checked: 1}, "verified"},
	}
	for _, c := range cases {
		if got := deriveVerificationLabel(c.v); got != c.want {
			t.Errorf("%s: label = %q, want %q", c.name, got, c.want)
		}
	}
}
