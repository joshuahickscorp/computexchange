package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"golang.org/x/crypto/bcrypt"
)

// accounts.go — self-serve buyer accounts, password auth, and the sandbox lane.
//
// Until now a buyer_id was a free-floating UUID the seed minted; there was no way
// to sign UP. This adds the account of record (buyers: UNIQUE email + bcrypt
// password_hash) and the two public handlers a frontend drives:
//
//   POST /v1/signup {"email","password"} -> 201 {"buyer_id","token","email"}
//   POST /v1/login  {"email","password"} -> 200 {"buyer_id","token"} | 401
//
// AUTH MODEL (why opaque sessions, not JWT): tokens are opaque random secrets
// (cx_sess_…), hashed at rest exactly like api_keys/worker_tokens — only the
// SHA-256 hash touches the DB, so a full DB read can never leak a live credential,
// and revocation/expiry are a row update (a stateless JWT cannot be revoked before
// it expires). authBuyer is extended to accept a cx_sess_ bearer as well as an api
// key, so the frontend stores the token as its cx_key and every existing buyer
// route works unchanged. No JWT-without-verification, no plaintext secret in logs.
//
// SANDBOX LANE: a new buyer is minted a cx_test_ key AND a small free-credit grant
// (buyers.free_credit_usd) so they can run real jobs before adding a card. The 402
// submit gate (createJob) is extended to EXEMPT spend up to the free credit, then
// require a card honestly once it is exhausted (BLACKHOLE: an honest boundary, not
// a silent free ride).

// bcryptCost is the work factor for password hashing. >= 12 per the mandate; 12 is
// the modern default-safe floor (a few hundred ms/verify), tunable up as hardware
// improves without a schema change (the cost is encoded in the hash).
const bcryptCost = 12

// sandboxFreeCreditUSD is the sandbox grant a new buyer receives so they can run
// jobs before adding a card. DEFAULT 0 (OFF): an unverified signup grants no free
// compute, so the lane cannot be Sybil-farmed for real supplier-paid work. An
// operator who has email verification (or another anti-abuse gate) in place opts in
// by setting CX_SANDBOX_CREDIT_USD to a small positive amount. When enabled, a grant
// is still hard-bounded per buyer: BuyerFreeCreditRemaining reserves in-flight job
// estimates, and the submit gate caps each cardless job's MaxUSD at the remainder.
const sandboxFreeCreditUSD = 0.0

// sessionTTL bounds an issued session token. A login mints a fresh token; the old
// one expires on its own. 30 days balances "do not re-login constantly" against a
// bounded blast radius for a leaked token (revocation is also available).
const sessionTTL = 30 * 24 * time.Hour

// --- store layer: accounts + sessions ---

// CreateBuyerAccount creates a buyer with a UNIQUE email and a bcrypt password
// hash, seeding the sandbox free-credit grant in the same row. Returns errEmailTaken
// on a duplicate email (the handler maps it to 409) so signup is honest about a
// collision rather than silently overwriting. The password is hashed here and the
// PLAINTEXT never leaves this function (never stored, never logged).
func (s *Store) CreateBuyerAccount(ctx context.Context, email, password string, freeCreditUSD float64) (uuid.UUID, error) {
	hash, err := bcrypt.GenerateFromPassword([]byte(password), bcryptCost)
	if err != nil {
		return uuid.Nil, err
	}
	var id uuid.UUID
	err = s.pool.QueryRow(ctx,
		`INSERT INTO buyers (email, password_hash, free_credit_usd)
		 VALUES (lower($1), $2, $3)
		 RETURNING id`,
		email, string(hash), freeCreditUSD,
	).Scan(&id)
	if err != nil {
		if isUniqueViolation(err) {
			return uuid.Nil, errEmailTaken
		}
		return uuid.Nil, err
	}
	return id, nil
}

// VerifyBuyerPassword looks up a buyer by email and constant-time-verifies the
// password against the stored bcrypt hash. Returns errNotFound when no such email
// OR the password is wrong — the handler turns BOTH into the same 401 so the
// response never reveals whether an email is registered (no user enumeration).
// bcrypt.CompareHashAndPassword is itself constant-time over the hash compare.
func (s *Store) VerifyBuyerPassword(ctx context.Context, email, password string) (uuid.UUID, error) {
	var (
		id   uuid.UUID
		hash *string
	)
	err := s.pool.QueryRow(ctx,
		`SELECT id, password_hash FROM buyers WHERE email = lower($1)`, email,
	).Scan(&id, &hash)
	if errors.Is(err, pgx.ErrNoRows) {
		// Run a dummy compare anyway so a missing email and a wrong password take
		// the same time — defeats timing-based user enumeration.
		_ = bcrypt.CompareHashAndPassword([]byte("$2a$12$"+strings.Repeat("x", 53)), []byte(password))
		return uuid.Nil, errNotFound
	}
	if err != nil {
		return uuid.Nil, err
	}
	if hash == nil {
		// Account exists but has no password (seed / API-key-only): cannot log in.
		return uuid.Nil, errNotFound
	}
	if err := bcrypt.CompareHashAndPassword([]byte(*hash), []byte(password)); err != nil {
		return uuid.Nil, errNotFound
	}
	return id, nil
}

// CreateSession mints an opaque cx_sess_ token for a buyer, storing ONLY its
// SHA-256 hash plus an expiry, and returns the raw token once (unrecoverable
// afterwards). Mirrors CreateWorkerToken/CreateAPIKey: the raw value never touches
// the DB, so a DB read can never leak a live session.
func (s *Store) CreateSession(ctx context.Context, buyerID uuid.UUID, ttl time.Duration) (string, error) {
	raw := newSecret("cx_sess_")
	if raw == "" {
		return "", errors.New("session token: entropy failure")
	}
	_, err := s.pool.Exec(ctx,
		`INSERT INTO sessions (token_hash, buyer_id, expires_at, revoked)
		 VALUES ($1, $2, $3, false)`,
		hashKey(raw), buyerID, time.Now().Add(ttl),
	)
	if err != nil {
		return "", err
	}
	return raw, nil
}

// LookupSession resolves a session bearer token to its buyer. errNotFound when the
// token is unknown, revoked, or expired — authBuyer turns that into a 401. Buyers
// authenticated by a session are never admin (admin is api-key-only by design).
func (s *Store) LookupSession(ctx context.Context, rawToken string) (AuthResult, error) {
	var r AuthResult
	err := s.pool.QueryRow(ctx,
		`SELECT buyer_id FROM sessions
		 WHERE token_hash = $1 AND revoked = false AND expires_at > now()`,
		hashKey(rawToken),
	).Scan(&r.BuyerID)
	if errors.Is(err, pgx.ErrNoRows) {
		return r, errNotFound
	}
	return r, err
}

// BuyerFreeCreditRemaining returns how much of a buyer's sandbox grant is still
// unspent: free_credit_usd minus the buyer's realized spend (the sum of
// buyer_charge debits, stored negative, so we negate). Never returns < 0. A buyer
// with no account row (e.g. a seeded/legacy buyer_id) has no grant → 0, so the 402
// gate behaves exactly as before for them.
func (s *Store) BuyerFreeCreditRemaining(ctx context.Context, buyerID uuid.UUID) (float64, error) {
	// One query, all NUMERIC arithmetic (no float drift): remaining = grant
	//   − realized spend (buyer_charge debits, stored negative → negate)
	//   − in-flight RESERVATIONS (estimated_usd of this buyer's not-yet-terminal jobs).
	// Reserving in-flight estimates closes the submit→charge TOCTOU: a second concurrent
	// submit sees the first job's estimate already deducted, so the per-buyer free pool
	// cannot be overspent across simultaneous submits. Conservative (estimate ≥ realized),
	// which errs toward rejecting, never toward free overspend. GREATEST clamps at 0.
	// No buyers row (seeded/legacy buyer_id) → no grant → 0, so the 402 gate is unchanged.
	var remaining float64
	err := s.pool.QueryRow(ctx,
		`SELECT GREATEST(
		          b.free_credit_usd
		          - COALESCE((SELECT -SUM(amount_usd) FROM ledger_entries
		                       WHERE buyer_id = b.id AND kind = 'buyer_charge'), 0)
		          - COALESCE((SELECT SUM(estimated_usd) FROM jobs
		                       WHERE buyer_id = b.id AND status IN ('queued','running','verifying')), 0),
		          0)::float8
		   FROM buyers b WHERE b.id = $1`, buyerID,
	).Scan(&remaining)
	if errors.Is(err, pgx.ErrNoRows) {
		return 0, nil
	}
	return remaining, err
}

// errEmailTaken is returned by CreateBuyerAccount on a duplicate email (UNIQUE
// violation), so signup can answer 409 instead of a generic 500.
var errEmailTaken = errors.New("email already registered")

// isUniqueViolation reports whether err is a Postgres unique-constraint violation
// (SQLSTATE 23505), used to distinguish a duplicate email from a real DB error.
func isUniqueViolation(err error) bool {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		return pgErr.Code == "23505"
	}
	return false
}

// --- HTTP handlers ---

// signupRequest is the POST /v1/signup body.
type signupRequest struct {
	Email    string `json:"email"`
	Password string `json:"password"`
}

// handleSignup creates a buyer account (bcrypt the password), mints a sandbox
// cx_test_ key + a free-credit grant, issues a session token, and returns 201.
// The returned token is what the frontend stores as its cx_key (authBuyer accepts
// it as a bearer). The sandbox key is also returned so a CLI/SDK caller has a
// long-lived credential without the session. Never logs the password.
func (s *Server) handleSignup(w http.ResponseWriter, r *http.Request) {
	var req signupRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid signup json: "+err.Error())
		return
	}
	email := normalizeEmail(req.Email)
	if !looksLikeEmail(email) {
		writeErr(w, http.StatusBadRequest, "a valid email is required")
		return
	}
	if len(req.Password) < 8 {
		writeErr(w, http.StatusBadRequest, "password must be at least 8 characters")
		return
	}

	grant := sandboxFreeCreditUSD
	if v := envFloat("CX_SANDBOX_CREDIT_USD", grant); v >= 0 {
		grant = v
	}
	buyerID, err := s.store.CreateBuyerAccount(r.Context(), email, req.Password, grant)
	if errors.Is(err, errEmailTaken) {
		writeErr(w, http.StatusConflict, "email already registered")
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "creating account: "+err.Error())
		return
	}

	// Sandbox lane: a cx_test_ key so the buyer can run jobs immediately. The grant
	// already lives on the buyer row (the 402 gate reads it); the key just gives them
	// a credential. A mint failure is non-fatal to signup — the session token below
	// still authenticates them — but is surfaced in the response so it is not silent.
	_, sandboxKey, _, kerr := s.store.CreateAPIKey(r.Context(), buyerID, "sandbox", true)

	token, err := s.store.CreateSession(r.Context(), buyerID, sessionTTL)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "issuing session: "+err.Error())
		return
	}

	resp := map[string]any{
		"buyer_id":        buyerID,
		"token":           token,
		"email":           email,
		"free_credit_usd": grant,
	}
	if kerr == nil {
		resp["sandbox_key"] = sandboxKey // cx_test_… · revealed once, for CLI/SDK use
	} else {
		resp["sandbox_key_error"] = kerr.Error() // honest: the grant stands, the key mint did not
	}
	writeJSON(w, http.StatusCreated, resp)
}

// handleLogin verifies an email+password and issues a fresh session token. A bad
// email OR a bad password is the SAME 401 (no user enumeration). 200 on success.
func (s *Server) handleLogin(w http.ResponseWriter, r *http.Request) {
	var req signupRequest // same shape: {email, password}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid login json: "+err.Error())
		return
	}
	email := normalizeEmail(req.Email)
	buyerID, err := s.store.VerifyBuyerPassword(r.Context(), email, req.Password)
	if errors.Is(err, errNotFound) {
		writeErr(w, http.StatusUnauthorized, "invalid email or password")
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "verifying credentials: "+err.Error())
		return
	}
	token, err := s.store.CreateSession(r.Context(), buyerID, sessionTTL)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "issuing session: "+err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"buyer_id": buyerID, "token": token})
}

// --- small helpers ---

// normalizeEmail trims and lowercases an email for case-insensitive uniqueness.
func normalizeEmail(s string) string { return strings.ToLower(strings.TrimSpace(s)) }

// envFloat reads a float env var, falling back to def when unset/unparseable.
func envFloat(name string, def float64) float64 {
	if v := strings.TrimSpace(os.Getenv(name)); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			return f
		}
	}
	return def
}

// looksLikeEmail is a deliberately minimal sanity check (exactly one @, with a
// non-empty local part and a dotted domain). Real deliverability is proven by a
// verification email, not a regex; this only rejects obvious garbage at the door.
func looksLikeEmail(s string) bool {
	at := strings.IndexByte(s, '@')
	if at <= 0 || at != strings.LastIndexByte(s, '@') {
		return false
	}
	domain := s[at+1:]
	return strings.Contains(domain, ".") && !strings.HasPrefix(domain, ".") && !strings.HasSuffix(domain, ".")
}
