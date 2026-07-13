package main

// webauthn.go — passkey (WebAuthn) login for the /admin operator panel.
//
// The operator registers a device passkey (Touch ID / a security key) once, then
// signs into /admin with it — phishing-resistant, no shared bearer key to paste or
// leak. This is a SELF-CONTAINED admin auth layer:
//   - admin_credentials  stores the operator's registered passkeys (public keys only)
//   - admin_sessions      stores the SHA-256 hash of the cx_admin_ session token
//   - authAdmin (api.go)  accepts a valid cx_admin_ session cookie OR the existing
//                         admin bearer key (kept as BREAK-GLASS so a lost passkey can
//                         never lock the operator out)
//
// Bootstrap: the register ceremonies are authAdmin-gated, so the FIRST passkey is
// registered while authenticated by the admin bearer key (paste it once); after that
// the passkey session authenticates everything, including registering more passkeys.
//
// HONESTY (BLACKHOLE): with no passkey registered, /admin still works via the bearer
// key — nothing is faked or bypassed. WebAuthn crypto is delegated to the audited
// go-webauthn library; we never hand-roll signature verification.

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/go-webauthn/webauthn/webauthn"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

const (
	adminSessionCookie   = "cx_admin"      // holds the raw cx_admin_ session token (httpOnly)
	adminChallengeCookie = "cx_admin_chal" // holds the in-flight WebAuthn SessionData (httpOnly, short-lived)
	adminSessionTTL      = 12 * time.Hour  // a browser admin session; re-auth with the passkey after
	adminChallengeTTL    = 5 * time.Minute // a ceremony must finish quickly
)

// adminUser is the single operator, as the go-webauthn User. A fixed, stable handle
// (the same across ceremonies) is required so credentials stay associated. It carries
// the operator's already-loaded credentials for the ceremony.
type adminUser struct {
	creds []webauthn.Credential
}

func (u *adminUser) WebAuthnID() []byte                         { return []byte("cx-operator") }
func (u *adminUser) WebAuthnName() string                       { return "operator" }
func (u *adminUser) WebAuthnDisplayName() string                { return "Computexchange Operator" }
func (u *adminUser) WebAuthnCredentials() []webauthn.Credential { return u.creds }

// webAuthn builds the relying-party config from the environment. RP ID is the bare
// domain (no scheme/port); origins are the full https:// origins the browser sends.
// Prod: CX_ADMIN_RP_ID=computexchange.net (or SITE_HOST). Dev falls back to localhost.
func (s *Server) webAuthn() (*webauthn.WebAuthn, error) {
	rpID := firstNonEmpty(os.Getenv("CX_ADMIN_RP_ID"), os.Getenv("SITE_HOST"), "localhost")
	var origins []string
	if o := os.Getenv("CX_ADMIN_ORIGIN"); o != "" {
		for _, part := range strings.Split(o, ",") {
			if p := strings.TrimSpace(part); p != "" {
				origins = append(origins, p)
			}
		}
	} else if rpID == "localhost" {
		origins = []string{"http://localhost:8080"}
	} else {
		origins = []string{"https://" + rpID}
	}
	return webauthn.New(&webauthn.Config{
		RPID:          rpID,
		RPDisplayName: "Computexchange",
		RPOrigins:     origins,
	})
}

func firstNonEmpty(vs ...string) string {
	for _, v := range vs {
		if v != "" {
			return v
		}
	}
	return ""
}

// --- Store helpers (admin_credentials + admin_sessions) ----------------------

// loadAdminCredentials returns every non-revoked operator passkey.
func (s *Store) loadAdminCredentials(ctx context.Context) ([]webauthn.Credential, error) {
	rows, err := s.pool.Query(ctx, `SELECT credential FROM admin_credentials WHERE revoked = false`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var creds []webauthn.Credential
	for rows.Next() {
		var raw []byte
		if err := rows.Scan(&raw); err != nil {
			return nil, err
		}
		var c webauthn.Credential
		if err := json.Unmarshal(raw, &c); err != nil {
			return nil, err
		}
		creds = append(creds, c)
	}
	return creds, rows.Err()
}

// countAdminCredentials reports how many non-revoked passkeys are registered
// (drives the UI: show "register" when 0, "sign in" when ≥1).
func (s *Store) countAdminCredentials(ctx context.Context) (int, error) {
	var n int
	err := s.pool.QueryRow(ctx, `SELECT count(*) FROM admin_credentials WHERE revoked = false`).Scan(&n)
	return n, err
}

// saveAdminCredential persists a freshly registered passkey.
func (s *Store) saveAdminCredential(ctx context.Context, c *webauthn.Credential, label string) error {
	raw, err := json.Marshal(c)
	if err != nil {
		return err
	}
	tag, err := s.pool.Exec(ctx,
		`INSERT INTO admin_credentials (credential_id, credential, label)
		 VALUES ($1, $2, $3)
		 ON CONFLICT (credential_id) DO UPDATE SET credential = EXCLUDED.credential
		 WHERE admin_credentials.revoked = false`,
		c.ID, raw, label)
	if err != nil {
		return err
	}
	if tag.RowsAffected() == 0 {
		return errors.New("admin credential was revoked and cannot be re-registered")
	}
	return nil
}

// updateAdminCredential re-persists a credential after a login (its sign_count
// advanced) and stamps last_used_at.
func (s *Store) updateAdminCredential(ctx context.Context, c *webauthn.Credential) error {
	raw, err := json.Marshal(c)
	if err != nil {
		return err
	}
	tag, err := s.pool.Exec(ctx,
		`UPDATE admin_credentials SET credential = $2, last_used_at = now()
		  WHERE credential_id = $1 AND revoked = false`,
		c.ID, raw)
	if err != nil {
		return err
	}
	if tag.RowsAffected() == 0 {
		return errors.New("admin credential is missing or revoked")
	}
	return nil
}

// createAdminSession mints an opaque cx_admin_ token, storing only its hash. The
// session is linked to the exact passkey row that authenticated this login, so a
// later admin action can name a stable, non-secret credential and session id.
func (s *Store) createAdminSession(ctx context.Context, credentialID []byte, ttl time.Duration) (string, error) {
	raw := newSecret("cx_admin_")
	if raw == "" {
		return "", errors.New("admin session: entropy failure")
	}
	var inserted bool
	err := s.pool.QueryRow(ctx, `
		INSERT INTO admin_sessions (token_hash, expires_at, revoked, admin_credential_id)
		SELECT $1, $2, false, cred.id
		  FROM admin_credentials cred
		 WHERE cred.credential_id = $3
		   AND cred.revoked = false
		RETURNING true`, hashKey(raw), time.Now().Add(ttl), credentialID).Scan(&inserted)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", errors.New("admin session: authenticated credential no longer exists")
	}
	if err != nil {
		return "", err
	}
	return raw, nil
}

// LookupAdminSession resolves a live raw session token to its secret-free actor.
// The raw token and its hash never leave this lookup boundary.
func (s *Store) LookupAdminSession(ctx context.Context, raw string) (AdminActor, error) {
	var actor AdminActor
	var sessionID uuid.UUID
	err := s.pool.QueryRow(ctx, `
		SELECT sesh.id, cred.id, COALESCE(NULLIF(cred.label,''), 'passkey')
		  FROM admin_sessions sesh
		  JOIN admin_credentials cred
		    ON cred.id = sesh.admin_credential_id
		 WHERE sesh.token_hash = $1
		   AND cred.revoked = false
		   AND sesh.revoked = false
		   AND sesh.expires_at > now()`, hashKey(raw)).Scan(
		&sessionID, &actor.PrincipalID, &actor.Label)
	if errors.Is(err, pgx.ErrNoRows) {
		return AdminActor{}, errNotFound
	}
	if err != nil {
		return AdminActor{}, err
	}
	actor.Mode = AdminAuthPasskeySession
	actor.SessionID = &sessionID
	actor.AttributionScope = AdminAttributionCredentialOnly
	return actor, nil
}

// adminSessionValid reports whether a raw cx_admin_ token is a live admin session.
func (s *Store) adminSessionValid(ctx context.Context, raw string) bool {
	_, err := s.LookupAdminSession(ctx, raw)
	return err == nil
}

func (s *Store) revokeAdminSession(ctx context.Context, raw string) error {
	_, err := s.pool.Exec(ctx, `UPDATE admin_sessions SET revoked = true WHERE token_hash = $1`, hashKey(raw))
	return err
}

// --- challenge cookie (in-flight SessionData) --------------------------------

func setChallengeCookie(w http.ResponseWriter, r *http.Request, sd *webauthn.SessionData) error {
	raw, err := json.Marshal(sd)
	if err != nil {
		return err
	}
	http.SetCookie(w, &http.Cookie{
		Name:     adminChallengeCookie,
		Value:    base64.RawURLEncoding.EncodeToString(raw),
		Path:     "/admin",
		HttpOnly: true,
		Secure:   isSecure(r),
		SameSite: http.SameSiteStrictMode,
		MaxAge:   int(adminChallengeTTL.Seconds()),
	})
	return nil
}

func readChallengeCookie(r *http.Request) (*webauthn.SessionData, error) {
	c, err := r.Cookie(adminChallengeCookie)
	if err != nil {
		return nil, errors.New("no in-flight challenge (start the ceremony again)")
	}
	raw, err := base64.RawURLEncoding.DecodeString(c.Value)
	if err != nil {
		return nil, err
	}
	var sd webauthn.SessionData
	if err := json.Unmarshal(raw, &sd); err != nil {
		return nil, err
	}
	return &sd, nil
}

func clearCookie(w http.ResponseWriter, r *http.Request, name string) {
	http.SetCookie(w, &http.Cookie{
		Name: name, Value: "", Path: "/admin", HttpOnly: true,
		Secure: isSecure(r), SameSite: http.SameSiteStrictMode, MaxAge: -1,
	})
}

// isSecure reports whether a cookie must be HTTPS-only. Production forces Secure
// even if a proxy header is missing, so an ingress misconfiguration fails closed;
// local development can still use plain HTTP unless TLS or its proxy header is
// present.
func isSecure(r *http.Request) bool {
	if strings.EqualFold(strings.TrimSpace(os.Getenv("CX_ENV")), "production") {
		return true
	}
	if r.TLS != nil {
		return true
	}
	return strings.EqualFold(r.Header.Get("X-Forwarded-Proto"), "https")
}

// --- handlers ----------------------------------------------------------------

// GET /admin/passkey/status — public. Tells the UI whether any passkey is registered
// (so it offers register vs sign-in) and whether THIS browser already holds a valid
// admin session (so it can skip straight to the panel).
func (s *Server) handleAdminPasskeyStatus(w http.ResponseWriter, r *http.Request) {
	n, err := s.store.countAdminCredentials(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "counting credentials: "+err.Error())
		return
	}
	authed := false
	if c, err := r.Cookie(adminSessionCookie); err == nil && c.Value != "" {
		authed = s.store.adminSessionValid(r.Context(), c.Value)
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"registered":    n > 0,
		"credentials":   n,
		"authenticated": authed,
	})
}

// POST /admin/passkey/register/begin — authAdmin-gated (bearer key bootstraps the
// first passkey; a passkey session registers subsequent ones). Returns creation
// options and stashes the challenge.
func (s *Server) handleAdminRegisterBegin(w http.ResponseWriter, r *http.Request) {
	wa, err := s.webAuthn()
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "webauthn config: "+err.Error())
		return
	}
	creds, err := s.store.loadAdminCredentials(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "loading credentials: "+err.Error())
		return
	}
	user := &adminUser{creds: creds}
	// Exclude already-registered credentials so the same authenticator isn't enrolled twice.
	options, sessionData, err := wa.BeginRegistration(user,
		webauthn.WithExclusions(webauthn.Credentials(user.WebAuthnCredentials()).CredentialDescriptors()))
	if err != nil {
		writeErr(w, http.StatusBadRequest, "begin registration: "+err.Error())
		return
	}
	if err := setChallengeCookie(w, r, sessionData); err != nil {
		writeErr(w, http.StatusInternalServerError, "stashing challenge: "+err.Error())
		return
	}
	writeJSON(w, http.StatusOK, options)
}

// POST /admin/passkey/register/finish — authAdmin-gated. Verifies attestation, stores
// the credential.
func (s *Server) handleAdminRegisterFinish(w http.ResponseWriter, r *http.Request) {
	wa, err := s.webAuthn()
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "webauthn config: "+err.Error())
		return
	}
	sd, err := readChallengeCookie(r)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	creds, err := s.store.loadAdminCredentials(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "loading credentials: "+err.Error())
		return
	}
	// The label rides as a query param so it is not part of the signed attestation body.
	label := r.URL.Query().Get("label")
	if label == "" {
		label = "passkey"
	}
	user := &adminUser{creds: creds}
	cred, err := wa.FinishRegistration(user, *sd, r)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "finish registration: "+err.Error())
		return
	}
	if err := s.store.saveAdminCredential(r.Context(), cred, label); err != nil {
		writeErr(w, http.StatusInternalServerError, "saving credential: "+err.Error())
		return
	}
	clearCookie(w, r, adminChallengeCookie)
	writeJSON(w, http.StatusOK, map[string]any{"registered": true, "label": label})
}

// POST /admin/passkey/login/begin — public. Returns assertion options for the
// operator's registered passkeys.
func (s *Server) handleAdminLoginBegin(w http.ResponseWriter, r *http.Request) {
	wa, err := s.webAuthn()
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "webauthn config: "+err.Error())
		return
	}
	creds, err := s.store.loadAdminCredentials(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "loading credentials: "+err.Error())
		return
	}
	if len(creds) == 0 {
		writeErr(w, http.StatusConflict, "no passkey registered yet — register one first (bearer admin key required)")
		return
	}
	user := &adminUser{creds: creds}
	options, sessionData, err := wa.BeginLogin(user)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "begin login: "+err.Error())
		return
	}
	if err := setChallengeCookie(w, r, sessionData); err != nil {
		writeErr(w, http.StatusInternalServerError, "stashing challenge: "+err.Error())
		return
	}
	writeJSON(w, http.StatusOK, options)
}

// POST /admin/passkey/login/finish — public. Verifies the assertion, advances the
// clone-detection counter, and issues the cx_admin_ session cookie.
func (s *Server) handleAdminLoginFinish(w http.ResponseWriter, r *http.Request) {
	wa, err := s.webAuthn()
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "webauthn config: "+err.Error())
		return
	}
	sd, err := readChallengeCookie(r)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	creds, err := s.store.loadAdminCredentials(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "loading credentials: "+err.Error())
		return
	}
	user := &adminUser{creds: creds}
	cred, err := wa.FinishLogin(user, *sd, r)
	if err != nil {
		writeErr(w, http.StatusUnauthorized, "finish login: "+err.Error())
		return
	}
	// A decreasing sign counter means a cloned authenticator — refuse the login.
	if cred.Authenticator.CloneWarning {
		writeErr(w, http.StatusUnauthorized, "authenticator clone detected — login refused")
		return
	}
	if err := s.store.updateAdminCredential(r.Context(), cred); err != nil {
		writeErr(w, http.StatusInternalServerError, "updating credential: "+err.Error())
		return
	}
	token, err := s.store.createAdminSession(r.Context(), cred.ID, adminSessionTTL)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "issuing session: "+err.Error())
		return
	}
	clearCookie(w, r, adminChallengeCookie)
	http.SetCookie(w, &http.Cookie{
		Name:     adminSessionCookie,
		Value:    token,
		Path:     "/",
		HttpOnly: true,
		Secure:   isSecure(r),
		SameSite: http.SameSiteLaxMode,
		MaxAge:   int(adminSessionTTL.Seconds()),
	})
	writeJSON(w, http.StatusOK, map[string]any{"authenticated": true})
}

// POST /admin/passkey/logout — revoke this browser's admin session.
func (s *Server) handleAdminLogout(w http.ResponseWriter, r *http.Request) {
	if c, err := r.Cookie(adminSessionCookie); err == nil && c.Value != "" {
		_ = s.store.revokeAdminSession(r.Context(), c.Value)
	}
	clearCookie(w, r, adminSessionCookie)
	// Path "/" cookie clear (the session cookie is Path=/).
	http.SetCookie(w, &http.Cookie{Name: adminSessionCookie, Value: "", Path: "/", HttpOnly: true,
		Secure: isSecure(r), SameSite: http.SameSiteLaxMode, MaxAge: -1})
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

// lookupAdminSessionActor is the cookie path authAdmin consults before the bearer
// key. Returning the actor together with validity prevents a second lookup and
// preserves passkey-first attribution when a request carries both credentials.
func (s *Server) lookupAdminSessionActor(r *http.Request) (AdminActor, bool) {
	c, err := r.Cookie(adminSessionCookie)
	if err != nil || c.Value == "" {
		return AdminActor{}, false
	}
	actor, err := s.store.LookupAdminSession(r.Context(), c.Value)
	return actor, err == nil
}

// adminSessionSweep deletes expired/revoked admin sessions (called off the ticker).
func (s *Store) adminSessionSweep(ctx context.Context) error {
	_, err := s.pool.Exec(ctx,
		`DELETE FROM admin_sessions WHERE revoked = true OR expires_at < now()`)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return fmt.Errorf("admin session sweep: %w", err)
	}
	return nil
}
