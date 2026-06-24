package main

import (
	"context"
	"errors"
	"sort"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// scheduler.go — matching + the Postgres-backed job queue.
//
// BLACKHOLE compression: the action plan named NATS for the job queue. We
// delete that whole dependency and dev service and back the queue with the
// tasks table using SELECT ... FOR UPDATE SKIP LOCKED. A poll claims exactly
// one eligible task atomically; concurrent pollers never collide.

// ErrNoSupply is returned by Match when no worker can serve a task.
var ErrNoSupply = errors.New("no eligible supply")

// MatchTask is the matching view of a task (pure inputs to Match).
type MatchTask struct {
	JobType     string
	MinMemoryGB float32
	HWClasses   []string // nil/empty = any
	Tier        string   // batch | priority | trusted
}

// MatchWorker is the matching view of a candidate worker. MemoryGB is the
// SAFE/effective allocatable memory (after the supplier's reserved headroom)
// when the worker has heartbeated it, else its total memory. Throttled means the
// worker is pausing for memory pressure and must not be selected for any work. Warm
// means this worker already has the job's model loaded (warm-routing, D3): it earns
// a small re-rank bonus so an otherwise-equal warm worker is preferred (avoiding a
// cold model load), but it is NEVER a filter — a cold worker is still fully eligible.
type MatchWorker struct {
	ID         uuid.UUID
	HWClass    string
	MemoryGB   float32
	Reputation float32
	TPS        map[string]float32 // job_type -> tokens/sec
	LastSeen   time.Time
	Tier       int  // 0-3
	Throttled  bool // currently pausing new claims (memory pressure)
	Warm       bool // already has the job's model warm in its pool (re-rank bonus only)
}

// Match filters and ranks candidate workers for a task, returning the eligible
// set in score order, all sharing ONE hardware class (the top scorer's), so any
// redundancy peer is same-class — required to avoid false-positive mismatches
// from cross-hardware nondeterminism. Pure function; the claim happens in
// ClaimTask. Direct port of the action plan's Match.
func Match(t MatchTask, workers []MatchWorker) ([]MatchWorker, error) {
	var candidates []MatchWorker
	for _, w := range workers {
		if time.Since(w.LastSeen) > 60*time.Second {
			continue // stale liveness
		}
		if w.Throttled {
			continue // pausing for memory pressure — unsafe to dispatch
		}
		if w.MemoryGB < t.MinMemoryGB {
			continue
		}
		if len(t.HWClasses) > 0 && !containsStr(t.HWClasses, w.HWClass) {
			continue
		}
		if t.Tier == "trusted" && w.Tier < 2 {
			continue
		}
		candidates = append(candidates, w)
	}
	if len(candidates) == 0 {
		return nil, ErrNoSupply
	}
	// Score = reputation × throughput for this job type, with a SMALL warm bonus so a
	// worker that already has the job's model loaded edges out an otherwise-equal cold
	// one (warm-routing, D3). Highest first.
	sort.Slice(candidates, func(i, j int) bool {
		return matchScore(candidates[i], t.JobType) > matchScore(candidates[j], t.JobType)
	})
	// Redundancy peers must be SAME hardware class: keep only the class of the
	// top scorer.
	winClass := candidates[0].HWClass
	same := candidates[:0:0]
	for _, w := range candidates {
		if w.HWClass == winClass {
			same = append(same, w)
		}
	}
	return same, nil
}

// warmBonus is the multiplicative re-rank factor a worker earns when it already has
// the job's model warm (warm-routing, D3). It is deliberately SMALL (5%): it tips an
// otherwise-equal or near-equal contest toward the warm worker (skipping a cold model
// load) without letting warmth override a meaningfully faster/higher-reputation cold
// worker. It is a ranking nudge only — Match's hard filters above are untouched, so a
// cold worker is never excluded.
const warmBonus = 1.05

// matchScore ranks a candidate for a job type: reputation × throughput, scaled by the
// warm bonus when the worker has the model warm. Pure and tiny so the warm preference
// is unit-testable in isolation (see TestMatchPrefersWarmWorker).
func matchScore(w MatchWorker, jobType string) float32 {
	s := w.Reputation * w.TPS[jobType]
	if w.Warm {
		s *= warmBonus
	}
	return s
}

func containsStr(xs []string, x string) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}

// hwClassCostRank maps a hardware class to a COST/CAPACITY rank: cheaper, smaller
// classes rank LOWER, expensive high-VRAM classes rank HIGHER. The ranking is the
// rough $/hr (and scarcity) ordering of the supply: a CPU box is the cheapest place
// to run a tiny job; the big accelerators (nvidia_80g/nvidia_180g/apple_silicon_ultra)
// are the most expensive and should be reserved for work that actually needs them.
// It is used ONLY as a claim-time preference (see ClaimTask's ORDER BY): so a small
// job an idle cheap worker could take is not handed to — and does not tie up — an
// expensive worker while a cheaper eligible class is online. It never filters: an
// unknown class sorts at the cheap end (rank 0) so a new/unranked class is still
// fully claimable, and a job whose only online supply is expensive is still claimed.
// hwClassCostRankSQL below MUST mirror this table exactly (the SQL evaluates the same
// rank for OTHER online workers); keep the two in lockstep.
func hwClassCostRank(hwClass string) int {
	switch hwClass {
	case "cpu":
		return 0
	case "apple_silicon_base":
		return 1
	case "apple_silicon_pro":
		return 2
	case "nvidia_24g":
		return 3
	case "apple_silicon_max":
		return 4
	case "nvidia_48g":
		return 5
	case "nvidia_80g":
		return 6
	case "apple_silicon_ultra", "apple_silicon_cluster":
		// A co-located cluster advertises summed member memory; it is as scarce/
		// expensive as the top single-box tier, so it ranks alongside ultra.
		return 7
	case "nvidia_180g":
		return 8
	default:
		return 0 // unknown/new class: treat as cheapest so it is never deprioritized
	}
}

// hwClassCostRankSQL is the SQL transcription of hwClassCostRank: a CASE that yields
// the same integer cost rank for a worker's hw_class column. ClaimTask uses it to ask
// "is a strictly-CHEAPER eligible class online for this job?" without re-deriving the
// table on the Go side for every candidate worker. Substituted with the worker-row
// alias (e.g. "w2.hw_class"). MUST stay byte-for-byte consistent with the Go switch
// above — if you add or reorder a class, change both.
func hwClassCostRankSQL(col string) string {
	return `CASE ` + col + `
	          WHEN 'cpu' THEN 0
	          WHEN 'apple_silicon_base' THEN 1
	          WHEN 'apple_silicon_pro' THEN 2
	          WHEN 'nvidia_24g' THEN 3
	          WHEN 'apple_silicon_max' THEN 4
	          WHEN 'nvidia_48g' THEN 5
	          WHEN 'nvidia_80g' THEN 6
	          WHEN 'apple_silicon_ultra' THEN 7
	          WHEN 'apple_silicon_cluster' THEN 7
	          WHEN 'nvidia_180g' THEN 8
	          ELSE 0
	        END`
}

// SelectRedundancyPeer picks a same-hardware-class worker to re-run a task for
// within-class redundancy verification, excluding the worker that already ran it.
// Thin wrapper over SelectRedundancyPeerExcluding (the anchor worker's class is
// the class peers must match, and it is itself excluded). modelRef lets the ranker
// prefer a peer that already has the model warm (warm-routing, D3); "" = no model.
func (s *Store) SelectRedundancyPeer(ctx context.Context, jobType, modelRef string, minMemGB float32, primaryWorker uuid.UUID) (uuid.UUID, error) {
	return s.SelectRedundancyPeerExcluding(ctx, jobType, modelRef, minMemGB, primaryWorker, nil)
}

// SelectRedundancyPeerExcluding picks the best live same-hardware-class worker to
// re-run a chunk, excluding `anchor` (whose class anchors the search) and every
// id in `also`. It fetches live candidates, scores them with the pure Match
// function pinned to the anchor's class, and returns the top scorer's id. A peer that
// already has modelRef warm earns the small warm-routing re-rank bonus (a re-run that
// avoids a cold model load), so among otherwise-equal same-class peers the warm one
// wins; modelRef "" disables the preference. Returns ErrNoSupply when no distinct
// same-class peer is online — used by the verification coordinator to assign a
// tiebreak third opinion that is genuinely a different worker from the two that
// already disagreed.
func (s *Store) SelectRedundancyPeerExcluding(ctx context.Context, jobType, modelRef string, minMemGB float32, anchor uuid.UUID, also []uuid.UUID) (uuid.UUID, error) {
	// The anchor's hardware class anchors the search: peers must match it.
	primary, err := s.GetWorkerProfile(ctx, anchor)
	if err != nil {
		return uuid.Nil, err
	}

	candidates, err := s.CandidateWorkers(ctx, jobType, modelRef, minMemGB)
	if err != nil {
		return uuid.Nil, err
	}
	excluded := map[uuid.UUID]bool{anchor: true}
	for _, id := range also {
		excluded[id] = true
	}
	// Drop every excluded worker — a peer must be a genuinely different worker.
	pruned := candidates[:0]
	for _, c := range candidates {
		if !excluded[c.ID] {
			pruned = append(pruned, c)
		}
	}

	ranked, err := Match(MatchTask{
		JobType:     jobType,
		MinMemoryGB: minMemGB,
		HWClasses:   []string{primary.HWClass}, // same class only
		Tier:        "batch",
	}, pruned)
	if err != nil {
		return uuid.Nil, err // ErrNoSupply when no same-class peer is free
	}
	return ranked[0].ID, nil
}

// ClaimedTask is what a worker receives from a successful poll claim.
type ClaimedTask struct {
	TaskID           uuid.UUID
	JobID            uuid.UUID
	JobType          string
	ModelRef         string
	InputRef         string // this task's input chunk key (presigned to input_url)
	ResultKey        string // where the worker writes its result (presigned to output_url)
	OutputRef        string // the job-level merged output key (manifest)
	Tier             string
	VerifPolicy      []byte
	JobTypeSpec      []byte // full submitted JobType JSON (tag + labels/schema/max_tokens/...)
	OfferedRateUsdHr float32
	ChunkIndex       int
	IsHoneypot       bool
}

// ClaimTask atomically claims the single best eligible task for a worker using
// FOR UPDATE SKIP LOCKED. This is the heart of the PG-backed queue:
//
//  1. compute the supplier's tier from reputation + lifetime completed tasks,
//  2. scan visible queued/retrying tasks of non-cancelled jobs the worker is
//     HARD-FILTERED to be able to run (see the WHERE clause), ordered priority-tier
//     first, THEN the hardware-matched routing preference (defer tasks a strictly
//     CHEAPER eligible class is online for, so an expensive worker does not tie
//     itself up on work a cheaper idle class could take), then oldest,
//  3. SKIP LOCKED so parallel pollers each grab a different row,
//  4. stamp claimed_by / claimed_at / worker_id and return the dispatch.
//
// Returns (nil, nil) when no task is available (handler answers 204), or
// errNotFound when the worker has not registered.
//
// Scheduler V2 hard filter (the #1 goal: a worker can NEVER claim a task it
// cannot SAFELY run). The `next` CTE JOINs the claiming worker's row + its
// supplier and rejects any task the worker is incapable of or ineligible for:
//   - supplier active (quarantine gate): s.status = 'active'
//   - memory:    COALESCE(j.min_memory_gb,0) <= COALESCE(w.effective_memory_gb, w.memory_gb)
//     (effective = available − reserved headroom from the live heartbeat;
//     falls back to total memory before the first heartbeat — no regression)
//   - throttle:  NOT w.throttled  (the worker is not pausing for memory pressure)
//   - hardware:  j.hw_classes IS NULL OR w.hw_class = ANY(j.hw_classes)
//   - job type:  w.supported_jobs @> ARRAY[j.job_type]
//   - model:     job has no model_ref OR w.supported_models @> ARRAY[j.model_ref]
//   - residency: j.data_residency IS NULL OR s.data_country = ANY(j.data_residency)
//   - tier gate: j.tier <> 'trusted' OR $2 >= 2
//   - payout:    COALESCE(j.offered_rate_usd_hr,1e9) >= COALESCE(w.min_payout_usd_hr,0)
func (s *Store) ClaimTask(ctx context.Context, w WorkerAuth) (*ClaimedTask, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)

	// Reject unregistered workers loudly rather than silently claiming nothing.
	// We also read the worker's hw_class here (same row, no extra round-trip) to
	// compute its cost rank for the hardware-matched routing preference below.
	var hwClass string
	if err := tx.QueryRow(ctx,
		`SELECT COALESCE(hw_class,'') FROM workers WHERE id = $1`, w.WorkerID,
	).Scan(&hwClass); errors.Is(err, pgx.ErrNoRows) {
		return nil, errNotFound
	} else if err != nil {
		return nil, err
	}
	// Cost rank of THIS claiming worker (cheaper class = lower rank). Passed to the
	// claim as $3 so the routing preference can ask "is a strictly-cheaper eligible
	// class online?" against this single canonical Go table (hwClassCostRank) — the
	// SQL only re-derives ranks for the OTHER online workers it compares against.
	selfCostRank := hwClassCostRank(hwClass)

	// Supplier tier gate (for trusted-tier jobs) from reputation + lifetime
	// completed tasks across all the supplier's workers.
	var rep float32
	var jobsDone uint64
	if err := tx.QueryRow(ctx,
		`SELECT s.reputation,
		        (SELECT count(*) FROM tasks t
		         WHERE t.worker_id IN (SELECT id FROM workers WHERE supplier_id = s.id)
		           AND t.status = 'complete')
		 FROM suppliers s WHERE s.id = $1`,
		w.SupplierID,
	).Scan(&rep, &jobsDone); err != nil {
		return nil, err
	}
	tier := reputationTier(rep, jobsDone)

	// Claim the best eligible task: priority-tier jobs first, then the cheapest-
	// sufficient-class routing preference (see the ORDER BY), then oldest. The
	// claim flips the task to 'running' (the worker is about to execute it) and
	// stamps started_at, so the stale-task requeue loop can detect a worker that
	// claimed but never committed past the deadline. The `next` CTE JOINs the
	// claiming worker (w.id = $1) + its supplier so every per-row filter can see
	// the worker's capability and the supplier's status (the hard filter).
	var c ClaimedTask
	err = tx.QueryRow(ctx,
		`WITH next AS (
		   SELECT t.id,
		     -- Hardware-matched routing (cost preference, NOT a filter): is a strictly
		     -- CHEAPER hardware class than THIS claiming worker ($3 = its hwClassCostRank)
		     -- online AND eligible for this job right now? "Eligible" reuses the EXACT
		     -- hard-filter predicates this claim applies (live <60s, supplier active, not
		     -- throttled, enough effective/total memory, the job's hw_classes pin,
		     -- supported_jobs, and supported_models), so we only defer a task a cheaper
		     -- class could ACTUALLY take. When true, an expensive worker steps back (ORDER
		     -- BY below sorts these last) and lets the cheaper class grab it — a small embed
		     -- job never ties up an nvidia_180g/apple_silicon_ultra box while a cpu/base
		     -- worker is idle. If NO cheaper eligible class is online, this is false and the
		     -- task ranks normally: the job is still claimed here (the cap is "cheapest
		     -- SUFFICIENT class", never starvation). SKIP LOCKED + the hard filter above are
		     -- untouched — this only re-orders tasks the claiming worker already passes.
		     EXISTS (
		       SELECT 1 FROM workers w2
		         JOIN suppliers s2 ON s2.id = w2.supplier_id
		       WHERE w2.id <> w.id
		         AND w2.last_seen_at IS NOT NULL
		         AND w2.last_seen_at > now() - interval '60 seconds'
		         AND s2.status = 'active'
		         AND NOT COALESCE(w2.throttled, false)
		         AND (`+hwClassCostRankSQL("w2.hw_class")+`) < $3
		         AND COALESCE(j.min_memory_gb,0) <= COALESCE(w2.effective_memory_gb, w2.memory_gb, 0)
		         AND (j.hw_classes IS NULL OR w2.hw_class = ANY(j.hw_classes))
		         AND COALESCE(w2.supported_jobs,'{}') @> ARRAY[j.job_type]
		         AND (COALESCE(j.model_ref,'') = '' OR COALESCE(w2.supported_models,'{}') @> ARRAY[j.model_ref])
		     ) AS cheaper_class_online
		   FROM tasks t
		     JOIN jobs j ON j.id = t.job_id
		     JOIN workers w ON w.id = $1
		     JOIN suppliers s ON s.id = w.supplier_id
		   WHERE t.status IN ('queued','retrying')
		     AND COALESCE(t.visible_at, t.created_at) <= now()
		     -- claimable when unclaimed, OR pre-claimed (pinned) to THIS worker and
		     -- not yet started (a tiebreak/hedge dispatch pinned to a chosen peer).
		     AND (t.claimed_by IS NULL OR (t.claimed_by = $1 AND t.started_at IS NULL))
		     AND j.status NOT IN ('cancelled','failed')
		     AND s.status = 'active'
		     -- SAFE memory: effective allocatable (after the supplier's reserved
		     -- headroom) once the worker has heartbeated it, else total memory_gb
		     -- (a just-registered worker is unchanged — no regression).
		     AND COALESCE(j.min_memory_gb,0) <= COALESCE(w.effective_memory_gb, w.memory_gb, 0)
		     -- never hand work to a worker pausing for memory pressure.
		     AND NOT COALESCE(w.throttled, false)
		     AND (j.hw_classes IS NULL OR w.hw_class = ANY(j.hw_classes))
		     AND COALESCE(w.supported_jobs,'{}') @> ARRAY[j.job_type]
		     AND (COALESCE(j.model_ref,'') = '' OR COALESCE(w.supported_models,'{}') @> ARRAY[j.model_ref])
		     AND (j.data_residency IS NULL OR s.data_country = ANY(j.data_residency))
		     -- Elite-supplier gate (DEEP_RESEARCH_V2 §6.4 anti-defection): a high
		     -- min_reputation job is claimable only by a supplier who earned that
		     -- reputation on the platform (0 = any supplier; the unchanged path).
		     AND COALESCE(j.min_reputation,0) <= s.reputation
		     AND (j.tier <> 'trusted' OR $2 >= 2)
		     AND (COALESCE(j.offered_rate_usd_hr,1e9) >= COALESCE(w.min_payout_usd_hr,0))
		     -- Budget Governor (Plane C §12 / Plane D §14 D8): when the job has a hard
		     -- spend cap, NEVER dispatch a new task whose projected charge would breach
		     -- it. Projected = already-charged on this job's tasks (buyer_charge debits,
		     -- same ledger shape refundJobChargesTx uses) + the per-task estimate for
		     -- every IN-FLIGHT (claimed, running, not-yet-committed) task of this job
		     -- (each will charge at commit, so it is exposure-in-flight) + ONE more for
		     -- the candidate. Counting in-flight work is what makes the cap hold under
		     -- the agent's bounded concurrency ([2,4] permits): without it, several
		     -- tasks could be claimed+running but uncommitted (contributing $0 charged)
		     -- and each then charge past the cap. A capped+exhausted job's tasks stay
		     -- queued (the cap PREVENTS dispatch — it never refunds); jobs with no cap
		     -- are unaffected (j.max_usd IS NULL).
		     AND (j.max_usd IS NULL OR (
		           (SELECT COALESCE(SUM(-le.amount_usd),0) FROM ledger_entries le
		            WHERE le.kind = 'buyer_charge'
		              AND le.task_id IN (SELECT id FROM tasks WHERE job_id = j.id))
		           + (SELECT count(*) FROM tasks it
		                WHERE it.job_id = j.id AND it.status = 'running')
		             * (COALESCE(j.estimated_usd,0) / NULLIF(j.task_count,0))
		           + COALESCE(j.estimated_usd,0) / NULLIF(j.task_count,0)
		         ) <= j.max_usd)
		   -- Pinned-to-this-worker jumps the line, then priority tier, THEN the
		   -- hardware-matched routing preference: within a tier, prefer tasks NO cheaper
		   -- eligible class is online for (cheaper_class_online = false sorts first), so
		   -- an expensive worker defers work a cheaper idle class could take instead of
		   -- tying itself up on it; finally oldest-first. The cost preference sits BELOW
		   -- pin + priority on purpose — a hedge/tiebreak pinned here, and a priority job,
		   -- are still served promptly even by an expensive worker. It only re-orders
		   -- already-eligible tasks; the hard filter above and SKIP LOCKED are unchanged.
		   ORDER BY (t.claimed_by = $1) DESC, (j.tier = 'priority') DESC,
		            cheaper_class_online ASC, t.created_at ASC
		   FOR UPDATE OF t SKIP LOCKED
		   LIMIT 1
		 )
		 UPDATE tasks
		   SET claimed_by = $1, claimed_at = now(), worker_id = $1,
		       status = 'running', started_at = now()
		 FROM next, jobs j
		 WHERE tasks.id = next.id AND j.id = tasks.job_id
		 RETURNING tasks.id, tasks.job_id, j.job_type, COALESCE(j.model_ref,''),
		           COALESCE(tasks.input_ref,''), COALESCE(tasks.result_key,''),
		           COALESCE(j.output_ref,''), j.tier,
		           COALESCE(j.verification_policy,'{}'::jsonb),
		           COALESCE(j.job_type_spec,'null'::jsonb),
		           COALESCE(j.offered_rate_usd_hr,0), COALESCE(tasks.chunk_index,0),
		           tasks.is_honeypot`,
		w.WorkerID, int(tier), selfCostRank,
	).Scan(&c.TaskID, &c.JobID, &c.JobType, &c.ModelRef, &c.InputRef, &c.ResultKey,
		&c.OutputRef, &c.Tier, &c.VerifPolicy, &c.JobTypeSpec, &c.OfferedRateUsdHr,
		&c.ChunkIndex, &c.IsHoneypot)
	// A REAL error (not "no eligible task") aborts; ErrNoRows just means no work —
	// we still fall through to the budget-stop sweep, then return (nil, nil). NB:
	// `claimed` is "a row came back", i.e. err == nil — NOT `!ErrNoRows` (which would
	// also be true for a real error and wrongly treat it as a claim).
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	claimed := err == nil
	if claimed {
		// First claim on a queued job moves the job to running.
		if _, err := tx.Exec(ctx,
			`UPDATE jobs SET status = 'running' WHERE id = $1 AND status = 'queued'`,
			c.JobID); err != nil {
			return nil, err
		}
		// Budget Governor: this task WAS dispatched within cap. If the job's
		// projected spend has crossed 80% of the cap, warn once on the timeline so a
		// buyer sees the approach before the stop (Plane C §12 near_limit / warning).
		if err := budgetWarnOnDispatch(ctx, tx, c.JobID); err != nil {
			return nil, err
		}
	}
	// Budget Governor stop: whether or not we just claimed, flag any CAPPED job that
	// still has a claimable task the budget predicate is refusing to dispatch —
	// flip budget_state to paused_for_budget and emit budget_stopped EXACTLY once
	// (the state transition is the guard). Scoped to max_usd IS NOT NULL, so an
	// uncapped job pays nothing here. The cap PREVENTS dispatch; it never refunds.
	stopped, err := markBudgetStoppedJobs(ctx, tx)
	if err != nil {
		return nil, err
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	// Advance the budget-stop counter only after the commit lands (Plane D D21): a
	// rolled-back claim must not inflate cx_budget_stops_total. The state transition
	// inside the tx guarantees each paused job is counted exactly once.
	if stopped > 0 {
		metrics.budgetStops.Add(int64(stopped))
	}
	if !claimed {
		return nil, nil // no work available -> 204
	}
	return &c, nil
}

// SchedulerExplanation breaks down WHY the claim filter is (or is not) handing a
// given worker work right now. Each field is a count of currently-claimable tasks
// rejected for that reason — and the buckets are MUTUALLY EXCLUSIVE: a task is
// attributed to the FIRST hard-filter predicate it fails, in the exact order
// ClaimTask applies them. So the counts sum to the visible claimable queue, and a
// worker that "looks slow" can be read at a glance: large no_queued_tasks means an
// empty queue, large memory_mismatch means it is too small for what is queued, and
// eligible > 0 means the queue genuinely has work it could take. Diagnostic only —
// this never claims, never mutates, and runs the SAME predicates as ClaimTask.
type SchedulerExplanation struct {
	WorkerID          uuid.UUID `json:"worker_id"`
	NoQueuedTasks     int       `json:"no_queued_tasks"`    // no claimable task exists at all (empty queue)
	MemoryMismatch    int       `json:"memory_mismatch"`    // job min_memory_gb > worker effective (or total) memory
	ModelMismatch     int       `json:"model_mismatch"`     // job needs a model the worker has not loaded
	JobTypeMismatch   int       `json:"job_type_mismatch"`  // worker does not support the job's type
	HWClassMismatch   int       `json:"hw_class_mismatch"`  // job pins hw_classes the worker is not in
	ResidencyMismatch int       `json:"residency_mismatch"` // supplier's data_country not in job's data_residency
	Throttled         int       `json:"throttled"`          // worker is pausing for memory pressure
	PayoutFloor       int       `json:"payout_floor"`       // economic/trust gate: offered rate below the worker's floor, OR a trusted-tier job the supplier's tier (<2) cannot take
	SupplierInactive  int       `json:"supplier_inactive"`  // supplier not active (quarantine/suspension gate)
	Eligible          int       `json:"eligible"`           // tasks this worker COULD claim right now
}

// SchedulerExplain runs the SAME hard-filter predicates as ClaimTask against every
// currently-claimable task and returns COUNTS of why each was rejected for this
// worker (or counts it eligible). It is the read-only mirror of ClaimTask's `next`
// CTE: the worker + supplier row are joined exactly as the claim joins them, and the
// per-task predicates are evaluated in the SAME order via a CASE so each task lands
// in its FIRST failing bucket. The trusted-tier gate is fed the worker's supplier
// tier computed in Go (reputationTier) — identical to the $2 ClaimTask passes —
// keeping it in lockstep with the claim; a task blocked only by it is reported under
// payout_floor (both are "this worker is not entitled to this job's economics/trust",
// and the required reason set has no separate tier slot). errNotFound when the worker
// is unregistered (the handler turns that into 404).
//
// Scope of "currently claimable" mirrors the claim's own visibility rules so the
// explanation describes the same queue the claim sees: tasks in ('queued','retrying')
// that are visible now, unclaimed (or pre-pinned to THIS worker and not yet started),
// whose job is not cancelled/failed. The Budget Governor gate is intentionally NOT a
// bucket here (it pauses dispatch, surfaced separately via budget_state); this view
// answers the capability/eligibility question ClaimTask's hard filter answers.
func (s *Store) SchedulerExplain(ctx context.Context, workerID uuid.UUID) (*SchedulerExplanation, error) {
	// Reject unregistered workers loudly (same as ClaimTask) rather than reporting an
	// all-zero explanation that hides the real problem.
	var exists bool
	if err := s.pool.QueryRow(ctx,
		`SELECT true FROM workers WHERE id = $1`, workerID,
	).Scan(&exists); errors.Is(err, pgx.ErrNoRows) {
		return nil, errNotFound
	} else if err != nil {
		return nil, err
	}

	// Supplier tier from reputation + lifetime completed tasks — the EXACT figure
	// ClaimTask computes and passes as $2 to its trusted-tier gate. Computed in Go so
	// the tier math (reputationTier) stays the single source of truth.
	var rep float32
	var jobsDone uint64
	if err := s.pool.QueryRow(ctx,
		`SELECT s.reputation,
		        (SELECT count(*) FROM tasks t
		         WHERE t.worker_id IN (SELECT id FROM workers WHERE supplier_id = s.id)
		           AND t.status = 'complete')
		 FROM suppliers s
		 WHERE s.id = (SELECT supplier_id FROM workers WHERE id = $1)`,
		workerID,
	).Scan(&rep, &jobsDone); err != nil {
		return nil, err
	}
	tier := reputationTier(rep, jobsDone)

	// Bucket every claimable task by its FIRST failing predicate, in ClaimTask order.
	// The CASE arms below are a line-for-line transcription of the `next` CTE's WHERE
	// clause (negated, in the same sequence): supplier active → memory → throttle →
	// hardware → job type → model → residency → tier → payout → else eligible. The
	// outer FROM only restricts to the claim's own claimable-visibility rules, so the
	// reason buckets partition exactly the queue ClaimTask would scan.
	e := SchedulerExplanation{WorkerID: workerID}
	err := s.pool.QueryRow(ctx,
		`WITH w AS (SELECT * FROM workers WHERE id = $1),
		      claimable AS (
		        SELECT j.min_memory_gb, j.hw_classes, j.data_residency, j.job_type,
		               j.model_ref, j.offered_rate_usd_hr, j.tier AS job_tier, j.min_reputation
		        FROM tasks t
		          JOIN jobs j ON j.id = t.job_id
		        WHERE t.status IN ('queued','retrying')
		          AND COALESCE(t.visible_at, t.created_at) <= now()
		          AND (t.claimed_by IS NULL OR (t.claimed_by = $1 AND t.started_at IS NULL))
		          AND j.status NOT IN ('cancelled','failed')
		      ),
		      classified AS (
		        SELECT CASE
		          WHEN s.status <> 'active' THEN 'supplier_inactive'
		          WHEN COALESCE(c.min_memory_gb,0) > COALESCE(w.effective_memory_gb, w.memory_gb, 0) THEN 'memory_mismatch'
		          WHEN COALESCE(w.throttled, false) THEN 'throttled'
		          WHEN NOT (c.hw_classes IS NULL OR w.hw_class = ANY(c.hw_classes)) THEN 'hw_class_mismatch'
		          WHEN NOT (COALESCE(w.supported_jobs,'{}') @> ARRAY[c.job_type]) THEN 'job_type_mismatch'
		          WHEN NOT (COALESCE(c.model_ref,'') = '' OR COALESCE(w.supported_models,'{}') @> ARRAY[c.model_ref]) THEN 'model_mismatch'
		          WHEN NOT (c.data_residency IS NULL OR s.data_country = ANY(c.data_residency)) THEN 'residency_mismatch'
		          WHEN COALESCE(c.min_reputation,0) > s.reputation THEN 'reputation_too_low'
		          WHEN NOT (c.job_tier <> 'trusted' OR $2 >= 2) THEN 'tier_gate'
		          WHEN NOT (COALESCE(c.offered_rate_usd_hr,1e9) >= COALESCE(w.min_payout_usd_hr,0)) THEN 'payout_floor'
		          ELSE 'eligible'
		        END AS reason
		        FROM claimable c, w
		          JOIN suppliers s ON s.id = w.supplier_id
		      )
		 SELECT
		   count(*) FILTER (WHERE reason = 'memory_mismatch'),
		   count(*) FILTER (WHERE reason = 'model_mismatch'),
		   count(*) FILTER (WHERE reason = 'job_type_mismatch'),
		   count(*) FILTER (WHERE reason = 'hw_class_mismatch'),
		   count(*) FILTER (WHERE reason = 'residency_mismatch'),
		   count(*) FILTER (WHERE reason = 'throttled'),
		   count(*) FILTER (WHERE reason = 'payout_floor' OR reason = 'tier_gate' OR reason = 'reputation_too_low'),
		   count(*) FILTER (WHERE reason = 'supplier_inactive'),
		   count(*) FILTER (WHERE reason = 'eligible')
		 FROM classified`,
		workerID, int(tier),
	).Scan(&e.MemoryMismatch, &e.ModelMismatch, &e.JobTypeMismatch, &e.HWClassMismatch,
		&e.ResidencyMismatch, &e.Throttled, &e.PayoutFloor, &e.SupplierInactive, &e.Eligible)
	if err != nil {
		return nil, err
	}
	// No claimable task at all → the queue is empty for this worker. This is the
	// "nothing eligible" case the endpoint exists to make visible: every per-reason
	// bucket is zero because there was nothing to reject.
	if e.MemoryMismatch+e.ModelMismatch+e.JobTypeMismatch+e.HWClassMismatch+
		e.ResidencyMismatch+e.Throttled+e.PayoutFloor+e.SupplierInactive+e.Eligible == 0 {
		e.NoQueuedTasks = 1
	}
	return &e, nil
}

// budgetThresholdFrac is the fraction of a budget cap at which the governor warns
// the buyer it is approaching the limit (Plane C §12: tracking → near_limit).
const budgetThresholdFrac = 0.80

// perTaskEstimateUSD is one task's share of a job's up-front estimate
// (estimated_usd / task_count) — the cost of dispatching ONE more task. Zero when
// the job has no tasks (avoids a divide-by-zero; an empty job dispatches nothing).
// This is the exact per-task figure the claim's budget gate adds to already-charged
// spend, kept here as the pure, testable definition the SQL mirrors.
func perTaskEstimateUSD(estimatedUSD float64, taskCount int) float64 {
	if taskCount <= 0 {
		return 0
	}
	return estimatedUSD / float64(taskCount)
}

// budgetWouldBreach reports whether dispatching ONE more task would push a capped
// job's PROJECTED charge past its cap: already-charged (sum of buyer_charge debits
// on the job's tasks) + one task's estimate > max_usd. A non-positive cap means
// "no cap" (never breaches) — matching the NULL-cap persistence. This is the pure
// mirror of the claim's SKIP-LOCKED budget predicate; the cap PREVENTS this
// dispatch, it never triggers a refund.
func budgetWouldBreach(chargedUSD, perTaskUSD, maxUSD float64) bool {
	if maxUSD <= 0 {
		return false
	}
	return chargedUSD+perTaskUSD > maxUSD
}

// budgetNearLimit reports whether a capped job's projected charge has reached the
// warn threshold (budgetThresholdFrac of the cap) — the tracking → near_limit
// transition. Pure mirror of budgetWarnOnDispatch's SQL guard.
func budgetNearLimit(chargedUSD, perTaskUSD, maxUSD float64) bool {
	if maxUSD <= 0 {
		return false
	}
	return chargedUSD+perTaskUSD >= budgetThresholdFrac*maxUSD
}

// budgetProjectedExpr is the SQL fragment for a job's PROJECTED charge: everything
// already charged on its tasks (buyer_charge debits, the same ledger shape
// refundJobChargesTx uses) PLUS one more task's estimated cost
// (estimated_usd / task_count). It is the exact quantity the claim's budget gate
// compares to max_usd, reused here so the warn/stop bookkeeping agrees with the
// gate to the cent. `j` must be the alias of the jobs row in the enclosing query.
const budgetProjectedExpr = `(
   (SELECT COALESCE(SUM(-le.amount_usd),0) FROM ledger_entries le
    WHERE le.kind = 'buyer_charge'
      AND le.task_id IN (SELECT id FROM tasks WHERE job_id = j.id))
   + (SELECT count(*) FROM tasks it WHERE it.job_id = j.id AND it.status = 'running')
     * (COALESCE(j.estimated_usd,0) / NULLIF(j.task_count,0))
   + COALESCE(j.estimated_usd,0) / NULLIF(j.task_count,0)
 )`

// budgetWarnOnDispatch emits a one-shot budget_warning event when a capped job's
// projected spend has reached budgetThresholdFrac of its cap, and advances
// budget_state tracking → near_limit. Idempotent: the event fires only on the
// transition (the WHERE guards on budget_state = 'tracking'), so repeated
// dispatches near the cap do not spam the timeline. No-op for uncapped jobs.
func budgetWarnOnDispatch(ctx context.Context, tx pgx.Tx, jobID uuid.UUID) error {
	var crossed bool
	err := tx.QueryRow(ctx,
		`UPDATE jobs j
		   SET budget_state = 'near_limit'
		 WHERE j.id = $1
		   AND j.max_usd IS NOT NULL
		   AND j.budget_state = 'tracking'
		   AND `+budgetProjectedExpr+` >= $2 * j.max_usd
		 RETURNING true`,
		jobID, budgetThresholdFrac,
	).Scan(&crossed)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil // not capped, already past 'tracking', or below the threshold
	}
	if err != nil {
		return err
	}
	if crossed {
		return insertEventTx(ctx, tx, jobID, nil, "budget_warning",
			"Approaching your budget cap (>= 80% projected).", nil)
	}
	return nil
}

// markBudgetStoppedJobs flips every CAPPED job that still has a claimable task the
// budget gate is refusing to dispatch into budget_state = 'paused_for_budget' and
// emits a budget_stopped event for each, EXACTLY once (the state transition is the
// guard, so a repeated poll does not re-emit). It mirrors the claim's own
// claimable-task predicate (queued/retrying, visible-now, unclaimed, job not
// terminal) so a job is paused only when there is real work the cap is holding
// back. The cap PREVENTS dispatch; no money moves and nothing is refunded. Returns
// the number of jobs newly paused this call so the caller can advance the
// cx_budget_stops_total counter AFTER the transaction commits (a rolled-back claim
// must not inflate the metric).
func markBudgetStoppedJobs(ctx context.Context, tx pgx.Tx) (int, error) {
	rows, err := tx.Query(ctx,
		`UPDATE jobs j
		   SET budget_state = 'paused_for_budget'
		 WHERE j.max_usd IS NOT NULL
		   AND j.budget_state NOT IN ('paused_for_budget','cancelled_by_budget')
		   AND j.status NOT IN ('cancelled','failed','complete')
		   AND `+budgetProjectedExpr+` > j.max_usd
		   AND EXISTS (
		     SELECT 1 FROM tasks t
		     WHERE t.job_id = j.id
		       AND t.status IN ('queued','retrying')
		       AND t.claimed_by IS NULL
		       AND COALESCE(t.visible_at, t.created_at) <= now()
		   )
		 RETURNING j.id`)
	if err != nil {
		return 0, err
	}
	var paused []uuid.UUID
	for rows.Next() {
		var id uuid.UUID
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return 0, err
		}
		paused = append(paused, id)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return 0, err
	}
	for _, id := range paused {
		if err := insertEventTx(ctx, tx, id, nil, "budget_stopped",
			"This job is paused before exceeding your budget cap.", nil); err != nil {
			return 0, err
		}
	}
	return len(paused), nil
}
