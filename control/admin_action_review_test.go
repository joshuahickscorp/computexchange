package main

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
)

func TestAdminActionReviewLimitIsBounded(t *testing.T) {
	tests := []struct {
		input   int
		want    int
		wantErr bool
	}{
		{input: 0, want: AdminActionReviewDefaultLimit},
		{input: 1, want: 1},
		{input: AdminActionReviewMaxLimit, want: AdminActionReviewMaxLimit},
		{input: -1, wantErr: true},
		{input: AdminActionReviewMaxLimit + 1, wantErr: true},
	}
	for _, tc := range tests {
		got, err := normalizeAdminActionReviewLimit(tc.input)
		if tc.wantErr {
			if !errors.Is(err, errAdminActionReviewLimit) {
				t.Fatalf("limit %d error=%v, want %v", tc.input, err, errAdminActionReviewLimit)
			}
			continue
		}
		if err != nil || got != tc.want {
			t.Fatalf("limit %d = %d,%v; want %d,nil", tc.input, got, err, tc.want)
		}
	}
}

func TestAdminActionReviewCursorRoundTripsExactKeyset(t *testing.T) {
	id := uuid.New()
	inputTime := time.Date(2026, 7, 11, 19, 2, 3, 456789321, time.FixedZone("EDT", -4*60*60))
	encoded, err := encodeAdminActionReviewCursor(inputTime, id)
	if err != nil {
		t.Fatal(err)
	}
	if strings.ContainsAny(encoded, "+/=") {
		t.Fatalf("cursor is not unpadded URL-safe base64: %q", encoded)
	}
	decoded, err := decodeAdminActionReviewCursor(encoded)
	if err != nil {
		t.Fatal(err)
	}
	wantTime := time.UnixMicro(inputTime.UnixMicro()).UTC()
	if !decoded.CreatedAt.Equal(wantTime) || decoded.ID != id {
		t.Fatalf("decoded cursor=%+v, want time=%s id=%s", decoded, wantTime, id)
	}
}

func TestAdminActionReviewCursorRejectsMalformedAndUnsupported(t *testing.T) {
	valid, err := encodeAdminActionReviewCursor(time.Now(), uuid.New())
	if err != nil {
		t.Fatal(err)
	}
	raw, err := base64.RawURLEncoding.DecodeString(valid)
	if err != nil {
		t.Fatal(err)
	}
	wrongVersion := append([]byte(nil), raw...)
	wrongVersion[0]++
	zeroID := append([]byte(nil), raw...)
	for i := len(zeroID) - 16; i < len(zeroID); i++ {
		zeroID[i] = 0
	}
	tests := []string{
		"",
		"not*base64",
		valid + "=",
		base64.RawURLEncoding.EncodeToString(raw[:len(raw)-1]),
		base64.RawURLEncoding.EncodeToString(append(raw, 0)),
		base64.RawURLEncoding.EncodeToString(wrongVersion),
		base64.RawURLEncoding.EncodeToString(zeroID),
	}
	for _, encoded := range tests {
		if _, err := decodeAdminActionReviewCursor(encoded); !errors.Is(err, errAdminActionReviewCursor) {
			t.Fatalf("cursor %q error=%v, want %v", encoded, err, errAdminActionReviewCursor)
		}
	}
}

func TestAdminActionReviewPageUsesLastVisibleCompositeKey(t *testing.T) {
	stamp := time.Date(2026, 7, 11, 20, 0, 0, 0, time.UTC)
	items := []AdminActionReviewItem{
		{ID: uuid.MustParse("ffffffff-ffff-ffff-ffff-ffffffffffff"), CreatedAt: stamp, Kind: "first"},
		{ID: uuid.MustParse("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"), CreatedAt: stamp, Kind: "second"},
		{ID: uuid.MustParse("11111111-1111-1111-1111-111111111111"), CreatedAt: stamp, Kind: "lookahead"},
	}
	page, err := buildAdminActionReviewPage(items, 2)
	if err != nil {
		t.Fatal(err)
	}
	if len(page.Items) != 2 || page.Items[1].Kind != "second" || page.NextCursor == "" {
		t.Fatalf("page=%+v, want two visible items and a cursor", page)
	}
	position, err := decodeAdminActionReviewCursor(page.NextCursor)
	if err != nil {
		t.Fatal(err)
	}
	if !position.CreatedAt.Equal(stamp) || position.ID != items[1].ID {
		t.Fatalf("cursor position=%+v, want last visible item %+v", position, items[1])
	}
	final, err := buildAdminActionReviewPage(items[:2], 2)
	if err != nil {
		t.Fatal(err)
	}
	if final.NextCursor != "" || len(final.Items) != 2 {
		t.Fatalf("final page=%+v, want no next cursor", final)
	}
}

func TestAdminActionReviewDTOAndSelectExcludeSecretBearingFields(t *testing.T) {
	sessionID := uuid.New()
	item := AdminActionReviewItem{
		ID:        uuid.New(),
		CreatedAt: time.Now().UTC(),
		Kind:      "subsidy_fund_authorized",
		Reason:    "documented make-good",
		Actor: &AdminActionReviewActor{
			AuthenticationMode: AdminAuthPasskeySession,
			PrincipalID:        uuid.New(),
			SessionID:          &sessionID,
			AttributionScope:   AdminAttributionCredentialOnly,
		},
		Authority: &AdminMoneyAuthorityReview{
			IntentVersion:  1,
			RequestSHA256:  strings.Repeat("a", 64),
			CorrelationRef: uuid.NewString(),
			TargetKind:     "subsidy_fund",
			TargetID:       uuid.New(),
			FundID:         uuid.New(),
			FundRef:        "make-good-2026-07",
			AmountCents:    500,
			Currency:       "usd",
		},
	}
	payload, err := json.Marshal(AdminActionReviewPage{Items: []AdminActionReviewItem{item}})
	if err != nil {
		t.Fatal(err)
	}
	jsonText := string(payload)
	for _, required := range []string{`"authentication_mode"`, `"principal_id"`, `"request_sha256"`, `"amount_cents"`} {
		if !strings.Contains(jsonText, required) {
			t.Fatalf("review JSON missing %s: %s", required, jsonText)
		}
	}
	for _, forbidden := range []string{
		`"detail"`, `"external_treasury_ref"`, `"key_hash"`, `"token_hash"`,
		`"credential_id"`, `"credential"`, `"actor_label"`,
	} {
		if strings.Contains(jsonText, forbidden) {
			t.Fatalf("review JSON exposed forbidden field %s: %s", forbidden, jsonText)
		}
	}

	query := strings.ToLower(adminActionReviewBaseQuery)
	for _, forbidden := range []string{
		"detail", "external_treasury_ref", "key_hash", "token_hash",
		"credential_id", "credential", "actor_label",
	} {
		if strings.Contains(query, forbidden) {
			t.Fatalf("review query selects forbidden field %q: %s", forbidden, query)
		}
	}
	if strings.Contains(query, "select *") {
		t.Fatalf("review query must use an explicit allowlist: %s", query)
	}
	if !strings.Contains(adminActionReviewAfterCursorQuery,
		"(created_at, id) < ($1::timestamptz, $2::uuid)") ||
		!strings.Contains(adminActionReviewAfterCursorQuery,
			"ORDER BY created_at DESC, id DESC") {
		t.Fatalf("cursor query is not a stable composite keyset: %s", adminActionReviewAfterCursorQuery)
	}
}

func TestAdminActionReviewRejectsPartialOrInvalidTypedMoneyRows(t *testing.T) {
	principalID := uuid.New()
	mode := string(AdminAuthBreakGlassAPIKey)
	scope := string(AdminAttributionSharedCredentialOnly)
	reason := "bounded make-good"
	version := int32(1)
	digest := strings.Repeat("a", 64)
	correlation := uuid.NewString()
	targetKind := "subsidy_fund"
	targetID := uuid.New()
	fundID := targetID
	fundRef := "fund-review-test"
	amount := int64(100)
	currency := "usd"
	valid := adminActionReviewRow{
		ID:               uuid.New(),
		CreatedAt:        time.Now().UTC(),
		Kind:             "subsidy_fund_authorized",
		Reason:           &reason,
		ActorMode:        &mode,
		ActorPrincipalID: &principalID,
		AttributionScope: &scope,
		IntentVersion:    &version,
		RequestSHA256:    &digest,
		CorrelationRef:   &correlation,
		TargetKind:       &targetKind,
		TargetID:         &targetID,
		FundID:           &fundID,
		FundRef:          &fundRef,
		AmountCents:      &amount,
		Currency:         &currency,
	}
	item, err := adminActionReviewItemFromRow(valid)
	if err != nil || item.Actor == nil || item.Authority == nil {
		t.Fatalf("valid row item=%+v err=%v", item, err)
	}

	partialActor := valid
	partialActor.ActorPrincipalID = nil
	if _, err := adminActionReviewItemFromRow(partialActor); err == nil {
		t.Fatal("partial actor binding was accepted")
	}
	invalidDigest := valid
	badDigest := strings.Repeat("g", 64)
	invalidDigest.RequestSHA256 = &badDigest
	if _, err := adminActionReviewItemFromRow(invalidDigest); err == nil {
		t.Fatal("invalid request digest was accepted")
	}
	partialAuthority := valid
	partialAuthority.AmountCents = nil
	if _, err := adminActionReviewItemFromRow(partialAuthority); err == nil {
		t.Fatal("partial money authority binding was accepted")
	}

	legacy := adminActionReviewRow{
		ID: uuid.New(), CreatedAt: time.Now().UTC(),
		Kind: "subsidy_fund_authorized", Reason: &reason,
	}
	legacyItem, err := adminActionReviewItemFromRow(legacy)
	if err != nil || legacyItem.AttributionStatus != "legacy_unattributed" ||
		legacyItem.Actor != nil || legacyItem.Authority != nil {
		t.Fatalf("legacy money row was hidden or upgraded: item=%+v err=%v", legacyItem, err)
	}
}
