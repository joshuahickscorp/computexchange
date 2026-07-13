package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

func TestDemoAssetHandlerServesOnlyFixedFlatAllowlist(t *testing.T) {
	root := t.TempDir()
	want := []byte("png-fixture")
	if err := os.WriteFile(filepath.Join(root, "cx-mark-white.png"), want, 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("DEMO_ASSETS_PATH", root)

	req := httptest.NewRequest(http.MethodGet, "/assets/cx-mark-white.png", nil)
	req.SetPathValue("path", "cx-mark-white.png")
	rec := httptest.NewRecorder()
	(&Server{}).handleDemoAsset(rec, req)
	if rec.Code != http.StatusOK || rec.Body.String() != string(want) {
		t.Fatalf("allowlisted asset status=%d body=%q", rec.Code, rec.Body.String())
	}
	if got := rec.Header().Get("Content-Type"); got != "image/png" {
		t.Fatalf("content type=%q, want image/png", got)
	}
	if got := rec.Header().Get("X-Content-Type-Options"); got != "nosniff" {
		t.Fatalf("X-Content-Type-Options=%q, want nosniff", got)
	}

	for _, path := range []string{"../cx-mark-white.png", "nested/cx-mark-white.png", "unknown.png"} {
		req := httptest.NewRequest(http.MethodGet, "/assets/"+path, nil)
		req.SetPathValue("path", path)
		rec := httptest.NewRecorder()
		(&Server{}).handleDemoAsset(rec, req)
		if rec.Code != http.StatusNotFound {
			t.Errorf("path %q status=%d, want 404", path, rec.Code)
		}
	}
}
