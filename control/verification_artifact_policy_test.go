package main

import (
	"testing"
)

func TestVerificationArtifactPolicyUsesImmutableJobShapeAndClamps(t *testing.T) {
	embedOne := verificationArtifactMaxBytes("embed", 1, 0)
	embedMany := verificationArtifactMaxBytes("embed", 4096, 0)
	if embedOne < verificationArtifactPolicyFloor || embedMany <= embedOne || embedMany >= verificationArtifactAbsoluteMaxBytes {
		t.Fatalf("embed policy one/many = %d/%d", embedOne, embedMany)
	}

	short := verificationArtifactMaxBytes("batch_infer", 4, 64)
	long := verificationArtifactMaxBytes("batch_infer", 4, 4096)
	if long <= short {
		t.Fatalf("max_tokens did not widen bounded generation policy: short=%d long=%d", short, long)
	}

	if got := verificationArtifactMaxBytes("custom", 1<<30, 0); got != verificationArtifactAbsoluteMaxBytes {
		t.Fatalf("opaque policy = %d, want absolute cap %d", got, verificationArtifactAbsoluteMaxBytes)
	}
	if got := verificationArtifactMaxBytes("batch_infer", 1<<30, ^uint32(0)); got != verificationArtifactAbsoluteMaxBytes {
		t.Fatalf("overflowing job shape escaped clamp: got %d", got)
	}

	exactFinal := verificationArtifactMaxBytesForRecords("embed", 1, 4096, 0)
	legacyCeiling := verificationArtifactMaxBytes("embed", 4096, 0)
	if exactFinal != embedOne || exactFinal >= legacyCeiling {
		t.Fatalf("exact final-chunk cap=%d, one-row=%d legacy split cap=%d", exactFinal, embedOne, legacyCeiling)
	}
}
