package main

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"io"
	"os"
	"strings"
)

// crypto.go — at-rest token sealing (AES-256-GCM) and random credential minting.
// Token sealing degrades HONESTLY: with no env secret it passes values through
// with an explicit marker (so dev behavior is unchanged and "this is unencrypted"
// is visible in the DB). OAuth state is deliberately not signed here: the connector
// uses random, hashed, expiring, single-use database records instead (intake.go).

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
// value tagged `plain:` so local development remains explicit. Once a key is
// configured, any cipher or entropy failure returns an empty value: encryption
// failures must never downgrade a production secret back to plaintext.
func sealToken(plaintext string) string {
	key := tokenKey()
	if key == nil {
		return "plain:" + plaintext
	}
	return sealTokenWithReader(plaintext, key, rand.Reader)
}

func sealTokenWithReader(plaintext string, key []byte, random io.Reader) string {
	block, err := aes.NewCipher(key)
	if err != nil {
		return ""
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return ""
	}
	nonce := make([]byte, gcm.NonceSize())
	if _, err := io.ReadFull(random, nonce); err != nil {
		return ""
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
