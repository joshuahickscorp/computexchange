package main

import (
	"bytes"
	"errors"
	"strings"
	"testing"
)

// Item 16: readCapped returns the exact bytes for a within-cap source and the typed
// errIntakeTooLarge for an oversize one (it reads one byte past the cap to DETECT an
// overflow rather than truncating silently).
func TestReadCappedTypedError(t *testing.T) {
	got, err := readCapped(strings.NewReader("hello"), 16, "a.txt")
	if err != nil || string(got) != "hello" {
		t.Fatalf("within-cap read should return bytes; got %q err %v", got, err)
	}
	if _, err := readCapped(bytes.NewReader(make([]byte, 16)), 16, "a.bin"); err != nil {
		t.Fatalf("exactly-at-cap must be allowed; got %v", err)
	}
	_, err = readCapped(bytes.NewReader(make([]byte, 17)), 16, "big.bin")
	if !errors.Is(err, errIntakeTooLarge) {
		t.Fatalf("over-cap read must return the typed errIntakeTooLarge; got %v", err)
	}
}

// Item 17: fetchCappedDocs enforces per-file and aggregate caps with the typed error,
// and only fetches files whose extension is in the shared set (item 19 alignment, so
// a matched file is always actually fetched).
func TestFetchCappedDocsCaps(t *testing.T) {
	files := []RepoFile{{Path: "a.md"}, {Path: "b.txt"}, {Path: "c.pdf"}, {Path: "d.html"}}
	fetcher := func(size int) func(string) ([]byte, error) {
		return func(string) ([]byte, error) { return make([]byte, size), nil }
	}
	// Within both caps: only .md/.txt/.html fetched (.pdf skipped) -> 3 docs.
	docs, err := fetchCappedDocs(files, documentSetExts, fetcher(10), 100, 1000)
	if err != nil || len(docs) != 3 {
		t.Fatalf("within caps should return 3 docs (pdf skipped); got %d err %v", len(docs), err)
	}
	// Per-file cap exceeded.
	if _, err := fetchCappedDocs(files, documentSetExts, fetcher(50), 10, 100000); !errors.Is(err, errIntakeTooLarge) {
		t.Fatalf("over per-file cap must be typed errIntakeTooLarge; got %v", err)
	}
	// Aggregate cap exceeded (each file under per-file, but the running sum is not).
	if _, err := fetchCappedDocs(files, documentSetExts, fetcher(40), 100, 100); !errors.Is(err, errIntakeTooLarge) {
		t.Fatalf("over aggregate cap must be typed errIntakeTooLarge; got %v", err)
	}
}

// Item 19: detection and extraction agree on documentSetExts. A PDF-only listing is
// honestly UNSUPPORTED (never "supported then 0 records"); a >=3 markdown/text listing
// is the document-set pattern.
func TestDetectPipelinePdfHonesty(t *testing.T) {
	pdfOnly := []RepoFile{{Path: "a.pdf"}, {Path: "b.pdf"}, {Path: "c.pdf"}}
	if d := detectPipeline(pdfOnly); d.Supported {
		t.Fatalf("a PDF-only repo must be UNSUPPORTED (no extractor); got %+v", d)
	}
	mdRepo := []RepoFile{{Path: "x.md"}, {Path: "y.md"}, {Path: "z.txt"}}
	if d := detectPipeline(mdRepo); !d.Supported || d.Pattern != "document-set" {
		t.Fatalf("a >=3 markdown/text repo must be document-set; got %+v", d)
	}
}
