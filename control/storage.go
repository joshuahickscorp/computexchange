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

	st := &Storage{internal: internal, public: public, bucket: bucket}

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

// PutObject writes bytes to key via the internal client (input upload at submit).
func (s *Storage) PutObject(ctx context.Context, key string, data []byte, contentType string) error {
	if contentType == "" {
		contentType = "application/octet-stream"
	}
	_, err := s.internal.PutObject(ctx, s.bucket, key, bytes.NewReader(data), int64(len(data)),
		minio.PutObjectOptions{ContentType: contentType})
	if err != nil {
		return fmt.Errorf("put object %q: %w", key, err)
	}
	return nil
}

// GetObject reads the whole object at key via the internal client (result fetch
// at verification). Errors loudly if the object is missing — the verification
// path must never treat an absent result as a pass.
func (s *Storage) GetObject(ctx context.Context, key string) ([]byte, error) {
	obj, err := s.internal.GetObject(ctx, s.bucket, key, minio.GetObjectOptions{})
	if err != nil {
		return nil, fmt.Errorf("get object %q: %w", key, err)
	}
	defer obj.Close()
	data, err := io.ReadAll(obj)
	if err != nil {
		return nil, fmt.Errorf("read object %q: %w", key, err)
	}
	return data, nil
}
