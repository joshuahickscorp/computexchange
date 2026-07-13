package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestControlBuildInfoNeverFabricatesRelease(t *testing.T) {
	oldVersion, oldCommit, oldDate := controlVersion, controlCommit, controlBuildDate
	t.Cleanup(func() {
		controlVersion, controlCommit, controlBuildDate = oldVersion, oldCommit, oldDate
	})
	controlVersion = "dev"
	controlCommit = "unknown"
	controlBuildDate = "unknown"
	got := currentControlBuildInfo()
	if got.Version != "dev" || got.BuildDate != "unknown" {
		t.Fatalf("development build fabricated release identity: %+v", got)
	}
	if got.GoVersion == "" || got.Platform == "" {
		t.Fatalf("runtime identity missing: %+v", got)
	}
}

func TestControlBuildInfoUsesInjectedReleaseIdentity(t *testing.T) {
	oldVersion, oldCommit, oldDate := controlVersion, controlCommit, controlBuildDate
	t.Cleanup(func() {
		controlVersion, controlCommit, controlBuildDate = oldVersion, oldCommit, oldDate
	})
	controlVersion = "v1.2.3"
	controlCommit = "0123456789012345678901234567890123456789"
	controlBuildDate = "2026-07-10T12:00:00Z"
	got := currentControlBuildInfo()
	if got.Version != controlVersion || got.Commit != controlCommit || got.BuildDate != controlBuildDate {
		t.Fatalf("injected identity was not preserved: %+v", got)
	}
}

func TestVersionEndpointExposesBuildIdentityWithoutDependencies(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/version", nil)
	rec := httptest.NewRecorder()
	(&Server{}).handleVersion(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("GET /version status = %d, want 200", rec.Code)
	}
	var got ControlBuildInfo
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("decode build identity: %v (%s)", err, rec.Body.String())
	}
	if got.Version == "" || got.Commit == "" || got.GoVersion == "" || got.Platform == "" {
		t.Fatalf("incomplete build identity: %+v", got)
	}
}
