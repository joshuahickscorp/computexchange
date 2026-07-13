package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type scriptedWebhookResolver struct {
	mu      sync.Mutex
	answers map[string][]net.IPAddr
	calls   map[string]int
}

func (r *scriptedWebhookResolver) LookupIPAddr(_ context.Context, host string) ([]net.IPAddr, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.calls == nil {
		r.calls = map[string]int{}
	}
	r.calls[host]++
	answers, ok := r.answers[host]
	if !ok {
		return nil, &net.DNSError{Name: host, Err: "not scripted"}
	}
	return append([]net.IPAddr(nil), answers...), nil
}

func (r *scriptedWebhookResolver) callCount(host string) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.calls[host]
}

func loopbackAnswer() []net.IPAddr {
	return []net.IPAddr{{IP: net.ParseIP("127.0.0.1")}}
}

func TestWebhookPinnedTransportPreservesHostAndResolvesOnce(t *testing.T) {
	var gotHost string
	receiver := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotHost = r.Host
		w.WriteHeader(http.StatusNoContent)
	}))
	defer receiver.Close()

	receiverURL, err := url.Parse(receiver.URL)
	if err != nil {
		t.Fatal(err)
	}
	const host = "hooks.example.test"
	resolver := &scriptedWebhookResolver{answers: map[string][]net.IPAddr{host: loopbackAnswer()}}
	client := newWebhookHTTPClientWithPolicy(webhookTargetPolicy{
		resolver: resolver, allowPrivate: true, allowHTTP: true,
	})
	target := "http://" + net.JoinHostPort(host, receiverURL.Port()) + "/complete"
	req, err := http.NewRequest(http.MethodPost, target, strings.NewReader(`{"ok":true}`))
	if err != nil {
		t.Fatal(err)
	}
	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("pinned delivery: %v", err)
	}
	resp.Body.Close()
	if gotHost != net.JoinHostPort(host, receiverURL.Port()) {
		t.Fatalf("Host = %q, want registered host %q", gotHost, net.JoinHostPort(host, receiverURL.Port()))
	}
	if got := resolver.callCount(host); got != 1 {
		t.Fatalf("DNS lookups = %d, want exactly one validation+pin lookup", got)
	}
}

func TestWebhookPinnedTransportPreservesTLSSNI(t *testing.T) {
	var gotHost, gotSNI string
	receiver := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotHost = r.Host
		w.WriteHeader(http.StatusNoContent)
	}))
	receiver.TLS = &tls.Config{
		MinVersion: tls.VersionTLS12,
		GetConfigForClient: func(hello *tls.ClientHelloInfo) (*tls.Config, error) {
			gotSNI = hello.ServerName
			return nil, nil
		},
	}
	receiver.StartTLS()
	defer receiver.Close()

	receiverURL, err := url.Parse(receiver.URL)
	if err != nil {
		t.Fatal(err)
	}
	const host = "hooks.example.test"
	resolver := &scriptedWebhookResolver{answers: map[string][]net.IPAddr{host: loopbackAnswer()}}
	client := newWebhookHTTPClientWithPolicy(webhookTargetPolicy{
		resolver: resolver, allowPrivate: true,
		tlsConfig: &tls.Config{ // test server certificate is intentionally not for hooks.example.test
			MinVersion: tls.VersionTLS12, InsecureSkipVerify: true, //nolint:gosec -- local SNI test only
		},
	})
	target := "https://" + net.JoinHostPort(host, receiverURL.Port()) + "/complete"
	resp, err := client.Post(target, "application/json", bytes.NewReader([]byte(`{}`)))
	if err != nil {
		t.Fatalf("pinned TLS delivery: %v", err)
	}
	resp.Body.Close()
	if gotSNI != host {
		t.Fatalf("TLS SNI = %q, want %q", gotSNI, host)
	}
	if gotHost != net.JoinHostPort(host, receiverURL.Port()) {
		t.Fatalf("Host = %q, want %q", gotHost, net.JoinHostPort(host, receiverURL.Port()))
	}
}

func TestWebhookClientRefusesRedirectBeforeTarget(t *testing.T) {
	var targetHits atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		targetHits.Add(1)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer target.Close()
	source := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Location", target.URL)
		w.WriteHeader(http.StatusTemporaryRedirect)
	}))
	defer source.Close()

	resolver := &scriptedWebhookResolver{answers: map[string][]net.IPAddr{"127.0.0.1": loopbackAnswer()}}
	client := newWebhookHTTPClientWithPolicy(webhookTargetPolicy{
		resolver: resolver, allowPrivate: true, allowHTTP: true,
	})
	resp, err := client.Post(source.URL, "application/json", strings.NewReader(`{}`))
	if resp != nil && resp.Body != nil {
		resp.Body.Close()
	}
	if err == nil || !errors.Is(err, errWebhookRedirectRefused) || !webhookFailureIsPermanent(err) {
		t.Fatalf("redirect error = %v, want permanent errWebhookRedirectRefused", err)
	}
	if got := targetHits.Load(); got != 0 {
		t.Fatalf("redirect target received %d request(s), want zero", got)
	}
}

func TestWebhookTransportRejectsAnyPrivateDNSAnswer(t *testing.T) {
	const host = "hooks.example.test"
	resolver := &scriptedWebhookResolver{answers: map[string][]net.IPAddr{
		host: {
			{IP: net.ParseIP("8.8.8.8")},
			{IP: net.ParseIP("169.254.169.254")},
		},
	}}
	client := newWebhookHTTPClientWithPolicy(webhookTargetPolicy{
		resolver: resolver, allowPrivate: false, allowHTTP: true,
	})
	resp, err := client.Post("http://"+host+"/hook", "application/json", strings.NewReader(`{}`))
	if resp != nil && resp.Body != nil {
		resp.Body.Close()
	}
	if err == nil || !webhookFailureIsPermanent(err) || !strings.Contains(err.Error(), "non-public address") {
		t.Fatalf("private DNS answer error = %v, want permanent SSRF refusal", err)
	}
	if got := resolver.callCount(host); got != 1 {
		t.Fatalf("DNS lookups = %d, want one", got)
	}
}

func TestWebhookURLAndRetryPolicy(t *testing.T) {
	t.Setenv("CX_WEBHOOK_ALLOW_PRIVATE", "true")
	if allowPrivateWebhookHostsForProcess(false) {
		t.Fatal("production private-webhook policy was disabled by environment")
	}
	for _, raw := range []string{
		"http://hooks.example.test/path",
		"https://user:pass@hooks.example.test/path",
		"https:///missing-host",
		"https://hooks.example.test:0/path",
		"https://hooks.example.test/" + strings.Repeat("x", webhookURLMaxBytes),
	} {
		if _, err := validateWebhookURLSyntax(raw, false); err == nil {
			t.Fatalf("validateWebhookURLSyntax(%q) succeeded, want rejection", raw)
		}
	}
	if _, err := validateWebhookURLSyntax("https://hooks.example.test/path", false); err != nil {
		t.Fatalf("valid webhook URL rejected: %v", err)
	}

	want := []time.Duration{30 * time.Second, time.Minute, 2 * time.Minute, 4 * time.Minute}
	for i, expected := range want {
		if got := webhookRetryBackoff(i + 1); got != expected {
			t.Fatalf("backoff attempt %d = %s, want %s", i+1, got, expected)
		}
	}
	if got := webhookRetryBackoff(100); got != 6*time.Hour {
		t.Fatalf("capped backoff = %s, want 6h", got)
	}
	if !webhookHTTPStatusIsRetryable(http.StatusTooManyRequests) ||
		!webhookHTTPStatusIsRetryable(http.StatusServiceUnavailable) ||
		webhookHTTPStatusIsRetryable(http.StatusNotFound) {
		t.Fatal("HTTP retry classification drifted")
	}
	for _, raw := range []string{
		"127.0.0.1", "10.0.0.1", "100.64.0.1", "169.254.169.254",
		"192.0.2.1", "198.18.0.1", "203.0.113.1", "::1", "fc00::1", "2001:db8::1",
	} {
		if !isInternalWebhookIP(net.ParseIP(raw)) {
			t.Fatalf("special-use address %s classified as public", raw)
		}
	}
	if isInternalWebhookIP(net.ParseIP("8.8.8.8")) || isInternalWebhookIP(net.ParseIP("2606:4700:4700::1111")) {
		t.Fatal("public addresses classified as internal")
	}
}
