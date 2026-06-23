package main

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"io"
	"os"
	"strings"

	"github.com/google/uuid"
)

// crypto.go — at-rest token sealing (AES-256-GCM) and OAuth-state signing (HMAC).
// Both degrade HONESTLY: with no env secret they pass values through with an
// explicit marker (so dev behavior is unchanged and "this is unencrypted" is
// visible in the DB), and they harden the instant the secret is set. No silent
// half-security (BLACKHOLE: surface every failure).

// tokenKey derives a 32-byte AES key from CX_TOKEN_KEY (any length), or nil.
func tokenKey() []byte {
	k := os.Getenv("CX_TOKEN_KEY")
	if k == "" {
		return nil
	}
	sum := sha256.Sum256([]byte(k))
	return sum[:]
}

// sealToken AES-256-GCM-encrypts a secret for storage. With no key it returns the
// value tagged `plain:` so reads still work and the unencrypted state is visible
// (a KMS-backed key is the production step).
func sealToken(plaintext string) string {
	key := tokenKey()
	if key == nil {
		return "plain:" + plaintext
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return "plain:" + plaintext
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return "plain:" + plaintext
	}
	nonce := make([]byte, gcm.NonceSize())
	if _, err := io.ReadFull(rand.Reader, nonce); err != nil {
		return "plain:" + plaintext
	}
	ct := gcm.Seal(nonce, nonce, []byte(plaintext), nil)
	return "enc:" + base64.RawStdEncoding.EncodeToString(ct)
}

// openToken reverses sealToken, handling both markers. A sealed value with no key
// returns "" (the caller then errors honestly rather than using a bad token).
func openToken(stored string) string {
	if strings.HasPrefix(stored, "plain:") {
		return strings.TrimPrefix(stored, "plain:")
	}
	if !strings.HasPrefix(stored, "enc:") {
		return stored
	}
	key := tokenKey()
	if key == nil {
		return ""
	}
	raw, err := base64.RawStdEncoding.DecodeString(strings.TrimPrefix(stored, "enc:"))
	if err != nil {
		return ""
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return ""
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil || len(raw) < gcm.NonceSize() {
		return ""
	}
	nonce, ct := raw[:gcm.NonceSize()], raw[gcm.NonceSize():]
	pt, err := gcm.Open(nil, nonce, ct, nil)
	if err != nil {
		return ""
	}
	return string(pt)
}

// newSecret returns a cryptographically-random URL-safe credential with the given
// prefix (32 bytes of entropy). Used to mint worker tokens; only the hash of the
// result is ever stored. Returns "" on the (practically impossible) entropy failure
// so the caller fails honestly rather than minting a guessable token.
func newSecret(prefix string) string {
	b := make([]byte, 32)
	if _, err := io.ReadFull(rand.Reader, b); err != nil {
		return ""
	}
	return prefix + base64.RawURLEncoding.EncodeToString(b)
}

// signState binds a buyer id into an OAuth state value. With CX_STATE_SECRET it is
// HMAC-signed (CSRF-resistant); without, it is the bare id (unchanged dev behavior)
// so connect still works before the secret is set.
func signState(buyerID uuid.UUID) string {
	secret := os.Getenv("CX_STATE_SECRET")
	if secret == "" {
		return buyerID.String()
	}
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(buyerID.String()))
	return buyerID.String() + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

// verifyState recovers the buyer id from a state value, checking the HMAC when
// CX_STATE_SECRET is set. ok=false on tamper or format error.
func verifyState(state string) (uuid.UUID, bool) {
	secret := os.Getenv("CX_STATE_SECRET")
	if secret == "" {
		id, err := uuid.Parse(state)
		return id, err == nil
	}
	parts := strings.SplitN(state, ".", 2)
	if len(parts) != 2 {
		return uuid.UUID{}, false
	}
	id, err := uuid.Parse(parts[0])
	if err != nil {
		return uuid.UUID{}, false
	}
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(parts[0]))
	want := base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
	if !hmac.Equal([]byte(want), []byte(parts[1])) {
		return uuid.UUID{}, false
	}
	return id, true
}
