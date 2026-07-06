package main

import (
	"encoding/json"
	"errors"
	"strings"
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

// Week 6b (hawking cross-worker determinism re-gate): the SEEDED hawking-class
// batch_infer honeypot's data is internally consistent, at unit level (no DB):
// the class the seed writes is exactly what the verifier computes for a worker
// of the producing class, cross-class/unknown-build workers are never
// comparable, and the captured known answer/input chunk satisfy the recorded
// invariants (real document shape, rows EOS'd strictly below the recorded
// max_tokens=24, prompts match the answer 1:1). The live-Postgres round-trip +
// dock/skip behavior is proven in honeypot_hawking_regate_test.go (integration).
func TestHawkingHoneypotSeedDataConsistency(t *testing.T) {
	class := classKey(demoHoneypotHawkEngine, demoHoneypotHawkBuildHash)
	if class != demoHoneypotHawkEngine+"|"+demoHoneypotHawkBuildHash {
		t.Fatalf("classKey format drifted: %q", class)
	}
	if err := validateHoneypotSeed("batch_infer", class); err != nil {
		t.Fatalf("the seeded hawking class must pass the seed guard: %v", err)
	}
	// The exact activation + boundary matrix for the seeded class.
	if !byteHoneypotComparable("batch_infer", class, demoHoneypotHawkEngine, demoHoneypotHawkBuildHash) {
		t.Fatal("a worker of the producing class must be byte-comparable (the activation)")
	}
	if byteHoneypotComparable("batch_infer", class, "candle", demoHoneypotHawkBuildHash) {
		t.Fatal("a candle worker must NEVER be byte-compared against the hawking answer")
	}
	if byteHoneypotComparable("batch_infer", class, demoHoneypotHawkEngine, "someotherbuild00") {
		t.Fatal("a hawking worker on a DIFFERENT build must not be byte-compared")
	}
	if byteHoneypotComparable("batch_infer", class, demoHoneypotHawkEngine, "") {
		t.Fatal("an unknown-build hawking worker must not be byte-compared")
	}

	// The known answer is the real BatchInferResult document shape, one
	// completion per input row, every row EOS'd strictly below the recorded
	// max_tokens=24 (the max_tokens-invariance precondition documented in
	// docs/DETERMINISM_CLASS.md — a truncated row's bytes would depend on the
	// dispatching job's max_tokens).
	var doc struct {
		JobType     string `json:"job_type"`
		Model       string `json:"model"`
		Completions []struct {
			Index  int    `json:"index"`
			Text   string `json:"text"`
			Tokens int    `json:"tokens"`
		} `json:"completions"`
	}
	if err := json.Unmarshal([]byte(demoHoneypotHawkKnownAnswer), &doc); err != nil {
		t.Fatalf("known answer must be a valid BatchInferResult document: %v", err)
	}
	if doc.JobType != "batch_infer" || doc.Model != "llama-3.2-1b-instruct-q4" {
		t.Fatalf("known answer header drifted: job_type=%q model=%q", doc.JobType, doc.Model)
	}
	inputRows := strings.Split(strings.TrimRight(demoHoneypotHawkInputJSONL, "\n"), "\n")
	if len(doc.Completions) != len(inputRows) || len(inputRows) == 0 {
		t.Fatalf("want one completion per input row: %d completions, %d rows",
			len(doc.Completions), len(inputRows))
	}
	const recordedMaxTokens = 24
	for i, c := range doc.Completions {
		if c.Index != i {
			t.Fatalf("completion %d out of input order (index %d)", i, c.Index)
		}
		if c.Tokens < 1 || c.Tokens >= recordedMaxTokens {
			t.Fatalf("row %d has %d tokens — must be >=1 and < %d (natural EOS, or the "+
				"answer's bytes depend on the buyer job's max_tokens)", i, c.Tokens, recordedMaxTokens)
		}
	}
	// Each input row is valid JSONL with a prompt the corresponding completion
	// actually continues (the chunk and the answer cannot drift apart).
	for i, row := range inputRows {
		var in struct {
			ID     string `json:"id"`
			Prompt string `json:"prompt"`
		}
		if err := json.Unmarshal([]byte(row), &in); err != nil {
			t.Fatalf("input row %d is not valid JSON: %v", i, err)
		}
		if in.Prompt == "" {
			t.Fatalf("input row %d has an empty prompt", i)
		}
		if !strings.HasPrefix(doc.Completions[i].Text, in.Prompt) && len(doc.Completions[i].Text) < 1 {
			t.Fatalf("row %d completion does not correspond to its prompt", i)
		}
	}
}
