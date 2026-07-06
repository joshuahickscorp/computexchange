package main

import (
	"testing"
	"time"
	"unicode/utf8"

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

// TestScanJSONLRecommendsLongestStringField proves the Project Detection &
// Quotation 8->9 content-based detection (docs/internal/CREED_AND_PATH_TO_TEN.md):
// the sampled records' ACTUAL field data drives a longest-average-string
// recommendation for which column to embed/classify/extract, surfacing the
// previously-computed-but-dropped field information as a confirmable suggestion.
func TestScanJSONLRecommendsLongestStringField(t *testing.T) {
	// A realistic messy input: a short id, a short category label, and a long
	// free-text body. The body is unambiguously the text a buyer wants processed.
	input := []byte(`{"id":"1","category":"news","body":"A long-form article about distributed compute markets and idle GPUs."}
{"id":"2","category":"blog","body":"Another substantial paragraph of real prose that dwarfs the id and category fields in length."}
{"id":"3","category":"news","body":"Yet more multi-sentence content here, clearly the primary text column of this dataset."}
`)
	s := scanJSONL(input)

	// The recommendation is the long free-text column, not the id or the label.
	if s.RecommendedField != "body" {
		t.Fatalf("recommended_field=%q, want \"body\" (the longest-average-string column)", s.RecommendedField)
	}
	// The evidence is surfaced and ordered by avg string length DESC — body first.
	if len(s.FieldStats) != 3 {
		t.Fatalf("field_stats len=%d, want 3 (id, category, body)", len(s.FieldStats))
	}
	if s.FieldStats[0].Field != "body" {
		t.Fatalf("field_stats[0]=%q, want \"body\" (highest avg length first): %+v", s.FieldStats[0].Field, s.FieldStats)
	}
	// The recommendation is strictly the top of the evidence list.
	if s.FieldStats[0].Field != s.RecommendedField {
		t.Fatalf("recommendation (%q) must be FieldStats[0] (%q)", s.RecommendedField, s.FieldStats[0].Field)
	}
	// body's avg must strictly exceed both other candidates' — the heuristic is
	// genuinely content-driven, not coincidental.
	byField := map[string]FieldStat{}
	for _, fs := range s.FieldStats {
		byField[fs.Field] = fs
	}
	if !(byField["body"].AvgStringLen > byField["category"].AvgStringLen &&
		byField["category"].AvgStringLen > byField["id"].AvgStringLen) {
		t.Fatalf("avg string lengths must order body > category > id, got %+v", s.FieldStats)
	}
	// Every candidate carried in all 3 sampled records.
	for _, fs := range s.FieldStats {
		if fs.Occurrences != 3 {
			t.Fatalf("field %q occurrences=%d, want 3", fs.Field, fs.Occurrences)
		}
	}
}

// TestScanJSONLNoStringFieldNoRecommendation proves the honest negative: when no
// field carries string content (all-numeric records), there is NO recommendation
// — the heuristic never invents a text column that does not exist. The field
// evidence is still surfaced (avg 0) so the buyer sees the candidates.
func TestScanJSONLNoStringFieldNoRecommendation(t *testing.T) {
	input := []byte(`{"a":1,"b":2.5,"c":true}
{"a":9,"b":0.1,"c":false}
`)
	s := scanJSONL(input)
	if s.RecommendedField != "" {
		t.Fatalf("no string field must yield NO recommendation, got %q", s.RecommendedField)
	}
	if len(s.FieldStats) != 3 {
		t.Fatalf("field_stats must still list all candidates (avg 0), got %+v", s.FieldStats)
	}
	for _, fs := range s.FieldStats {
		if fs.AvgStringLen != 0 {
			t.Fatalf("a non-string field must have avg_string_len 0, got %+v", fs)
		}
	}
}

// TestEstimateTokensFixesMultiByteUndercounting proves the Project Detection &
// Quotation 6->6.5 fix (docs/internal/CREED_AND_PATH_TO_TEN.md): the OLD
// bytes/4 heuristic badly undercounted multi-byte UTF-8 (CJK/Cyrillic/etc.)
// text, since a 3-byte-per-rune script divided by 4 gives ~0.75 "tokens" per
// character when a real tokenizer is much closer to 1 token per character.
func TestEstimateTokensFixesMultiByteUndercounting(t *testing.T) {
	// 20 Japanese characters, 60 UTF-8 bytes (3 bytes/rune). The old bytes/4
	// heuristic would estimate ceil(60/4) = 15 tokens for 20 real characters —
	// FEWER estimated tokens than actual characters, which is implausible for
	// any real tokenizer. The fixed rune-based, script-aware estimate must be
	// close to the rune count, not a fraction of it.
	cjk := []byte("こんにちはこんにちはこんにちはこんにちは")
	runeCount := 20
	if got := utf8.RuneCount(cjk); got != runeCount {
		t.Fatalf("test fixture: want %d runes, got %d", runeCount, got)
	}
	old := int64((len(cjk) + 3) / 4) // the literal old bytes/4 (ceil) computation
	got := estimateTokens(cjk)
	if got <= old {
		t.Fatalf("fixed estimate (%d) must exceed the old bytes/4 estimate (%d) for CJK text — the whole point of the fix", got, old)
	}
	if got < int64(runeCount)/2 {
		t.Fatalf("fixed estimate (%d) is still implausibly low for %d real characters", got, runeCount)
	}
}

// TestEstimateTokensUnchangedForASCII proves the fix is additive, not a
// behavior change for the common English/ASCII case the old heuristic already
// handled reasonably — every existing ASCII-input test must keep passing
// unchanged (confirmed by the full suite), and this pins the exact expected
// arithmetic for a hand-picked ASCII case too.
func TestEstimateTokensUnchangedForASCII(t *testing.T) {
	ascii := []byte("the quick brown fox jumps over the lazy dog") // 44 bytes, all ASCII
	want := int64((44 + 3) / 4)                                    // ceil(44/4) = 11
	if got := estimateTokens(ascii); got != want {
		t.Fatalf("ASCII estimate changed: want %d (unchanged bytes/4 behavior), got %d", want, got)
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

// TestSustainedBatchETASecs proves the Thermal 6->7 sustained-throughput ETA
// adjustment (docs/internal/CREED_AND_PATH_TO_TEN.md): a LONG batch job whose ETA
// came from the PEAK-derived static target is derated to the measured sustained
// pace (36.6% slower → ~1.577× longer), while short jobs, non-batch tiers, and
// ETAs already driven by real observed history are left EXACTLY at peak. Pure — no
// DB.
func TestSustainedBatchETASecs(t *testing.T) {
	// A long batch job on the peak-derived static target IS derated.
	long := 600 // 10 min peak estimate — well past the throttle onset
	got := sustainedBatchETASecs(long, "batch", false /*usedObservedHistory*/)
	want := 947 // ceil(600 * 1/(1-0.366)) = ceil(946.37)
	if got != want {
		t.Fatalf("a long peak-derived batch ETA must derate to sustained: want %d, got %d", want, got)
	}
	if got <= long {
		t.Fatalf("the sustained ETA must be strictly longer than the peak ETA (%d), got %d", long, got)
	}

	// A SHORT batch job (under the threshold) finishes inside the peak regime — no
	// derating.
	shortPeak := sustainedETAThresholdSecs - 1
	if got := sustainedBatchETASecs(shortPeak, "batch", false); got != shortPeak {
		t.Fatalf("a short batch ETA (%ds < %ds threshold) must stay at peak, got %d", shortPeak, sustainedETAThresholdSecs, got)
	}

	// Exactly at the threshold: derating engages (>= threshold).
	if got := sustainedBatchETASecs(sustainedETAThresholdSecs, "batch", false); got <= sustainedETAThresholdSecs {
		t.Fatalf("at the threshold the sustained derating must engage, got %d (<= %d)", got, sustainedETAThresholdSecs)
	}

	// A non-batch tier (priority/trusted are latency tiers, not the minutes-long
	// sustained regime) is never derated, even when long.
	if got := sustainedBatchETASecs(long, "priority", false); got != long {
		t.Fatalf("a non-batch tier must not be derated, got %d (want %d)", got, long)
	}
	if got := sustainedBatchETASecs(long, "trusted", false); got != long {
		t.Fatalf("the trusted tier must not be derated, got %d (want %d)", got, long)
	}

	// An ETA already driven by REAL observed history must NOT be re-derated — the
	// observed durations already embody the machine's actual sustained pace, so
	// derating again would double-count.
	if got := sustainedBatchETASecs(long, "batch", true /*usedObservedHistory*/); got != long {
		t.Fatalf("an observed-history ETA must not be re-derated, got %d (want %d)", got, long)
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

// --- wall-clock speed-SLA quote (Speed Lane wave 2A) — pure unit tests ----------
// The DB-backed path (quote → firm submit → enforcement → refund) is proven by
// the real-infra integration test (sla_integration_test.go); these pin the pure
// math and the honesty gates without a DB.

// TestSLAGuaranteedSecsFormula pins the guarantee formula term by term:
// ceil(conservative × safety margin) + merge allowance — and its zero handling.
func TestSLAGuaranteedSecsFormula(t *testing.T) {
	// conservative 100s → ceil(100×1.25)=125 + 60 = 185.
	if got := slaGuaranteedSecs(100); got != 185 {
		t.Fatalf("slaGuaranteedSecs(100)=%d, want 185 (ceil(100*1.25)+60)", got)
	}
	// Ceil engages on fractional products: 1 → ceil(1.25)=2 + 60 = 62.
	if got := slaGuaranteedSecs(1); got != 62 {
		t.Fatalf("slaGuaranteedSecs(1)=%d, want 62 (ceil(1*1.25)+60)", got)
	}
	// No band → no guarantee, never a fabricated one.
	if got := slaGuaranteedSecs(0); got != 0 {
		t.Fatalf("slaGuaranteedSecs(0)=%d, want 0", got)
	}
	if got := slaGuaranteedSecs(-5); got != 0 {
		t.Fatalf("slaGuaranteedSecs(-5)=%d, want 0", got)
	}
	// Monotone: a bigger band never yields a smaller guarantee.
	if slaGuaranteedSecs(200) <= slaGuaranteedSecs(100) {
		t.Fatalf("guarantee must grow with the band: g(200)=%d g(100)=%d",
			slaGuaranteedSecs(200), slaGuaranteedSecs(100))
	}
	// The guarantee is always strictly ABOVE the conservative band itself (margin
	// + allowance are real additive slack, not a re-label of the band).
	for _, cons := range []int{1, 10, 47, 100, 3600} {
		if g := slaGuaranteedSecs(cons); g <= cons {
			t.Fatalf("guarantee %d must exceed its own conservative band %d", g, cons)
		}
	}
}

// TestSLAGuaranteeIsBuiltOnConservativeNotP50 pins the wave's core honesty rule:
// the guarantee basis is the planner's CONSERVATIVE band, so for any plan where
// conservative > p50 (always, by planner construction) the guarantee built on
// the band strictly exceeds what the p50 would have produced.
func TestSLAGuaranteeIsBuiltOnConservativeNotP50(t *testing.T) {
	p50, conservative := 60, 80 // planner invariant: conservative >= p50
	if g, gp := slaGuaranteedSecs(conservative), slaGuaranteedSecs(p50); g <= gp {
		t.Fatalf("guarantee on the conservative band (%d) must exceed a p50-based one (%d)", g, gp)
	}
}

// TestDeriveQuoteSLAHonestDegradation proves every precondition gate returns
// nil — no guarantee — and that the offer, when made, carries the documented
// premium math and every formula term.
func TestDeriveQuoteSLAHonestDegradation(t *testing.T) {
	const expected = 10.0
	// Supply below the SLA threshold → no guarantee.
	if sla := deriveQuoteSLA(false, true, 100, expected); sla != nil {
		t.Fatalf("sla offered without eligible supply: %+v", sla)
	}
	// ETA not planner-backed (planner disabled / thin rate cache) → no guarantee.
	if sla := deriveQuoteSLA(true, false, 100, expected); sla != nil {
		t.Fatalf("sla offered without planner-backed ETA: %+v", sla)
	}
	// Degenerate band → no guarantee.
	if sla := deriveQuoteSLA(true, true, 0, expected); sla != nil {
		t.Fatalf("sla offered with no conservative band: %+v", sla)
	}
	// Premium that rounds to $0 (zero-cost quote) → no guarantee (a $0 remedy is
	// not a commitment).
	if sla := deriveQuoteSLA(true, true, 100, 0); sla != nil {
		t.Fatalf("sla offered with an un-priceable premium: %+v", sla)
	}
	// All gates pass → the offer carries the documented terms.
	sla := deriveQuoteSLA(true, true, 100, expected)
	if sla == nil {
		t.Fatal("sla expected when every precondition holds")
	}
	if sla.GuaranteedSecs != slaGuaranteedSecs(100) {
		t.Fatalf("guaranteed_secs=%d, want %d", sla.GuaranteedSecs, slaGuaranteedSecs(100))
	}
	if want := roundUSD(expected * slaPremiumRate); sla.PremiumUSD != want {
		t.Fatalf("premium=%v, want %v (%.0f%% of expected)", sla.PremiumUSD, want, slaPremiumRate*100)
	}
	if sla.ConservativeModelSecs != 100 || sla.SafetyMarginFactor != slaSafetyMarginFactor ||
		sla.MergeAllowanceSecs != slaMergeAllowanceSecs || sla.Remedy == "" {
		t.Fatalf("offer must surface every formula term + the remedy text: %+v", sla)
	}
}

// TestSLAPremiumMath pins the surcharge rate on realistic figures.
func TestSLAPremiumMath(t *testing.T) {
	cases := []struct{ expected, want float64 }{
		{10.00, 1.50},
		{0.10, 0.015},
		{0.003072, roundUSD(0.003072 * 0.15)}, // a tiny real batch_infer quote still prices a non-zero premium at 6dp
	}
	for _, c := range cases {
		sla := deriveQuoteSLA(true, true, 50, c.expected)
		if sla == nil {
			t.Fatalf("expected an offer for expected=%v", c.expected)
		}
		if sla.PremiumUSD != c.want {
			t.Fatalf("premium for expected=%v: got %v want %v", c.expected, sla.PremiumUSD, c.want)
		}
	}
}

// TestSLARefundAmount pins the remedy figure: the full premium, capped at the
// chargeable amount — the refund nets a bill down, it never mints money.
func TestSLARefundAmount(t *testing.T) {
	if got := slaRefundAmount(1.50, 10.0); got != 1.50 {
		t.Fatalf("refund=%v, want the full premium 1.50", got)
	}
	if got := slaRefundAmount(1.50, 0.40); got != 0.40 {
		t.Fatalf("refund=%v, want capped at the chargeable 0.40", got)
	}
	if got := slaRefundAmount(0, 10.0); got != 0 {
		t.Fatalf("no premium → no refund, got %v", got)
	}
	if got := slaRefundAmount(1.50, 0); got != 0 {
		t.Fatalf("nothing chargeable → no refund, got %v", got)
	}
}

// TestSLASpanMissed pins the miss boundary: the promise is "within
// guaranteed_secs", so landing exactly ON the guarantee is a MET.
func TestSLASpanMissed(t *testing.T) {
	base := time.Date(2026, 7, 6, 12, 0, 0, 0, time.UTC)
	if slaSpanMissed(base, base.Add(90*time.Second), 90) {
		t.Fatal("landing exactly on the guarantee must be MET (within = inclusive)")
	}
	if !slaSpanMissed(base, base.Add(90*time.Second+time.Millisecond), 90) {
		t.Fatal("one millisecond past the guarantee must be a MISS")
	}
	if slaSpanMissed(base, base.Add(10*time.Second), 90) {
		t.Fatal("well inside the guarantee must be MET")
	}
}

// TestSLABindingValidation pins the submit-side binding rules at the pure
// level: a firm submission binds the guarantee ONLY when the quote actually
// carried a priced offer, and the committed price ceiling grows by exactly the
// premium (the cap covered the work; the surcharge is priced on top, never
// squeezed out of it). Mirrors createJob's binding block (api.go), which the
// integration test drives end-to-end over HTTP.
func TestSLABindingValidation(t *testing.T) {
	bind := func(q boundQuote) (guarantee int, premium, cap float64) {
		cap = q.CostMaxUSD
		if q.SLAGuaranteedSecs > 0 && q.SLAPremiumUSD > 0 {
			guarantee, premium = q.SLAGuaranteedSecs, q.SLAPremiumUSD
			cap = q.CostMaxUSD + q.SLAPremiumUSD
		}
		return
	}
	// SLA-bearing quote: guarantee + premium bind, ceiling grows by the premium.
	g, p, cap := bind(boundQuote{CostMaxUSD: 20, SLAGuaranteedSecs: 185, SLAPremiumUSD: 1.5})
	if g != 185 || p != 1.5 || cap != 21.5 {
		t.Fatalf("sla binding: got g=%d p=%v cap=%v, want 185/1.5/21.5", g, p, cap)
	}
	// No offer on the quote → price-only binding, byte-identical to pre-wave.
	g, p, cap = bind(boundQuote{CostMaxUSD: 20})
	if g != 0 || p != 0 || cap != 20 {
		t.Fatalf("price-only binding: got g=%d p=%v cap=%v, want 0/0/20", g, p, cap)
	}
	// A guarantee without a priced premium (defensive: half-written row) must
	// NOT bind — no remedy means no commitment.
	g, p, cap = bind(boundQuote{CostMaxUSD: 20, SLAGuaranteedSecs: 185})
	if g != 0 || p != 0 || cap != 20 {
		t.Fatalf("guarantee without premium must not bind: got g=%d p=%v cap=%v", g, p, cap)
	}
}
