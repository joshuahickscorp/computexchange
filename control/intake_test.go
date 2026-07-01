package main

import "testing"

// detectPipeline is the Concierge's brain: a pure function over a file listing.
// These cases pin the supported patterns, the ordering (first match wins), and the
// honest refusal on an unknown shape.
func TestDetectPipeline(t *testing.T) {
	cases := []struct {
		name      string
		files     []RepoFile
		pattern   string
		supported bool
		stages    int
	}{
		{"audio", []RepoFile{{Path: "calls/a.wav"}, {Path: "calls/b.mp3"}}, "audio-transcribe", true, 1},
		{"tabular", []RepoFile{{Path: "data/tickets.csv"}, {Path: "README.md"}}, "tabular-text", true, 2},
		// Item 19: detect + extract agree on documentSetExts (.md/.txt/.html). PDF is
		// NOT extractable yet, so it never counts toward the document-set threshold and
		// a PDF-only repo is honestly unsupported (no more "supported then 0 records").
		{"documents", []RepoFile{{Path: "a.md"}, {Path: "b.txt"}, {Path: "c.html"}}, "document-set", true, 1},
		{"pdf-only unsupported", []RepoFile{{Path: "a.pdf"}, {Path: "b.pdf"}, {Path: "c.pdf"}}, "unknown", false, 0},
		// Item 21: a source-code corpus (>=2 source files) maps to a chunked embed index.
		{"code repo", []RepoFile{{Path: "main.go"}, {Path: "lib.rs"}, {Path: "app.ts"}}, "code-repo", true, 1},
		// Item 24 regression fixtures, documenting current pattern PRIORITY (audio >
		// tabular > document-set > code-repo, first match wins). A stray .csv in a code
		// repo currently matches tabular (a known limitation, recorded honestly); a mixed
		// docs+code repo matches document-set before code-repo.
		{"csv stray in code repo (tabular wins by order)", []RepoFile{{Path: "notes.csv"}, {Path: "main.go"}, {Path: "lib.go"}}, "tabular-text", true, 2},
		{"mixed docs+code (document-set wins by order)", []RepoFile{{Path: "a.md"}, {Path: "b.md"}, {Path: "c.md"}, {Path: "x.go"}, {Path: "y.go"}}, "document-set", true, 1},
		// A single source file (below the threshold) is still unknown, not a code corpus.
		{"unknown", []RepoFile{{Path: "main.go"}, {Path: "Dockerfile"}}, "unknown", false, 0},
		{"audio beats text", []RepoFile{{Path: "a.wav"}, {Path: "x.csv"}}, "audio-transcribe", true, 1},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			d := detectPipeline(c.files)
			if d.Pattern != c.pattern {
				t.Fatalf("pattern = %q, want %q", d.Pattern, c.pattern)
			}
			if d.Supported != c.supported {
				t.Fatalf("supported = %v, want %v", d.Supported, c.supported)
			}
			if len(d.Stages) != c.stages {
				t.Fatalf("stages = %d, want %d", len(d.Stages), c.stages)
			}
			if !c.supported && d.Reason == "" {
				t.Fatal("an unsupported detection must carry an honest reason, never an empty plan")
			}
		})
	}
}
