package main

import (
	"context"

	"github.com/google/uuid"
)

// benchmark.go — the benchmark database surface. Persisting a WorkerCapability
// and its BenchResults is handled by store.UpsertWorker (one transaction);
// this file holds the *read* side the scheduler relies on: turning stored
// workers + their latest benchmarks into the MatchWorker candidates Match
// scores, and exposing a worker's full profile.

// CandidateWorkers loads the workers that could plausibly serve a job type into
// the matching view: liveness, memory, reputation, supplier tier, a job_type→tps map
// drawn from each worker's most recent benchmark per type, and whether the worker has
// modelRef WARM (warm-routing, D3 — a re-rank bonus in Match, never a filter). Only
// workers seen within the last 60s are returned (Match re-checks, but filtering in SQL
// keeps the candidate set small on the hot path). modelRef may be "" (no model
// requirement) → Warm is false for everyone and the ranking is unchanged.
func (s *Store) CandidateWorkers(ctx context.Context, jobType, modelRef string, minMemGB float32) ([]MatchWorker, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT w.id, COALESCE(w.hw_class,''),
		        COALESCE(w.effective_memory_gb, w.memory_gb, 0),
		        s.reputation, w.last_seen_at, s.tier,
		        COALESCE(w.throttled, false),
		        COALESCE((
		          SELECT br.tps FROM benchmark_results br
		          WHERE br.worker_id = w.id AND br.job_type = $1
		          ORDER BY br.measured_at DESC LIMIT 1
		        ), 0),
		        -- WARM: a fresh worker_model_state row for THIS model means the worker
		        -- still has it loaded (warm-routing re-rank). "" model → never warm.
		        ($3 <> '' AND EXISTS (
		          SELECT 1 FROM worker_model_state wms
		          WHERE wms.worker_id = w.id AND wms.model_id = $3
		            AND wms.last_seen_warm > now() - interval '60 seconds'
		        ))
		 FROM workers w JOIN suppliers s ON s.id = w.supplier_id
		 WHERE w.last_seen_at IS NOT NULL
		   AND w.last_seen_at > now() - interval '60 seconds'
		   -- SAFE/effective memory (after headroom) once heartbeated, else total.
		   AND COALESCE(w.effective_memory_gb, w.memory_gb, 0) >= $2
		   -- exclude workers pausing for memory pressure (no unsafe peer/hedge).
		   AND NOT COALESCE(w.throttled, false)
		   AND s.status = 'active'`,
		jobType, minMemGB, modelRef,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var out []MatchWorker
	for rows.Next() {
		var (
			m       MatchWorker
			tierRaw int16
			tps     float32
		)
		if err := rows.Scan(&m.ID, &m.HWClass, &m.MemoryGB, &m.Reputation,
			&m.LastSeen, &tierRaw, &m.Throttled, &tps, &m.Warm); err != nil {
			return nil, err
		}
		m.Tier = int(tierRaw)
		m.TPS = map[string]float32{jobType: tps}
		out = append(out, m)
	}
	return out, rows.Err()
}

// WorkerProfile is the full benchmark/profile view of one worker.
type WorkerProfile struct {
	WorkerID   uuid.UUID     `json:"worker_id"`
	SupplierID uuid.UUID     `json:"supplier_id"`
	HWClass    string        `json:"hw_class"`
	MemoryGB   float32       `json:"memory_gb"`
	BwGbps     float32       `json:"bw_gbps"`
	Version    string        `json:"version"`
	Benchmarks []BenchResult `json:"benchmarks"`
}

// GetWorkerProfile returns a worker plus all its benchmark lines.
func (s *Store) GetWorkerProfile(ctx context.Context, workerID uuid.UUID) (*WorkerProfile, error) {
	var p WorkerProfile
	err := s.pool.QueryRow(ctx,
		`SELECT id, supplier_id, COALESCE(hw_class,''), COALESCE(memory_gb,0),
		        COALESCE(bw_gbps,0), COALESCE(version,'')
		 FROM workers WHERE id = $1`,
		workerID,
	).Scan(&p.WorkerID, &p.SupplierID, &p.HWClass, &p.MemoryGB, &p.BwGbps, &p.Version)
	if err != nil {
		return nil, err
	}

	rows, err := s.pool.Query(ctx,
		`SELECT model_id, job_type, tps, eps, thermal_ok, COALESCE(p99_latency_ms,0)
		 FROM benchmark_results WHERE worker_id = $1 ORDER BY measured_at DESC`,
		workerID,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var (
			b   BenchResult
			p99 float32
		)
		if err := rows.Scan(&b.ModelID, &b.JobType, &b.TPS, &b.EPS, &b.ThermalOK, &p99); err != nil {
			return nil, err
		}
		b.P99MS = uint32(p99)
		p.Benchmarks = append(p.Benchmarks, b)
	}
	return &p, rows.Err()
}
