package main

import (
	"crypto/tls"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestCookieSecurityFailsClosedInProduction(t *testing.T) {
	t.Run("production forces secure without proxy metadata", func(t *testing.T) {
		t.Setenv("CX_ENV", "production")
		req := httptest.NewRequest(http.MethodGet, "http://control.test/admin", nil)
		if !isSecure(req) {
			t.Fatal("production request produced a non-Secure cookie policy")
		}
	})

	t.Run("development permits loopback HTTP", func(t *testing.T) {
		t.Setenv("CX_ENV", "development")
		req := httptest.NewRequest(http.MethodGet, "http://127.0.0.1/admin", nil)
		if isSecure(req) {
			t.Fatal("development HTTP unexpectedly forced Secure cookies")
		}
	})

	t.Run("direct TLS and proxy TLS stay secure", func(t *testing.T) {
		t.Setenv("CX_ENV", "development")
		direct := httptest.NewRequest(http.MethodGet, "https://control.test/admin", nil)
		direct.TLS = &tls.ConnectionState{}
		if !isSecure(direct) {
			t.Fatal("direct TLS request produced a non-Secure cookie policy")
		}
		proxied := httptest.NewRequest(http.MethodGet, "http://control.test/admin", nil)
		proxied.Header.Set("X-Forwarded-Proto", "https")
		if !isSecure(proxied) {
			t.Fatal("TLS proxy request produced a non-Secure cookie policy")
		}
	})
}

func TestAdminLogoutIsPublicAndIdempotentWithoutSessionCookie(t *testing.T) {
	for attempt := 1; attempt <= 2; attempt++ {
		req := httptest.NewRequest(http.MethodPost, "/admin/passkey/logout", nil)
		recorder := httptest.NewRecorder()

		// A zero-value server is intentional: an anonymous logout must not touch the
		// session store. Repeating it only clears stale browser state again.
		(&Server{}).handleAdminLogout(recorder, req)

		result := recorder.Result()
		if result.StatusCode != http.StatusOK {
			result.Body.Close()
			t.Fatalf("anonymous logout attempt %d status=%d, want %d", attempt, result.StatusCode, http.StatusOK)
		}
		if body := recorder.Body.String(); !strings.Contains(body, `"ok":true`) {
			result.Body.Close()
			t.Fatalf("anonymous logout attempt %d body=%q, want ok=true", attempt, body)
		}
		cookies := result.Cookies()
		clears := 0
		for _, cookie := range cookies {
			if cookie.Name == adminSessionCookie && cookie.MaxAge < 0 && cookie.Value == "" {
				clears++
			}
		}
		result.Body.Close()
		if clears != 2 {
			t.Fatalf("anonymous logout attempt %d cleared %d admin cookies, want both /admin and / paths: %v", attempt, clears, cookies)
		}
	}
}
