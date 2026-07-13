package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/minio/minio-go/v7"
)

// verificationArtifactAbsoluteMaxBytes is the final control-plane memory and
// transfer ceiling for one worker result. It deliberately matches the existing
// finite cap on ordinary untrusted HTTP request bodies. Job-specific policies
// below are usually much smaller, but no malformed or future job descriptor can
// widen this backstop.
const verificationArtifactAbsoluteMaxBytes int64 = maxRequestBodyBytes

var (
	// ErrVerificationArtifactTooLarge is the typed sentinel for an object that
	// exceeds the durable per-attempt result policy. Callers use errors.Is so a
	// deterministic rejection is distinguishable from a retryable store outage.
	ErrVerificationArtifactTooLarge = errors.New("verification artifact exceeds size limit")
	// ErrVerificationArtifactChanged means HEAD metadata, the subsequent bounded
	// GET, or a pinned digest disagreed. A mutable/stale object is never authority.
	ErrVerificationArtifactChanged = errors.New("verification artifact changed while reading")
)

// VerificationArtifactTooLargeError records the evidence needed to reject an
// attempt deterministically without copying the untrusted object. ObservedBytes
// is a lower bound when the HEAD became stale during the GET.
type VerificationArtifactTooLargeError struct {
	Key           string
	ObservedBytes int64
	MaxBytes      int64
}

func (e *VerificationArtifactTooLargeError) Error() string {
	return fmt.Sprintf("verification artifact %q is at least %d bytes; limit is %d: %v",
		e.Key, e.ObservedBytes, e.MaxBytes, ErrVerificationArtifactTooLarge)
}

func (e *VerificationArtifactTooLargeError) Unwrap() error {
	return ErrVerificationArtifactTooLarge
}

// SealedVerificationArtifact is the control-plane observation of one uploaded
// task attempt. Upload URLs point at mutable staging keys; verdicts and merges
// must instead bind the bytes the control plane actually read. Keying the sealed
// copy by a server-computed SHA-256 makes a retry after an object-store response
// loss idempotent and prevents a later staging-key overwrite from changing the
// artifact behind an already-recorded verdict.
type SealedVerificationArtifact struct {
	Key    string
	SHA256 string
	Bytes  int64
	Body   []byte
}

func sealedVerificationObjectKey(taskID uuid.UUID, attempt int16, digest string) string {
	return fmt.Sprintf("verification/%s/attempt-%d/%s.result", taskID, attempt, digest)
}

func oversizedVerificationEvidenceKey(taskID uuid.UUID, attempt int16, digest string) string {
	return fmt.Sprintf("verification/%s/attempt-%d/oversize/%s.json", taskID, attempt, digest)
}

func unavailableVerificationEvidenceKey(taskID uuid.UUID, attempt int16, digest string) string {
	return fmt.Sprintf("verification/%s/attempt-%d/unavailable/%s.json", taskID, attempt, digest)
}

func isOversizedVerificationEvidenceKey(key string) bool {
	return strings.Contains(key, "/oversize/") && strings.HasSuffix(key, ".json")
}

func isUnavailableVerificationEvidenceKey(key string) bool {
	return strings.Contains(key, "/unavailable/") && strings.HasSuffix(key, ".json")
}

// readExactObjectSizeBounded reads exactly the size returned by HEAD, plus at
// most one byte. That extra byte is the race detector: if a mutable staging key
// grew after HEAD, the read fails closed without following the new size. The
// allocation and bytes requested from r are therefore both <= maxBytes+1.
func readExactObjectSizeBounded(r io.Reader, key string, declaredBytes, maxBytes int64) ([]byte, error) {
	if maxBytes < 0 || declaredBytes < 0 {
		return nil, fmt.Errorf("verification artifact %q has invalid size policy %d/%d", key, declaredBytes, maxBytes)
	}
	if declaredBytes > maxBytes {
		return nil, &VerificationArtifactTooLargeError{Key: key, ObservedBytes: declaredBytes, MaxBytes: maxBytes}
	}
	// All production policies are far below max-int, but retain an explicit
	// conversion guard so a corrupt test/caller cannot wrap the allocation.
	if declaredBytes == int64(^uint(0)>>1) {
		return nil, &VerificationArtifactTooLargeError{Key: key, ObservedBytes: declaredBytes, MaxBytes: maxBytes}
	}
	readLimit := declaredBytes + 1
	buf := make([]byte, int(readLimit))
	n, err := io.ReadFull(io.LimitReader(r, readLimit), buf)
	switch {
	case err == nil:
		// io.ReadFull obtained the one byte beyond HEAD. If HEAD already named
		// the cap this is provably oversized; otherwise it is a mutation/stale
		// metadata conflict and must be re-observed from scratch.
		if int64(n) > maxBytes {
			return nil, &VerificationArtifactTooLargeError{Key: key, ObservedBytes: int64(n), MaxBytes: maxBytes}
		}
		return nil, fmt.Errorf("%w: object %q grew beyond HEAD size %d", ErrVerificationArtifactChanged, key, declaredBytes)
	case errors.Is(err, io.EOF), errors.Is(err, io.ErrUnexpectedEOF):
		if int64(n) != declaredBytes {
			return nil, fmt.Errorf("%w: object %q HEAD reported %d bytes but GET returned %d",
				ErrVerificationArtifactChanged, key, declaredBytes, n)
		}
		return buf[:n], nil
	default:
		return nil, err
	}
}

func (s *Storage) statVerificationObject(ctx context.Context, key string) (int64, error) {
	var size int64
	err := s.withRetry(ctx, func() (bool, error) {
		info, err := s.internal.StatObject(ctx, s.bucket, key, minio.StatObjectOptions{})
		if err == nil {
			size = info.Size
			return false, nil
		}
		if minio.ToErrorResponse(err).Code == "NoSuchKey" {
			return false, fmt.Errorf("stat verification object %q: %w", key, err)
		}
		return true, fmt.Errorf("stat verification object %q: %w", key, err)
	})
	return size, err
}

// readVerificationObjectBounded performs HEAD before GET, rejects an oversized
// HEAD without opening the body, then uses the exact-size + one-byte read above.
// expectedBytes, when non-nil, is pinned authority and is checked before GET.
func (s *Storage) readVerificationObjectBounded(ctx context.Context, key string, maxBytes int64, expectedBytes *int64) ([]byte, error) {
	if strings.TrimSpace(key) == "" {
		return nil, errors.New("verification artifact key is required")
	}
	if maxBytes <= 0 || maxBytes > verificationArtifactAbsoluteMaxBytes {
		return nil, fmt.Errorf("verification artifact %q has invalid limit %d", key, maxBytes)
	}
	started := time.Now()
	declared, err := s.statVerificationObject(ctx, key)
	if err != nil {
		return nil, err
	}
	if declared > maxBytes {
		return nil, &VerificationArtifactTooLargeError{Key: key, ObservedBytes: declared, MaxBytes: maxBytes}
	}
	if expectedBytes != nil && declared != *expectedBytes {
		return nil, fmt.Errorf("%w: object %q is %d bytes, pinned authority says %d",
			ErrVerificationArtifactChanged, key, declared, *expectedBytes)
	}
	if cached, ok := cachedVerificationBody(ctx, key); ok {
		if int64(len(cached)) != declared {
			return nil, fmt.Errorf("%w: cached object %q is %d bytes, HEAD now says %d",
				ErrVerificationArtifactChanged, key, len(cached), declared)
		}
		return cached, nil
	}
	// readExactObjectSizeBounded allocates declared+1 capacity so growth after
	// HEAD is observed without following an untrusted new size.  Reserve that
	// exact capacity before allocation; a busy global budget makes verification
	// pending instead of allowing concurrent large artifacts to exhaust RAM.
	if err := reserveVerificationMemory(ctx, declared+1); err != nil {
		return nil, fmt.Errorf("read verification object %q (%d bytes): %w", key, declared, err)
	}
	r, err := s.GetObjectReader(ctx, key)
	if err != nil {
		return nil, err
	}
	defer r.Close()
	body, err := readExactObjectSizeBounded(r, key, declared, maxBytes)
	if err != nil {
		return nil, err
	}
	observeTransfer("get", len(body), time.Since(started))
	cacheVerificationBody(ctx, key, body)
	return body, nil
}

const verificationDigestReadBufferBytes int64 = 32 << 10

// verifyVerificationObjectDigest reads a just-written control-owned object back
// without allocating a second artifact-sized slice.  It verifies exact length
// (including a one-byte growth probe) and the server-computed digest using a
// fixed 32 KiB buffer.  The original staging body remains the only large buffer
// and can be reused by the processor immediately after the durable pin.
func (s *Storage) verifyVerificationObjectDigest(ctx context.Context, key string, expectedBytes, maxBytes int64, expectedSHA string) error {
	if expectedBytes < 0 || expectedBytes > maxBytes {
		return &VerificationArtifactTooLargeError{Key: key, ObservedBytes: expectedBytes, MaxBytes: maxBytes}
	}
	declared, err := s.statVerificationObject(ctx, key)
	if err != nil {
		return err
	}
	if declared != expectedBytes {
		return fmt.Errorf("%w: object %q is %d bytes after PUT, expected %d",
			ErrVerificationArtifactChanged, key, declared, expectedBytes)
	}
	if err := reserveVerificationMemory(ctx, verificationDigestReadBufferBytes); err != nil {
		return fmt.Errorf("verify sealed object %q: %w", key, err)
	}
	r, err := s.GetObjectReader(ctx, key)
	if err != nil {
		return err
	}
	defer r.Close()
	started := time.Now()
	h := sha256.New()
	buf := make([]byte, verificationDigestReadBufferBytes)
	n, err := io.CopyBuffer(h, io.LimitReader(r, expectedBytes+1), buf)
	if err != nil {
		return err
	}
	if n != expectedBytes {
		return fmt.Errorf("%w: object %q returned %d bytes after PUT, expected %d",
			ErrVerificationArtifactChanged, key, n, expectedBytes)
	}
	if hex.EncodeToString(h.Sum(nil)) != expectedSHA {
		return fmt.Errorf("%w: object %q digest disagrees after PUT", ErrVerificationArtifactChanged, key)
	}
	observeTransfer("get", int(n), time.Since(started))
	return nil
}

// SealVerificationArtifact reads the worker-writable staging object, computes
// the authority digest itself, writes the exact bytes to a deterministic
// control-owned key, and reads that key back before returning. The public helper
// retains the original API and uses the absolute ceiling; production verification
// passes the narrower immutable per-attempt policy through the with-limit helper.
func (s *Storage) SealVerificationArtifact(ctx context.Context, taskID uuid.UUID, attempt int16, stagingKey string) (SealedVerificationArtifact, error) {
	return s.sealVerificationArtifact(ctx, taskID, attempt, stagingKey, nil)
}

func (s *Storage) sealVerificationArtifact(ctx context.Context, taskID uuid.UUID, attempt int16, stagingKey string, probe recoveryBoundaryProbe) (SealedVerificationArtifact, error) {
	return s.sealVerificationArtifactWithLimit(ctx, taskID, attempt, stagingKey, verificationArtifactAbsoluteMaxBytes, probe)
}

func (s *Storage) sealVerificationArtifactWithLimit(ctx context.Context, taskID uuid.UUID, attempt int16, stagingKey string, maxBytes int64, probe recoveryBoundaryProbe) (SealedVerificationArtifact, error) {
	if taskID == uuid.Nil {
		return SealedVerificationArtifact{}, fmt.Errorf("sealing verification artifact: task id is required")
	}
	if attempt < 0 {
		return SealedVerificationArtifact{}, fmt.Errorf("sealing verification artifact: invalid attempt %d", attempt)
	}
	if stagingKey == "" {
		return SealedVerificationArtifact{}, fmt.Errorf("sealing verification artifact: staging key is required")
	}

	body, err := s.readVerificationObjectBounded(ctx, stagingKey, maxBytes, nil)
	if err != nil {
		return SealedVerificationArtifact{}, fmt.Errorf("reading verification staging object: %w", err)
	}
	reachRecoveryBoundary(ctx, probe, BoundaryVerifyAfterStagingRead)
	sum := sha256.Sum256(body)
	digest := hex.EncodeToString(sum[:])
	key := sealedVerificationObjectKey(taskID, attempt, digest)
	if err := s.PutObject(ctx, key, body, "application/octet-stream"); err != nil {
		return SealedVerificationArtifact{}, fmt.Errorf("writing sealed verification object: %w", err)
	}
	reachRecoveryBoundary(ctx, probe, BoundaryVerifyAfterSealedPut)
	expected := int64(len(body))
	if err := s.verifyVerificationObjectDigest(ctx, key, expected, maxBytes, digest); err != nil {
		return SealedVerificationArtifact{}, fmt.Errorf("reading sealed verification object: %w", err)
	}
	reachRecoveryBoundary(ctx, probe, BoundaryVerifyAfterSealedReadback)

	return SealedVerificationArtifact{
		Key: key, SHA256: digest, Bytes: int64(len(body)), Body: body,
	}, nil
}

// sealOversizedVerificationEvidence writes a small, deterministic server-owned
// artifact describing the rejected observation. It never copies the worker body.
// Pinning this evidence lets the ordinary immutable plan/apply machinery record a
// terminal fail + retry instead of releasing the same oversized work forever.
func (s *Storage) sealOversizedVerificationEvidence(ctx context.Context, taskID uuid.UUID, attempt int16, sizeErr *VerificationArtifactTooLargeError) (SealedVerificationArtifact, error) {
	return s.sealOversizedVerificationEvidenceWithProbe(ctx, taskID, attempt, sizeErr, nil)
}

func (s *Storage) sealOversizedVerificationEvidenceWithProbe(ctx context.Context, taskID uuid.UUID, attempt int16, sizeErr *VerificationArtifactTooLargeError, probe recoveryBoundaryProbe) (SealedVerificationArtifact, error) {
	if taskID == uuid.Nil || attempt < 0 || sizeErr == nil || sizeErr.MaxBytes <= 0 || sizeErr.ObservedBytes <= sizeErr.MaxBytes {
		return SealedVerificationArtifact{}, errors.New("invalid oversized verification evidence")
	}
	keySum := sha256.Sum256([]byte(sizeErr.Key))
	evidence, err := json.Marshal(struct {
		Version              int    `json:"version"`
		Reason               string `json:"reason"`
		StagingKeySHA256     string `json:"staging_key_sha256"`
		ObservedBytesAtLeast int64  `json:"observed_bytes_at_least"`
		MaxBytes             int64  `json:"max_bytes"`
	}{
		Version: 1, Reason: "verification_artifact_too_large",
		StagingKeySHA256:     hex.EncodeToString(keySum[:]),
		ObservedBytesAtLeast: sizeErr.ObservedBytes, MaxBytes: sizeErr.MaxBytes,
	})
	if err != nil {
		return SealedVerificationArtifact{}, err
	}
	sum := sha256.Sum256(evidence)
	digest := hex.EncodeToString(sum[:])
	key := oversizedVerificationEvidenceKey(taskID, attempt, digest)
	if err := s.PutObject(ctx, key, evidence, "application/json"); err != nil {
		return SealedVerificationArtifact{}, fmt.Errorf("writing oversized verification evidence: %w", err)
	}
	reachRecoveryBoundary(ctx, probe, BoundaryVerifyAfterSealedPut)
	expected := int64(len(evidence))
	if err := s.verifyVerificationObjectDigest(ctx, key, expected, verificationArtifactAbsoluteMaxBytes, digest); err != nil {
		return SealedVerificationArtifact{}, fmt.Errorf("reading oversized verification evidence: %w", err)
	}
	reachRecoveryBoundary(ctx, probe, BoundaryVerifyAfterSealedReadback)
	return SealedVerificationArtifact{Key: key, SHA256: digest, Bytes: expected, Body: evidence}, nil
}

// sealUnavailableVerificationEvidence records a small server-owned observation
// after a committed staging key has remained missing or unstable across the
// bounded retry budget. It never claims that absent worker bytes were read. The
// evidence exists solely to let the ordinary immutable plan/apply transaction
// terminate the bad attempt without leaving its parent job hostage forever.
func (s *Storage) sealUnavailableVerificationEvidenceWithProbe(
	ctx context.Context,
	taskID uuid.UUID,
	attempt int16,
	stagingKey, reason string,
	leaseAttempts int,
	probe recoveryBoundaryProbe,
) (SealedVerificationArtifact, error) {
	if taskID == uuid.Nil || attempt < 0 || strings.TrimSpace(stagingKey) == "" ||
		(reason != "missing" && reason != "changed") || leaseAttempts <= 0 {
		return SealedVerificationArtifact{}, errors.New("invalid unavailable verification evidence")
	}
	keySum := sha256.Sum256([]byte(stagingKey))
	evidence, err := json.Marshal(struct {
		Version          int    `json:"version"`
		Reason           string `json:"reason"`
		StagingKeySHA256 string `json:"staging_key_sha256"`
		LeaseAttempts    int    `json:"lease_attempts"`
	}{
		Version: 1, Reason: "verification_artifact_" + reason,
		StagingKeySHA256: hex.EncodeToString(keySum[:]), LeaseAttempts: leaseAttempts,
	})
	if err != nil {
		return SealedVerificationArtifact{}, err
	}
	sum := sha256.Sum256(evidence)
	digest := hex.EncodeToString(sum[:])
	key := unavailableVerificationEvidenceKey(taskID, attempt, digest)
	if err := s.PutObject(ctx, key, evidence, "application/json"); err != nil {
		return SealedVerificationArtifact{}, fmt.Errorf("writing unavailable verification evidence: %w", err)
	}
	reachRecoveryBoundary(ctx, probe, BoundaryVerifyAfterSealedPut)
	expected := int64(len(evidence))
	if err := s.verifyVerificationObjectDigest(ctx, key, expected, verificationArtifactAbsoluteMaxBytes, digest); err != nil {
		return SealedVerificationArtifact{}, fmt.Errorf("reading unavailable verification evidence: %w", err)
	}
	reachRecoveryBoundary(ctx, probe, BoundaryVerifyAfterSealedReadback)
	return SealedVerificationArtifact{Key: key, SHA256: digest, Bytes: expected, Body: evidence}, nil
}

// ReadSealedVerificationArtifact retains the original call surface while making
// every existing caller bounded. Production verification uses the narrower
// with-limit variant tied to the immutable attempt snapshot.
func (s *Storage) ReadSealedVerificationArtifact(ctx context.Context, artifact VerificationArtifact) ([]byte, error) {
	return s.readSealedVerificationArtifactWithLimit(ctx, artifact, verificationArtifactAbsoluteMaxBytes)
}

func (s *Storage) readSealedVerificationArtifactWithLimit(ctx context.Context, artifact VerificationArtifact, maxBytes int64) ([]byte, error) {
	if artifact.Bytes < 0 || artifact.Bytes > maxBytes {
		return nil, &VerificationArtifactTooLargeError{Key: artifact.Key, ObservedBytes: artifact.Bytes, MaxBytes: maxBytes}
	}
	body, err := s.readVerificationObjectBounded(ctx, artifact.Key, maxBytes, &artifact.Bytes)
	if err != nil {
		return nil, err
	}
	sum := sha256.Sum256(body)
	if int64(len(body)) != artifact.Bytes || hex.EncodeToString(sum[:]) != artifact.SHA256 {
		return nil, fmt.Errorf("%w: sealed verification artifact %q no longer matches pinned authority", ErrVerificationArtifactChanged, artifact.Key)
	}
	return body, nil
}
