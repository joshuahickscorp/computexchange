package main

import (
	"context"
	"net/http"

	"github.com/google/uuid"
)

// moat.go — the data-moat tracking counters (docs/CREED_AND_PATH_TO_TEN.md, "Data
// moat & competitive defensibility" 2→3). The repo's own internal wargame document
// names three things a well-funded competitor cannot instantly copy: supplier
// relationships (retention, not just onboarding), verified settlement history, and
// buyer retention. Before this file, none of the three had a real, queryable number
// anywhere in the codebase — this is that number, computed from the real ledger,
// verification, and job tables, not estimated.

// MoatCounters is the real, current state of the three named moat components.
type MoatCounters struct {
	// SuppliersRetained counts suppliers who have received a RELEASED payout on
	// two or more distinct calendar days — a supplier paid twice in one day
	// (e.g. two tasks in the same session) is onboarding, not retention; coming
	// back on a later day is the real signal.
	SuppliersRetained int64 `json:"suppliers_retained"`
	// VerifiedSettlements counts distinct COMPLETE jobs that have at least one
	// real, independent verification credit (a passed honeypot or an agreeing
	// redundancy peer) recorded against them — the "we didn't just trust the
	// output, we checked it" count, not merely "jobs that finished."
	VerifiedSettlements int64 `json:"verified_settlements"`
	// BuyersRetained counts buyers with two or more COMPLETE jobs — a second
	// paid job from the same buyer, the buyer-side mirror of SuppliersRetained.
	BuyersRetained int64 `json:"buyers_retained"`
}

// MoatCounters computes the three real moat counters from the live database. Every
// number here is a COUNT, never a guess or a placeholder — if a counter reads 0, that
// is the honest current state of the moat, not a bug in the query.
func (s *Store) MoatCounters(ctx context.Context) (MoatCounters, error) {
	var m MoatCounters
	if err := s.pool.QueryRow(ctx, `
		SELECT count(*) FROM (
			SELECT supplier_id FROM ledger_entries
			WHERE kind = 'supplier_credit' AND payout_status = 'released' AND supplier_id IS NOT NULL
			GROUP BY supplier_id
			HAVING count(DISTINCT date_trunc('day', created_at)) >= 2
		) retained`).Scan(&m.SuppliersRetained); err != nil {
		return m, err
	}
	if err := s.pool.QueryRow(ctx, `
		SELECT count(DISTINCT j.id) FROM jobs j
		JOIN verification_events ve ON ve.job_id = j.id
		WHERE j.status = 'complete' AND ve.kind IN ('honeypot_pass', 'redundancy_match')`,
	).Scan(&m.VerifiedSettlements); err != nil {
		return m, err
	}
	if err := s.pool.QueryRow(ctx, `
		SELECT count(*) FROM (
			SELECT buyer_id FROM jobs WHERE status = 'complete'
			GROUP BY buyer_id
			HAVING count(*) >= 2
		) retained`).Scan(&m.BuyersRetained); err != nil {
		return m, err
	}
	return m, nil
}

// handleAdminMoat serves the real, current moat counters. Admin-gated like every
// other /admin/* data endpoint (see authAdmin in api.go).
func (s *Server) handleAdminMoat(w http.ResponseWriter, r *http.Request) {
	m, err := s.store.MoatCounters(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, m)
}

// ─────────────────────────────────────────────────────────────────────────────
// Data Moat & Competitive Defensibility 6→7 (docs/internal/CREED_AND_PATH_TO_TEN.md,
// "Make the verification data itself the product, not just proof of trust"): the
// redundancy/honeypot outcomes the verifier already records generate a dataset about
// *which suppliers are reliable for which job types* — the one thing a competitor
// starting from zero cannot have on day one. Rung 2→3 (MoatCounters above) turned the
// three named moats into scalar counters; this turns the per-(supplier, job-type)
// verification history into a real, queryable reliability view, the rung's own proof
// artifact ("a query or admin view showing per-supplier, per-job-type historical
// accuracy, derived from real completed verifications").
//
// Every rate here is a real ratio of real rows — a COUNT over tasks / verification_events,
// never a smoothed reputation scalar or an estimate. A denominator of 0 (a supplier that
// has never been honeypot-checked for this job type, say) yields a null rate, not a
// fabricated 1.0 or 0.0 — the honest "no data yet" the moat facet's own discipline
// requires, distinct from a real "0% pass" that a competitor should actually fear.
// ─────────────────────────────────────────────────────────────────────────────

// SupplierReliability is one (supplier, job_type) reliability cell: the three real
// verification-derived rates plus the raw counts they are computed from, so the view
// is auditable (a caller can see a 100% pass rate backed by 1 sample is weaker
// evidence than the same rate backed by 500). A *float64 rate is nil when its
// denominator is 0 — "not measured", never a faked figure.
type SupplierReliability struct {
	SupplierID uuid.UUID `json:"supplier_id"`
	JobType    string    `json:"job_type"`

	// Completion: of the tasks this supplier's workers reached a terminal state on
	// for this job type, the fraction that COMPLETED (vs failed). completed+failed
	// is the denominator; a still-running/queued task is not yet an outcome and is
	// excluded (it would otherwise depress the rate for work still in flight).
	TasksCompleted int64    `json:"tasks_completed"`
	TasksFailed    int64    `json:"tasks_failed"`
	CompletionRate *float64 `json:"completion_rate"` // completed / (completed+failed); nil = no terminal tasks yet

	// Honeypot: of the seeded known-answer tasks this supplier was checked on for
	// this job type, the fraction it answered correctly. Passes+fails is the
	// denominator — a supplier never handed a honeypot for this job type has a nil
	// rate, not a free 100%.
	HoneypotPasses int64    `json:"honeypot_passes"`
	HoneypotFails  int64    `json:"honeypot_fails"`
	HoneypotRate   *float64 `json:"honeypot_pass_rate"` // passes / (passes+fails); nil = never honeypot-checked

	// Redundancy: of the byte-exact cross-checks against an independent peer for this
	// job type, the fraction where this supplier AGREED with the peer. Matches +
	// mismatches is the denominator; cross-class/same-supplier events (recorded for
	// forensics but explicitly NOT counted as an independent check — see
	// verification_events.kind) are excluded, matching what the verifier itself counts.
	RedundancyMatches    int64    `json:"redundancy_matches"`
	RedundancyMismatches int64    `json:"redundancy_mismatches"`
	RedundancyRate       *float64 `json:"redundancy_agreement_rate"` // matches / (matches+mismatches); nil = never redundancy-checked
}

// rateOrNil is completed/total as a *float64, or nil when total is 0 — the honest
// "no data" the moat facet requires (a nil rate is distinct from a real 0.0/1.0).
func rateOrNil(numerator, denominator int64) *float64 {
	if denominator <= 0 {
		return nil
	}
	r := float64(numerator) / float64(denominator)
	return &r
}

// SupplierReliability computes the per-(supplier, job_type) reliability view from
// the real tasks + verification_events tables. Three independent aggregates — task
// completion (tasks → workers.supplier_id), honeypot outcomes, and redundancy
// outcomes (both from verification_events.supplier_id) — are each grouped by
// (supplier, job_type) and FULL-OUTER-joined on that key, so a cell appears if a
// supplier has ANY of the three kinds of history for a job type (a supplier that
// has completed real tasks but never been honeypot-checked still shows up, with a
// nil honeypot rate). Ordered by supplier then job_type for a stable admin view.
//
// job_type comes from the jobs row in every branch (tasks.job_id → jobs.job_type,
// verification_events.job_id → jobs.job_type) so the same "embed"/"batch_infer"
// label keys all three, never a task-local variant. NULL supplier rows (a task
// whose worker has no supplier, or a verification event with no attributed
// supplier) are dropped — a reliability cell must attribute to a real supplier.
func (s *Store) SupplierReliability(ctx context.Context) ([]SupplierReliability, error) {
	rows, err := s.pool.Query(ctx, `
		WITH task_outcomes AS (
			SELECT w.supplier_id AS supplier_id, j.job_type AS job_type,
			       count(*) FILTER (WHERE t.status = 'complete') AS completed,
			       count(*) FILTER (WHERE t.status = 'failed')   AS failed
			FROM tasks t
			JOIN jobs j    ON j.id = t.job_id
			JOIN workers w ON w.id = t.worker_id
			WHERE w.supplier_id IS NOT NULL
			  AND t.status IN ('complete','failed')
			GROUP BY w.supplier_id, j.job_type
		),
		hp AS (
			SELECT ve.supplier_id AS supplier_id, j.job_type AS job_type,
			       count(*) FILTER (WHERE ve.kind = 'honeypot_pass') AS passes,
			       count(*) FILTER (WHERE ve.kind = 'honeypot_fail') AS fails
			FROM verification_events ve
			JOIN jobs j ON j.id = ve.job_id
			WHERE ve.supplier_id IS NOT NULL
			  AND ve.kind IN ('honeypot_pass','honeypot_fail')
			GROUP BY ve.supplier_id, j.job_type
		),
		rd AS (
			SELECT ve.supplier_id AS supplier_id, j.job_type AS job_type,
			       count(*) FILTER (WHERE ve.kind = 'redundancy_match')    AS matches,
			       count(*) FILTER (WHERE ve.kind = 'redundancy_mismatch') AS mismatches
			FROM verification_events ve
			JOIN jobs j ON j.id = ve.job_id
			WHERE ve.supplier_id IS NOT NULL
			  AND ve.kind IN ('redundancy_match','redundancy_mismatch')
			GROUP BY ve.supplier_id, j.job_type
		)
		SELECT
			COALESCE(task_outcomes.supplier_id, hp.supplier_id, rd.supplier_id) AS supplier_id,
			COALESCE(task_outcomes.job_type,    hp.job_type,    rd.job_type)    AS job_type,
			COALESCE(task_outcomes.completed, 0), COALESCE(task_outcomes.failed, 0),
			COALESCE(hp.passes, 0),               COALESCE(hp.fails, 0),
			COALESCE(rd.matches, 0),              COALESCE(rd.mismatches, 0)
		FROM task_outcomes
		FULL OUTER JOIN hp
			ON hp.supplier_id = task_outcomes.supplier_id AND hp.job_type = task_outcomes.job_type
		FULL OUTER JOIN rd
			ON rd.supplier_id = COALESCE(task_outcomes.supplier_id, hp.supplier_id)
			AND rd.job_type   = COALESCE(task_outcomes.job_type,    hp.job_type)
		ORDER BY 1, 2`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []SupplierReliability
	for rows.Next() {
		var r SupplierReliability
		if err := rows.Scan(&r.SupplierID, &r.JobType,
			&r.TasksCompleted, &r.TasksFailed,
			&r.HoneypotPasses, &r.HoneypotFails,
			&r.RedundancyMatches, &r.RedundancyMismatches); err != nil {
			return nil, err
		}
		r.CompletionRate = rateOrNil(r.TasksCompleted, r.TasksCompleted+r.TasksFailed)
		r.HoneypotRate = rateOrNil(r.HoneypotPasses, r.HoneypotPasses+r.HoneypotFails)
		r.RedundancyRate = rateOrNil(r.RedundancyMatches, r.RedundancyMatches+r.RedundancyMismatches)
		out = append(out, r)
	}
	return out, rows.Err()
}

// handleAdminMoatReliability serves the per-(supplier, job_type) reliability view
// (Data Moat 6→7). Admin-gated like every other /admin/* data endpoint. Returns a
// JSON array (never null: an empty history is `[]`, an honest "no verification data
// yet", not an error).
func (s *Server) handleAdminMoatReliability(w http.ResponseWriter, r *http.Request) {
	rel, err := s.store.SupplierReliability(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	if rel == nil {
		rel = []SupplierReliability{}
	}
	writeJSON(w, http.StatusOK, rel)
}
