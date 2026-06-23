package main

import (
	"context"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
)

// seed.go — `control seed`: idempotently create the minimum rows a human needs to
// drive the whole system end to end, and print the credentials.
//
// Everything uses stable demo UUIDs + fixed tokens and ON CONFLICT DO NOTHING, so
// re-running is safe (no duplicates, no clobbering operator edits). api_keys
// stores only the SHA-256 hash of a key (hashKey), so the raw keys printed here
// are the only place they appear — copy them when you seed. The base schema
// already seeds the models catalogue; we ensure it is present anyway so a fresh
// DB that only ran Migrate (not schema.sql) still has a priced catalogue.

// Fixed demo identities + credentials. These are DEV placeholders, printed in
// the clear on purpose; rotate them before any non-local use.
const (
	demoSupplierID   = "00000000-0000-0000-0000-0000000000a1"
	demoSupplierID2  = "00000000-0000-0000-0000-0000000000a2"
	demoWorkerID     = "00000000-0000-0000-0000-0000000000b1"
	demoWorkerID2    = "00000000-0000-0000-0000-0000000000b2"
	demoBuyerID      = "00000000-0000-0000-0000-0000000000c1"
	demoAdminBuyerID = "00000000-0000-0000-0000-0000000000c2"

	demoWorkerToken  = "dev-worker-token-0001"
	demoWorkerToken2 = "dev-worker-token-0002"
	demoAPIKey       = "dev-api-key-0001"
	demoAdminAPIKey  = "dev-admin-key-0001"

	demoHoneypotEmbedRef = "honeypots/embed/0001/input.jsonl"
	demoHoneypotInferRef = "honeypots/batch_infer/0001/input.jsonl"
)

// seedDemo runs the idempotent seed against the pool and prints the credentials.
func seedDemo(ctx context.Context, pool *pgxpool.Pool) error {
	stmts := []struct {
		sql  string
		args []any
	}{
		// Active supplier (so its workers are eligible) with a healthy reputation.
		{`INSERT INTO suppliers (id, email, reputation, tier, status)
		  VALUES ($1, 'demo-supplier@example.com', 0.90, 2, 'active')
		  ON CONFLICT (id) DO NOTHING`, []any{demoSupplierID}},
		// A worker for that supplier, seen now so it passes liveness. It advertises
		// the demo job types + models so the Scheduler V2 hard filter (ClaimTask)
		// lets it claim the demo embed/infer jobs; min_payout_usd_hr 0 = takes any
		// rate; thermal_ok true. supplier data_country is set so any data-residency
		// constraint can match.
		{`INSERT INTO workers (id, supplier_id, hw_class, memory_gb, bw_gbps, last_seen_at, version,
		                       supported_jobs, supported_models, min_payout_usd_hr, thermal_ok)
		  VALUES ($1, $2, 'apple_silicon_max', 64, 400, now(), 'seed',
		          ARRAY['embed','batch_infer','batch_classification','json_extraction','rerank'],
		          ARRAY['all-minilm-l6-v2','llama-3.2-1b-instruct-q4'], 0, true)
		  ON CONFLICT (id) DO NOTHING`, []any{demoWorkerID, demoSupplierID}},
		// Give the demo supplier a data_country so data-residency-constrained jobs
		// can match it (harmless when a job is unrestricted).
		{`UPDATE suppliers SET data_country = 'US' WHERE id = $1 AND data_country IS NULL`,
			[]any{demoSupplierID}},
		// Worker token bound to that worker/supplier.
		{`INSERT INTO worker_tokens (token_hash, worker_id, supplier_id, revoked)
		  VALUES ($1, $2, $3, false)
		  ON CONFLICT (token_hash) DO NOTHING`, []any{hashKey(demoWorkerToken), demoWorkerID, demoSupplierID}},
		// A SECOND supplier + worker + token, so a local multi-supplier run (two
		// agent processes on one box — the local stand-in for "two+ Macs running
		// real jobs end-to-end") has a distinct second identity. Same class +
		// capabilities so within-class redundancy compares the two.
		{`INSERT INTO suppliers (id, email, reputation, tier, status)
		  VALUES ($1, 'demo-supplier-2@example.com', 0.90, 2, 'active')
		  ON CONFLICT (id) DO NOTHING`, []any{demoSupplierID2}},
		{`INSERT INTO workers (id, supplier_id, hw_class, memory_gb, bw_gbps, last_seen_at, version,
		                       supported_jobs, supported_models, min_payout_usd_hr, thermal_ok)
		  VALUES ($1, $2, 'apple_silicon_max', 64, 400, now(), 'seed',
		          ARRAY['embed','batch_infer','batch_classification','json_extraction','rerank'],
		          ARRAY['all-minilm-l6-v2','llama-3.2-1b-instruct-q4'], 0, true)
		  ON CONFLICT (id) DO NOTHING`, []any{demoWorkerID2, demoSupplierID2}},
		{`UPDATE suppliers SET data_country = 'US' WHERE id = $1 AND data_country IS NULL`,
			[]any{demoSupplierID2}},
		{`INSERT INTO worker_tokens (token_hash, worker_id, supplier_id, revoked)
		  VALUES ($1, $2, $3, false)
		  ON CONFLICT (token_hash) DO NOTHING`, []any{hashKey(demoWorkerToken2), demoWorkerID2, demoSupplierID2}},
		// Non-admin buyer API key (hash stored, raw printed).
		{`INSERT INTO api_keys (buyer_id, key_hash, is_admin, revoked)
		  VALUES ($1, $2, false, false)
		  ON CONFLICT (key_hash) DO NOTHING`, []any{demoBuyerID, hashKey(demoAPIKey)}},
		// Admin buyer API key.
		{`INSERT INTO api_keys (buyer_id, key_hash, is_admin, revoked)
		  VALUES ($1, $2, true, false)
		  ON CONFLICT (key_hash) DO NOTHING`, []any{demoAdminBuyerID, hashKey(demoAdminAPIKey)}},
		// Ensure the priced model catalogue exists (mirrors schema.sql seed).
		{`INSERT INTO models (id, family, quant, kind, dim, job_type, price_per_1k, price_per_unit, min_memory_gb, hf_repo)
		  VALUES ('all-minilm-l6-v2','minilm',NULL,'embed',384,'embed',0.00100000,NULL,2,'sentence-transformers/all-MiniLM-L6-v2')
		  ON CONFLICT (id) DO NOTHING`, nil},
		{`INSERT INTO models (id, family, quant, kind, dim, job_type, price_per_1k, price_per_unit, min_memory_gb, hf_repo)
		  VALUES ('llama-3.2-1b-instruct-q4','llama','q4_k_m','gguf',NULL,'batch_infer',0.00200000,NULL,4,'unsloth/Llama-3.2-1B-Instruct-GGUF')
		  ON CONFLICT (id) DO NOTHING`, nil},
		// A couple of honeypots with known answers so honeypot tasks have probes.
		{`INSERT INTO honeypots (job_type, input_ref, known_answer)
		  SELECT 'embed', $1, $2
		  WHERE NOT EXISTS (SELECT 1 FROM honeypots WHERE job_type='embed' AND input_ref=$1)`,
			[]any{demoHoneypotEmbedRef, []byte(`{"vectors":[[1,0,0]]}`)}},
		{`INSERT INTO honeypots (job_type, input_ref, known_answer)
		  SELECT 'batch_infer', $1, $2
		  WHERE NOT EXISTS (SELECT 1 FROM honeypots WHERE job_type='batch_infer' AND input_ref=$1)`,
			[]any{demoHoneypotInferRef, []byte(`{"text":"42"}`)}},
	}
	for _, st := range stmts {
		if _, err := pool.Exec(ctx, st.sql, st.args...); err != nil {
			return fmt.Errorf("seed %q: %w", st.sql, err)
		}
	}

	// Sanity: confirm the demo UUIDs parse (they are compile-time constants, but
	// a typo would otherwise surface only as a silent FK failure above).
	for _, id := range []string{demoSupplierID, demoSupplierID2, demoWorkerID, demoWorkerID2, demoBuyerID, demoAdminBuyerID} {
		if _, err := uuid.Parse(id); err != nil {
			return fmt.Errorf("seed: bad demo uuid %q: %w", id, err)
		}
	}

	fmt.Println("seed complete — demo credentials (DEV ONLY):")
	fmt.Printf("  supplier_id   = %s\n", demoSupplierID)
	fmt.Printf("  worker_id     = %s\n", demoWorkerID)
	fmt.Printf("  worker_token  = %s   (X-Worker-Token header)\n", demoWorkerToken)
	fmt.Printf("  worker_token2 = %s   (second supplier — local multi-agent run)\n", demoWorkerToken2)
	fmt.Printf("  api_key       = %s   (Authorization: Bearer ...)\n", demoAPIKey)
	fmt.Printf("  admin_api_key = %s   (Authorization: Bearer ..., admin routes)\n", demoAdminAPIKey)
	return nil
}
