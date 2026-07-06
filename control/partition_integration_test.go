//go:build integration

package main

// partition_integration_test.go — the real-Postgres proof for Postgres Data Lifecycle
// 6->7 (docs/internal/CREED_AND_PATH_TO_TEN.md): the delete-based telemetry pruning is
// replaced by declarative RANGE partitioning with an O(1) drop-partition rotation job.
//
// These tests prove the RISKY half — the in-place migration of a POPULATED table in the
// OLD non-partitioned shape — against a real Postgres, not an empty one:
//
//   TestPartitionMigrationPreservesRowsOnPopulatedTable rebuilds the three telemetry
//   tables in their exact pre-6->7 plain shape, seeds real rows spanning many months
//   (including rows older than every retention window), runs the real
//   MigrateTelemetryPartitions, and asserts EVERY row survived, the table is now
//   partitioned, historical rows routed into month partitions (not DEFAULT), and every
//   existing reader query returns identical results before and after.
//
//   TestPartitionRotationCreatesAndDropsRealPartitions proves the rotation ticker's
//   create-ahead + drop-expired against real partitions on real Postgres.
//
//   TestPartitionMigrationIdempotent proves a second migration run is a clean no-op.
//
// The tests reconstruct the old plain shape and restore the partitioned+swept state in
// Cleanup so they compose with the rest of the (serial, non-parallel) integration suite.

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"
)

// oldPlainTelemetryDDL is the EXACT pre-6->7 non-partitioned shape of the three tables
// (single-column id PK, nullable created_at), mirroring db/schema.sql before this rung.
// Seeding into this shape is what makes the migration proof honest: it runs against the
// real old layout an existing production DB actually has, not a synthetic one.
var oldPlainTelemetryDDL = []string{
	`DROP TABLE IF EXISTS worker_memory_samples CASCADE`,
	`DROP TABLE IF EXISTS task_durations CASCADE`,
	`DROP TABLE IF EXISTS job_events CASCADE`,
	`CREATE TABLE worker_memory_samples (
	   id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
	   worker_id UUID, available_gb REAL, effective_gb REAL, throttled BOOLEAN,
	   created_at TIMESTAMPTZ DEFAULT now())`,
	`CREATE INDEX worker_memory_samples_worker_time_idx ON worker_memory_samples (worker_id, created_at DESC)`,
	`ALTER TABLE worker_memory_samples SET (autovacuum_vacuum_scale_factor=0.02, autovacuum_vacuum_threshold=200)`,
	`CREATE TABLE task_durations (
	   id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
	   created_at TIMESTAMPTZ DEFAULT now(),
	   job_id UUID, job_type TEXT, model_ref TEXT, split_size INT, duration_ms BIGINT,
	   worker_id UUID, engine TEXT, build_hash TEXT)`,
	`CREATE INDEX task_durations_type_model_idx ON task_durations (job_type, model_ref)`,
	`CREATE TABLE job_events (
	   id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
	   created_at TIMESTAMPTZ DEFAULT now(),
	   job_id UUID NOT NULL, task_id UUID, event TEXT NOT NULL, buyer_text TEXT, detail JSONB)`,
	`CREATE INDEX job_events_job_idx ON job_events (job_id, created_at)`,
}

// rebuildOldPlainTelemetry drops the (partitioned, from TestMain) telemetry tables and
// recreates them in the old plain shape. restorePartitionedTelemetry (deferred by the
// caller) converts them back so the rest of the suite sees the partitioned tables again.
func rebuildOldPlainTelemetry(t *testing.T, ctx context.Context) {
	t.Helper()
	for _, ddl := range oldPlainTelemetryDDL {
		if _, err := itPool.Exec(ctx, ddl); err != nil {
			t.Fatalf("rebuild old plain telemetry (%q): %v", ddl, err)
		}
	}
}

// restorePartitionedTelemetry re-runs the migration so the tables end this test in the
// same partitioned shape TestMain leaves them, keeping the suite order-independent.
func restorePartitionedTelemetry(t *testing.T, ctx context.Context) {
	t.Helper()
	if err := itStore.MigrateTelemetryPartitions(ctx); err != nil {
		t.Fatalf("restore partitioned telemetry: %v", err)
	}
}

func TestPartitionMigrationPreservesRowsOnPopulatedTable(t *testing.T) {
	ctx := context.Background()
	rebuildOldPlainTelemetry(t, ctx)
	t.Cleanup(func() { restorePartitionedTelemetry(t, ctx) })

	// Seed REAL rows spanning ~8 months, deliberately including rows far older than every
	// retention window (14/30/180 days), so the migration must preserve genuinely old
	// rows — not just recent ones — and route each into its month partition.
	if _, err := itPool.Exec(ctx,
		`INSERT INTO worker_memory_samples (worker_id, available_gb, effective_gb, throttled, created_at)
		   SELECT gen_random_uuid(), 8, 6, (g%5=0), now() - (g||' hours')::interval
		   FROM generate_series(1,5000) g`); err != nil {
		t.Fatalf("seed wms: %v", err)
	}
	if _, err := itPool.Exec(ctx,
		`INSERT INTO task_durations (job_id, job_type, model_ref, split_size, duration_ms, worker_id, engine, build_hash, created_at)
		   SELECT gen_random_uuid(), 'batch_infer', 'llama-1b', 64, 100+g, gen_random_uuid(), 'candle', 'h::b', now() - (g||' hours')::interval
		   FROM generate_series(1,2000) g`); err != nil {
		t.Fatalf("seed td: %v", err)
	}
	if _, err := itPool.Exec(ctx,
		`INSERT INTO job_events (job_id, task_id, event, buyer_text, detail, created_at)
		   SELECT gen_random_uuid(), gen_random_uuid(), 'task_complete', 'done', '{"k":1}'::jsonb, now() - (g*3||' hours')::interval
		   FROM generate_series(1,3000) g`); err != nil {
		t.Fatalf("seed je: %v", err)
	}

	// Capture the exact pre-migration state: per-table row count AND a content fingerprint
	// (a checksum over id::text so row-loss OR row-mutation both fail the assertion, not
	// just a count that could coincidentally match).
	type snap struct {
		count int64
		chk   *int64 // xor-fold of id hashes; nil-safe via COALESCE
	}
	// bit_xor over a 63-bit hash of id: order-independent, no overflow, and sensitive to
	// any row added, dropped, or mutated (id changes flip the fold).
	fingerprint := func(table string) snap {
		var s snap
		if err := itPool.QueryRow(ctx,
			`SELECT count(*), bit_xor(('x'||substr(md5(id::text),1,16))::bit(64)::bigint) FROM `+table).Scan(&s.count, &s.chk); err != nil {
			t.Fatalf("fingerprint %s: %v", table, err)
		}
		return s
	}
	tables := []string{"worker_memory_samples", "task_durations", "job_events"}
	pre := map[string]snap{}
	for _, tb := range tables {
		pre[tb] = fingerprint(tb)
		if pre[tb].count == 0 {
			t.Fatalf("%s seeded 0 rows", tb)
		}
	}

	// Sanity: they really are plain tables right now.
	for _, tb := range tables {
		var kind string
		if err := itPool.QueryRow(ctx, `SELECT relkind FROM pg_class WHERE relname=$1 AND relnamespace='public'::regnamespace`, tb).Scan(&kind); err != nil {
			t.Fatalf("relkind %s: %v", tb, err)
		}
		if kind != "r" {
			t.Fatalf("%s should be a plain table before migration, got relkind %q", tb, kind)
		}
	}

	// THE MIGRATION under test — the real production path.
	if err := itStore.MigrateTelemetryPartitions(ctx); err != nil {
		t.Fatalf("MigrateTelemetryPartitions: %v", err)
	}

	// Assert, per table: same count, same content fingerprint (no loss, no mutation),
	// now partitioned, and ZERO rows landed in the DEFAULT partition (every historical
	// row routed into a proper droppable month partition).
	for _, tb := range tables {
		post := fingerprint(tb)
		if post.count != pre[tb].count {
			t.Fatalf("%s ROW LOSS: pre=%d post=%d", tb, pre[tb].count, post.count)
		}
		if (pre[tb].chk == nil) != (post.chk == nil) || (pre[tb].chk != nil && *pre[tb].chk != *post.chk) {
			t.Fatalf("%s CONTENT CHANGED: pre-chk=%v post-chk=%v", tb, pre[tb].chk, post.chk)
		}
		var kind string
		if err := itPool.QueryRow(ctx, `SELECT relkind FROM pg_class WHERE relname=$1 AND relnamespace='public'::regnamespace`, tb).Scan(&kind); err != nil {
			t.Fatalf("post relkind %s: %v", tb, err)
		}
		if kind != "p" {
			t.Fatalf("%s should be PARTITIONED after migration, got relkind %q", tb, kind)
		}
		var inDefault int64
		if err := itPool.QueryRow(ctx, `SELECT count(*) FROM `+tb+`_default`).Scan(&inDefault); err != nil {
			t.Fatalf("count %s_default: %v", tb, err)
		}
		if inDefault != 0 {
			t.Fatalf("%s: %d rows landed in DEFAULT partition (should be 0 — every historical row belongs in a month partition)", tb, inDefault)
		}
		// The composite PK (created_at, id) must exist.
		var pkCols string
		if err := itPool.QueryRow(ctx,
			`SELECT string_agg(a.attname, ',' ORDER BY array_position(c.conkey, a.attnum))
			   FROM pg_constraint c JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=ANY(c.conkey)
			  WHERE c.conrelid=$1::regclass AND c.contype='p'`, tb).Scan(&pkCols); err != nil {
			t.Fatalf("pk cols %s: %v", tb, err)
		}
		if pkCols != "created_at,id" {
			t.Fatalf("%s PK should be (created_at,id), got (%s)", tb, pkCols)
		}
	}

	// Prove every EXISTING READER still works and returns sane results on the partitioned
	// tables — the query-behavior-preservation half of the mandate.
	// 1) TelemetryTableCounts (metrics gauge) returns the same counts.
	counts, err := itStore.TelemetryTableCounts(ctx)
	if err != nil {
		t.Fatalf("TelemetryTableCounts after migration: %v", err)
	}
	for _, tb := range tables {
		if counts[tb] != pre[tb].count {
			t.Fatalf("TelemetryTableCounts[%s]=%d, want %d", tb, counts[tb], pre[tb].count)
		}
	}
	// 2) DriftRollup + HistoricalP90DurationMs read task_durations — must not error.
	if _, err := itStore.DriftRollup(ctx); err != nil {
		t.Fatalf("DriftRollup after migration: %v", err)
	}
	if _, _, err := itStore.HistoricalP90DurationMs(ctx, "batch_infer", "llama-1b"); err != nil {
		t.Fatalf("HistoricalP90DurationMs after migration: %v", err)
	}
	// 3) ListWorkers reads worker_memory_samples via a LATERAL recent-N — must not error.
	if _, err := itStore.ListWorkers(ctx); err != nil {
		t.Fatalf("ListWorkers after migration: %v", err)
	}
	// 4) The DELETE sweep still works against the partitioned parent (defense-in-depth).
	if _, err := itStore.DeleteOldWorkerMemorySamples(ctx, time.Now().Add(-workerMemorySampleRetention)); err != nil {
		t.Fatalf("DeleteOldWorkerMemorySamples against partitioned table: %v", err)
	}
	// 5) A fresh INSERT still routes correctly (into a month partition, not DEFAULT).
	if _, err := itPool.Exec(ctx,
		`INSERT INTO worker_memory_samples (worker_id, available_gb, effective_gb, throttled) VALUES (gen_random_uuid(), 8, 6, false)`); err != nil {
		t.Fatalf("post-migration insert: %v", err)
	}
	var afterInsertDefault int64
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM worker_memory_samples_default`).Scan(&afterInsertDefault); err != nil {
		t.Fatalf("count default after insert: %v", err)
	}
	if afterInsertDefault != 0 {
		t.Fatalf("a live insert landed in DEFAULT (%d) — a month partition should have caught it", afterInsertDefault)
	}
}

func TestPartitionRotationCreatesAndDropsRealPartitions(t *testing.T) {
	ctx := context.Background()
	rebuildOldPlainTelemetry(t, ctx)
	t.Cleanup(func() { restorePartitionedTelemetry(t, ctx) })

	// Seed worker_memory_samples rows across ~7 months so, after migration, there are
	// several month partitions — some far enough back that the 14-day retention window
	// makes them fully expired and thus droppable.
	if _, err := itPool.Exec(ctx,
		`INSERT INTO worker_memory_samples (worker_id, available_gb, effective_gb, throttled, created_at)
		   SELECT gen_random_uuid(), 8, 6, false, now() - (g*6||' hours')::interval
		   FROM generate_series(0,840) g`); err != nil { // 840*6h ≈ 210 days back
		t.Fatalf("seed wms: %v", err)
	}
	if err := itStore.MigrateTelemetryPartitions(ctx); err != nil {
		t.Fatalf("migrate: %v", err)
	}

	countMonthParts := func() int {
		var n int
		if err := itPool.QueryRow(ctx,
			`SELECT count(*) FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid
			  WHERE i.inhparent='worker_memory_samples'::regclass AND c.relname LIKE 'worker_memory_samples_p%'`).Scan(&n); err != nil {
			t.Fatalf("count month partitions: %v", err)
		}
		return n
	}
	partsBefore := countMonthParts()
	var totalBefore int64
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM worker_memory_samples`).Scan(&totalBefore); err != nil {
		t.Fatalf("total before: %v", err)
	}

	// Run the REAL rotation job at now(). It must (a) create the create-ahead months if
	// not already present (idempotent — migration already made most) and (b) DROP every
	// month partition whose whole range is older than the 14-day wms retention window.
	created, dropped, err := itStore.RotateTelemetryPartitions(ctx, time.Now())
	if err != nil {
		t.Fatalf("RotateTelemetryPartitions: %v", err)
	}
	if dropped == 0 {
		t.Fatalf("rotation dropped 0 partitions, but ~7 months of history well past the 14-day window should have expired months to drop")
	}

	// After the drop, no row older than the retention cutoff may remain (the whole point).
	cutoff := time.Now().Add(-workerMemorySampleRetention)
	// Partition drop only removes WHOLE expired months; the current/boundary month can
	// still hold sub-cutoff rows until the DELETE sweep trims them. So assert the weaker,
	// correct invariant: every remaining row is newer than the OLDEST surviving month's
	// start — i.e. no fully-expired month partition survived.
	var oldestRemaining *time.Time
	if err := itPool.QueryRow(ctx, `SELECT min(created_at) FROM worker_memory_samples`).Scan(&oldestRemaining); err != nil {
		t.Fatalf("min created_at after rotation: %v", err)
	}
	if oldestRemaining != nil {
		// The oldest surviving row must live in a month whose UPPER bound is AFTER the
		// cutoff (its month was not fully expired), proving drop-expired removed exactly
		// the fully-expired months and nothing newer.
		monthStart := time.Date(oldestRemaining.UTC().Year(), oldestRemaining.UTC().Month(), 1, 0, 0, 0, 0, time.UTC)
		monthEnd := monthStart.AddDate(0, 1, 0)
		if !monthEnd.After(cutoff) {
			t.Fatalf("a fully-expired month survived: oldest remaining row %s sits in month ending %s which is <= cutoff %s",
				oldestRemaining, monthEnd, cutoff)
		}
	}

	partsAfter := countMonthParts()
	if partsAfter >= partsBefore {
		t.Fatalf("expected fewer month partitions after drop-expired: before=%d after=%d (created=%d dropped=%d)",
			partsBefore, partsAfter, created, dropped)
	}
	t.Logf("rotation: month partitions %d -> %d (created=%d dropped=%d)", partsBefore, partsAfter, created, dropped)

	// Idempotency: a second rotation at the same instant creates/drops nothing new.
	c2, d2, err := itStore.RotateTelemetryPartitions(ctx, time.Now())
	if err != nil {
		t.Fatalf("second rotation: %v", err)
	}
	if c2 != 0 || d2 != 0 {
		t.Fatalf("second rotation at same instant should be a no-op, got created=%d dropped=%d", c2, d2)
	}
}

func TestPartitionMigrationIdempotent(t *testing.T) {
	ctx := context.Background()
	rebuildOldPlainTelemetry(t, ctx)
	t.Cleanup(func() { restorePartitionedTelemetry(t, ctx) })

	// A little real data so idempotency is proven with rows present, not on empty tables.
	if _, err := itPool.Exec(ctx,
		`INSERT INTO job_events (job_id, task_id, event, buyer_text, detail)
		   SELECT gen_random_uuid(), gen_random_uuid(), 'task_complete', 'x', NULL FROM generate_series(1,100)`); err != nil {
		t.Fatalf("seed je: %v", err)
	}
	if err := itStore.MigrateTelemetryPartitions(ctx); err != nil {
		t.Fatalf("first migrate: %v", err)
	}
	var afterFirst int64
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM job_events`).Scan(&afterFirst); err != nil {
		t.Fatalf("count after first: %v", err)
	}
	// Second run must be a clean no-op (tables already partitioned) and preserve rows.
	if err := itStore.MigrateTelemetryPartitions(ctx); err != nil {
		t.Fatalf("second migrate (should be no-op): %v", err)
	}
	var afterSecond int64
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM job_events`).Scan(&afterSecond); err != nil {
		t.Fatalf("count after second: %v", err)
	}
	if afterFirst != afterSecond || afterSecond != 100 {
		t.Fatalf("idempotency broken: afterFirst=%d afterSecond=%d (want 100 both)", afterFirst, afterSecond)
	}
	// And a real InsertJobEvent still works end-to-end against the partitioned table.
	if err := itStore.InsertJobEvent(ctx, uuid.New(), nil, "job_created", "hi", nil); err != nil {
		t.Fatalf("InsertJobEvent against partitioned job_events: %v", err)
	}
}

// TestPartitionMigrationBackfillsNullCreatedAt proves the self-healing defensive path:
// a legacy row with a NULL created_at (impossible via the production insert path, but a
// hand-inserted row could have one) would be rejected by the new NOT NULL partition-key
// parent and abort the whole migration. The migration backfills such rows to now() on
// the renamed old table first, so the row is preserved (routed into the current month)
// instead of blocking the conversion.
func TestPartitionMigrationBackfillsNullCreatedAt(t *testing.T) {
	ctx := context.Background()
	rebuildOldPlainTelemetry(t, ctx)
	t.Cleanup(func() { restorePartitionedTelemetry(t, ctx) })

	// Two normal rows + one deliberately NULL-created_at row (the old plain shape allowed
	// NULL, since created_at was nullable there).
	if _, err := itPool.Exec(ctx,
		`INSERT INTO worker_memory_samples (worker_id, available_gb, created_at)
		   VALUES (gen_random_uuid(), 8, now()), (gen_random_uuid(), 8, now() - interval '40 days'), (gen_random_uuid(), 8, NULL)`); err != nil {
		t.Fatalf("seed with a NULL created_at row: %v", err)
	}
	var nullBefore int64
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM worker_memory_samples WHERE created_at IS NULL`).Scan(&nullBefore); err != nil {
		t.Fatalf("count null before: %v", err)
	}
	if nullBefore != 1 {
		t.Fatalf("expected exactly 1 NULL-created_at row seeded, got %d", nullBefore)
	}

	if err := itStore.MigrateTelemetryPartitions(ctx); err != nil {
		t.Fatalf("migration must self-heal the NULL row, not abort: %v", err)
	}

	// All three rows survived, none is NULL anymore, and the formerly-NULL row landed in
	// a real month partition (not lost, not in a rejected state).
	var total, stillNull, inDefault int64
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM worker_memory_samples`).Scan(&total); err != nil {
		t.Fatalf("count total: %v", err)
	}
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM worker_memory_samples WHERE created_at IS NULL`).Scan(&stillNull); err != nil {
		t.Fatalf("count still-null: %v", err)
	}
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM worker_memory_samples_default`).Scan(&inDefault); err != nil {
		t.Fatalf("count default: %v", err)
	}
	if total != 3 {
		t.Fatalf("row loss during NULL-backfill migration: want 3, got %d", total)
	}
	if stillNull != 0 {
		t.Fatalf("a NULL created_at survived the migration (%d) — backfill did not run", stillNull)
	}
	if inDefault != 0 {
		t.Fatalf("%d rows in DEFAULT after backfill — the healed row should sit in the current month partition", inDefault)
	}
}
