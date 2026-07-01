package main

import (
	"strings"
	"testing"
)

// Item 21: extractCode chunks source files deterministically into {"text":...} embed
// records — fixed line windows, stable order, same bytes -> same chunks (which the
// redundancy verifier requires so two workers embed identical inputs).
func TestExtractCodeChunks(t *testing.T) {
	files := []namedContent{{Path: "a.go", Content: []byte("l1\nl2\nl3\nl4\nl5")}}
	out, n := extractCode(files, 2)
	if n != 3 {
		t.Fatalf("5 lines / window 2 should yield 3 chunks; got %d", n)
	}
	if !strings.Contains(string(out), `"text"`) {
		t.Fatalf("each record must be a {\"text\":...} embed line; got %q", out)
	}
	// Deterministic: same input -> identical bytes.
	out2, _ := extractCode(files, 2)
	if string(out) != string(out2) {
		t.Fatal("extractCode must be deterministic (same bytes for the same input)")
	}
	// All-blank file -> no records (no empty embeds).
	if _, n := extractCode([]namedContent{{Path: "e.go", Content: []byte("\n\n  \n")}}, 2); n != 0 {
		t.Fatalf("an all-blank file should yield 0 records; got %d", n)
	}
}
