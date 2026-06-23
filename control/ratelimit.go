package main

import (
	"context"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"
)

// ratelimit.go — a small in-memory token-bucket limiter. No external dependency
// (BLACKHOLE: a 60-line need does not earn a new import). Keyed by caller identity
// (remote IP for the whole surface; credential id for authenticated spam). Buckets
// refill lazily by elapsed time and idle ones are swept so the map stays bounded.

type tokenBucket struct {
	tokens float64
	last   time.Time
}

type rateLimiter struct {
	mu      sync.Mutex
	buckets map[string]*tokenBucket
	rate    float64 // tokens added per second
	burst   float64 // bucket capacity (and initial fill)
}

func newRateLimiter(ratePerSec, burst float64) *rateLimiter {
	return &rateLimiter{buckets: make(map[string]*tokenBucket), rate: ratePerSec, burst: burst}
}

// allow consumes one token for key, refilling by the time since its last request.
// Returns false when the bucket is empty (caller should reject with 429).
func (rl *rateLimiter) allow(key string) bool {
	now := time.Now()
	rl.mu.Lock()
	defer rl.mu.Unlock()
	b := rl.buckets[key]
	if b == nil {
		rl.buckets[key] = &tokenBucket{tokens: rl.burst - 1, last: now}
		return true
	}
	b.tokens += now.Sub(b.last).Seconds() * rl.rate
	if b.tokens > rl.burst {
		b.tokens = rl.burst
	}
	b.last = now
	if b.tokens >= 1 {
		b.tokens--
		return true
	}
	return false
}

// sweep drops buckets that have sat idle long enough to have fully refilled, so
// evicting them loses no live limiting state. Bounds memory under churny IPs/keys.
func (rl *rateLimiter) sweep() {
	now := time.Now()
	rl.mu.Lock()
	defer rl.mu.Unlock()
	for k, b := range rl.buckets {
		if now.Sub(b.last).Seconds()*rl.rate >= rl.burst {
			delete(rl.buckets, k)
		}
	}
}

// clientIP resolves the caller's address, honoring the X-Forwarded-For / X-Real-IP
// the TLS-terminating proxy (Caddy) sets in front, and falling back to RemoteAddr.
// For X-Forwarded-For we take the LAST hop — the value our own trusted proxy
// appended — because earlier entries are client-supplied and therefore spoofable.
func clientIP(r *http.Request) string {
	if xff := r.Header.Get("X-Forwarded-For"); xff != "" {
		if i := strings.LastIndexByte(xff, ','); i >= 0 {
			return strings.TrimSpace(xff[i+1:])
		}
		return strings.TrimSpace(xff)
	}
	if xr := r.Header.Get("X-Real-IP"); xr != "" {
		return strings.TrimSpace(xr)
	}
	if host, _, err := net.SplitHostPort(r.RemoteAddr); err == nil {
		return host
	}
	return r.RemoteAddr
}

// isRemote reports whether the request is from a non-loopback client — a real
// external caller (which in production arrives via the proxy and resolves to a
// public IP through X-Forwarded-For). Loopback is the box itself and the in-process
// test harness: trusted, and never rate-limited. This is also what keeps the
// integration tests (all from 127.0.0.1, demo credentials) from tripping limits.
func isRemote(r *http.Request) bool {
	ip := net.ParseIP(clientIP(r))
	return ip != nil && !ip.IsLoopback()
}

// limitByIP wraps a handler, rejecting (429) requests from a remote IP over the
// limit before auth even runs — bounding brute-force and floods. Health/metrics and
// loopback are exempt.
func (rl *rateLimiter) limitByIP(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/healthz", "/metrics":
			next.ServeHTTP(w, r)
			return
		}
		if isRemote(r) && !rl.allow(clientIP(r)) {
			writeErr(w, http.StatusTooManyRequests, "rate limit exceeded")
			return
		}
		next.ServeHTTP(w, r)
	})
}

// startRateLimitSweeper periodically sweeps the server's limiters until ctx is done.
func (s *Server) startRateLimitSweeper(ctx context.Context) {
	t := time.NewTicker(time.Minute)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			s.ipLimiter.sweep()
			s.buyerLimiter.sweep()
			s.workerLimiter.sweep()
		}
	}
}
