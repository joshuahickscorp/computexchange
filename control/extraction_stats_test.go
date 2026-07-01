package main

import "testing"

// Item 23 (file dimension): docStats reports files matched/used/skipped + records + bytes
// for a fetch-and-chunk extraction, so the intake launch response is honest about what was
// used vs left out of a repo.
func TestDocStats(t *testing.T) {
	all := []RepoFile{
		{Path: "a.md"}, {Path: "b.txt"}, {Path: "c.html"}, {Path: "main.go"}, {Path: "logo.png"},
	}
	used := []namedContent{
		{Path: "a.md", Content: []byte("hello")},    // 5 bytes
		{Path: "b.txt", Content: []byte("worldly")}, // 7 bytes
	}
	s := docStats(all, documentSetExts, used, 4)
	if s.FilesMatched != 3 {
		t.Fatalf("files_matched = %d, want 3 (.md/.txt/.html)", s.FilesMatched)
	}
	if s.FilesUsed != 2 {
		t.Fatalf("files_used = %d, want 2", s.FilesUsed)
	}
	if s.FilesSkipped != 3 {
		t.Fatalf("files_skipped = %d, want 3 (listing minus used)", s.FilesSkipped)
	}
	if s.Records != 4 {
		t.Fatalf("records = %d, want 4", s.Records)
	}
	if s.Bytes != 12 {
		t.Fatalf("bytes = %d, want 12 (5+7)", s.Bytes)
	}
}
