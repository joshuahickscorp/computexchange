package main

import (
	"testing"

	"github.com/google/uuid"
)

// Plane C quote — pure unit tests for the JSONL preflight scanner and the
// risk/confidence assessor (no DB). The endpoint + persistence are covered by the
// integration suite (TestQuoteEndpointPersistsAssumptions).

func TestScanJSONLCountsAndFields(t *testing.T) {
	input := []byte(`{"id":"a","text":"hello world"}

{"id":"b","text":"compute"}
{"id":"c","prompt":"go"}
`)
	s := scanJSONL(input)
	if s.Records != 3 {
		t.Fatalf("records=%d, want 3 (blank line dropped)", s.Records)
	}
	if s.MalformedRecords != 0 || s.FirstBadLine != 0 {
		t.Fatalf("clean input flagged malformed: %+v", s)
	}
	// Token heuristic is bytes/4, rounded up, and never claims to be exact.
	if s.EstimatedTokens <= 0 || int(s.EstimatedTokens) < s.Bytes/4 {
		t.Fatalf("estimated_tokens=%d for %d bytes looks wrong", s.EstimatedTokens, s.Bytes)
	}
	// Field union across records: id, text, prompt (sorted).
	want := []string{"id", "prompt", "text"}
	if len(s.DetectedFields) != 3 || s.DetectedFields[0] != "id" ||
		s.DetectedFields[1] != "prompt" || s.DetectedFields[2] != "text" {
		t.Fatalf("detected_fields=%v, want %v", s.DetectedFields, want)
	}
}

func TestScanJSONLFindsFirstMalformedLine(t *testing.T) {
	// Line 1 valid, line 2 blank (skipped, still counts toward line number),
	// line 3 malformed → first_bad_line must be 3, not 2.
	input := []byte("{\"ok\":1}\n\n{not json}\n{\"ok\":2}\n")
	s := scanJSONL(input)
	if s.Records != 3 {
		t.Fatalf("records=%d, want 3", s.Records)
	}
	if s.MalformedRecords != 1 {
		t.Fatalf("malformed=%d, want 1", s.MalformedRecords)
	}
	if s.FirstBadLine != 3 {
		t.Fatalf("first_bad_line=%d, want 3 (line numbers count blanks)", s.FirstBadLine)
	}
}

func TestScanJSONLEmpty(t *testing.T) {
	s := scanJSONL([]byte("\n  \n\n"))
	if s.Records != 0 || s.EstimatedTokens != 0 || len(s.DetectedFields) != 0 {
		t.Fatalf("empty input should be all-zero, got %+v", s)
	}
}

func TestAssessRiskSupplyDrivesOOMAndConfidence(t *testing.T) {
	clean := QuoteInputScan{Records: 100, Bytes: 4000, EstimatedTokens: 1000}

	// Ample supply + known model, but NONE warm → low OOM, high-ish confidence, and
	// cold-start honestly medium (a cold load is still possible).
	oom, cold, conf, warn := assessRisk(clean, 8, 0, 4.0)
	if oom != "low" {
		t.Fatalf("oom=%q with 8 eligible workers, want low", oom)
	}
	if cold != "medium" {
		t.Fatalf("cold-start should be medium with no warm supply, got %q", cold)
	}
	if conf.Score < 0.6 {
		t.Fatalf("confidence=%v too low for ample supply", conf.Score)
	}
	if len(warn) != 0 {
		t.Fatalf("clean input + supply should warn nothing, got %v", warn)
	}

	// Warm-routing (D3): the SAME supply but with warm eligible workers → cold-start
	// drops to low and confidence rises, because the job can start without a load.
	oomWarm, coldWarm, confWarm, _ := assessRisk(clean, 8, 3, 4.0)
	if oomWarm != "low" {
		t.Fatalf("warm supply should not worsen OOM, got %q", oomWarm)
	}
	if coldWarm != "low" {
		t.Fatalf("warm eligible workers should make cold-start low, got %q", coldWarm)
	}
	if confWarm.Score <= conf.Score {
		t.Fatalf("warm supply should raise confidence (%v !> %v)", confWarm.Score, conf.Score)
	}

	// No eligible supply → high OOM, a buyer-visible warning, lower confidence.
	oom2, _, conf2, warn2 := assessRisk(clean, 0, 0, 4.0)
	if oom2 != "high" {
		t.Fatalf("oom=%q with 0 eligible workers, want high", oom2)
	}
	if conf2.Score >= conf.Score {
		t.Fatalf("no supply should lower confidence (%v !< %v)", conf2.Score, conf.Score)
	}
	if len(warn2) == 0 {
		t.Fatal("no eligible supply must surface a warning")
	}
}

func TestAssessRiskMalformedLowersConfidenceAndWarns(t *testing.T) {
	bad := QuoteInputScan{Records: 100, Bytes: 4000, MalformedRecords: 5, FirstBadLine: 12}
	_, _, conf, warn := assessRisk(bad, 8, 0, 4.0)
	foundMalformedWarn := false
	for _, w := range warn {
		if len(w) > 0 && (containsSub(w, "malformed")) {
			foundMalformedWarn = true
		}
	}
	if !foundMalformedWarn {
		t.Fatalf("malformed records must produce a warning, got %v", warn)
	}
	if conf.Score >= 0.8 {
		t.Fatalf("malformed input should lower confidence, got %v", conf.Score)
	}
}

// Plane D D4 memory-floor feedback — applyMemoryFloorRisk escalates OOM + lowers
// confidence when the model's floor exceeds the MEDIAN effective memory of eligible
// workers, softly cautions when the floor merely approaches the median, and leaves
// risk alone (only adding a reassuring reason) when the median comfortably clears
// the floor. Pure; the live median query is covered by TestMemorySampleRecorded
// (integration). assessRisk itself stays untouched (its tests above still pass).
func TestApplyMemoryFloorRisk(t *testing.T) {
	base := QuoteConfidence{Score: 0.80, Reasons: []string{"baseline"}}

	// Floor (24) over the median eligible effective memory (16) → escalate low→medium,
	// confidence drops, a reason names the median.
	oom, conf := applyMemoryFloorRisk("low", base, 24, 16)
	if oom != "medium" {
		t.Fatalf("floor>median should escalate low→medium, got %q", oom)
	}
	if conf.Score >= base.Score {
		t.Fatalf("floor>median must lower confidence (%v !< %v)", conf.Score, base.Score)
	}
	if !reasonsMention(conf.Reasons, "exceeds the median") {
		t.Fatalf("expected an explainable median reason, got %v", conf.Reasons)
	}

	// Already high stays high (no overflow past the top of the ladder).
	if oom2, _ := applyMemoryFloorRisk("high", base, 24, 16); oom2 != "high" {
		t.Fatalf("high should stay high, got %q", oom2)
	}

	// Floor (15) within memFloorTightMargin of the median (16) → no escalation, but a
	// softer confidence shave + a "little headroom" caution.
	oomTight, confTight := applyMemoryFloorRisk("low", base, 15, 16)
	if oomTight != "low" {
		t.Fatalf("a tight-but-clearing floor should NOT escalate, got %q", oomTight)
	}
	if confTight.Score >= base.Score {
		t.Fatalf("a tight floor should still shave confidence (%v !< %v)", confTight.Score, base.Score)
	}

	// Floor (8) comfortably under the median (64) → risk unchanged, confidence kept,
	// only a reassuring reason added.
	oomOK, confOK := applyMemoryFloorRisk("low", base, 8, 64)
	if oomOK != "low" {
		t.Fatalf("ample median should leave OOM low, got %q", oomOK)
	}
	if confOK.Score != base.Score {
		t.Fatalf("ample median should not change confidence (%v != %v)", confOK.Score, base.Score)
	}
	if !reasonsMention(confOK.Reasons, "comfortably clears") {
		t.Fatalf("expected a reassuring reason, got %v", confOK.Reasons)
	}
}

func reasonsMention(reasons []string, sub string) bool {
	for _, r := range reasons {
		if containsSub(r, sub) {
			return true
		}
	}
	return false
}

// Plane D D7 quote binding — the buyer-facing quote handle round-trips through
// quoteIDToUUID: the "q_<uuid>" form POST /v1/quote returns AND a bare uuid both
// resolve to the same quotes.id, and garbage is a clear caller error (never a
// silently-dropped binding). Pure; the not-expired/match checks + invoice
// quoted-vs-actual are covered by TestQuoteBindingMatchAndExpiry (integration).
func TestQuoteIDToUUIDRoundTrip(t *testing.T) {
	id := uuid.New()

	// The "q_<uuid>" handle (what the buyer is handed) parses back to the bare id.
	got, err := quoteIDToUUID("q_" + id.String())
	if err != nil {
		t.Fatalf("q_ handle should parse, got %v", err)
	}
	if got != id {
		t.Fatalf("q_ handle resolved to %v, want %v", got, id)
	}

	// A bare uuid is accepted too, so a buyer can pass back either shape.
	got2, err := quoteIDToUUID(id.String())
	if err != nil {
		t.Fatalf("bare uuid should parse, got %v", err)
	}
	if got2 != id {
		t.Fatalf("bare uuid resolved to %v, want %v", got2, id)
	}

	// Surrounding whitespace is tolerated (handles copy-pasted with a trailing newline).
	if got3, err := quoteIDToUUID("  q_" + id.String() + "  "); err != nil || got3 != id {
		t.Fatalf("padded handle: got (%v,%v), want (%v,nil)", got3, err, id)
	}

	// Garbage is a caller error, not a zero-uuid that would mis-bind.
	for _, bad := range []string{"", "q_", "q_not-a-uuid", "deadbeef", "q_q_" + id.String()} {
		if _, err := quoteIDToUUID(bad); err == nil {
			t.Fatalf("quoteIDToUUID(%q) should error", bad)
		}
	}
}

// Plane D D6 / errata C-Errata-6 — the quote-to-actual drift feedback's pure core:
// once a (job_type, model) has enough committed durations the ETA uses the OBSERVED
// p90 per-task seconds, and with no/thin history it falls back to the static target.
// The sample-count gate + the SQL p90 live in the store (HistoricalP90DurationMs,
// covered by integration TestTaskDurationRecorded); this pins the ms→secs conversion
// and the fallback the estimator depends on.
func TestPerTaskSecsFromP90FallbackAndConversion(t *testing.T) {
	// No trustworthy history (the store returns 0 below the sample floor / on error)
	// must fall back to the static target, never to zero.
	if got := perTaskSecsFromP90(0); got != targetTaskSecs {
		t.Fatalf("p90ms=0 must fall back to targetTaskSecs(%d), got %d", targetTaskSecs, got)
	}
	if got := perTaskSecsFromP90(-5); got != targetTaskSecs {
		t.Fatalf("negative p90ms must fall back to targetTaskSecs(%d), got %d", targetTaskSecs, got)
	}

	// A real observed p90 drives the per-task seconds, rounding UP so a sub-second p90
	// still costs a whole second (a job never takes < 1s/task).
	if got := perTaskSecsFromP90(1); got != 1 {
		t.Fatalf("p90ms=1 must round up to 1s, got %d", got)
	}
	if got := perTaskSecsFromP90(1000); got != 1 {
		t.Fatalf("p90ms=1000 must be 1s, got %d", got)
	}
	if got := perTaskSecsFromP90(1001); got != 2 {
		t.Fatalf("p90ms=1001 must round up to 2s, got %d", got)
	}
	// A p90 well above the static target overrides it (the observed reality wins once
	// enough history exists), proving the feedback is live, not cosmetic.
	if got := perTaskSecsFromP90(90000); got != 90 || got <= targetTaskSecs {
		t.Fatalf("observed p90=90000ms must override the %ds target, got %d", targetTaskSecs, got)
	}
}

func containsSub(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
