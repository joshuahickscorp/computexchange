package main

import (
	"context"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// AdminAuthMode is the authentication ceremony that established an admin
// request. It is deliberately a closed set: an audit row must never collapse a
// passkey session and a shared break-glass credential into the same anonymous
// "admin" identity.
type AdminAuthMode string

const (
	AdminAuthPasskeySession   AdminAuthMode = "passkey_session"
	AdminAuthBreakGlassAPIKey AdminAuthMode = "break_glass_api_key"
)

// AdminAttributionScope states the strongest identity claim the credential can
// support. A passkey identifies one registered authenticator. A break-glass API
// key may be held by more than one person, so it can identify only that shared
// credential, never a particular human operator.
type AdminAttributionScope string

const (
	AdminAttributionCredentialOnly       AdminAttributionScope = "credential_only"
	AdminAttributionSharedCredentialOnly AdminAttributionScope = "shared_credential_only"
)

// AdminActor is the secret-free identity attached to an authenticated admin
// request and, for money-authority writes, copied into the durable audit fact.
// PrincipalID is mode-discriminated: admin_credentials.id for a passkey session
// and api_keys.id for a break-glass key. SessionID is present only for passkeys.
// Raw credentials, token hashes, and WebAuthn credential bytes never enter it.
type AdminActor struct {
	Mode             AdminAuthMode         `json:"authentication_mode"`
	PrincipalID      uuid.UUID             `json:"principal_id"`
	SessionID        *uuid.UUID            `json:"session_id,omitempty"`
	AttributionScope AdminAttributionScope `json:"attribution_scope"`
	Label            string                `json:"label,omitempty"`
}

var errAdminActorUnauthorized = errors.New("admin actor is no longer authorized")

// adminActorFromContext returns the actor established by authAdmin. Callers use
// the boolean instead of accepting a zero-value actor so an unwrapped internal
// route cannot accidentally gain money authority.
func adminActorFromContext(ctx context.Context) (AdminActor, bool) {
	actor, ok := ctx.Value(ctxAdmin).(AdminActor)
	if !ok || validateAdminActorShape(actor) != nil {
		return AdminActor{}, false
	}
	return actor, true
}

func validateAdminActorShape(actor AdminActor) error {
	if actor.PrincipalID == uuid.Nil {
		return fmt.Errorf("%w: missing principal id", errAdminActorUnauthorized)
	}
	switch actor.Mode {
	case AdminAuthPasskeySession:
		if actor.SessionID == nil || *actor.SessionID == uuid.Nil {
			return fmt.Errorf("%w: passkey actor has no session id", errAdminActorUnauthorized)
		}
		if actor.AttributionScope != AdminAttributionCredentialOnly {
			return fmt.Errorf("%w: invalid passkey attribution scope", errAdminActorUnauthorized)
		}
	case AdminAuthBreakGlassAPIKey:
		if actor.SessionID != nil {
			return fmt.Errorf("%w: break-glass actor unexpectedly has a session id", errAdminActorUnauthorized)
		}
		if actor.AttributionScope != AdminAttributionSharedCredentialOnly {
			return fmt.Errorf("%w: invalid break-glass attribution scope", errAdminActorUnauthorized)
		}
	default:
		return fmt.Errorf("%w: unsupported authentication mode %q", errAdminActorUnauthorized, actor.Mode)
	}
	return nil
}

// revalidateAdminActor closes the revoke-between-middleware-and-commit window
// for an authority-bearing transaction. FOR SHARE linearizes an API-key/session
// revocation with the mutation: whichever locks first is the operation that took
// effect first. The label is display metadata and is intentionally not authority.
func revalidateAdminActor(ctx context.Context, tx pgx.Tx, actor AdminActor) error {
	if err := validateAdminActorShape(actor); err != nil {
		return err
	}

	var one int
	switch actor.Mode {
	case AdminAuthPasskeySession:
		err := tx.QueryRow(ctx, `
			SELECT 1
			  FROM admin_sessions sesh
			  JOIN admin_credentials cred
			    ON cred.id = sesh.admin_credential_id
			 WHERE sesh.id = $1
			   AND cred.id = $2
			   AND cred.revoked = false
			   AND sesh.revoked = false
			   AND sesh.expires_at > now()
			 FOR SHARE OF sesh, cred`, *actor.SessionID, actor.PrincipalID).Scan(&one)
		if errors.Is(err, pgx.ErrNoRows) {
			return errAdminActorUnauthorized
		}
		if err != nil {
			return fmt.Errorf("revalidate passkey admin actor: %w", err)
		}
	case AdminAuthBreakGlassAPIKey:
		err := tx.QueryRow(ctx, `
			SELECT 1
			  FROM api_keys
			 WHERE id = $1 AND is_admin = true AND revoked = false
			 FOR SHARE`, actor.PrincipalID).Scan(&one)
		if errors.Is(err, pgx.ErrNoRows) {
			return errAdminActorUnauthorized
		}
		if err != nil {
			return fmt.Errorf("revalidate break-glass admin actor: %w", err)
		}
	}
	return nil
}
