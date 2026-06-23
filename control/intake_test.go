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
		{"documents", []RepoFile{{Path: "a.md"}, {Path: "b.txt"}, {Path: "c.pdf"}}, "document-set", true, 1},
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
