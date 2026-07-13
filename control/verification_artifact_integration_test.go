//go:build integration

package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"testing"

	"github.com/google/uuid"
)

func TestVerificationArtifactSealIsContentAddressedAndStagingOverwriteSafe(t *testing.T) {
	ctx := context.Background()
	taskID := uuid.New()
	stagingKey := fmt.Sprintf("proof/verification-staging/%s/result.bin", taskID)
	original := []byte("the bytes the control plane verified\n")
	if err := itStorage.PutObject(ctx, stagingKey, original, "application/octet-stream"); err != nil {
		t.Fatalf("put staging object: %v", err)
	}

	sealed, err := itStorage.SealVerificationArtifact(ctx, taskID, 0, stagingKey)
	if err != nil {
		t.Fatalf("seal verification artifact: %v", err)
	}
	wantSum := sha256.Sum256(original)
	if sealed.SHA256 != hex.EncodeToString(wantSum[:]) || sealed.Bytes != int64(len(original)) {
		t.Fatalf("sealed observation = sha %q bytes %d, want %x/%d",
			sealed.SHA256, sealed.Bytes, wantSum, len(original))
	}

	// A worker URL can remain live after commit. Overwriting that staging key must
	// not rewrite the content-addressed authority object the verdict will bind.
	if err := itStorage.PutObject(ctx, stagingKey, []byte("later overwrite\n"), "application/octet-stream"); err != nil {
		t.Fatalf("overwrite staging object: %v", err)
	}
	got, err := itStorage.GetObject(ctx, sealed.Key)
	if err != nil {
		t.Fatalf("get sealed object after staging overwrite: %v", err)
	}
	if !bytes.Equal(got, original) {
		t.Fatalf("sealed bytes changed after staging overwrite: got %q want %q", got, original)
	}

	// Replaying the seal while staging still contains the original bytes converges
	// to the same key. Restore it here to model object-store response loss rather
	// than a conflicting worker mutation (the durable pin rejects that separately).
	if err := itStorage.PutObject(ctx, stagingKey, original, "application/octet-stream"); err != nil {
		t.Fatalf("restore staging object: %v", err)
	}
	replayed, err := itStorage.SealVerificationArtifact(ctx, taskID, 0, stagingKey)
	if err != nil {
		t.Fatalf("replay seal: %v", err)
	}
	if replayed.Key != sealed.Key || replayed.SHA256 != sealed.SHA256 || replayed.Bytes != sealed.Bytes {
		t.Fatalf("seal replay diverged: first=%+v replay=%+v", sealed, replayed)
	}
}

func TestVerificationArtifactOversizeAndSealedMutationFailClosed(t *testing.T) {
	ctx := context.Background()
	taskID := uuid.New()
	stagingKey := fmt.Sprintf("proof/verification-staging/%s/oversized.bin", taskID)
	const maxBytes int64 = 32
	oversized := bytes.Repeat([]byte("x"), int(maxBytes+1))
	if err := itStorage.PutObject(ctx, stagingKey, oversized, "application/octet-stream"); err != nil {
		t.Fatalf("put oversized staging object: %v", err)
	}
	_, err := itStorage.sealVerificationArtifactWithLimit(ctx, taskID, 0, stagingKey, maxBytes, nil)
	if !errors.Is(err, ErrVerificationArtifactTooLarge) {
		t.Fatalf("oversized staging seal = %v, want %v", err, ErrVerificationArtifactTooLarge)
	}
	var sizeErr *VerificationArtifactTooLargeError
	if !errors.As(err, &sizeErr) || sizeErr.ObservedBytes != maxBytes+1 || sizeErr.MaxBytes != maxBytes {
		t.Fatalf("oversized staging evidence = %#v", sizeErr)
	}

	valid := []byte("small sealed authority")
	if err := itStorage.PutObject(ctx, stagingKey, valid, "application/octet-stream"); err != nil {
		t.Fatalf("put valid staging object: %v", err)
	}
	sealed, err := itStorage.sealVerificationArtifactWithLimit(ctx, taskID, 1, stagingKey, maxBytes, nil)
	if err != nil {
		t.Fatalf("seal valid bounded artifact: %v", err)
	}
	authority := VerificationArtifact{Key: sealed.Key, SHA256: sealed.SHA256, Bytes: sealed.Bytes}
	mutation := bytes.Repeat([]byte("z"), len(valid)) // same size: digest must catch it
	if err := itStorage.PutObject(ctx, sealed.Key, mutation, "application/octet-stream"); err != nil {
		t.Fatalf("mutate sealed object: %v", err)
	}
	if _, err := itStorage.readSealedVerificationArtifactWithLimit(ctx, authority, maxBytes); !errors.Is(err, ErrVerificationArtifactChanged) {
		t.Fatalf("same-size sealed mutation = %v, want %v", err, ErrVerificationArtifactChanged)
	}

	// Oversize evidence is a small server-owned object. It binds only the size
	// observation and does not copy a byte from the rejected worker body.
	evidence, err := itStorage.sealOversizedVerificationEvidence(ctx, taskID, 0, sizeErr)
	if err != nil {
		t.Fatalf("seal oversized rejection evidence: %v", err)
	}
	if !isOversizedVerificationEvidenceKey(evidence.Key) || evidence.Bytes >= 1<<10 {
		t.Fatalf("oversize evidence is not small/content-addressed: %+v", evidence)
	}
	if bytes.Contains(evidence.Body, oversized) {
		t.Fatal("oversize evidence copied rejected worker bytes")
	}
}
