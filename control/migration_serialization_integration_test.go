//go:build integration

package main

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// Two replicas are allowed to start at the same instant. Both must observe one
// complete schema transition, never interleave trigger replacement, and both
// Migrate calls must finish successfully.
func TestMigrateSerializesConcurrentReplicas(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()

	secondPool, err := pgxpool.New(ctx, os.Getenv("DATABASE_URL"))
	if err != nil {
		t.Fatalf("second migration pool: %v", err)
	}
	defer secondPool.Close()

	errCh := make(chan error, 2)
	go func() { errCh <- itStore.Migrate(ctx) }()
	go func() { errCh <- NewStore(secondPool).Migrate(ctx) }()
	for i := 0; i < 2; i++ {
		if err := <-errCh; err != nil {
			t.Fatalf("concurrent migration %d: %v", i+1, err)
		}
	}

	// Prove the final successful caller did not return a session carrying a
	// stacked/leaked lock to its pool. Use a third pool so PostgreSQL cannot hand
	// this assertion the same session (where advisory locks are re-entrant).
	probePool, err := pgxpool.New(ctx, os.Getenv("DATABASE_URL"))
	if err != nil {
		t.Fatalf("migration lock probe pool: %v", err)
	}
	defer probePool.Close()
	probeConn, err := probePool.Acquire(ctx)
	if err != nil {
		t.Fatalf("migration lock probe connection: %v", err)
	}
	defer probeConn.Release()
	var acquired bool
	if err := probeConn.QueryRow(ctx,
		`SELECT pg_try_advisory_lock(hashtextextended($1, 0))`, schemaMigrationAdvisoryLock).Scan(&acquired); err != nil {
		t.Fatalf("probe migration lock: %v", err)
	}
	if !acquired {
		t.Fatal("Migrate returned with the schema advisory lock still held")
	}
	var unlocked bool
	if err := probeConn.QueryRow(ctx,
		`SELECT pg_advisory_unlock(hashtextextended($1, 0))`, schemaMigrationAdvisoryLock).Scan(&unlocked); err != nil || !unlocked {
		t.Fatalf("release migration probe lock: unlocked=%v err=%v", unlocked, err)
	}
}
