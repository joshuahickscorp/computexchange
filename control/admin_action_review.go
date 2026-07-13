package main

import (
	"context"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
)

const (
	// AdminActionReviewDefaultLimit is intentionally smaller than the historical
	// fixed 200-row view. Callers may ask for more, but never more than the bounded
	// maximum below.
	AdminActionReviewDefaultLimit = 100
	AdminActionReviewMaxLimit     = 200

	adminActionReviewCursorVersion = byte(1)
	adminActionReviewCursorBytes   = 1 + 8 + 16 // version + Unix microseconds + UUID
)

var (
	errAdminActionReviewLimit  = errors.New("invalid admin action review limit")
	errAdminActionReviewCursor = errors.New("invalid admin action review cursor")
)

// AdminActionReviewActor is credential-level attribution, not a human-identity
// claim. In particular, shared_credential_only means every holder of the same
// break-glass key is intentionally indistinguishable in this local audit.
//
// Only stable, non-secret database ids are exposed. Raw passkey credential ids,
// public-key material, API-key hashes, and session-token hashes are never selected.
type AdminActionReviewActor struct {
	AuthenticationMode AdminAuthMode         `json:"authentication_mode"`
	PrincipalID        uuid.UUID             `json:"principal_id"`
	SessionID          *uuid.UUID            `json:"session_id,omitempty"`
	AttributionScope   AdminAttributionScope `json:"attribution_scope"`
}

// AdminMoneyAuthorityReview is the typed, secret-free authorization binding.
// External treasury references and the legacy arbitrary JSON detail blob are
// deliberately absent. RequestSHA256 identifies the canonical semantic request;
// it is not a credential hash.
type AdminMoneyAuthorityReview struct {
	IntentVersion    int       `json:"intent_version"`
	RequestSHA256    string    `json:"request_sha256"`
	CorrelationRef   string    `json:"correlation_ref"`
	TargetKind       string    `json:"target_kind"`
	TargetID         uuid.UUID `json:"target_id"`
	FundID           uuid.UUID `json:"fund_id"`
	FundRef          string    `json:"fund_ref"`
	AuthorizationRef string    `json:"authorization_ref,omitempty"`
	AmountCents      int64     `json:"amount_cents"`
	Currency         string    `json:"currency"`
}

// AdminActionReviewItem is the allowlisted review representation. Keep this
// separate from AdminAction: the latter contains the legacy arbitrary `detail`
// JSON and must never be serialized by the secret-safe review endpoint.
type AdminActionReviewItem struct {
	ID            uuid.UUID  `json:"id"`
	CreatedAt     time.Time  `json:"created_at"`
	Kind          string     `json:"kind"`
	TaskID        *uuid.UUID `json:"task_id,omitempty"`
	SupplierID    *uuid.UUID `json:"supplier_id,omitempty"`
	LedgerEntryID *uuid.UUID `json:"ledger_entry_id,omitempty"`
	Reason        string     `json:"reason,omitempty"`
	// legacy_unattributed is an honest migration marker for money actions created
	// before credential provenance existed. Such rows remain reviewable but are
	// never silently upgraded or accepted as authority for a new pool/retry.
	AttributionStatus string                     `json:"attribution_status,omitempty"`
	Actor             *AdminActionReviewActor    `json:"actor,omitempty"`
	Authority         *AdminMoneyAuthorityReview `json:"authority,omitempty"`
}

// AdminActionReviewPage is a stable descending page. NextCursor is opaque to API
// consumers and binds the (created_at,id) of the last visible item, preventing
// equal timestamps from causing duplicates or omissions.
type AdminActionReviewPage struct {
	Items      []AdminActionReviewItem `json:"items"`
	NextCursor string                  `json:"next_cursor,omitempty"`
}

type adminActionReviewCursor struct {
	CreatedAt time.Time
	ID        uuid.UUID
}

func normalizeAdminActionReviewLimit(limit int) (int, error) {
	switch {
	case limit == 0:
		return AdminActionReviewDefaultLimit, nil
	case limit < 0 || limit > AdminActionReviewMaxLimit:
		return 0, fmt.Errorf("%w: must be between 1 and %d", errAdminActionReviewLimit, AdminActionReviewMaxLimit)
	default:
		return limit, nil
	}
}

func encodeAdminActionReviewCursor(createdAt time.Time, id uuid.UUID) (string, error) {
	if createdAt.IsZero() || id == uuid.Nil {
		return "", fmt.Errorf("%w: missing timestamp or id", errAdminActionReviewCursor)
	}
	raw := make([]byte, adminActionReviewCursorBytes)
	raw[0] = adminActionReviewCursorVersion
	binary.BigEndian.PutUint64(raw[1:9], uint64(createdAt.UTC().UnixMicro()))
	copy(raw[9:], id[:])
	return base64.RawURLEncoding.EncodeToString(raw), nil
}

func decodeAdminActionReviewCursor(encoded string) (adminActionReviewCursor, error) {
	if encoded == "" {
		return adminActionReviewCursor{}, fmt.Errorf("%w: empty", errAdminActionReviewCursor)
	}
	raw, err := base64.RawURLEncoding.DecodeString(encoded)
	if err != nil || len(raw) != adminActionReviewCursorBytes {
		return adminActionReviewCursor{}, fmt.Errorf("%w: malformed", errAdminActionReviewCursor)
	}
	// Reject alternate/non-canonical encodings so one cursor has exactly one wire
	// representation (padding, standard-base64 alphabet, and trailing bytes fail).
	if base64.RawURLEncoding.EncodeToString(raw) != encoded || raw[0] != adminActionReviewCursorVersion {
		return adminActionReviewCursor{}, fmt.Errorf("%w: unsupported or non-canonical", errAdminActionReviewCursor)
	}
	id, err := uuid.FromBytes(raw[9:])
	if err != nil || id == uuid.Nil {
		return adminActionReviewCursor{}, fmt.Errorf("%w: missing id", errAdminActionReviewCursor)
	}
	return adminActionReviewCursor{
		CreatedAt: time.UnixMicro(int64(binary.BigEndian.Uint64(raw[1:9]))).UTC(),
		ID:        id,
	}, nil
}

// The select list is an allowlist. Do not replace it with SELECT *: omitted
// columns include detail, actor_label, and any values reachable only through the
// credential, session, API-key, or treasury tables.
const adminActionReviewSelectColumns = `
	id, created_at, kind, task_id, supplier_id, ledger_entry_id, reason,
	actor_mode, actor_principal_id, actor_session_id, attribution_scope,
	intent_version, request_sha256, correlation_ref, target_kind, target_id,
	fund_id, fund_ref, authorization_ref, amount_cents, currency`

const adminActionReviewBaseQuery = `SELECT ` + adminActionReviewSelectColumns + `
	FROM admin_actions`

const adminActionReviewFirstPageQuery = adminActionReviewBaseQuery + `
	ORDER BY created_at DESC, id DESC
	LIMIT $1`

const adminActionReviewAfterCursorQuery = adminActionReviewBaseQuery + `
	WHERE (created_at, id) < ($1::timestamptz, $2::uuid)
	ORDER BY created_at DESC, id DESC
	LIMIT $3`

type adminActionReviewRow struct {
	ID               uuid.UUID
	CreatedAt        time.Time
	Kind             string
	TaskID           *uuid.UUID
	SupplierID       *uuid.UUID
	LedgerEntryID    *uuid.UUID
	Reason           *string
	ActorMode        *string
	ActorPrincipalID *uuid.UUID
	ActorSessionID   *uuid.UUID
	AttributionScope *string
	IntentVersion    *int32
	RequestSHA256    *string
	CorrelationRef   *string
	TargetKind       *string
	TargetID         *uuid.UUID
	FundID           *uuid.UUID
	FundRef          *string
	AuthorizationRef *string
	AmountCents      *int64
	Currency         *string
}

func isMoneyAuthorityAction(kind string) bool {
	return kind == "subsidy_fund_authorized" || kind == "payout_subsidy_authorized"
}

func adminActionReviewItemFromRow(row adminActionReviewRow) (AdminActionReviewItem, error) {
	item := AdminActionReviewItem{
		ID:            row.ID,
		CreatedAt:     row.CreatedAt,
		Kind:          row.Kind,
		TaskID:        row.TaskID,
		SupplierID:    row.SupplierID,
		LedgerEntryID: row.LedgerEntryID,
	}
	if row.Reason != nil {
		item.Reason = *row.Reason
	}

	actorFields := 0
	for _, present := range []bool{
		row.ActorMode != nil,
		row.ActorPrincipalID != nil,
		row.ActorSessionID != nil,
		row.AttributionScope != nil,
	} {
		if present {
			actorFields++
		}
	}
	if actorFields != 0 {
		if row.ActorMode == nil || row.ActorPrincipalID == nil || row.AttributionScope == nil {
			return AdminActionReviewItem{}, fmt.Errorf("admin action %s has a partial actor binding", row.ID)
		}
		actor := AdminActor{
			Mode:             AdminAuthMode(*row.ActorMode),
			PrincipalID:      *row.ActorPrincipalID,
			SessionID:        row.ActorSessionID,
			AttributionScope: AdminAttributionScope(*row.AttributionScope),
		}
		if err := validateAdminActorShape(actor); err != nil {
			return AdminActionReviewItem{}, fmt.Errorf("admin action %s actor: %w", row.ID, err)
		}
		item.Actor = &AdminActionReviewActor{
			AuthenticationMode: actor.Mode,
			PrincipalID:        actor.PrincipalID,
			SessionID:          actor.SessionID,
			AttributionScope:   actor.AttributionScope,
		}
		item.AttributionStatus = "credential_attributed"
	}

	if !isMoneyAuthorityAction(row.Kind) {
		return item, nil
	}
	// Older installations can contain actorless money actions. There is no honest
	// way to infer which passkey/key performed them. Keep them visible and clearly
	// marked; all partially upgraded rows still fail closed below.
	if item.Actor == nil && row.IntentVersion == nil && row.RequestSHA256 == nil &&
		row.CorrelationRef == nil && row.TargetKind == nil && row.TargetID == nil &&
		row.FundID == nil && row.FundRef == nil && row.AuthorizationRef == nil &&
		row.AmountCents == nil && row.Currency == nil {
		item.AttributionStatus = "legacy_unattributed"
		return item, nil
	}
	if item.Actor == nil || row.IntentVersion == nil || row.RequestSHA256 == nil ||
		row.CorrelationRef == nil || row.TargetKind == nil || row.TargetID == nil ||
		row.FundID == nil || row.FundRef == nil || row.AmountCents == nil || row.Currency == nil {
		return AdminActionReviewItem{}, fmt.Errorf("money admin action %s has a partial typed binding", row.ID)
	}
	if *row.IntentVersion != 1 || len(*row.RequestSHA256) != 64 ||
		strings.Trim(*row.RequestSHA256, "0123456789abcdef") != "" ||
		strings.TrimSpace(*row.CorrelationRef) == "" || strings.TrimSpace(*row.FundRef) == "" ||
		*row.TargetID == uuid.Nil || *row.FundID == uuid.Nil || *row.AmountCents <= 0 ||
		*row.Currency != "usd" || strings.TrimSpace(item.Reason) == "" {
		return AdminActionReviewItem{}, fmt.Errorf("money admin action %s has an invalid typed binding", row.ID)
	}
	authorizationRef := ""
	if row.AuthorizationRef != nil {
		authorizationRef = *row.AuthorizationRef
	}
	switch row.Kind {
	case "subsidy_fund_authorized":
		if *row.TargetKind != "subsidy_fund" || authorizationRef != "" {
			return AdminActionReviewItem{}, fmt.Errorf("subsidy fund action %s has the wrong target shape", row.ID)
		}
	case "payout_subsidy_authorized":
		if *row.TargetKind != "supplier_liability" || strings.TrimSpace(authorizationRef) == "" ||
			row.LedgerEntryID == nil || *row.LedgerEntryID != *row.TargetID {
			return AdminActionReviewItem{}, fmt.Errorf("payout subsidy action %s has the wrong target shape", row.ID)
		}
	}
	item.Authority = &AdminMoneyAuthorityReview{
		IntentVersion:    int(*row.IntentVersion),
		RequestSHA256:    *row.RequestSHA256,
		CorrelationRef:   *row.CorrelationRef,
		TargetKind:       *row.TargetKind,
		TargetID:         *row.TargetID,
		FundID:           *row.FundID,
		FundRef:          *row.FundRef,
		AuthorizationRef: authorizationRef,
		AmountCents:      *row.AmountCents,
		Currency:         *row.Currency,
	}
	return item, nil
}

func buildAdminActionReviewPage(rows []AdminActionReviewItem, limit int) (AdminActionReviewPage, error) {
	page := AdminActionReviewPage{Items: make([]AdminActionReviewItem, 0, min(len(rows), limit))}
	if len(rows) <= limit {
		page.Items = append(page.Items, rows...)
		return page, nil
	}
	page.Items = append(page.Items, rows[:limit]...)
	last := page.Items[len(page.Items)-1]
	next, err := encodeAdminActionReviewCursor(last.CreatedAt, last.ID)
	if err != nil {
		return AdminActionReviewPage{}, err
	}
	page.NextCursor = next
	return page, nil
}

// ListAdminActionsPage returns a secret-safe, descending keyset page. cursor is
// the opaque NextCursor from a prior page; an empty cursor starts at the newest
// action. limit=0 selects the default. Invalid limits/cursors fail before a query.
func (s *Store) ListAdminActionsPage(ctx context.Context, limit int, cursor string) (AdminActionReviewPage, error) {
	bounded, err := normalizeAdminActionReviewLimit(limit)
	if err != nil {
		return AdminActionReviewPage{}, err
	}

	query := adminActionReviewFirstPageQuery
	args := []any{bounded + 1}
	if cursor != "" {
		position, err := decodeAdminActionReviewCursor(cursor)
		if err != nil {
			return AdminActionReviewPage{}, err
		}
		query = adminActionReviewAfterCursorQuery
		args = []any{position.CreatedAt, position.ID, bounded + 1}
	}

	dbRows, err := s.pool.Query(ctx, query, args...)
	if err != nil {
		return AdminActionReviewPage{}, err
	}
	defer dbRows.Close()

	items := make([]AdminActionReviewItem, 0, bounded+1)
	for dbRows.Next() {
		var row adminActionReviewRow
		if err := dbRows.Scan(
			&row.ID, &row.CreatedAt, &row.Kind, &row.TaskID, &row.SupplierID,
			&row.LedgerEntryID, &row.Reason, &row.ActorMode, &row.ActorPrincipalID,
			&row.ActorSessionID, &row.AttributionScope, &row.IntentVersion,
			&row.RequestSHA256, &row.CorrelationRef, &row.TargetKind, &row.TargetID,
			&row.FundID, &row.FundRef, &row.AuthorizationRef, &row.AmountCents,
			&row.Currency,
		); err != nil {
			return AdminActionReviewPage{}, err
		}
		item, err := adminActionReviewItemFromRow(row)
		if err != nil {
			return AdminActionReviewPage{}, err
		}
		items = append(items, item)
	}
	if err := dbRows.Err(); err != nil {
		return AdminActionReviewPage{}, err
	}
	return buildAdminActionReviewPage(items, bounded)
}
