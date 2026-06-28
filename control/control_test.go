package main

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"math"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
)

// TestObserveMiddlewareRequestID proves the access-log middleware: it generates a
// correlation id when the request has none, propagates an incoming one verbatim,
// echoes it on the response, and passes the handler's status through unchanged.
func TestObserveMiddlewareRequestID(t *testing.T) {
	h := observe(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusTeapot)
	}))

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/jobs/x", nil))
	if rec.Header().Get("X-Request-ID") == "" {
		t.Fatal("observe must set X-Request-ID when the request has none")
	}
	if rec.Code != http.StatusTeapot {
		t.Fatalf("observe must pass the handler status through: got %d", rec.Code)
	}

	rec2 := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/jobs/x", nil)
	req.Header.Set("X-Request-ID", "trace-123")
	h.ServeHTTP(rec2, req)
	if got := rec2.Header().Get("X-Request-ID"); got != "trace-123" {
		t.Fatalf("observe must propagate an incoming X-Request-ID: got %q", got)
	}
}

// TestSecureHTMLHeaders proves the defensive headers set on same-origin HTML
// responses (anti-MIME-sniff, anti-clickjacking, no referrer leakage).
func TestSecureHTMLHeaders(t *testing.T) {
	rec := httptest.NewRecorder()
	secureHTMLHeaders(rec)
	for k, want := range map[string]string{
		"X-Content-Type-Options": "nosniff",
		"X-Frame-Options":        "SAMEORIGIN",
		"Referrer-Policy":        "no-referrer",
	} {
		if got := rec.Header().Get(k); got != want {
			t.Fatalf("%s = %q, want %q", k, got, want)
		}
	}
}

// TestStoreBreaker proves the object-store circuit breaker: it stays closed under
// healthy/mixed traffic, OPENS only after `threshold` consecutive fully-failed
// calls, fails fast until the cooldown elapses, then closes; and a single healthy
// call resets the failure count (so a missing object / success never trips it).
func TestStoreBreaker(t *testing.T) {
	t0 := time.Unix(1_700_000_000, 0)
	b := newStoreBreaker(3, 10*time.Second)

	if !b.allow(t0) {
		t.Fatal("a fresh breaker must allow")
	}
	// Two failures then a healthy call must NOT open it (reset on health).
	b.record(t0, false)
	b.record(t0, false)
	b.record(t0, true)
	if !b.allow(t0) {
		t.Fatal("a healthy call must reset the failure count before the threshold")
	}
	// Three consecutive failures trip it open for the cooldown.
	b.record(t0, false)
	b.record(t0, false)
	b.record(t0, false)
	if b.allow(t0) {
		t.Fatal("breaker must open after the threshold of consecutive failures")
	}
	if b.allow(t0.Add(9 * time.Second)) {
		t.Fatal("breaker must stay open during the cooldown")
	}
	if !b.allow(t0.Add(11 * time.Second)) {
		t.Fatal("breaker must close once the cooldown elapses")
	}
}

// control_test.go — pure-function + handler-seam unit tests. No DB, no object
// store: every test here exercises logic that is either pure or short-circuits
// before any I/O (the auth 401 paths reject before the store lookup). Tests that
// would need a live Postgres are out of scope here and are gated elsewhere.

// --- cosine + embedding comparison ---

func TestCosine(t *testing.T) {
	const eps = 1e-9
	cases := []struct {
		name string
		a, b []float64
		want float64
	}{
		{"identical", []float64{1, 2, 3}, []float64{1, 2, 3}, 1},
		{"orthogonal", []float64{1, 0}, []float64{0, 1}, 0},
		{"opposite", []float64{1, 0}, []float64{-1, 0}, -1},
		{"scaled-identical", []float64{1, 1}, []float64{5, 5}, 1},
		{"length-mismatch", []float64{1, 2, 3}, []float64{1, 2}, 0},
		{"empty", []float64{}, []float64{}, 0},
		{"zero-vector", []float64{0, 0}, []float64{1, 1}, 0},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := cosine(c.a, c.b)
			if math.Abs(got-c.want) > eps {
				t.Fatalf("cosine(%v,%v) = %v, want %v", c.a, c.b, got, c.want)
			}
		})
	}
}

func TestCosineNoPanic(t *testing.T) {
	// Mismatched / empty inputs must never panic.
	_ = cosine(nil, nil)
	_ = cosine([]float64{1}, nil)
	_ = cosine(nil, []float64{1})
}

// embed results must pass the 0.999 gate when near-identical and fail when not.
func TestResultsAgreeEmbed(t *testing.T) {
	identical := []byte(`{"vectors":[[1,0,0],[0,1,0]]}`)
	// A tiny perturbation: cosine ~0.99999, above the 0.999 gate → still agrees.
	near := []byte(`{"vectors":[[1,0.001,0],[0.001,1,0]]}`)
	// A large divergence on one vector drags the mean below 0.999 → disagrees.
	far := []byte(`{"vectors":[[1,0,0],[1,1,0]]}`)

	if !resultsAgree("embed", identical, identical) {
		t.Fatal("identical embeddings should agree")
	}
	if !resultsAgree("embed", identical, near) {
		t.Fatal("near-identical embeddings (>0.999) should agree")
	}
	if resultsAgree("embed", identical, far) {
		t.Fatal("divergent embeddings (<0.999) should NOT agree")
	}
}

func TestResultsAgreeEmbedShapeMismatchFails(t *testing.T) {
	a := []byte(`{"vectors":[[1,0,0]]}`)
	b := []byte(`{"vectors":[[1,0,0],[0,1,0]]}`) // different vector count
	if resultsAgree("embed", a, b) {
		t.Fatal("mismatched vector counts must not agree")
	}
	if resultsAgree("embed", []byte("not json"), a) {
		t.Fatal("unparseable embedding blob must not agree")
	}
}

// non-embed types fall back to exact byte equality.
func TestResultsAgreeExactForOtherTypes(t *testing.T) {
	a := []byte(`{"text":"42"}`)
	if !resultsAgree("batch_infer", a, a) {
		t.Fatal("identical deterministic bytes should agree")
	}
	if resultsAgree("batch_infer", a, []byte(`{"text":"43"}`)) {
		t.Fatal("differing deterministic bytes should NOT agree")
	}
	// The cosine gate must not leak into non-embed types: two distinct embedding
	// blobs that WOULD pass cosine must still be unequal as raw bytes here.
	x := []byte(`{"vectors":[[1,0,0]]}`)
	y := []byte(`{"vectors":[[1,0.0001,0]]}`)
	if resultsAgree("image_gen", x, y) {
		t.Fatal("non-embed types must compare bytes, not cosine")
	}
}

func TestMajorityVoteEmbeddingAware(t *testing.T) {
	v1 := []byte(`{"vectors":[[1,0,0]]}`)
	v2 := []byte(`{"vectors":[[1,0.0005,0]]}`) // cosine-equal to v1
	bad := []byte(`{"vectors":[[0,1,0]]}`)     // orthogonal to v1
	got := majorityVote("embed", [][]byte{v1, v2, bad})
	// v1 and v2 agree (2 of 3) → majority is one of them, not bad.
	if bytes.Equal(got, bad) {
		t.Fatal("majority vote picked the orthogonal outlier")
	}
}

// --- Turbo workload verifiers (batch_classification / json_extraction / rerank) ---

func TestResultsAgreeBatchClassification(t *testing.T) {
	a := []byte(`{"job_type":"batch_classification","model":"m","count":2,"labels":[{"index":0,"label":"spam"},{"index":1,"label":"ham"}]}`)
	// Same labels, different ITEM ORDER → still agrees (matched by index).
	reordered := []byte(`{"labels":[{"index":1,"label":"ham"},{"index":0,"label":"spam"}]}`)
	// One label flipped → disagrees.
	wrong := []byte(`{"labels":[{"index":0,"label":"spam"},{"index":1,"label":"spam"}]}`)
	if !resultsAgree("batch_classification", a, a) {
		t.Fatal("identical classification must agree")
	}
	if !resultsAgree("batch_classification", a, reordered) {
		t.Fatal("same labels in different order must agree (matched by index)")
	}
	if resultsAgree("batch_classification", a, wrong) {
		t.Fatal("a flipped label must NOT agree")
	}
	// Malformed / empty / count-mismatch must all be REJECTED loudly, never a pass.
	if resultsAgree("batch_classification", a, []byte(`not json`)) {
		t.Fatal("malformed classification blob must not agree")
	}
	if resultsAgree("batch_classification", a, []byte(`{"labels":[{"index":0,"label":"spam"}]}`)) {
		t.Fatal("differing item count must not agree")
	}
	if resultsAgree("batch_classification", []byte(`{"labels":[]}`), []byte(`{"labels":[]}`)) {
		t.Fatal("empty label set must not agree (nothing verified)")
	}
}

func TestResultsAgreeJSONExtraction(t *testing.T) {
	a := []byte(`{"job_type":"json_extraction","items":[{"index":0,"json":{"name":"ann","age":30}}]}`)
	// Same object, KEYS REORDERED → canonical equality still agrees.
	reordered := []byte(`{"items":[{"index":0,"json":{"age":30,"name":"ann"}}]}`)
	// Different value → disagrees.
	diff := []byte(`{"items":[{"index":0,"json":{"name":"bob","age":30}}]}`)
	if !resultsAgree("json_extraction", a, reordered) {
		t.Fatal("key-reordered identical objects must agree (canonical JSON)")
	}
	if resultsAgree("json_extraction", a, diff) {
		t.Fatal("a differing extracted value must NOT agree")
	}
	if resultsAgree("json_extraction", a, []byte(`garbage`)) {
		t.Fatal("malformed extraction blob must not agree")
	}
	if resultsAgree("json_extraction", []byte(`{"items":[]}`), []byte(`{"items":[]}`)) {
		t.Fatal("empty item set must not agree")
	}
}

func TestResultsAgreeRerank(t *testing.T) {
	a := []byte(`{"job_type":"rerank","rankings":[{"index":0,"order":[2,0,1]}]}`)
	same := []byte(`{"rankings":[{"index":0,"order":[2,0,1]}]}`)
	diff := []byte(`{"rankings":[{"index":0,"order":[0,1,2]}]}`)
	if !resultsAgree("rerank", a, same) {
		t.Fatal("identical rankings must agree")
	}
	if resultsAgree("rerank", a, diff) {
		t.Fatal("a differing order must NOT agree (rerank is exact)")
	}
	if resultsAgree("rerank", a, []byte(`{"rankings":[{"index":0,"order":[2,0]}]}`)) {
		t.Fatal("a different-length order must NOT agree")
	}
	if resultsAgree("rerank", a, []byte(`nope`)) {
		t.Fatal("malformed rerank blob must not agree")
	}
}

func TestCanonicalJSON(t *testing.T) {
	x, ok := canonicalJSON([]byte(`{"b":1,"a":2}`))
	if !ok {
		t.Fatal("valid object must canonicalize")
	}
	y, _ := canonicalJSON([]byte(`{"a":2,"b":1}`))
	if !bytes.Equal(x, y) {
		t.Fatalf("key order must not matter: %q vs %q", x, y)
	}
	if _, ok := canonicalJSON([]byte(`{bad`)); ok {
		t.Fatal("malformed JSON must report ok=false")
	}
	if _, ok := canonicalJSON(nil); ok {
		t.Fatal("empty input must report ok=false")
	}
}

// --- the hard filter (item A): a worker can NEVER be matched/claimed for work it
// cannot run. Match enforces hw_class / memory / tier here; supported_jobs /
// supported_models / min_payout / data_residency are enforced in ClaimTask's SQL
// and proven by the integration test TestClaimHardFilter. ---

func TestMatchHardFilterNeverReturnsIneligible(t *testing.T) {
	now := time.Now()
	// The one worker is wrong on EVERY dimension Match owns at once: wrong hw_class
	// for the constraint, and too little memory. It must never appear in the result.
	bad := mw(uuid.New(), "cpu", 2, 0.99, 100, 3, now)
	got, err := Match(MatchTask{
		JobType: "embed", MinMemoryGB: 16, Tier: "batch",
		HWClasses: []string{"apple_silicon_max"}, // bad is cpu → excluded
	}, []MatchWorker{bad})
	if err != ErrNoSupply {
		t.Fatalf("an ineligible-on-every-axis worker must yield ErrNoSupply, got %v / %v", got, err)
	}
	for _, w := range got {
		if w.ID == bad.ID {
			t.Fatal("Match returned a worker that fails the hard filter")
		}
	}
}

// --- JSONL chunk split ---

func TestSplitJSONL(t *testing.T) {
	in := []byte("a\nb\nc\nd\ne\n")
	chunks := splitJSONL(in, 2)
	if len(chunks) != 3 {
		t.Fatalf("expected 3 chunks (2+2+1), got %d", len(chunks))
	}
	if got := string(chunks[0]); got != "a\nb\n" {
		t.Fatalf("chunk0 = %q, want \"a\\nb\\n\"", got)
	}
	if got := string(chunks[2]); got != "e\n" {
		t.Fatalf("remainder chunk = %q, want \"e\\n\"", got)
	}
	// Total lines preserved across chunks (count newline-terminated lines).
	total := 0
	for _, c := range chunks {
		total += bytes.Count(c, []byte("\n"))
	}
	if total != 5 {
		t.Fatalf("line count not preserved: got %d, want 5", total)
	}
}

func TestSplitJSONLDropsBlankLines(t *testing.T) {
	in := []byte("a\n\n\nb\n  \nc\n")
	chunks := splitJSONL(in, 256)
	if len(chunks) != 1 {
		t.Fatalf("expected 1 chunk, got %d", len(chunks))
	}
	if got := string(chunks[0]); got != "a\nb\nc\n" {
		t.Fatalf("blank lines not dropped: %q", got)
	}
}

func TestSplitJSONLEmpty(t *testing.T) {
	if got := splitJSONL(nil, 256); got != nil {
		t.Fatalf("empty input should yield nil, got %v", got)
	}
	if got := splitJSONL([]byte("\n \n"), 256); got != nil {
		t.Fatalf("whitespace-only input should yield nil, got %v", got)
	}
}

func TestSplitJSONLCRLF(t *testing.T) {
	in := []byte("a\r\nb\r\n")
	chunks := splitJSONL(in, 256)
	if len(chunks) != 1 || string(chunks[0]) != "a\nb\n" {
		t.Fatalf("CRLF not normalized: %q", chunks)
	}
}

func TestSplitSizeOfDefault(t *testing.T) {
	if got := splitSizeOf(nil); got != defaultSplitSize {
		t.Fatalf("nil params: got %d, want %d", got, defaultSplitSize)
	}
	if got := splitSizeOf(json.RawMessage(`{}`)); got != defaultSplitSize {
		t.Fatalf("empty params: got %d, want %d", got, defaultSplitSize)
	}
	if got := splitSizeOf(json.RawMessage(`{"split_size":0}`)); got != defaultSplitSize {
		t.Fatalf("zero split_size: got %d, want %d", got, defaultSplitSize)
	}
	if got := splitSizeOf(json.RawMessage(`{"split_size":10}`)); got != 10 {
		t.Fatalf("explicit split_size: got %d, want 10", got)
	}
	if got := splitSizeOf(json.RawMessage(`not json`)); got != defaultSplitSize {
		t.Fatalf("bad params: got %d, want %d", got, defaultSplitSize)
	}
}

// adaptiveSplitSize: explicit override wins; otherwise embeddings pack more
// items/chunk than generation, and everything stays in the [1,4096] band.
func TestAdaptiveSplitSize(t *testing.T) {
	// Explicit override always wins, regardless of job type.
	if got := adaptiveSplitSize("embed", json.RawMessage(`{"split_size":7}`), 0); got != 7 {
		t.Fatalf("explicit override: got %d, want 7", got)
	}
	embed := adaptiveSplitSize("embed", nil, 0)
	infer := adaptiveSplitSize("batch_infer", nil, 0)
	if embed <= infer {
		t.Fatalf("embeddings should pack more items/chunk than generation: embed=%d infer=%d", embed, infer)
	}
	for _, jt := range []string{"embed", "batch_infer", "image_gen", "rerank", "unknown_type"} {
		n := adaptiveSplitSize(jt, nil, 0)
		if n < 1 || n > 4096 {
			t.Fatalf("%s split size %d out of [1,4096]", jt, n)
		}
	}
	// Length-aware: long prompts shrink generation tasks (prefill-bound); embed is not.
	if short, long := adaptiveSplitSize("batch_classification", nil, 120), adaptiveSplitSize("batch_classification", nil, 1200); long >= short {
		t.Fatalf("long-prompt classification should split smaller: short=%d long=%d", short, long)
	}
	if adaptiveSplitSize("embed", nil, 1200) != adaptiveSplitSize("embed", nil, 120) {
		t.Fatalf("embed split must not depend on input length")
	}
}

// --- result merge (item E): per-job-type flattening to buyer-ready JSONL ---

func TestMergeResultObject(t *testing.T) {
	var buf bytes.Buffer
	// embed: two vectors → two indexed lines, base offset respected.
	n, err := mergeResultObject(&buf, "embed", []byte(`{"vectors":[[1,2],[3,4]]}`), 10)
	if err != nil || n != 2 {
		t.Fatalf("embed merge: n=%d err=%v", n, err)
	}
	if !strings.Contains(buf.String(), `"index":10`) || !strings.Contains(buf.String(), `"index":11`) {
		t.Fatalf("embed merge did not stamp global indices: %s", buf.String())
	}
	// batch_classification: one label line.
	buf.Reset()
	n, err = mergeResultObject(&buf, "batch_classification", []byte(`{"labels":[{"index":0,"label":"spam"}]}`), 0)
	if err != nil || n != 1 || !strings.Contains(buf.String(), `"label":"spam"`) {
		t.Fatalf("classification merge: n=%d err=%v out=%s", n, err, buf.String())
	}
	// A malformed result for its type is REJECTED loudly (never silently merged).
	buf.Reset()
	if _, err := mergeResultObject(&buf, "embed", []byte(`{"vectors":[]}`), 0); err == nil {
		t.Fatal("empty embed result must be rejected, not merged")
	}
	if _, err := mergeResultObject(&buf, "batch_classification", []byte(`not json`), 0); err == nil {
		t.Fatal("malformed classification result must be rejected")
	}
	// Unknown / batch_infer type: completions flattened one per line.
	buf.Reset()
	n, err = mergeResultObject(&buf, "batch_infer", []byte(`{"completions":[{"text":"a"},{"text":"b"}]}`), 0)
	if err != nil || n != 2 {
		t.Fatalf("batch_infer merge: n=%d err=%v", n, err)
	}
}

// encodeEmbedBinTest builds a CXEM binary embedding artifact (the exact layout the
// agent emits) for the merge/verification tests. Kept tiny + local to the test.
func encodeEmbedBinTest(dim uint32, rows [][]float32) []byte {
	var b bytes.Buffer
	b.Write(embedBinMagic)
	var hdr [12]byte
	binary.LittleEndian.PutUint32(hdr[0:4], embedBinVersion)
	binary.LittleEndian.PutUint32(hdr[4:8], dim)
	binary.LittleEndian.PutUint32(hdr[8:12], uint32(len(rows)))
	b.Write(hdr[:])
	for _, row := range rows {
		for _, f := range row {
			var fb [4]byte
			binary.LittleEndian.PutUint32(fb[:], math.Float32bits(f))
			b.Write(fb[:])
		}
	}
	return b.Bytes()
}

// TestMergeEmbedBinary proves the binary embedding merge (PLANE_D D5/D15): two
// per-chunk CXEM artifacts of the same dim concatenate into ONE valid CXEM file
// whose count is the sum and whose rows are in chunk order — and that the merged
// binary is materially smaller than the equivalent merged JSONL. Malformed inputs
// (mixed shapes, dim mismatch, truncated body) are surfaced loudly, never merged.
func TestMergeEmbedBinary(t *testing.T) {
	dim := uint32(4)
	c0 := encodeEmbedBinTest(dim, [][]float32{{1, 2, 3, 4}, {5, 6, 7, 8}})
	c1 := encodeEmbedBinTest(dim, [][]float32{{9, 10, 11, 12}})
	results := []PrimaryResult{{ChunkIndex: 0, ResultRef: "c0"}, {ChunkIndex: 1, ResultRef: "c1"}}

	out, err := mergeEmbedBinary([][]byte{c0, c1}, results)
	if err != nil {
		t.Fatalf("mergeEmbedBinary: %v", err)
	}
	if !isEmbedBinary(out) {
		t.Fatal("merged artifact lost its CXEM magic")
	}
	if got := binary.LittleEndian.Uint32(out[8:12]); got != dim {
		t.Fatalf("merged dim = %d, want %d", got, dim)
	}
	if got := binary.LittleEndian.Uint32(out[12:16]); got != 3 {
		t.Fatalf("merged count = %d, want 3 (2+1)", got)
	}
	wantLen := embedBinHeaderLen + 3*int(dim)*4
	if len(out) != wantLen {
		t.Fatalf("merged length = %d, want %d", len(out), wantLen)
	}
	// Row order preserved across chunks: last row is chunk 1's [9..12].
	last := math.Float32frombits(binary.LittleEndian.Uint32(out[wantLen-16 : wantLen-12]))
	if last != 9 {
		t.Fatalf("first float of last row = %v, want 9 (chunk order preserved)", last)
	}

	// Decode-equivalence + size win vs the JSONL the JSON path would emit for the
	// same 3 vectors (one {"index","vector"} line each).
	rows, ok := parseEmbeddingVectors(out)
	if !ok || len(rows) != 3 || rows[2][0] != 9 {
		t.Fatalf("parseEmbeddingVectors(merged) = %v ok=%v", rows, ok)
	}
	var jsonl bytes.Buffer
	for i, r := range rows {
		line, _ := json.Marshal(map[string]any{"index": i, "vector": r})
		jsonl.Write(line)
		jsonl.WriteByte('\n')
	}
	if len(out) >= jsonl.Len() {
		t.Fatalf("binary merge (%d B) must beat JSONL (%d B) for the same rows", len(out), jsonl.Len())
	}

	// Mixed shapes: a JSON chunk where the first was binary is a real bug, surfaced.
	if _, err := mergeEmbedBinary([][]byte{c0, []byte(`{"vectors":[[1,2,3,4]]}`)}, results); err == nil {
		t.Fatal("mixed binary+JSON chunks must error, not silently merge")
	}
	// Dim mismatch across chunks must error.
	cWide := encodeEmbedBinTest(8, [][]float32{{1, 2, 3, 4, 5, 6, 7, 8}})
	if _, err := mergeEmbedBinary([][]byte{c0, cWide}, results); err == nil {
		t.Fatal("embeddings of different dim must not merge")
	}
	// Truncated body (header claims more floats than present) must error.
	bad := append([]byte(nil), c1...)
	bad = bad[:len(bad)-4]
	if _, err := mergeEmbedBinary([][]byte{bad}, results[:1]); err == nil {
		t.Fatal("truncated binary chunk must be rejected")
	}
}

// TestParseEmbeddingVectorsBothShapes proves verification is format-agnostic: a
// binary embed blob and the JSON blob for the SAME vectors decode to the same rows,
// so resultsAgree("embed", ...) passes when one peer is binary and the other JSON
// (a worker is never wrongly docked for choosing the binary artifact).
func TestParseEmbeddingVectorsBothShapes(t *testing.T) {
	bin := encodeEmbedBinTest(3, [][]float32{{1, 0, 0}, {0, 1, 0}})
	js := []byte(`{"vectors":[[1,0,0],[0,1,0]]}`)
	rb, okB := parseEmbeddingVectors(bin)
	rj, okJ := parseEmbeddingVectors(js)
	if !okB || !okJ || len(rb) != 2 || len(rj) != 2 {
		t.Fatalf("parse failed: bin ok=%v rj ok=%v", okB, okJ)
	}
	if rb[1][1] != 1 || rj[1][1] != 1 {
		t.Fatalf("decoded rows differ from input: %v / %v", rb, rj)
	}
	// Cross-format agreement: identical vectors, one binary one JSON → agree.
	if !resultsAgree("embed", bin, js) {
		t.Fatal("binary vs JSON embeddings of the same vectors must agree")
	}
	// A malformed binary blob is a disagreement, never a parse-pass.
	if _, ok := parseEmbeddingVectors([]byte("CXEMshort")); ok {
		t.Fatal("malformed binary blob must not parse ok")
	}
}

// validJobTypes is the closed set: the three Turbo workloads are accepted, junk
// is rejected (handleCreateJob rejects an unknown job_type.type as 400).
func TestValidJobTypes(t *testing.T) {
	for _, jt := range []string{"embed", "batch_infer", "batch_classification", "json_extraction", "rerank"} {
		if !validJobTypes[jt] {
			t.Fatalf("%q must be a valid job type", jt)
		}
	}
	if validJobTypes["nonsense"] {
		t.Fatal("unknown job type must be rejected")
	}
}

// --- splitCharge (flat take + hold) ---

func TestSplitCharge(t *testing.T) {
	buyer, supplier, task := uuid.New(), uuid.New(), uuid.New()
	now := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	const charge = 10.0
	const hold = 600

	entries := splitCharge(buyer, supplier, task, charge, hold, now)
	if len(entries) != 3 {
		t.Fatalf("expected 3 ledger entries, got %d", len(entries))
	}

	var byKind = map[string]LedgerEntry{}
	for _, e := range entries {
		byKind[e.Kind] = e
	}

	bc, ok := byKind[KindBuyerCharge]
	if !ok || bc.AmountUSD != -charge {
		t.Fatalf("buyer_charge = %+v, want amount %v", bc, -charge)
	}
	wantSupplier := charge * supplierShareRate
	sc, ok := byKind[KindSupplierCredit]
	if !ok || math.Abs(sc.AmountUSD-wantSupplier) > 1e-9 {
		t.Fatalf("supplier_credit amount = %v, want %v", sc.AmountUSD, wantSupplier)
	}
	if sc.PayoutStatus != PayoutHeld {
		t.Fatalf("supplier_credit payout_status = %q, want held", sc.PayoutStatus)
	}
	if sc.ReleaseAt == nil || !sc.ReleaseAt.Equal(now.Add(600*time.Second)) {
		t.Fatalf("supplier_credit release_at = %v, want now+600s", sc.ReleaseAt)
	}
	wantPlatform := charge - charge*supplierShareRate
	pt, ok := byKind[KindPlatformTake]
	if !ok || math.Abs(pt.AmountUSD-wantPlatform) > 1e-9 {
		t.Fatalf("platform_take amount = %v, want %v", pt.AmountUSD, wantPlatform)
	}

	// the flat split sums back to the gross charge (no FP drift).
	if math.Abs((sc.AmountUSD+pt.AmountUSD)-charge) > 1e-9 {
		t.Fatalf("supplier+platform = %v, want %v", sc.AmountUSD+pt.AmountUSD, charge)
	}
}

// --- Match filtering + scoring ---

func mw(id uuid.UUID, hw string, mem, rep, tps float32, tier int, seen time.Time) MatchWorker {
	return MatchWorker{
		ID: id, HWClass: hw, MemoryGB: mem, Reputation: rep,
		TPS: map[string]float32{"embed": tps}, LastSeen: seen, Tier: tier,
	}
}

func TestMatchFiltersAndScores(t *testing.T) {
	now := time.Now()
	hi := mw(uuid.New(), "apple_silicon_max", 64, 0.9, 100, 2, now)
	lo := mw(uuid.New(), "apple_silicon_max", 64, 0.9, 10, 2, now)
	stale := mw(uuid.New(), "apple_silicon_max", 64, 0.9, 200, 2, now.Add(-2*time.Minute))
	tooSmall := mw(uuid.New(), "apple_silicon_max", 4, 0.9, 999, 2, now)

	got, err := Match(MatchTask{JobType: "embed", MinMemoryGB: 8, Tier: "batch"},
		[]MatchWorker{lo, hi, stale, tooSmall})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(got) != 2 {
		t.Fatalf("expected 2 eligible (stale+small filtered), got %d", len(got))
	}
	if got[0].ID != hi.ID {
		t.Fatal("highest reputation×tps should rank first")
	}
}

// The Plane B horizon class is accepted by the closed hw-class set (lockstep with
// agent/src/types.rs HardwareClass + proto/manifest.schema.json). The summed-memory
// routing it enables is proven against the live claim filter in
// integration_test.go (TestClusterSummedMemoryRouting).
func TestClusterHWClassValid(t *testing.T) {
	if !validHWClasses["apple_silicon_cluster"] {
		t.Fatal("apple_silicon_cluster must be a valid hw_class (Plane B horizon)")
	}
	// The pre-existing classes are still valid (no drift) and a junk class is not.
	for _, c := range []string{"apple_silicon_base", "apple_silicon_pro", "apple_silicon_max", "apple_silicon_ultra", "cpu"} {
		if !validHWClasses[c] {
			t.Fatalf("hw_class %q unexpectedly invalid", c)
		}
	}
	if validHWClasses["nvidia_h100"] {
		t.Fatal("closed set must reject unknown classes")
	}
}

func TestMatchExcludesThrottledWorker(t *testing.T) {
	now := time.Now()
	// A healthy worker and an otherwise-stronger one that is throttling for memory
	// pressure. The throttled worker must NEVER be returned, even though its
	// reputation×tps score is higher.
	ok := mw(uuid.New(), "apple_silicon_max", 64, 0.9, 100, 2, now)
	hot := mw(uuid.New(), "apple_silicon_max", 64, 0.95, 200, 2, now)
	hot.Throttled = true
	got, err := Match(MatchTask{JobType: "embed", MinMemoryGB: 8, Tier: "batch"},
		[]MatchWorker{hot, ok})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(got) != 1 || got[0].ID != ok.ID {
		t.Fatalf("throttled worker must be excluded; got %d workers", len(got))
	}
	// When the ONLY candidate is throttled, there is no safe supply.
	solo := mw(uuid.New(), "apple_silicon_max", 64, 0.9, 100, 2, now)
	solo.Throttled = true
	if _, err := Match(MatchTask{JobType: "embed", MinMemoryGB: 8, Tier: "batch"},
		[]MatchWorker{solo}); err != ErrNoSupply {
		t.Fatalf("only-throttled candidate should be ErrNoSupply, got %v", err)
	}
}

func TestMatchSameClassOnly(t *testing.T) {
	now := time.Now()
	// Top scorer is apple_silicon_max; a different-class worker must be dropped
	// from the result so redundancy peers share one hardware class.
	apple := mw(uuid.New(), "apple_silicon_max", 64, 0.9, 100, 2, now)
	other := mw(uuid.New(), "apple_silicon_base", 64, 0.95, 50, 2, now)
	got, err := Match(MatchTask{JobType: "embed", MinMemoryGB: 8, Tier: "batch"},
		[]MatchWorker{apple, other})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	for _, w := range got {
		if w.HWClass != "apple_silicon_max" {
			t.Fatalf("result mixed hardware classes: %s", w.HWClass)
		}
	}
}

// Warm-routing (docs/PLANE_D.md §9 D3): among otherwise-EQUAL same-class workers,
// the one that already has the job's model warm must rank first (it skips a cold
// model load). The bonus is a re-rank NUDGE only: it never excludes the cold worker
// (both stay eligible) and never overrides a meaningfully stronger cold worker.
func TestMatchPrefersWarmWorker(t *testing.T) {
	now := time.Now()
	// Two identical same-class workers; only `warm` has the model loaded.
	cold := mw(uuid.New(), "apple_silicon_max", 64, 0.9, 100, 2, now)
	warm := mw(uuid.New(), "apple_silicon_max", 64, 0.9, 100, 2, now)
	warm.Warm = true

	got, err := Match(MatchTask{JobType: "embed", MinMemoryGB: 8, Tier: "batch"},
		[]MatchWorker{cold, warm})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(got) != 2 {
		t.Fatalf("warm preference must not drop the cold worker; got %d", len(got))
	}
	if got[0].ID != warm.ID {
		t.Fatal("a warm worker should rank ahead of an equal cold one")
	}

	// The nudge must NOT override a clearly faster cold worker: a cold worker with
	// much higher throughput still wins over a warm-but-slow one (warm only re-ranks
	// near-ties; it is a small 5% bonus, not a trump card).
	fastCold := mw(uuid.New(), "apple_silicon_max", 64, 0.9, 100, 2, now)
	slowWarm := mw(uuid.New(), "apple_silicon_max", 64, 0.9, 50, 2, now)
	slowWarm.Warm = true
	got2, err := Match(MatchTask{JobType: "embed", MinMemoryGB: 8, Tier: "batch"},
		[]MatchWorker{slowWarm, fastCold})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got2[0].ID != fastCold.ID {
		t.Fatal("a 5% warm bonus must not override a 2x-faster cold worker")
	}
}

func TestMatchTrustedTierGate(t *testing.T) {
	now := time.Now()
	lowTier := mw(uuid.New(), "apple_silicon_max", 64, 0.9, 100, 1, now)
	_, err := Match(MatchTask{JobType: "embed", MinMemoryGB: 8, Tier: "trusted"},
		[]MatchWorker{lowTier})
	if err != ErrNoSupply {
		t.Fatalf("trusted job with only tier<2 worker should be ErrNoSupply, got %v", err)
	}
}

func TestMatchNoSupply(t *testing.T) {
	_, err := Match(MatchTask{JobType: "embed", MinMemoryGB: 8, Tier: "batch"}, nil)
	if err != ErrNoSupply {
		t.Fatalf("empty candidates should be ErrNoSupply, got %v", err)
	}
}

func TestMatchHWClassConstraint(t *testing.T) {
	now := time.Now()
	apple := mw(uuid.New(), "apple_silicon_max", 64, 0.9, 100, 2, now)
	_, err := Match(MatchTask{
		JobType: "embed", MinMemoryGB: 8, Tier: "batch",
		HWClasses: []string{"apple_silicon_ultra"}, // apple_silicon_max worker excluded
	}, []MatchWorker{apple})
	if err != ErrNoSupply {
		t.Fatalf("hw_class constraint should exclude apple, got %v", err)
	}
}

// --- reputation deltas + tiers ---

func TestUpdateReputationDeltas(t *testing.T) {
	const eps = 1e-6
	cases := []struct {
		event ReputationEvent
		start float32
		want  float32
	}{
		{EventTaskSuccess, 0.5, 0.501},
		{EventHoneypotPass, 0.5, 0.502},
		{EventRedundancyMatch, 0.5, 0.501},
		{EventMismatch, 0.5, 0.4},
		{EventHoneypotFail, 0.5, 0.35},
		{EventTimeout, 0.5, 0.48},
		{EventThermalThrottle, 0.5, 0.495},
		{EventResultCorrupt, 0.5, 0.3},
		{EventSpoofingDetected, 0.5, 0.0}, // -1.0 clamps to 0
		{EventTaskSuccess, 1.0, 1.0},      // clamp at the ceiling
		{EventHoneypotFail, 0.10, 0.0},    // clamp at the floor
	}
	for _, c := range cases {
		got := updateReputation(c.start, c.event)
		if math.Abs(float64(got-c.want)) > eps {
			t.Fatalf("updateReputation(%v, %s) = %v, want %v", c.start, c.event, got, c.want)
		}
	}
}

func TestReputationTier(t *testing.T) {
	cases := []struct {
		rep  float32
		jobs uint64
		want uint8
	}{
		{0.95, 5000, 3},
		{0.90, 5000, 3},
		{0.90, 4999, 2}, // jobs gate not met for tier 3, but tier 2 thresholds met
		{0.85, 500, 2},
		{0.80, 500, 2},
		{0.80, 499, 1}, // tier 2 jobs gate not met, tier 1 met
		{0.70, 100, 1},
		{0.60, 100, 1},
		{0.60, 99, 0}, // tier 1 jobs gate not met
		{0.50, 100000, 0},
		{0.0, 0, 0},
	}
	for _, c := range cases {
		if got := reputationTier(c.rep, c.jobs); got != c.want {
			t.Fatalf("reputationTier(%v, %d) = %d, want %d", c.rep, c.jobs, got, c.want)
		}
	}
}

// --- fracCount ---

func TestFracCount(t *testing.T) {
	cases := []struct {
		n    int
		frac float32
		want int
	}{
		{10, 0.0, 0},
		{10, 0.1, 1},
		{10, 0.15, 2}, // round(1.5) = 2
		{10, 0.5, 5},
		{10, 1.0, 10},
		{10, 1.5, 10}, // clamped to n
		{0, 0.5, 0},
		{10, -0.1, 0}, // negative → 0
		{3, 0.5, 2},   // round(1.5)=2
	}
	for _, c := range cases {
		if got := fracCount(c.n, c.frac); got != c.want {
			t.Fatalf("fracCount(%d, %v) = %d, want %d", c.n, c.frac, got, c.want)
		}
	}
}

// --- Budget Governor projection math (Plane C §12 / Plane D §14 D8) ---
//
// These exercise the PURE budget functions the claim's SKIP-LOCKED gate mirrors
// (the same dual-implementation pattern as Match ↔ the SQL claim). Money math is
// sacred: the cap must PREVENT a dispatch whose projected charge would exceed it,
// and never trigger on an unset cap.
func TestPerTaskEstimateUSD(t *testing.T) {
	cases := []struct {
		est   float64
		count int
		want  float64
	}{
		{1.0, 4, 0.25},
		{0.5, 1, 0.5},
		{1.0, 0, 0}, // no tasks → no per-task cost (no divide-by-zero)
		{0, 8, 0},
	}
	for _, c := range cases {
		if got := perTaskEstimateUSD(c.est, c.count); got != c.want {
			t.Fatalf("perTaskEstimateUSD(%v,%d) = %v, want %v", c.est, c.count, got, c.want)
		}
	}
}

func TestBudgetWouldBreach(t *testing.T) {
	cases := []struct {
		name                  string
		charged, perTask, max float64
		want                  bool
	}{
		{"unset cap never breaches", 100, 100, 0, false},
		{"negative cap treated as unset", 5, 5, -1, false},
		{"room for one more", 0.40, 0.10, 1.00, false},         // 0.50 <= 1.00
		{"exactly at cap is allowed", 0.90, 0.10, 1.00, false}, // 1.00 <= 1.00, not >
		{"one more would exceed", 0.95, 0.10, 1.00, true},      // 1.05 > 1.00
		{"already over (charged alone)", 1.20, 0.00, 1.00, true},
		{"tiny cap, first task exceeds", 0.00, 0.50, 0.01, true},
	}
	for _, c := range cases {
		if got := budgetWouldBreach(c.charged, c.perTask, c.max); got != c.want {
			t.Fatalf("%s: budgetWouldBreach(%v,%v,%v) = %v, want %v",
				c.name, c.charged, c.perTask, c.max, got, c.want)
		}
	}
}

func TestBudgetNearLimit(t *testing.T) {
	// Values chosen exactly representable in binary float (eighths/quarters) so the
	// boundary is tested without FP drift — the helper mirrors the SQL's own float
	// comparison, and the warning it gates is advisory (emits an event), so a true
	// boundary value is included via a clean power-of-two fraction.
	cases := []struct {
		name                  string
		charged, perTask, max float64
		want                  bool
	}{
		{"unset cap never near", 100, 100, 0, false},
		{"well below threshold", 0.25, 0.25, 2.00, false},   // 0.50 < 1.60
		{"at threshold (clean fp)", 1.50, 0.25, 2.00, true}, // 1.75 >= 1.60
		{"above threshold", 1.75, 0.25, 2.00, true},         // 2.00 >= 1.60
	}
	for _, c := range cases {
		if got := budgetNearLimit(c.charged, c.perTask, c.max); got != c.want {
			t.Fatalf("%s: budgetNearLimit(%v,%v,%v) = %v, want %v",
				c.name, c.charged, c.perTask, c.max, got, c.want)
		}
	}
}

// --- auth middleware 401 paths (no DB; these reject before the store lookup) ---

func TestAuthBuyerRejectsMissingHeader(t *testing.T) {
	// nil store is safe: these paths return before any store call.
	s := &Server{}
	h := s.authBuyer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("handler must not run without a valid bearer token")
	}))
	for _, hdr := range []string{"", "Token abc", "Bearer", "Basic xyz"} {
		req := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
		if hdr != "" {
			req.Header.Set("Authorization", hdr)
		}
		rr := httptest.NewRecorder()
		h.ServeHTTP(rr, req)
		if rr.Code != http.StatusUnauthorized {
			t.Fatalf("Authorization=%q: got %d, want 401", hdr, rr.Code)
		}
		if !strings.Contains(rr.Body.String(), "error") {
			t.Fatalf("Authorization=%q: body missing error field: %s", hdr, rr.Body.String())
		}
	}
}

func TestAuthWorkerRejectsMissingToken(t *testing.T) {
	s := &Server{}
	h := s.authWorker(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("handler must not run without a worker token")
	}))
	req := httptest.NewRequest(http.MethodGet, "/v1/worker/poll", nil)
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, req)
	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("missing X-Worker-Token: got %d, want 401", rr.Code)
	}
}

func TestBearerParsing(t *testing.T) {
	cases := []struct {
		header string
		want   string
		ok     bool
	}{
		{"Bearer abc123", "abc123", true},
		{"bearer abc123", "abc123", true}, // case-insensitive scheme
		{"Bearer   spaced  ", "spaced", true},
		{"Token abc", "", false},
		{"Bearer", "", false},
		{"", "", false},
	}
	for _, c := range cases {
		req := httptest.NewRequest(http.MethodGet, "/", nil)
		if c.header != "" {
			req.Header.Set("Authorization", c.header)
		}
		got, ok := bearer(req)
		if ok != c.ok || got != c.want {
			t.Fatalf("bearer(%q) = (%q,%v), want (%q,%v)", c.header, got, ok, c.want, c.ok)
		}
	}
}

// --- long-poll wait_ms parsing (Plane D §7 D1) ---

// parseWaitMs governs the worker poll long-poll budget: absent/malformed/non-positive
// is the original single-shot poll (0), a positive value is honored, and anything
// over longPollCap is clamped so a hostile wait_ms can never pin a request goroutine.
func TestParseWaitMs(t *testing.T) {
	cases := []struct {
		query string
		want  time.Duration
	}{
		{"", 0},             // no param → single-shot (unchanged)
		{"wait_ms=0", 0},    // non-positive → no wait
		{"wait_ms=-500", 0}, // negative → no wait
		{"wait_ms=abc", 0},  // malformed → no wait (never errors the poll)
		{"wait_ms=250", 250 * time.Millisecond},
		{"wait_ms=25000", 25 * time.Second}, // exactly the cap
		{"wait_ms=99999999", longPollCap},   // over the cap → clamped, never unbounded
		{"other=1", 0},                      // unrelated param → no wait
	}
	for _, c := range cases {
		req := httptest.NewRequest(http.MethodGet, "/v1/worker/poll?"+c.query, nil)
		if got := parseWaitMs(req); got != c.want {
			t.Fatalf("parseWaitMs(%q) = %v, want %v", c.query, got, c.want)
		}
	}
	// The cap must stay safely below the agent's 35s transport ceiling (protocol.rs
	// POLL_TIMEOUT) so the server never holds a poll past when the client gives up.
	if longPollCap >= 35*time.Second {
		t.Fatalf("longPollCap %v must be < the agent's 35s transport ceiling", longPollCap)
	}
}

// --- modelPrice + estimate helpers (pure) ---

func TestModelPrice(t *testing.T) {
	if got := modelPrice(ModelRow{PricePer1K: 0.002}); got != 0.002 {
		t.Fatalf("per-1k price: got %v, want 0.002", got)
	}
	// price_per_unit is scaled to a per-1k figure when there is no per-1k price.
	if got := modelPrice(ModelRow{PricePerUnit: 0.005}); math.Abs(got-5.0) > 1e-9 {
		t.Fatalf("per-unit scaled price: got %v, want 5.0", got)
	}
}

func TestTierMultiplier(t *testing.T) {
	if tierMultiplier("batch") != 1.0 || tierMultiplier("priority") != 1.5 || tierMultiplier("trusted") != 2.0 {
		t.Fatal("tier multipliers drifted from batch=1.0, priority=1.5, trusted=2.0")
	}
	if tierMultiplier("nonsense") != 1.0 {
		t.Fatal("unknown tier should default to 1.0")
	}
}

// TestPollDispatchManifestShape is a regression guard for a contract bug found in
// the first live end-to-end run: the Rust agent decodes TaskDispatch.manifest, so
// model.kind must be a valid backend ("gguf") and inputs must marshal as an empty
// array, never null — a nil slice or empty kind makes the agent fail to decode the
// poll response and the task never executes.
func TestPollDispatchManifestShape(t *testing.T) {
	disp := TaskDispatch{
		Manifest: JobManifest{
			JobType: JobType{Type: "embed"},
			Model:   ModelRef{Kind: "gguf", Ref: "all-minilm-l6-v2"},
			Inputs:  []InputRef{},
			Tier:    "batch",
		},
		ResultKey: "jobs/x/tasks/0/result.json",
	}
	b, err := json.Marshal(disp)
	if err != nil {
		t.Fatal(err)
	}
	s := string(b)
	if !strings.Contains(s, `"kind":"gguf"`) {
		t.Errorf("model.kind must be present and gguf, got: %s", s)
	}
	if strings.Contains(s, `"inputs":null`) {
		t.Errorf("inputs must marshal as [] not null, got: %s", s)
	}
	if !strings.Contains(s, `"inputs":[]`) {
		t.Errorf("inputs must marshal as empty array, got: %s", s)
	}
}
