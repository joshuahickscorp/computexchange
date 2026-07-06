package main

import (
	"context"
	"encoding/json"
	"net/http"
)

// alpha_request.go — the public site's alpha-access capture (docs/
// CREED_AND_PATH_TO_TEN.md, "Public site & conversion" 4→5). Before this, the
// release beat said "ask for alpha access" with no mechanism to ask through —
// this is that mechanism: unauthenticated (a prospective buyer/supplier has no
// account yet), rate-limited by the same global per-IP limiter every other
// route gets (see Routes() in api.go), and it fails loudly rather than
// pretending to capture something it didn't.

type alphaRequestBody struct {
	Email string `json:"email"`
	Role  string `json:"role"` // "buyer" | "supplier" | "" — which CTA was clicked
	Note  string `json:"note"`
}

const (
	alphaRequestNoteMaxLen = 2000
	alphaRoleMaxLen        = 32
)

// handleAlphaRequest records a real alpha-access request. Same email-validation
// helper as signup (normalizeEmail/looksLikeEmail) so "a working email" is the
// only real requirement — this is a lead capture, not an account.
func (s *Server) handleAlphaRequest(w http.ResponseWriter, r *http.Request) {
	var req alphaRequestBody
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid alpha-request json: "+err.Error())
		return
	}
	email := normalizeEmail(req.Email)
	if !looksLikeEmail(email) {
		writeErr(w, http.StatusBadRequest, "a valid email is required")
		return
	}
	role := req.Role
	if len(role) > alphaRoleMaxLen {
		role = role[:alphaRoleMaxLen]
	}
	note := req.Note
	if len(note) > alphaRequestNoteMaxLen {
		note = note[:alphaRequestNoteMaxLen]
	}
	if err := s.store.CreateAlphaRequest(r.Context(), email, role, note, clientIP(r)); err != nil {
		writeErr(w, http.StatusInternalServerError, "recording alpha request: "+err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, map[string]bool{"ok": true})
}

// CreateAlphaRequest persists one alpha-access lead. Never rejects a duplicate
// email — a prospective buyer and a prospective supplier from the same address
// are two distinct, equally real signals, and a stranger re-submitting isn't an
// error condition worth surfacing to them.
func (s *Store) CreateAlphaRequest(ctx context.Context, email, role, note, sourceIP string) error {
	_, err := s.pool.Exec(ctx,
		`INSERT INTO alpha_requests (email, role, note, source_ip) VALUES ($1, $2, $3, $4)`,
		email, role, note, sourceIP,
	)
	return err
}
