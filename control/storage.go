package main

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"log"
	"net/url"
	"os"
	"strings"
	"sync/atomic"
	"time"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

// storage.go — the one object-storage layer (MinIO / any S3-compatible). It owns
// every read and write of job inputs and task results, and it mints the
// presigned URLs the agent reaches.
//
// Two endpoints, deliberately: S3_ENDPOINT is how the control plane reaches the
// store (e.g. the docker-network host `minio:9000`), and S3_PUBLIC_ENDPOINT is
// the host the *agent* reaches (e.g. `localhost:9000` or a public CDN). Presigned
// URLs are signed against the public endpoint so the agent can use them verbatim;
// control-side PutObject/GetObject go through the internal endpoint. Mixing them
// up is the classic "works on the control box, 403s on the worker" bug, so they
// are two explicit, separately-signing clients.

// Storage wraps the internal (control-side I/O) and public (presign) clients plus
// the bucket they operate on. NOT an interface: one implementation, one caller —
// an interface here would be ceremony (BLACKHOLE: collapse the indirection).
type Storage struct {
	internal *minio.Client // control-side PutObject/GetObject
	public   *minio.Client // signs presigned URLs the agent reaches
	bucket   string
	breaker  *storeBreaker // fail-fast guard for a sustained store outage
}

// NewStorage builds the object client from the environment, ensures the bucket
// exists (creating it if missing), and returns a ready Storage. S3 is mandatory:
// if S3_ENDPOINT / S3_BUCKET / credentials are unset the whole system has no
// object store, which is fatal (BLACKHOLE: missing config is never a silent
// fallback). The bucket check is done eagerly at startup so a misconfigured or
// unreachable store fails loudly here, not on the first job submission.
func NewStorage(ctx context.Context) (*Storage, error) {
	endpoint := os.Getenv("S3_ENDPOINT")
	bucket := os.Getenv("S3_BUCKET")
	access := os.Getenv("S3_ACCESS_KEY")
	secret := os.Getenv("S3_SECRET_KEY")
	region := os.Getenv("S3_REGION")
	if region == "" {
		region = "us-east-1"
	}
	if endpoint == "" || bucket == "" || access == "" || secret == "" {
		return nil, fmt.Errorf("object storage is mandatory but unconfigured: " +
			"set S3_ENDPOINT, S3_BUCKET, S3_ACCESS_KEY, S3_SECRET_KEY")
	}
	// S3_PUBLIC_ENDPOINT defaults to the internal endpoint when unset (single-host
	// dev where control and agent reach the store at the same address).
	publicEndpoint := os.Getenv("S3_PUBLIC_ENDPOINT")
	if publicEndpoint == "" {
		publicEndpoint = endpoint
	}

	internal, err := newMinio(endpoint, access, secret, region)
	if err != nil {
		return nil, fmt.Errorf("building internal S3 client: %w", err)
	}
	public, err := newMinio(publicEndpoint, access, secret, region)
	if err != nil {
		return nil, fmt.Errorf("building public S3 client: %w", err)
	}

	st := &Storage{internal: internal, public: public, bucket: bucket,
		breaker: newStoreBreaker(5, 10*time.Second)} // open after 5 fully-failed calls; cool down 10s

	// Bucket check at startup — surface an unreachable or misconfigured store now.
	cctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	exists, err := internal.BucketExists(cctx, bucket)
	if err != nil {
		return nil, fmt.Errorf("object store unreachable at %s: %w", endpoint, err)
	}
	if !exists {
		if err := internal.MakeBucket(cctx, bucket, minio.MakeBucketOptions{Region: region}); err != nil {
			return nil, fmt.Errorf("creating bucket %q: %w", bucket, err)
		}
		log.Printf("storage: created bucket %q at %s", bucket, endpoint)
	} else {
		log.Printf("storage: bucket %q present at %s (public presign endpoint %s)", bucket, endpoint, publicEndpoint)
	}
	return st, nil
}

// newMinio parses an endpoint that may carry a scheme (http://host:port) into the
// host:port + secure flag minio-go wants. A bare host:port defaults to non-TLS,
// which is correct for the dev MinIO; a real deployment passes https://.
func newMinio(endpoint, access, secret, region string) (*minio.Client, error) {
	secure := false
	host := endpoint
	if strings.Contains(endpoint, "://") {
		u, err := url.Parse(endpoint)
		if err != nil {
			return nil, fmt.Errorf("invalid S3 endpoint %q: %w", endpoint, err)
		}
		secure = u.Scheme == "https"
		host = u.Host
	}
	return minio.New(host, &minio.Options{
		Creds:  credentials.NewStaticV4(access, secret, ""),
		Secure: secure,
		Region: region,
	})
}

// PresignGet returns a time-limited GET URL for key, signed against the PUBLIC
// endpoint so the agent can fetch the object directly.
func (s *Storage) PresignGet(ctx context.Context, key string, ttl time.Duration) (string, error) {
	u, err := s.public.PresignedGetObject(ctx, s.bucket, key, ttl, url.Values{})
	if err != nil {
		return "", fmt.Errorf("presign GET %q: %w", key, err)
	}
	return u.String(), nil
}

// PresignPut returns a time-limited PUT URL for key (public endpoint) so the
// agent can upload its result object directly.
func (s *Storage) PresignPut(ctx context.Context, key string, ttl time.Duration) (string, error) {
	u, err := s.public.PresignedPutObject(ctx, s.bucket, key, ttl)
	if err != nil {
		return "", fmt.Errorf("presign PUT %q: %w", key, err)
	}
	return u.String(), nil
}

// ObjectExists reports whether an object exists at key — a StatObject HEAD via
// the internal client, the cheapest possible check (no body transfer). A missing
// object (NoSuchKey) is a definitive false, never retried; a transient error
// retries and, if exhausted, SURFACES — an unknown existence must never be
// reported as "absent" (the watchdog uses this to decide whether a buyer gets a
// partial-checkpoint URL, and silently dropping one would lose real work).
func (s *Storage) ObjectExists(ctx context.Context, key string) (bool, error) {
	var exists bool
	err := s.withRetry(ctx, func() (bool, error) {
		_, serr := s.internal.StatObject(ctx, s.bucket, key, minio.StatObjectOptions{})
		if serr == nil {
			exists = true
			return false, nil
		}
		if minio.ToErrorResponse(serr).Code == "NoSuchKey" {
			exists = false
			return false, nil // definitive: the store answered "not there"
		}
		return true, fmt.Errorf("stat object %q: %w", key, serr)
	})
	return exists, err
}

// errStoreCircuitOpen is returned (fast) while the breaker is open after a sustained
// object-store outage, instead of making every caller grind through full retries.
var errStoreCircuitOpen = fmt.Errorf("object store circuit open (recent sustained failures); failing fast")

// storeBreaker is a minimal circuit breaker over the object store: after `threshold`
// consecutive fully-failed calls it OPENS for `cooldown`, so a sustained outage can't
// tie up the bounded DB pool / request goroutines with every caller retrying. Any
// call the store ANSWERS (success, or a definitive reply like NoSuchKey — the store
// is up) closes it. Pure atomics with an injected clock → unit-testable, no store.
type storeBreaker struct {
	failures  atomic.Int32
	openUntil atomic.Int64 // unix nanos; > now = open
	threshold int32
	cooldown  time.Duration
}

func newStoreBreaker(threshold int32, cooldown time.Duration) *storeBreaker {
	return &storeBreaker{threshold: threshold, cooldown: cooldown}
}

// allow reports whether a call may proceed (breaker closed, or cooldown elapsed).
func (b *storeBreaker) allow(now time.Time) bool { return now.UnixNano() >= b.openUntil.Load() }

// record updates the breaker after a call: healthy resets it; a fully-failed call
// increments and trips the breaker once the threshold is reached.
func (b *storeBreaker) record(now time.Time, healthy bool) {
	if healthy {
		b.failures.Store(0)
		b.openUntil.Store(0)
		return
	}
	if b.failures.Add(1) >= b.threshold {
		b.openUntil.Store(now.Add(b.cooldown).UnixNano())
		b.failures.Store(0)
	}
}

// withRetry runs op with bounded backoff (3 attempts, ctx-aware) for transient
// store/network errors, gated by the circuit breaker — the single source of truth
// for the object-store retry policy. op returns retry=true to try again, or
// retry=false to stop now (err==nil on success, or a definitive error the caller
// already wrapped, e.g. a missing object that must NOT be retried). A call the store
// answers closes the breaker; a call that exhausts transient retries trips it.
func (s *Storage) withRetry(ctx context.Context, op func() (retry bool, err error)) error {
	if !s.breaker.allow(time.Now()) {
		return errStoreCircuitOpen
	}
	var lastErr error
	for attempt := 0; attempt < 3; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(time.Duration(attempt) * 200 * time.Millisecond):
			}
		}
		retry, err := op()
		if !retry {
			s.breaker.record(time.Now(), true) // store responded (success or definitive) → healthy
			return err
		}
		lastErr = err
	}
	s.breaker.record(time.Now(), false) // exhausted transient retries → a real store failure
	return lastErr
}

// PutObject writes bytes to key via the internal client (input upload at submit,
// result merge). Idempotent on the key, so it retries transient store blips.
func (s *Storage) PutObject(ctx context.Context, key string, data []byte, contentType string) error {
	if contentType == "" {
		contentType = "application/octet-stream"
	}
	// A PUT to a fixed key is idempotent → safe to retry through a transient blip.
	// The transfer histogram (Data Transfer & Artifact I/O 9->10) is recorded on the
	// SUCCESSFUL attempt only, and times just that attempt's real network round trip —
	// a retried-past-a-blip PUT records the wall time of the attempt that actually
	// moved the bytes, not the wasted retry budget before it (which would inflate the
	// throughput denominator with time no object crossed the wire).
	return s.withRetry(ctx, func() (bool, error) {
		start := time.Now()
		if _, err := s.internal.PutObject(ctx, s.bucket, key, bytes.NewReader(data), int64(len(data)),
			minio.PutObjectOptions{ContentType: contentType}); err != nil {
			return true, fmt.Errorf("put object %q: %w", key, err)
		}
		observeTransfer("put", len(data), time.Since(start))
		return false, nil
	})
}

// GetObject reads the whole object at key via the internal client (result fetch at
// verification). Transient errors retry; a missing object (NoSuchKey) is definitive
// and surfaced loudly — the verification path must never treat an absent result as
// a pass (so it is never retried away, never swallowed).
func (s *Storage) GetObject(ctx context.Context, key string) ([]byte, error) {
	var data []byte
	err := s.withRetry(ctx, func() (bool, error) {
		start := time.Now()
		obj, err := s.internal.GetObject(ctx, s.bucket, key, minio.GetObjectOptions{})
		if err != nil {
			return true, err
		}
		d, rerr := io.ReadAll(obj)
		obj.Close()
		if rerr == nil {
			data = d
			// Transfer histogram (Data Transfer & Artifact I/O 9->10): the wall time
			// spans the real GetObject + ReadAll (the whole object off the wire), and
			// the byte count is the object's real size — recorded on success only, so a
			// definitive NoSuchKey (no bytes moved) never lands a spurious 0-throughput
			// sample (observeTransfer also clamps a 0-length read out for the same reason).
			observeTransfer("get", len(d), time.Since(start))
			return false, nil
		}
		if minio.ToErrorResponse(rerr).Code == "NoSuchKey" {
			return false, fmt.Errorf("get object %q: %w", key, rerr)
		}
		return true, fmt.Errorf("get object %q: %w", key, rerr)
	})
	return data, err
}

// PutObjectStream uploads r to key with UNKNOWN size (-1), for a caller that has
// a stream, not a []byte, and does not want to buffer it first to learn its
// length (Data Transfer & Artifact I/O 7->8 / Scalability Headroom 7->8: the
// canonical-input tee in createJob's streaming split). minio-go internally
// switches to a streaming multipart upload when size<0, so this never buffers
// the whole object in control-plane memory. NOT retried: r is a single-pass
// stream (often the read side of an io.Pipe whose write side is being driven
// live by another goroutine), so re-attempting from byte zero after a partial
// failure is not safe the way a []byte PutObject retry is — the caller sees the
// error directly and decides whether to fail the whole submission.
func (s *Storage) PutObjectStream(ctx context.Context, key string, r io.Reader, contentType string) error {
	if contentType == "" {
		contentType = "application/octet-stream"
	}
	_, err := s.internal.PutObject(ctx, s.bucket, key, r, -1, minio.PutObjectOptions{ContentType: contentType})
	if err != nil {
		return fmt.Errorf("put object stream %q: %w", key, err)
	}
	return nil
}

// GetObjectReader opens a streaming reader on the object at key, WITHOUT reading
// it into memory (Data Transfer & Artifact I/O 7->8, "Stream the control-plane
// storage layer end to end"). Unlike GetObject/withRetry, a transient mid-stream
// read error cannot be silently retried from byte zero without either buffering
// everything (defeating the point) or re-opening a fresh ranged GET — so retry
// here is deliberately limited to the OPEN call (a stat-equivalent, cheap and
// idempotent): once the caller starts reading the returned io.ReadCloser, any
// error surfaces directly to them. A missing object (NoSuchKey) surfaces
// immediately, never retried, matching GetObject's contract. Callers MUST Close
// the returned reader.
func (s *Storage) GetObjectReader(ctx context.Context, key string) (io.ReadCloser, error) {
	if !s.breaker.allow(time.Now()) {
		return nil, errStoreCircuitOpen
	}
	var lastErr error
	for attempt := 0; attempt < 3; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-time.After(time.Duration(attempt) * 200 * time.Millisecond):
			}
		}
		obj, err := s.internal.GetObject(ctx, s.bucket, key, minio.GetObjectOptions{})
		if err != nil {
			lastErr = fmt.Errorf("get object reader %q: %w", key, err)
			continue
		}
		// minio-go's GetObject only issues the real request lazily on first Read/Stat —
		// Stat here forces that now, inside the retry loop, so a 404/network failure is
		// caught and retried/reported HERE rather than surfacing later, mid-stream, to a
		// caller that already committed to the "open succeeded" path.
		if _, serr := obj.Stat(); serr != nil {
			obj.Close()
			if minio.ToErrorResponse(serr).Code == "NoSuchKey" {
				return nil, fmt.Errorf("get object reader %q: %w", key, serr)
			}
			lastErr = fmt.Errorf("get object reader %q: %w", key, serr)
			continue
		}
		s.breaker.record(time.Now(), true)
		return obj, nil
	}
	s.breaker.record(time.Now(), false)
	return nil, lastErr
}
