package main

import (
	"errors"
	"io"
	"math"
)

var errRemoteResponseTooLarge = errors.New("remote response exceeds configured size limit")

// readBoundedRemoteBody reads one upstream response and reserves a sentinel byte
// beyond the configured ceiling. The sentinel distinguishes an exact-limit body
// from a larger body; callers never decode or log a silently truncated prefix.
func readBoundedRemoteBody(r io.Reader, maxBytes int64) ([]byte, error) {
	if r == nil {
		return nil, errors.New("remote response body is nil")
	}
	if maxBytes <= 0 || maxBytes == math.MaxInt64 {
		return nil, errors.New("remote response size limit is invalid")
	}
	body, err := io.ReadAll(io.LimitReader(r, maxBytes+1))
	if err != nil {
		return nil, err
	}
	if int64(len(body)) > maxBytes {
		return nil, errRemoteResponseTooLarge
	}
	return body, nil
}
