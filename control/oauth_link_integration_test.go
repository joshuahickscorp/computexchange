//go:build integration

package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"
)

type oauthTestRoundTripper func(*http.Request) (*http.Response, error)

func (f oauthTestRoundTripper) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

// A state is a one-shot database capability even when callbacks race. The raw
// state and initiation secrets must never be persisted, and database time—not a
// caller-controlled timestamp—decides whether consumption is still allowed.
func TestOAuthLinkStateAtomicConsumeAndExpiry(t *testing.T) {
	ctx := context.Background()
	state := newSecret("cx_oauth_state_")
	initiation := newSecret("cx_oauth_init_")
	if err := itStore.CreateOAuthLinkState(ctx, demoBuyerUUID, githubOAuthProvider, state, initiation, time.Now().Add(time.Minute)); err != nil {
		t.Fatalf("create OAuth state: %v", err)
	}
	t.Cleanup(func() {
		_, _ = itPool.Exec(context.Background(),
			`DELETE FROM oauth_link_states WHERE state_hash IN ($1,$2)`,
			hashKey(state), hashKey(state+"-expired"))
	})

	var storedState, storedInitiation string
	if err := itPool.QueryRow(ctx,
		`SELECT state_hash, initiation_hash FROM oauth_link_states WHERE state_hash=$1`,
		hashKey(state)).Scan(&storedState, &storedInitiation); err != nil {
		t.Fatalf("read stored OAuth state: %v", err)
	}
	if storedState != hashKey(state) || storedInitiation != hashKey(initiation) {
		t.Fatalf("stored hashes do not match minted capabilities")
	}
	if storedState == state || storedInitiation == initiation {
		t.Fatal("raw OAuth capabilities reached durable storage")
	}

	const contenders = 16
	start := make(chan struct{})
	errCh := make(chan error, contenders)
	var wins atomic.Int32
	var wg sync.WaitGroup
	for i := 0; i < contenders; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			buyerID, err := itStore.ConsumeOAuthLinkState(ctx, githubOAuthProvider, state, initiation)
			if err == nil {
				if buyerID != demoBuyerUUID {
					errCh <- fmt.Errorf("consumed buyer = %s, want %s", buyerID, demoBuyerUUID)
					return
				}
				wins.Add(1)
				return
			}
			if !errors.Is(err, errOAuthLinkStateInvalid) {
				errCh <- err
			}
		}()
	}
	close(start)
	wg.Wait()
	close(errCh)
	for err := range errCh {
		t.Errorf("concurrent consume: %v", err)
	}
	if got := wins.Load(); got != 1 {
		t.Fatalf("atomic consume winners = %d, want exactly 1", got)
	}
	if _, err := itStore.ConsumeOAuthLinkState(ctx, githubOAuthProvider, state, initiation); !errors.Is(err, errOAuthLinkStateInvalid) {
		t.Fatalf("replay error = %v, want errOAuthLinkStateInvalid", err)
	}

	// Keep the table's expires_at > created_at invariant while moving a second
	// attempt wholly into the past, then prove database-side expiry rejection.
	expiredState := state + "-expired"
	if err := itStore.CreateOAuthLinkState(ctx, demoBuyerUUID, githubOAuthProvider, expiredState, initiation, time.Now().Add(time.Minute)); err != nil {
		t.Fatalf("create expiring OAuth state: %v", err)
	}
	if _, err := itPool.Exec(ctx,
		`UPDATE oauth_link_states
		    SET created_at=now()-interval '2 minutes', expires_at=now()-interval '1 minute'
		  WHERE state_hash=$1`, hashKey(expiredState)); err != nil {
		t.Fatalf("expire OAuth state fixture: %v", err)
	}
	if _, err := itStore.ConsumeOAuthLinkState(ctx, githubOAuthProvider, expiredState, initiation); !errors.Is(err, errOAuthLinkStateInvalid) {
		t.Fatalf("expired consume error = %v, want errOAuthLinkStateInvalid", err)
	}
}

// Two browser initiations receive independent HttpOnly cookies. Swapping those
// cookies cannot consume either browser's state; the correct pair succeeds once,
// links to the initiating buyer, and every replay is rejected before token exchange.
func TestGitHubOAuthCrossSessionAndReplay(t *testing.T) {
	ctx := context.Background()
	var cleanupStates []string
	var cleanupSource uuid.UUID
	t.Cleanup(func() {
		for _, state := range cleanupStates {
			_, _ = itPool.Exec(context.Background(),
				`DELETE FROM oauth_link_states WHERE state_hash=$1`, hashKey(state))
		}
		if cleanupSource != uuid.Nil {
			_, _ = itPool.Exec(context.Background(),
				`DELETE FROM git_sources WHERE id=$1`, cleanupSource)
		}
	})

	var exchanges atomic.Int32
	fakeGitHub := &GitHubApp{
		clientID:     "oauth-test-client",
		clientSecret: "oauth-test-secret",
		redirect:     itHTTP.URL + "/v1/connect/github/callback",
		http: &http.Client{Transport: oauthTestRoundTripper(func(r *http.Request) (*http.Response, error) {
			if r.URL.String() != "https://github.com/login/oauth/access_token" {
				return nil, fmt.Errorf("unexpected GitHub URL %s", r.URL)
			}
			if err := r.ParseForm(); err != nil {
				return nil, err
			}
			if r.Form.Get("code") != "valid-code" {
				return nil, fmt.Errorf("unexpected authorization code")
			}
			exchanges.Add(1)
			return &http.Response{
				StatusCode: http.StatusOK,
				Header:     make(http.Header),
				Body:       io.NopCloser(strings.NewReader(`{"access_token":"gho_oauth_link_test"}`)),
				Request:    r,
			}, nil
		})},
	}
	previousGitHub := githubApp
	githubApp = fakeGitHub
	t.Cleanup(func() { githubApp = previousGitHub })

	type initiation struct {
		state  string
		cookie *http.Cookie
	}
	begin := func() initiation {
		t.Helper()
		req, err := http.NewRequest(http.MethodGet, itHTTP.URL+"/v1/connect/github", nil)
		if err != nil {
			t.Fatalf("new initiation request: %v", err)
		}
		req.Header.Set("Authorization", "Bearer "+demoAPIKey)
		// Prove the production TLS-proxy cookie shape while using httptest HTTP.
		req.Header.Set("X-Forwarded-Proto", "https")
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatalf("initiate OAuth: %v", err)
		}
		defer resp.Body.Close()
		body, _ := io.ReadAll(resp.Body)
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("initiate status = %d: %s", resp.StatusCode, body)
		}
		if resp.Header.Get("Cache-Control") != "no-store" {
			t.Fatalf("initiate Cache-Control = %q, want no-store", resp.Header.Get("Cache-Control"))
		}
		var out struct {
			AuthorizeURL string `json:"authorize_url"`
		}
		if err := json.Unmarshal(body, &out); err != nil {
			t.Fatalf("decode initiation: %v", err)
		}
		authorizeURL, err := url.Parse(out.AuthorizeURL)
		if err != nil {
			t.Fatalf("parse authorize URL: %v", err)
		}
		state := authorizeURL.Query().Get("state")
		if len(state) < 50 || strings.Contains(state, demoBuyerUUID.String()) {
			t.Fatalf("state is not an opaque high-entropy value: %q", state)
		}
		cleanupStates = append(cleanupStates, state)
		var initiationCookie *http.Cookie
		for _, c := range resp.Cookies() {
			if c.Name == githubOAuthInitiationCookie {
				initiationCookie = c
				break
			}
		}
		if initiationCookie == nil {
			t.Fatal("initiation cookie missing")
		}
		if !initiationCookie.HttpOnly || !initiationCookie.Secure || initiationCookie.SameSite != http.SameSiteLaxMode ||
			initiationCookie.Path != "/v1/connect/github/callback" || initiationCookie.MaxAge <= 0 {
			t.Fatalf("unsafe initiation cookie: %#v", initiationCookie)
		}
		return initiation{state: state, cookie: initiationCookie}
	}

	a := begin()
	b := begin()
	if a.state == b.state || a.cookie.Value == b.cookie.Value {
		t.Fatal("independent browser initiations reused an OAuth capability")
	}

	callback := func(state string, cookie *http.Cookie) (*http.Response, []byte) {
		t.Helper()
		u := itHTTP.URL + "/v1/connect/github/callback?state=" + url.QueryEscape(state) + "&code=valid-code"
		req, err := http.NewRequest(http.MethodGet, u, nil)
		if err != nil {
			t.Fatalf("new callback request: %v", err)
		}
		req.Header.Set("X-Forwarded-Proto", "https")
		if cookie != nil {
			req.AddCookie(cookie)
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatalf("OAuth callback: %v", err)
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		return resp, body
	}

	resp, body := callback(a.state, b.cookie)
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("cross-session callback status = %d, want 400: %s", resp.StatusCode, body)
	}
	if exchanges.Load() != 0 {
		t.Fatal("cross-session callback reached GitHub token exchange")
	}
	var consumedAt *time.Time
	if err := itPool.QueryRow(ctx,
		`SELECT consumed_at FROM oauth_link_states WHERE state_hash=$1`, hashKey(a.state)).Scan(&consumedAt); err != nil {
		t.Fatalf("read state after cross-session attempt: %v", err)
	}
	if consumedAt != nil {
		t.Fatal("wrong browser cookie burned the legitimate browser's state")
	}

	resp, body = callback(a.state, a.cookie)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("correct callback status = %d, want 200: %s", resp.StatusCode, body)
	}
	if exchanges.Load() != 1 {
		t.Fatalf("token exchanges = %d, want 1", exchanges.Load())
	}
	var connected struct {
		SourceID string `json:"source_id"`
	}
	if err := json.Unmarshal(body, &connected); err != nil {
		t.Fatalf("decode callback: %v", err)
	}
	sourceID, err := uuid.Parse(connected.SourceID)
	if err != nil {
		t.Fatalf("source id = %q: %v", connected.SourceID, err)
	}
	cleanupSource = sourceID
	var linkedBuyer uuid.UUID
	if err := itPool.QueryRow(ctx, `SELECT buyer_id FROM git_sources WHERE id=$1`, sourceID).Scan(&linkedBuyer); err != nil {
		t.Fatalf("read linked source: %v", err)
	}
	if linkedBuyer != demoBuyerUUID {
		t.Fatalf("linked source buyer = %s, want initiator %s", linkedBuyer, demoBuyerUUID)
	}

	resp, body = callback(a.state, a.cookie)
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("replay status = %d, want 400: %s", resp.StatusCode, body)
	}
	if exchanges.Load() != 1 {
		t.Fatalf("replay reached token exchange; calls = %d", exchanges.Load())
	}
}
