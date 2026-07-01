package main

import (
	"strings"
	"testing"
)

// Item 20: a recognized-but-not-launchable workload (audio from a repo) is gated, so
// the launch handler refuses early instead of fetching the source and failing
// mid-extract on the unwired binary-audio path.
func TestPatternLaunchable(t *testing.T) {
	if patternLaunchable("audio-transcribe") {
		t.Fatal("audio-transcribe must NOT be launchable from a repo (binary-audio path unwired)")
	}
	for _, p := range []string{"tabular-text", "document-set", "code-repo"} {
		if !patternLaunchable(p) {
			t.Fatalf("%s must be launchable", p)
		}
	}
	if patternLaunchable("unknown") {
		t.Fatal("unknown must not be launchable")
	}
}

// detectPipeline carries the launchable flag: audio is RECOGNIZED (Supported) but NOT
// launchable from a repo; tabular is both.
func TestDetectPipelineLaunchableFlag(t *testing.T) {
	audio := detectPipeline([]RepoFile{{Path: "a.wav"}, {Path: "b.mp3"}})
	if !audio.Supported || audio.Launchable {
		t.Fatalf("audio must be Supported but not Launchable; got %+v", audio)
	}
	tab := detectPipeline([]RepoFile{{Path: "t.csv"}})
	if !tab.Supported || !tab.Launchable {
		t.Fatalf("tabular must be Supported and Launchable; got %+v", tab)
	}
}

// Item 18: a truncated listing marks the detection low-confidence with an honest note,
// so a partial repo never yields a confidently-wrong plan.
func TestWithTruncationWarning(t *testing.T) {
	got := withTruncationWarning(DetectedPipeline{Pattern: "document-set", Supported: true, Launchable: true})
	if !got.Truncated {
		t.Fatal("truncation must set Truncated=true")
	}
	if !strings.Contains(strings.ToLower(got.Reason), "truncated") {
		t.Fatalf("truncation must add an honest reason mentioning truncation; got %q", got.Reason)
	}
}
