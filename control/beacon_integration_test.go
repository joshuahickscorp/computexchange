//go:build integration

package main

// beacon_integration_test.go — proof for docs/internal/CREED_AND_PATH_TO_TEN.md,
// "Public site & conversion" 6→7, "Make the funnel observable". Drives the REAL
// POST /v1/beacon endpoint against the REAL Postgres + control plane (same
// itHTTP/itPool fixtures as the rest of the integration matrix) and confirms a
// real row lands in site_events — then confirms the admin funnel report reads
// it back as a real, non-zero, queryable count, not a guess.

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"

	"github.com/google/uuid"
)

// TestBeaconRecordsRealRow proves one POST /v1/beacon call produces one real,
// correctly-shaped row in site_events — the actual proof artifact the rung asks
// for, not just a 201 status code.
func TestBeaconRecordsRealRow(t *testing.T) {
	reset(t)
	ctx := context.Background()
	itPool.Exec(ctx, `TRUNCATE site_events`)
	t.Cleanup(func() { itPool.Exec(ctx, `TRUNCATE site_events`) })

	pageID := uuid.New().String()
	beat := int16(3)
	body := map[string]any{
		"page_id":  pageID,
		"event":    "scroll_depth",
		"beat":     beat,
		"path":     "/",
		"referrer": "https://news.ycombinator.com/item?id=12345&extra=tracking",
	}
	code, out := req(t, "POST", "/v1/beacon", body, jsonCT())
	if code != http.StatusCreated {
		t.Fatalf("POST /v1/beacon: want 201, got %d: %s", code, out)
	}

	// The strongest proof: query Postgres directly, not through the app's own
	// report endpoint, so a bug that both writes and reads back wrong data
	// consistently can't hide from this test.
	var (
		gotEvent        string
		gotBeat         *int16
		gotPath         string
		gotReferrerHost string
		gotPageID       uuid.UUID
		gotSourceIP     string
	)
	row := itPool.QueryRow(ctx,
		`SELECT event_type, beat, path, referrer_host, page_id, source_ip FROM site_events WHERE page_id = $1`,
		pageID)
	if err := row.Scan(&gotEvent, &gotBeat, &gotPath, &gotReferrerHost, &gotPageID, &gotSourceIP); err != nil {
		t.Fatalf("querying real site_events row: %v", err)
	}
	if gotEvent != "scroll_depth" {
		t.Fatalf("event_type = %q, want scroll_depth", gotEvent)
	}
	if gotBeat == nil || *gotBeat != 3 {
		t.Fatalf("beat = %v, want 3", gotBeat)
	}
	if gotPath != "/" {
		t.Fatalf("path = %q, want /", gotPath)
	}
	// referrer_host must be reduced to the HOST only — never the path or query
	// string (which here would otherwise leak "item?id=12345&extra=tracking").
	if gotReferrerHost != "news.ycombinator.com" {
		t.Fatalf("referrer_host = %q, want host-only news.ycombinator.com (no path/query)", gotReferrerHost)
	}
	if gotPageID.String() != pageID {
		t.Fatalf("page_id = %s, want %s", gotPageID, pageID)
	}
	if gotSourceIP == "" {
		t.Fatalf("source_ip was not recorded")
	}

	// Bad event type is rejected loudly, never silently dropped or coerced.
	badCode, _ := req(t, "POST", "/v1/beacon", map[string]any{"page_id": uuid.New().String(), "event": "not_a_real_event"}, jsonCT())
	if badCode != http.StatusBadRequest {
		t.Fatalf("unknown event type: want 400, got %d", badCode)
	}

	// Malformed page_id is rejected loudly too.
	badID, _ := req(t, "POST", "/v1/beacon", map[string]any{"page_id": "not-a-uuid", "event": "pageview"}, jsonCT())
	if badID != http.StatusBadRequest {
		t.Fatalf("non-uuid page_id: want 400, got %d", badID)
	}
}

// TestBeaconFunnelReportReflectsRealEvents proves the admin funnel report
// (GET /admin/funnel) is a real read of site_events, not a hand-typed or cached
// number: it must be admin-gated, must start honestly at zero for a freshly
// truncated table, and must reflect exactly the events this test inserts via
// the real endpoint — one pageview, one receipts_open, two cta_clicks (two
// distinct "detail" values), and scroll_depth on two distinct beats.
func TestBeaconFunnelReportReflectsRealEvents(t *testing.T) {
	reset(t)
	ctx := context.Background()
	itPool.Exec(ctx, `TRUNCATE site_events`)
	t.Cleanup(func() { itPool.Exec(ctx, `TRUNCATE site_events`) })

	// Gated: no credential -> 401, same convention as every other /admin/* route.
	if code, _ := req(t, "GET", "/admin/funnel", nil); code != http.StatusUnauthorized {
		t.Fatalf("unauthenticated /admin/funnel: want 401, got %d", code)
	}

	// Honest zero before any event lands.
	code, out := req(t, "GET", "/admin/funnel", nil, adminKey())
	if code != http.StatusOK {
		t.Fatalf("/admin/funnel (empty): want 200, got %d: %s", code, out)
	}
	var zero FunnelReport
	if err := json.Unmarshal(out, &zero); err != nil {
		t.Fatalf("decode empty funnel report: %v (%s)", err, out)
	}
	if zero.Pageviews != 0 || zero.ReceiptsOpens != 0 || zero.CTAClicks != 0 || len(zero.ScrollByBeat) != 0 {
		t.Fatalf("funnel report on an empty table was not honestly zero: %+v", zero)
	}

	events := []map[string]any{
		{"page_id": uuid.New().String(), "event": "pageview"},
		{"page_id": uuid.New().String(), "event": "receipts_open"},
		{"page_id": uuid.New().String(), "event": "cta_click", "detail": "alpha-request:buyer"},
		{"page_id": uuid.New().String(), "event": "cta_click", "detail": "demo"},
		{"page_id": uuid.New().String(), "event": "scroll_depth", "beat": int16(0)},
		{"page_id": uuid.New().String(), "event": "scroll_depth", "beat": int16(6)},
	}
	for _, e := range events {
		if code, out := req(t, "POST", "/v1/beacon", e, jsonCT()); code != http.StatusCreated {
			t.Fatalf("seeding beacon event %v: want 201, got %d: %s", e, code, out)
		}
	}

	code, out = req(t, "GET", "/admin/funnel", nil, adminKey())
	if code != http.StatusOK {
		t.Fatalf("/admin/funnel: want 200, got %d: %s", code, out)
	}
	var rep FunnelReport
	if err := json.Unmarshal(out, &rep); err != nil {
		t.Fatalf("decode funnel report: %v (%s)", err, out)
	}
	if rep.Pageviews != 1 {
		t.Fatalf("pageviews = %d, want 1", rep.Pageviews)
	}
	if rep.ReceiptsOpens != 1 {
		t.Fatalf("receipts_opens = %d, want 1", rep.ReceiptsOpens)
	}
	if rep.CTAClicks != 2 {
		t.Fatalf("cta_clicks = %d, want 2", rep.CTAClicks)
	}
	if rep.CTAClicksByType["alpha-request:buyer"] != 1 || rep.CTAClicksByType["demo"] != 1 {
		t.Fatalf("cta_clicks_by_detail = %+v, want alpha-request:buyer=1 demo=1", rep.CTAClicksByType)
	}
	if rep.ScrollByBeat["0"] != 1 || rep.ScrollByBeat["6"] != 1 {
		t.Fatalf("scroll_depth_by_beat = %+v, want {0:1, 6:1}", rep.ScrollByBeat)
	}
}
