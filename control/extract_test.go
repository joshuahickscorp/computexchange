package main

import (
	"strings"
	"testing"
)

// CSV → JSONL must pick the text-heavy column (not an id/score), one record per row.
func TestExtractTabularCSV(t *testing.T) {
	csv := "id,body,score\n1,\"hello world this is the body text\",5\n2,\"another longer body of content here\",3\n"
	out, n, err := extractTabular("data.csv", []byte(csv))
	if err != nil {
		t.Fatal(err)
	}
	if n != 2 {
		t.Fatalf("records = %d, want 2", n)
	}
	if !strings.Contains(string(out), "hello world this is the body text") {
		t.Fatalf("did not extract the body column: %s", out)
	}
	if strings.Contains(string(out), `"text":"5"`) || strings.Contains(string(out), `"text":"1"`) {
		t.Fatal("picked the wrong (numeric) column")
	}
}

// .jsonl passes through; blank lines dropped, invalid JSON rejected.
func TestExtractTabularJSONL(t *testing.T) {
	out, n, err := extractTabular("d.jsonl", []byte("{\"text\":\"a\"}\n\n{\"text\":\"b\"}\n"))
	if err != nil {
		t.Fatal(err)
	}
	if n != 2 {
		t.Fatalf("records = %d, want 2 (blank dropped)", n)
	}
	if !strings.Contains(string(out), `"a"`) || !strings.Contains(string(out), `"b"`) {
		t.Fatalf("passthrough lost content: %s", out)
	}
	if _, _, err := extractTabular("bad.jsonl", []byte("not json\n")); err == nil {
		t.Fatal("invalid jsonl must error, never silently pass")
	}
}

// Documents → one JSONL record per non-empty doc.
func TestExtractDocuments(t *testing.T) {
	out, n := extractDocuments([]namedContent{
		{Path: "a.md", Content: []byte("document one")},
		{Path: "b.txt", Content: []byte("   ")},
		{Path: "c.txt", Content: []byte("document three")},
	})
	if n != 2 {
		t.Fatalf("records = %d, want 2 (blank dropped)", n)
	}
	if !strings.Contains(string(out), "document one") || !strings.Contains(string(out), "document three") {
		t.Fatalf("missing docs: %s", out)
	}
}
