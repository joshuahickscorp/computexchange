package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// TestClientIPResistsXFFSpoofing proves the XFF-spoofing item in
// docs/SECURITY.md's self-administered attack checklist: an attacker who sends
// their own X-Forwarded-For value must NOT be able to make clientIP() (and
// therefore every per-IP rate limit / recorded source IP) resolve to a spoofed
// address. Caddy (the trusted TLS-terminating proxy in front of this service)
// APPENDS its own observed peer address as the LAST hop rather than replacing
// the header, so the correct defense is "take the last hop, never the first" —
// this test proves clientIP actually does that, not just that it parses XFF at
// all.
func TestClientIPResistsXFFSpoofing(t *testing.T) {
	// A single, unadorned XFF (the common case: one proxy hop) is trusted as-is.
	r := httptest.NewRequest(http.MethodGet, "/", nil)
	r.Header.Set("X-Forwarded-For", "203.0.113.9")
	if got := clientIP(r); got != "203.0.113.9" {
		t.Fatalf("single-hop XFF: want 203.0.113.9, got %q", got)
	}

	// An attacker prepends a fake IP; Caddy appends the REAL peer it observed as
	// the last hop. clientIP must resolve to the last (real) hop, never the
	// attacker-supplied first one.
	r2 := httptest.NewRequest(http.MethodGet, "/", nil)
	r2.Header.Set("X-Forwarded-For", "6.6.6.6, 203.0.113.9")
	if got := clientIP(r2); got != "203.0.113.9" {
		t.Fatalf("spoofed multi-hop XFF: want the real last hop 203.0.113.9, got %q (attacker's 6.6.6.6 must be ignored)", got)
	}

	// Multiple attacker-prepended hops must not change the outcome — only the
	// last (Caddy-appended) entry counts, no matter how many fake ones precede it.
	r3 := httptest.NewRequest(http.MethodGet, "/", nil)
	r3.Header.Set("X-Forwarded-For", "1.1.1.1, 2.2.2.2, 3.3.3.3, 203.0.113.9")
	if got := clientIP(r3); got != "203.0.113.9" {
		t.Fatalf("multiple spoofed hops: want the real last hop 203.0.113.9, got %q", got)
	}

	// No XFF at all falls back to X-Real-IP, then RemoteAddr — never empty.
	r4 := httptest.NewRequest(http.MethodGet, "/", nil)
	r4.Header.Set("X-Real-IP", "198.51.100.7")
	if got := clientIP(r4); got != "198.51.100.7" {
		t.Fatalf("X-Real-IP fallback: want 198.51.100.7, got %q", got)
	}
}

// TestIsRemoteTrustsXFFLoopbackClaimUnconditionally documents a REAL, honest
// finding from the XFF-spoofing checklist item in docs/SECURITY.md: isRemote
// trusts clientIP's resolved value completely, including a caller-supplied
// "X-Forwarded-For: 127.0.0.1" — there is no code-level check that the request
// actually arrived via the trusted proxy (Caddy) rather than hitting this
// process directly. In THIS deployment that is not exploitable in practice
// because the control plane's port is Docker `expose`d, never `ports`-published
// (docker-compose.prod.yml) — nothing outside the compose network can reach it
// to send a request without XFF already set by Caddy. But that is a NETWORK
// TOPOLOGY guarantee, not an application-level one; if this binary is ever run
// with its port directly exposed (a bare `go run`, a different deploy
// topology, a misconfigured compose file), an external caller could spoof
// "X-Forwarded-For: 127.0.0.1" and have their rate-limited requests silently
// exempted. Named here rather than silently assumed away — see docs/SECURITY.md.
func TestIsRemoteTrustsXFFLoopbackClaimUnconditionally(t *testing.T) {
	r := httptest.NewRequest(http.MethodGet, "/", nil)
	r.Header.Set("X-Forwarded-For", "127.0.0.1")
	// httptest.NewRequest's default RemoteAddr ("192.0.2.1:1234") is NOT
	// loopback, so this proves isRemote follows the spoofed XFF claim over the
	// real observed peer address — the actual current behavior, not the ideal.
	if isRemote(r) {
		t.Fatal("expected isRemote to follow the (spoofable) XFF claim of loopback — if this now returns true, the code changed and docs/SECURITY.md's gap note is stale and should be updated")
	}
}
