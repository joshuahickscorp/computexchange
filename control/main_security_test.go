package main

import (
	"net/http"
	"strings"
	"testing"
	"time"
)

func TestProductionRefusesMissingVerificationSampleSecret(t *testing.T) {
	for _, tc := range []struct {
		name      string
		cxEnv     string
		stripeKey string
	}{
		{name: "production environment", cxEnv: "production", stripeKey: "sk_test_local"},
		{name: "production abbreviation", cxEnv: "prod", stripeKey: "sk_test_local"},
		{name: "live money even without environment flag", stripeKey: "sk_live_example"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			missing, err := validateHardeningSecretConfig(
				tc.cxEnv, tc.stripeKey, "a-token-key-with-at-least-32-unpredictable-bytes", "",
			)
			if !missing || err == nil {
				t.Fatalf("missing verification secret: missing=%v err=%v, want fatal configuration error", missing, err)
			}
			if !strings.Contains(err.Error(), "CX_VERIFICATION_SAMPLE_SECRET") ||
				!strings.Contains(err.Error(), "refusing to start") {
				t.Fatalf("configuration error does not identify the fail-closed boundary: %v", err)
			}
		})
	}

	for _, unsafe := range []string{"short", insecureDevelopmentSamplingSecret} {
		missing, err := validateHardeningSecretConfig(
			"production", "sk_test_local", "a-token-key-with-at-least-32-unpredictable-bytes", unsafe,
		)
		if !missing || err == nil {
			t.Fatalf("unsafe verification secret %q accepted: missing=%v err=%v", unsafe, missing, err)
		}
	}

	missing, err := validateHardeningSecretConfig(
		"production", "sk_live_example", "a-token-key-with-at-least-32-unpredictable-bytes",
		"a-unique-verification-sample-secret-with-at-least-32-bytes",
	)
	if missing || err != nil {
		t.Fatalf("complete production hardening config rejected: missing=%v err=%v", missing, err)
	}
	if missing, err := validateHardeningSecretConfig(
		"production", "sk_live_example", "short",
		"a-unique-verification-sample-secret-with-at-least-32-bytes",
	); !missing || err == nil || !strings.Contains(err.Error(), "CX_TOKEN_KEY") {
		t.Fatalf("short CX_TOKEN_KEY accepted: missing=%v err=%v", missing, err)
	}
}

func TestDBMaxConnsIsBoundedBeforeInt32Conversion(t *testing.T) {
	for _, tc := range []struct {
		raw  string
		want int32
	}{
		{"", defaultDBMaxConns},
		{"1", 1},
		{"1000", 1000},
	} {
		got, err := parseDBMaxConns(tc.raw)
		if err != nil || got != tc.want {
			t.Fatalf("parseDBMaxConns(%q)=(%d,%v), want (%d,nil)", tc.raw, got, err, tc.want)
		}
	}
	for _, raw := range []string{"0", "-1", "1001", "2147483648", "999999999999999999999", "not-a-number"} {
		if _, err := parseDBMaxConns(raw); err == nil {
			t.Fatalf("unsafe DB_MAX_CONNS %q accepted", raw)
		}
	}
}

func TestProductionAndLiveMoneyRefuseDemoSeed(t *testing.T) {
	for _, tc := range []struct{ env, key string }{
		{"production", "sk_test_local"},
		{"prod", ""},
		{"development", "sk_live_example"},
	} {
		if err := validateSeedAllowed(tc.env, tc.key); err == nil || !strings.Contains(err.Error(), "refusing") {
			t.Fatalf("seed allowed for env=%q key=%q: %v", tc.env, tc.key, err)
		}
	}
	if err := validateSeedAllowed("development", "sk_test_local"); err != nil {
		t.Fatalf("development seed rejected: %v", err)
	}
}

func TestHTTPServerHasConnectionAndHeaderBounds(t *testing.T) {
	srv := newHTTPServer(":0", http.NotFoundHandler())
	if srv.ReadHeaderTimeout <= 0 || srv.ReadTimeout <= 0 || srv.IdleTimeout <= 0 {
		t.Fatalf("server timeouts must all be positive: %+v", srv)
	}
	if srv.MaxHeaderBytes <= 0 || srv.MaxHeaderBytes > 64<<10 {
		t.Fatalf("MaxHeaderBytes=%d, want a positive <=64KiB bound", srv.MaxHeaderBytes)
	}
}

func TestRequestLogQuotesControlCharacters(t *testing.T) {
	line := formatRequestLog("id\nforged", "GET\nforged", "/safe\nforged", 200, time.Second)
	if strings.ContainsRune(line, '\n') || strings.ContainsRune(line, '\r') {
		t.Fatalf("request-derived control character reached log line: %q", line)
	}
	for _, want := range []string{`id="id\nforged"`, `method="GET\nforged"`, `path="/safe\nforged"`} {
		if !strings.Contains(line, want) {
			t.Fatalf("quoted log line %q missing %q", line, want)
		}
	}
}

func setValidLiveEconomicSchedule(t *testing.T) {
	t.Helper()
	t.Setenv(economicScheduleVersionEnv, "test-schedule-v1")
	t.Setenv(processorPercentBPSEnv, "290")
	t.Setenv(processorFixedUSDEnv, "0.30")
	t.Setenv(controlPerTaskUSDEnv, "0.001")
	t.Setenv(targetMarginBPSEnv, "500")
}

func TestLiveMoneyConfigFailsClosed(t *testing.T) {
	const (
		billingSecret = "whsec_billing_private_marker"
		connectSecret = "whsec_connect_private_marker"
	)

	t.Run("development without live key remains optional", func(t *testing.T) {
		if err := validateLiveMoneyConfig("development", "sk_test_local", "", ""); err != nil {
			t.Fatalf("non-live development config rejected: %v", err)
		}
	})

	for _, tc := range []struct {
		name      string
		cxEnv     string
		stripeKey string
	}{
		{name: "production environment", cxEnv: "production", stripeKey: "sk_test_local"},
		{name: "production abbreviation", cxEnv: "prod", stripeKey: "sk_test_local"},
		{name: "live key without environment flag", cxEnv: "development", stripeKey: "sk_live_example"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			setValidLiveEconomicSchedule(t)
			if err := validateLiveMoneyConfig(tc.cxEnv, tc.stripeKey, billingSecret, connectSecret); err != nil {
				t.Fatalf("complete live-money config rejected: %v", err)
			}
		})
	}

	t.Run("production requires a Stripe API key", func(t *testing.T) {
		setValidLiveEconomicSchedule(t)
		err := validateLiveMoneyConfig("production", "", billingSecret, connectSecret)
		if err == nil || !strings.Contains(err.Error(), "STRIPE_SECRET_KEY") {
			t.Fatalf("missing Stripe API key did not fail closed: %v", err)
		}
	})

	for _, tc := range []struct {
		name           string
		billingSecret  string
		connectSecret  string
		wantIdentifier string
	}{
		{name: "missing buyer webhook", connectSecret: connectSecret, wantIdentifier: "STRIPE_WEBHOOK_SECRET"},
		{name: "missing connect webhook", billingSecret: billingSecret, wantIdentifier: "CX_CONNECT_WEBHOOK_SECRET"},
		{name: "same secret for separate endpoints", billingSecret: billingSecret, connectSecret: billingSecret, wantIdentifier: "must be distinct"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			setValidLiveEconomicSchedule(t)
			err := validateLiveMoneyConfig("production", "sk_live_example", tc.billingSecret, tc.connectSecret)
			if err == nil || !strings.Contains(err.Error(), tc.wantIdentifier) {
				t.Fatalf("error=%v, want identifier %q", err, tc.wantIdentifier)
			}
			if strings.Contains(err.Error(), billingSecret) || strings.Contains(err.Error(), connectSecret) {
				t.Fatalf("configuration error leaked secret material: %v", err)
			}
		})
	}

	t.Run("invalid economic schedule", func(t *testing.T) {
		setValidLiveEconomicSchedule(t)
		t.Setenv(processorFixedUSDEnv, "")
		err := validateLiveMoneyConfig("production", "sk_live_example", billingSecret, connectSecret)
		if err == nil || !strings.Contains(err.Error(), processorFixedUSDEnv) ||
			!strings.Contains(err.Error(), "refusing to start") {
			t.Fatalf("missing economic input did not fail closed: %v", err)
		}
		if strings.Contains(err.Error(), billingSecret) || strings.Contains(err.Error(), connectSecret) {
			t.Fatalf("configuration error leaked secret material: %v", err)
		}
	})
}

func TestLiveConnectURLsRequireOwnHTTPSOrigin(t *testing.T) {
	goodReturn := "https://computexchange.net/earn?connected=1"
	goodRefresh := "https://computexchange.net/earn/connect/refresh"
	if err := validateLiveConnectURLConfig(
		"production", "sk_live_example", goodReturn, goodRefresh, "computexchange.net",
	); err != nil {
		t.Fatalf("valid first-party Connect URLs rejected: %v", err)
	}
	for _, tc := range []struct {
		name, ret, refresh, host string
	}{
		{"missing return", "", goodRefresh, "computexchange.net"},
		{"missing refresh", goodReturn, "", "computexchange.net"},
		{"missing site host", goodReturn, goodRefresh, ""},
		{"http return", "http://computexchange.net/earn", goodRefresh, "computexchange.net"},
		{"competitor origin", "https://compute.exchange/earn", goodRefresh, "computexchange.net"},
		{"subdomain lookalike", "https://computexchange.net.attacker.test/earn", goodRefresh, "computexchange.net"},
		{"credential URL", "https://user@computexchange.net/earn", goodRefresh, "computexchange.net"},
		{"nondefault port", "https://computexchange.net:8443/earn", goodRefresh, "computexchange.net"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if err := validateLiveConnectURLConfig(
				"production", "sk_live_example", tc.ret, tc.refresh, tc.host,
			); err == nil || !strings.Contains(err.Error(), "refusing to start") {
				t.Fatalf("unsafe Connect URL config accepted: %v", err)
			}
		})
	}
	if err := validateLiveConnectURLConfig(
		"development", "sk_test_example", "", "", "",
	); err != nil {
		t.Fatalf("non-live development startup unexpectedly requires Connect URLs: %v", err)
	}
}
