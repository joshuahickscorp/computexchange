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
// drawn from each worker's most recent benchmark per type, whether the worker has
// modelRef WARM (warm-routing, D3 — a re-rank bonus in Match, never a filter), and
// whether it is thermally degraded (docs/internal/CREED_AND_PATH_TO_TEN.md, "Thermal
// sustained-vs-peak throughput on fanless Apple Silicon" 4→5 — also a re-rank
// penalty in Match, never a filter: a throttling worker is still real, usable
// supply). Only workers seen within the last 60s are returned (Match re-checks, but
// filtering in SQL keeps the candidate set small on the hot path). An exact
// current-matrix (jobType, modelRef) capability is mandatory; legacy array-only
// workers are not candidates. modelRef may be "" only if such a production cell
// exists; Warm is then false.
func (s *Store) CandidateWorkers(ctx context.Context, jobType, modelRef string, minMemGB float32) ([]MatchWorker, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT w.id, w.supplier_id, COALESCE(w.hw_class,''),
		        COALESCE(w.engine,''), COALESCE(w.build_hash,''),
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
		        )),
		        -- Thermally degraded = NOT thermal_ok. Defaults thermal_ok's own
		        -- column default (true) through COALESCE, so a worker that predates
		        -- this column (or a fresh registration whose benchmarks haven't
		        -- landed yet) is NOT penalized for a measurement it never had.
		        NOT COALESCE(w.thermal_ok, true)
		 FROM workers w JOIN suppliers s ON s.id = w.supplier_id
		 WHERE w.last_seen_at IS NOT NULL
		   AND w.last_seen_at > now() - interval '60 seconds'
		   -- SAFE/effective memory (after headroom) once heartbeated, else total.
		   AND COALESCE(w.effective_memory_gb, w.memory_gb, 0) >= $2
		   -- exclude workers pausing for memory pressure (no unsafe peer/hedge).
		   AND NOT COALESCE(w.throttled, false)
		   AND s.status = 'active'
		   AND EXISTS (
		     SELECT 1 FROM worker_authorized_capabilities wac
		      WHERE wac.worker_id = w.id
		        AND wac.job_type = $1
		        AND wac.model_ref = $3
		        AND wac.matrix_sha256 = $4
		   )`,
		jobType, minMemGB, modelRef, generatedRuntimeMatrixSHA256,
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
		if err := rows.Scan(&m.ID, &m.SupplierID, &m.HWClass, &m.Engine, &m.BuildHash, &m.MemoryGB, &m.Reputation,
			&m.LastSeen, &tierRaw, &m.Throttled, &tps, &m.Warm, &m.ThermalDegraded); err != nil {
			return nil, err
		}
		m.Tier = int(tierRaw)
		m.TPS = map[string]float32{jobType: tps}
		out = append(out, m)
	}
	return out, rows.Err()
}

// FleetRateRow is one live, eligible worker's measured-rate view for the
// fan-out planner (Speed Lane wave 1B, planner.go): the worker's cached
// measured tps for the job type (worker_tps_cache — 0 when it has never
// benchmarked this type), whether it currently reports the job's model WARM
// (worker_model_state, the same 60s freshness window the claim path uses), and
// the most recent MEASURED cold-load wall-clock for that model
// (benchmark_results.load_ms — persisted since the warm-pool facet but never
// read at planning time before this wave; 0 = no measurement, the planner
// substitutes its documented default). Row eligibility mirrors the claim
// path's hard filter (live <60s, supplier active, not throttled, effective
// memory, exact current-matrix job/model cell) so the planner never plans onto a worker the
// scheduler could not actually dispatch to.
type FleetRateRow struct {
	WorkerID  uuid.UUID
	TPS       float32
	Warm      bool
	LoadMS    int64
	Throttled bool
}

// FleetRateSnapshot loads the live eligible fleet's measured rates for a
// (jobType, modelRef, minMemGB) shape — the planner's input. modelRef may be
// "": Warm is then false for every row (no model to be warm for) and the
// load_ms lookup is skipped. Read-only; not on the claim hot path (called at
// submit/quote time and from the endgame sweep's decision log only).
func (s *Store) FleetRateSnapshot(ctx context.Context, jobType, modelRef string, minMemGB float32) ([]FleetRateRow, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT w.id,
		        COALESCE(wtc.tps, 0),
		        ($2 <> '' AND EXISTS (
		          SELECT 1 FROM worker_model_state wms
		          WHERE wms.worker_id = w.id AND wms.model_id = $2
		            AND wms.last_seen_warm > now() - interval '60 seconds'
		        )),
		        COALESCE((
		          SELECT br.load_ms FROM benchmark_results br
		          WHERE br.worker_id = w.id AND br.model_id = $2 AND br.load_ms > 0
		          ORDER BY br.measured_at DESC LIMIT 1
		        ), 0),
		        COALESCE(w.throttled, false)
		 FROM workers w JOIN suppliers s ON s.id = w.supplier_id
		 LEFT JOIN worker_tps_cache wtc ON wtc.worker_id = w.id AND wtc.job_type = $1
		 WHERE w.last_seen_at IS NOT NULL
		   AND w.last_seen_at > now() - interval '60 seconds'
		   AND s.status = 'active'
		   AND NOT COALESCE(w.throttled, false)
		   AND COALESCE(w.effective_memory_gb, w.memory_gb, 0) >= $3
		   AND EXISTS (
		     SELECT 1 FROM worker_authorized_capabilities wac
		      WHERE wac.worker_id = w.id
		        AND wac.job_type = $1
		        AND wac.model_ref = $2
		        AND wac.matrix_sha256 = $4
		   )`,
		jobType, modelRef, minMemGB, generatedRuntimeMatrixSHA256,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []FleetRateRow
	for rows.Next() {
		var r FleetRateRow
		if err := rows.Scan(&r.WorkerID, &r.TPS, &r.Warm, &r.LoadMS, &r.Throttled); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// WorkerProfile is the full benchmark/profile view of one worker. Engine + BuildHash
// are the finer verification-class axes (alongside HWClass) so the redundancy-peer
// path can pin a peer to the anchor's full (hw_class, engine, build_hash) class.
type WorkerProfile struct {
	WorkerID   uuid.UUID     `json:"worker_id"`
	SupplierID uuid.UUID     `json:"supplier_id"`
	HWClass    string        `json:"hw_class"`
	Engine     string        `json:"engine"`
	BuildHash  string        `json:"build_hash"`
	MemoryGB   float32       `json:"memory_gb"`
	BwGbps     float32       `json:"bw_gbps"`
	Version    string        `json:"version"`
	Benchmarks []BenchResult `json:"benchmarks"`
}

// GetWorkerProfile returns a worker plus all its benchmark lines.
func (s *Store) GetWorkerProfile(ctx context.Context, workerID uuid.UUID) (*WorkerProfile, error) {
	var p WorkerProfile
	err := s.pool.QueryRow(ctx,
		`SELECT id, supplier_id, COALESCE(hw_class,''),
		        COALESCE(engine,''), COALESCE(build_hash,''),
		        COALESCE(memory_gb,0), COALESCE(bw_gbps,0), COALESCE(version,'')
		 FROM workers WHERE id = $1`,
		workerID,
	).Scan(&p.WorkerID, &p.SupplierID, &p.HWClass, &p.Engine, &p.BuildHash,
		&p.MemoryGB, &p.BwGbps, &p.Version)
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
