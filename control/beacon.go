package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/url"
	"strconv"

	"github.com/google/uuid"
)

// beacon.go — the public site's funnel beacon (docs/internal/
// CREED_AND_PATH_TO_TEN.md, "Public site & conversion" 6→7, "Make the funnel
// observable"). Before this, the site's own analysis of itself said "zero
// analytics of any kind" — every claim about the funnel (does anyone scroll
// past the arrival beat? does anyone open the receipts panel before bouncing?
// does the alpha-request CTA even get seen?) was a guess. This is a minimal,
// self-hosted, cookie-free beacon: no CDN script, no third-party pixel, no
// cookie, logging four event kinds into Postgres so those questions have a
// real, queryable answer instead of a guess.
//
// Cookie-free by construction, not just by omission of a Set-Cookie header:
// the client sends a page_id it generated in memory (crypto.randomUUID()) at
// page load, purely so the handful of events one pageview emits (the
// pageview itself, whichever scroll-depth thresholds were crossed, a
// receipts-panel open, a CTA click) can be grouped together in the report
// below. That id is never persisted (no cookie, no localStorage) and never
// sent back on a later visit, so it identifies one page load, not a visitor
// — nothing here would ever need a cookie-consent banner.

const (
	beaconDetailMaxLen = 128
	beaconPathMaxLen   = 256
)

var beaconEventTypes = map[string]bool{
	"pageview":      true,
	"scroll_depth":  true,
	"receipts_open": true,
	"cta_click":     true,
}

type beaconBody struct {
	PageID   string `json:"page_id"`
	Event    string `json:"event"`
	Beat     *int16 `json:"beat,omitempty"`
	Detail   string `json:"detail,omitempty"`
	Path     string `json:"path,omitempty"`
	Referrer string `json:"referrer,omitempty"`
}

// handleBeacon records one funnel event. Unauthenticated (a page viewer has no
// account), rate-limited by the same global per-IP limiter every route gets
// (see Routes() in api.go) — a real cap on write volume without a per-visitor
// cookie to key a tighter limiter on.
func (s *Server) handleBeacon(w http.ResponseWriter, r *http.Request) {
	var req beaconBody
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid beacon json: "+err.Error())
		return
	}
	pageID, err := uuid.Parse(req.PageID)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "page_id must be a uuid")
		return
	}
	if !beaconEventTypes[req.Event] {
		writeErr(w, http.StatusBadRequest, "unknown event type")
		return
	}
	detail := req.Detail
	if len(detail) > beaconDetailMaxLen {
		detail = detail[:beaconDetailMaxLen]
	}
	path := req.Path
	if len(path) > beaconPathMaxLen {
		path = path[:beaconPathMaxLen]
	}
	if err := s.store.RecordSiteEvent(r.Context(), pageID, req.Event, req.Beat, detail, path, referrerHost(req.Referrer), clientIP(r)); err != nil {
		writeErr(w, http.StatusInternalServerError, "recording beacon event: "+err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, map[string]bool{"ok": true})
}

// referrerHost reduces a full referrer URL down to just its host — never the
// path or query string, which could otherwise leak a search query or a
// referring page's own tracking parameters into this database.
func referrerHost(referrer string) string {
	if referrer == "" {
		return ""
	}
	u, err := url.Parse(referrer)
	if err != nil || u.Host == "" {
		return ""
	}
	return u.Host
}

// RecordSiteEvent persists one funnel-beacon event.
func (s *Store) RecordSiteEvent(ctx context.Context, pageID uuid.UUID, event string, beat *int16, detail, path, referrerHost, sourceIP string) error {
	_, err := s.pool.Exec(ctx,
		`INSERT INTO site_events (page_id, event_type, beat, detail, path, referrer_host, source_ip)
		 VALUES ($1, $2, $3, $4, $5, $6, $7)`,
		pageID, event, beat, nullIfEmpty(detail), nullIfEmpty(path), nullIfEmpty(referrerHost), sourceIP,
	)
	return err
}

func nullIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}

// FunnelReport is the real, queryable answer to "does anyone use the funnel"
// — the proof artifact this rung's plan asks for, not a guess. Every count is
// computed straight from site_events; a report with all-zero counts is the
// honest state of a page nobody has loaded yet, not a bug in the query.
type FunnelReport struct {
	Pageviews       int64            `json:"pageviews"`
	ReceiptsOpens   int64            `json:"receipts_opens"`
	CTAClicks       int64            `json:"cta_clicks"`
	ScrollByBeat    map[string]int64 `json:"scroll_depth_by_beat"`
	CTAClicksByType map[string]int64 `json:"cta_clicks_by_detail"`
}

// FunnelReport computes the current funnel counters from the live database.
func (s *Store) FunnelReport(ctx context.Context) (FunnelReport, error) {
	rep := FunnelReport{ScrollByBeat: map[string]int64{}, CTAClicksByType: map[string]int64{}}
	if err := s.pool.QueryRow(ctx,
		`SELECT count(*) FROM site_events WHERE event_type = 'pageview'`,
	).Scan(&rep.Pageviews); err != nil {
		return rep, err
	}
	if err := s.pool.QueryRow(ctx,
		`SELECT count(*) FROM site_events WHERE event_type = 'receipts_open'`,
	).Scan(&rep.ReceiptsOpens); err != nil {
		return rep, err
	}
	if err := s.pool.QueryRow(ctx,
		`SELECT count(*) FROM site_events WHERE event_type = 'cta_click'`,
	).Scan(&rep.CTAClicks); err != nil {
		return rep, err
	}
	rows, err := s.pool.Query(ctx,
		`SELECT beat, count(*) FROM site_events WHERE event_type = 'scroll_depth' AND beat IS NOT NULL GROUP BY beat ORDER BY beat`)
	if err != nil {
		return rep, err
	}
	for rows.Next() {
		var beat int16
		var n int64
		if err := rows.Scan(&beat, &n); err != nil {
			rows.Close()
			return rep, err
		}
		rep.ScrollByBeat[strconv.Itoa(int(beat))] = n
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return rep, err
	}
	rows2, err := s.pool.Query(ctx,
		`SELECT coalesce(detail, ''), count(*) FROM site_events WHERE event_type = 'cta_click' GROUP BY detail`)
	if err != nil {
		return rep, err
	}
	defer rows2.Close()
	for rows2.Next() {
		var detail string
		var n int64
		if err := rows2.Scan(&detail, &n); err != nil {
			return rep, err
		}
		rep.CTAClicksByType[detail] = n
	}
	return rep, rows2.Err()
}

// handleAdminFunnel serves the real, current funnel report. Admin-gated like
// every other /admin/* data endpoint (see authAdmin in api.go) — the raw
// event counts are an operator surface, not something to expose publicly.
func (s *Server) handleAdminFunnel(w http.ResponseWriter, r *http.Request) {
	rep, err := s.store.FunnelReport(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, rep)
}
