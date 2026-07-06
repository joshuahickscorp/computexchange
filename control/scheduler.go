package main

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"sync/atomic"
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

// noHedgePeerFound counts DISPATCH-time "heterogeneous-fleet silent degradation"
// events (Scheduling & Matching Engine 7->8, docs/internal/CREED_AND_PATH_TO_TEN.md
// "Make heterogeneous-fleet degradation visible instead of silent"): a
// hedge/tiebreak/redundancy peer was genuinely NEEDED for a task's class, the
// fleet HAD live eligible-for-the-job-type supply, but NO independent same-class
// peer existed — so redundancy/warm-routing/tiebreak quietly degraded with no
// operational signal. It is incremented in SelectRedundancyPeerExcluding (the ONE
// same-class-peer search every hedge/tiebreak/redundancy path funnels through) and
// exposed as cx_no_hedge_peer_total on /metrics; monitoring/alerts.yml alerts on a
// sustained non-zero rate. Deliberately NOT counted when the fleet has zero live
// eligible supply at all (that is an empty-fleet condition, not the "supply exists
// but not of the right class" heterogeneous-degradation this signal is for).
var noHedgePeerFound atomic.Int64

// NoHedgePeerCount returns the running count of heterogeneous-fleet no-peer events
// (see noHedgePeerFound). Read by handleMetrics for the cx_no_hedge_peer_total
// exposition and by tests to assert the signal fired.
func NoHedgePeerCount() int64 { return noHedgePeerFound.Load() }

// claimDurationBucketsMs are the fixed histogram bucket upper bounds
// (milliseconds) for cx_claim_duration_ms, exposed on /metrics. Deliberately
// finer-grained than taskDurationBucketsMs (a claim is a single transaction
// expected to land in single-digit-to-low-hundreds ms even under a deep queue,
// not the minutes a task's wall-clock execution can take).
var claimDurationBucketsMs = []float64{1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000}

// claimHistogram is an in-process, lock-free cumulative-bucket latency
// histogram (Scalability headroom 4.5->5): every ClaimTask call — real traffic
// or a synthetic load generator hitting /v1/worker/poll — is observed here, so
// /metrics can report a REAL p50/p90/p99 for the claim hot path under
// whatever load the process actually saw, instead of an estimated ceiling.
// Process-lifetime only (matches metricsState's counters) — no DB round trip
// on the hot path, unlike the DB-backed cx_task_duration_ms histogram.
type claimHistogram struct {
	buckets  [12]atomic.Int64 // cumulative count, observation <= bucket bound (len == len(claimDurationBucketsMs))
	overflow atomic.Int64     // observation > every bound (folded into +Inf)
	sumMs    atomic.Int64     // sum of all observations, ms (for the _sum series)
	count    atomic.Int64     // total observations (for the _count series)
}

var claimDuration claimHistogram

func init() {
	if len(claimDurationBucketsMs) != len(claimDuration.buckets) {
		panic("claimDurationBucketsMs length must match claimHistogram.buckets length")
	}
}

func (h *claimHistogram) observe(d time.Duration) {
	ms := float64(d) / float64(time.Millisecond)
	h.count.Add(1)
	h.sumMs.Add(int64(ms))
	placed := false
	for i, bound := range claimDurationBucketsMs {
		if ms <= bound {
			h.buckets[i].Add(1)
			placed = true
			break
		}
	}
	if !placed {
		h.overflow.Add(1)
	}
}

// snapshot returns cumulative bucket counts (Prometheus histogram semantics:
// each bucket counts every observation <= its bound, so the LAST bucket here
// already equals count-overflow — the exported +Inf line adds overflow back).
func (h *claimHistogram) snapshot() (cumulative []int64, count int64, sumMs int64) {
	cumulative = make([]int64, len(claimDurationBucketsMs))
	var running int64
	for i := range claimDurationBucketsMs {
		running += h.buckets[i].Load()
		cumulative[i] = running
	}
	return cumulative, h.count.Load(), h.sumMs.Load()
}

// MatchTask is the matching view of a task (pure inputs to Match).
type MatchTask struct {
	JobType     string
	MinMemoryGB float32
	HWClasses   []string // nil/empty = any
	Tier        string   // batch | priority | trusted
	// PinEngine / PinBuildHash optionally restrict candidates to the SAME finer
	// verification class as a redundancy anchor. They are set ONLY on the
	// redundancy-peer path (SelectRedundancyPeerExcluding) so a peer drawn to
	// byte-compare a result shares (hw_class, engine, build_hash) with the primary.
	// The general claim path leaves them empty (no extra filter, scheduling
	// unchanged). An empty PinBuildHash means "do not pin on build" (so a fleet that
	// does not yet advertise build hashes still matches within hw_class+engine); an
	// empty PinEngine likewise does not pin on engine. When set, a candidate must
	// match EXACTLY — an unknown-build ("") candidate is NOT pinned-equal to a known
	// build, so it is excluded from a same-build peer search (it instead falls to
	// provisional trust at verify time, never a cross-class byte-dock).
	PinEngine    string
	PinBuildHash string
}

// MatchWorker is the matching view of a candidate worker. MemoryGB is the
// SAFE/effective allocatable memory (after the supplier's reserved headroom)
// when the worker has heartbeated it, else its total memory. Throttled means the
// worker is pausing for memory pressure and must not be selected for any work. Warm
// means this worker already has the job's model loaded (warm-routing, D3): it earns
// a small re-rank bonus so an otherwise-equal warm worker is preferred (avoiding a
// cold model load), but it is NEVER a filter — a cold worker is still fully eligible.
type MatchWorker struct {
	ID uuid.UUID
	// SupplierID is the operator behind this worker. The redundancy-peer path
	// excludes candidates from the ANCHOR's supplier: a same-supplier "peer" is
	// not an independent cross-check (a multi-worker supplier could verify its own
	// forged result), so it can never be a valid redundancy/tiebreak peer.
	SupplierID uuid.UUID
	HWClass    string
	// Engine + BuildHash are the finer verification-class axes carried alongside
	// HWClass so a redundancy peer can be pinned to the SAME (hw_class, engine,
	// build_hash) — byte-exact verification only ever compares peers in one class
	// (two engines / two builds can emit different bytes on identical hardware).
	Engine     string
	BuildHash  string
	MemoryGB   float32
	Reputation float32
	TPS        map[string]float32 // job_type -> tokens/sec
	LastSeen   time.Time
	Tier       int  // 0-3
	Throttled  bool // currently pausing new claims (memory pressure)
	Warm       bool // already has the job's model warm in its pool (re-rank bonus only)
	// ThermalDegraded is the INVERSE of workers.thermal_ok (docs/internal/
	// CREED_AND_PATH_TO_TEN.md, "Thermal sustained-vs-peak throughput on fanless
	// Apple Silicon" 4→5): true when at least one of this worker's benchmarks
	// failed the sustained-load thermal proxy (runners.rs sustained_throughput —
	// late-window throughput dropped >15% below early-window). Deliberately
	// inverted from the DB column's polarity so the Go zero value (false, i.e.
	// "not known to degrade") is the SAFE default — a test/call site that never
	// sets this field (every pre-existing MatchWorker literal in this codebase)
	// is unaffected, exactly like an older/never-benchmarked worker that has not
	// had the chance to fail the probe. Unlike Throttled (a live, momentary
	// pause), this is NOT a hard filter — a throttling chip is still real, usable
	// supply, just one whose advertised (peak) TPS overstates what it sustains.
	// See thermalPenalty/matchScore below.
	ThermalDegraded bool
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
		// Finer verification-class pins (redundancy-peer path only): when set, a
		// peer must share the anchor's engine AND build hash so any byte-exact
		// comparison stays WITHIN one class. Empty pin = do not filter on that axis.
		if t.PinEngine != "" && w.Engine != t.PinEngine {
			continue
		}
		if t.PinBuildHash != "" && w.BuildHash != t.PinBuildHash {
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

// thermalPenalty is the multiplicative re-rank penalty applied to a candidate whose
// stored thermal_ok came back false — i.e. ThermalDegraded (docs/internal/
// CREED_AND_PATH_TO_TEN.md, "Thermal sustained-vs-peak throughput on fanless Apple
// Silicon" 4→5). Its advertised TPS comes from `bench`'s 20-second peak probe — real
// measurement (scripts/bench-sustained, item 3→4 of the same facet) shows a
// throttling fanless chip can lose 20-40% of that peak once a job runs for real
// minutes, so its peak-derived score overstates what it will actually sustain across
// a multi-minute job. 0.7 (a 30% haircut) sits inside that real measured range:
// enough that an otherwise-equal degraded candidate loses a close contest to a
// non-degraded one, but not so large that a throttling chip with a genuinely much
// higher peak becomes unpickable — it is a re-rank, same spirit as warmBonus, never
// a hard filter (a throttling worker is still real, usable supply; Match's filters
// above are untouched).
const thermalPenalty = 0.7

// matchScore ranks a candidate for a job type: reputation × throughput, scaled by the
// warm bonus when the worker has the model warm and by the thermal penalty when its
// sustained-load behavior is known to be poor. Pure and tiny so both preferences are
// unit-testable in isolation (see TestMatchPrefersWarmWorker,
// TestMatchPenalizesPoorThermalHistory).
func matchScore(w MatchWorker, jobType string) float32 {
	s := w.Reputation * w.TPS[jobType]
	if w.Warm {
		s *= warmBonus
	}
	if w.ThermalDegraded {
		s *= thermalPenalty
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
	return s.SelectRedundancyPeerExcluding(ctx, jobType, modelRef, minMemGB, primaryWorker, nil, nil)
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
func (s *Store) SelectRedundancyPeerExcluding(ctx context.Context, jobType, modelRef string, minMemGB float32, anchor uuid.UUID, also, alsoSuppliers []uuid.UUID) (uuid.UUID, error) {
	// The anchor's hardware class anchors the search: peers must match it.
	primary, err := s.GetWorkerProfile(ctx, anchor)
	if err != nil {
		return uuid.Nil, err
	}

	candidates, err := s.CandidateWorkers(ctx, jobType, modelRef, minMemGB)
	if err != nil {
		return uuid.Nil, err
	}
	// A redundancy peer must be INDEPENDENT: a different worker AND a different
	// supplier from the anchor (backlog P0 item 6). prunePeers enforces both, so a
	// multi-worker supplier can never be drawn as its own cross-check peer.
	pruned := prunePeers(candidates, anchor, primary.SupplierID, also, alsoSuppliers)

	ranked, err := Match(MatchTask{
		JobType:     jobType,
		MinMemoryGB: minMemGB,
		HWClasses:   []string{primary.HWClass}, // same hw class only
		// Pin the FINER verification class too: a redundancy peer must share the
		// anchor's engine AND build hash, so any byte-exact comparison stays within
		// one (hw_class, engine, build_hash) class. An anchor with an unknown
		// ("") build hash does NOT pin on build (PinBuildHash stays empty), so the
		// search degrades to hw_class+engine rather than excluding every peer — the
		// missing finer axis is then handled at verify time (provisional trust on a
		// cross-class byte mismatch), never a fabricated same-class pass.
		PinEngine:    primary.Engine,
		PinBuildHash: primary.BuildHash,
		Tier:         "batch",
	}, pruned)
	if err != nil {
		// Heterogeneous-fleet silent-degradation signal (Scheduling & Matching Engine
		// 7->8, docs/internal/CREED_AND_PATH_TO_TEN.md). No same-class peer was found.
		// Count it as a degradation event ONLY when there WERE independent candidates
		// to consider (pruned non-empty) — i.e. live, eligible-for-this-job-type,
		// distinct-worker-and-supplier supply existed, just not of the anchor's
		// hardware/verification class. That is the exact thin-mixed-fleet case where
		// redundancy, warm-routing, and tiebreak quietly stop working with no visible
		// signal. We test `pruned`, NOT the raw `candidates`: CandidateWorkers always
		// includes the anchor itself (and any same-supplier worker), so a fleet with
		// ONLY the anchor online yields a non-empty candidates but an EMPTY pruned —
		// an empty-independent-fleet condition, already obvious, deliberately NOT
		// counted here (only the "supply exists but wrong class" case is).
		if errors.Is(err, ErrNoSupply) && len(pruned) > 0 {
			noHedgePeerFound.Add(1)
		}
		return uuid.Nil, err // ErrNoSupply when no same-class peer is free
	}
	// Speed Lane wave 1B (planner.go rankPeersBySpeed): among the eligible
	// same-class peers, dispatch to the one that will FINISH the re-run
	// soonest — warm-for-the-model first (a cold GGUF load dwarfs any tps
	// edge), then highest measured tps for the job type, then Match's own
	// reputation-weighted order as the residual tie-break. Every caller of
	// this function (straggler hedge, tiebreak, dispute re-verify, no-peer
	// probe) wants either "the fastest peer" or merely "a peer exists", so
	// the re-rank is safe for all of them: eligibility (class/engine/build
	// pins, independence pruning) is untouched — only the ORDER among already
	// eligible peers changes.
	return rankPeersBySpeed(ranked, jobType)[0].ID, nil
}

// SelectEndgameRacePeer picks the peer for an ENDGAME RACE duplicate (Speed
// Lane wave 1B, workers.go raceEndgameTails): the fastest IDLE same-class
// independent worker that would not pay a cold model load. It is deliberately
// stricter than SelectRedundancyPeerExcluding on two axes:
//
//   - IDLE only: the race exists to convert SPARE capacity into tail latency
//     cut. Pinning the duplicate to a busy worker just queues it behind that
//     worker's current task — no win, pure duplicate cost. BusyWorkerIDs
//     resolves idleness (no running task, no pinned queued/retrying claim) in
//     one query over the eligible candidates.
//   - WARM only (when the job has a model): racing the tail onto a peer that
//     must first cold-load the model for minutes cannot beat a chunk that is
//     already running on a warm worker in the common case — that is the same
//     physics as the cold-model hedge-storm suppression on the straggler side
//     (latency_watchdog.go). A model-less job type has nothing to load and
//     skips the warm gate.
//
// Same independence rules as every redundancy draw (distinct worker AND
// distinct supplier from the anchor, same (hw_class, engine, build_hash)
// class). Returns ErrNoSupply when no idle warm same-class peer is free — the
// sweep simply leaves the chunk to the ordinary 90s hedge path.
func (s *Store) SelectEndgameRacePeer(ctx context.Context, jobType, modelRef string, minMemGB float32, anchor uuid.UUID) (uuid.UUID, error) {
	primary, err := s.GetWorkerProfile(ctx, anchor)
	if err != nil {
		return uuid.Nil, err
	}
	candidates, err := s.CandidateWorkers(ctx, jobType, modelRef, minMemGB)
	if err != nil {
		return uuid.Nil, err
	}
	pruned := prunePeers(candidates, anchor, primary.SupplierID, nil, nil)
	ranked, err := Match(MatchTask{
		JobType:      jobType,
		MinMemoryGB:  minMemGB,
		HWClasses:    []string{primary.HWClass},
		PinEngine:    primary.Engine,
		PinBuildHash: primary.BuildHash,
		Tier:         "batch",
	}, pruned)
	if err != nil {
		return uuid.Nil, err // ErrNoSupply when no same-class peer is online at all
	}
	ids := make([]uuid.UUID, len(ranked))
	for i, w := range ranked {
		ids[i] = w.ID
	}
	busy, err := s.BusyWorkerIDs(ctx, ids)
	if err != nil {
		return uuid.Nil, err
	}
	idle := make([]MatchWorker, 0, len(ranked))
	for _, w := range ranked {
		if busy[w.ID] {
			continue
		}
		if modelRef != "" && !w.Warm {
			continue // never cold-race: a minutes-long load cannot cut a seconds tail
		}
		idle = append(idle, w)
	}
	if len(idle) == 0 {
		return uuid.Nil, ErrNoSupply
	}
	return rankPeersBySpeed(idle, jobType)[0].ID, nil
}

// prunePeers drops every candidate that cannot serve as an INDEPENDENT redundancy
// peer for an anchor: the anchor worker itself, every id in `also`, and any worker
// from the anchor's OWN supplier (`anchorSupplier`). A same-supplier peer is not an
// independent cross-check — a multi-worker supplier could verify its own forged
// result — so it is never eligible (backlog P0 item 6). Pure, so the
// supplier-distinctness guarantee is unit-tested without a database. An unknown
// (`uuid.Nil`) anchor supplier disables only the supplier gate, never the worker gate.
func prunePeers(candidates []MatchWorker, anchor, anchorSupplier uuid.UUID, also, alsoSuppliers []uuid.UUID) []MatchWorker {
	excluded := map[uuid.UUID]bool{anchor: true}
	for _, id := range also {
		excluded[id] = true
	}
	// Suppliers that cannot supply an independent peer: the anchor's own supplier,
	// plus any in `alsoSuppliers` (e.g. the OTHER disputants in a tiebreak, so the
	// third opinion is independent of BOTH sides, not just the committing side).
	excludedSup := map[uuid.UUID]bool{}
	if anchorSupplier != uuid.Nil {
		excludedSup[anchorSupplier] = true
	}
	for _, id := range alsoSuppliers {
		if id != uuid.Nil {
			excludedSup[id] = true
		}
	}
	out := make([]MatchWorker, 0, len(candidates))
	for _, c := range candidates {
		if excluded[c.ID] {
			continue
		}
		if excludedSup[c.SupplierID] {
			continue // anchor's (or another disputant's) supplier — not independent
		}
		out = append(out, c)
	}
	return out
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
//     itself up on work a cheaper idle class could take), THEN the throughput
//     tiebreak (this worker's measured tps for the task's job type, then its
//     bw_gbps — selection only, so a faster worker drains its quickest work first),
//     then oldest,
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
//
// ClaimTaskSQL renders the EXACT, verbatim claim CTE ClaimTask executes,
// parameterized only by claimedByPredicate — the one condition split out by
// P-splitclaim (either the pinned branch "t.claimed_by = $1 AND
// t.started_at IS NULL" or the general "t.claimed_by IS NULL" branch).
// Everything else — every JOIN, every correlated subquery (cheaper_class_online,
// warm_for_task, job_dispatched_count, the budget-governor projected-spend
// subqueries), the worker_tps_cache LEFT JOIN (worker_tps — Control Plane Hot
// Path 7->8: a maintained cache, not a correlated subquery, since that rung),
// and the full computed ORDER BY — is fixed, literal SQL text, identical to
// what ClaimTask itself binds and executes via $1 (worker id), $2 (tier), $3
// (selfCostRank).
//
// PATCH (Control plane hot path 4.5->5, docs/internal/CREED_AND_PATH_TO_TEN.md
// "Make the benchmark measure the real query"): this function is the ONE
// place this SQL text is written. It used to live only as an inline closure
// captured inside ClaimTask, so the bench harness (scripts/bench-local.sh)
// could measure nothing but a hand-simplified stand-in (a bare 6-column
// WHERE + a 2-column ORDER BY, missing every JOIN, every correlated
// subquery, and the real computed ORDER BY) run with the planner's
// enable_seqscan forced off — cosmetically similar, provably NOT the shipped
// query. Exporting the render function lets `control print-claim-sql` (see
// main.go) print this exact string to stdout for the harness to EXPLAIN
// ANALYZE verbatim, and lets a Go test assert ClaimTask's own prepared SQL
// (captured via a tracing query tracer) is byte-identical to this output —
// so "the benchmark runs the real query" is a provable equality, not an
// assertion.
func ClaimTaskSQL(claimedByPredicate string) string {
	return fmt.Sprintf(`WITH me AS (
	   -- The ONE claiming worker + its supplier, resolved ONCE (w.id = $1). Every
	   -- per-JOB hard filter below (memory, hw_classes, supported_jobs/models,
	   -- residency, reputation, private-pool, tier, payout floor) compares the
	   -- JOB against THIS worker/supplier — none of it depends on the individual
	   -- task, so it belongs here, computed once per job, not once per task.
	   SELECT w.id AS worker_id, w.supplier_id, w.hw_class,
	          w.effective_memory_gb, w.memory_gb, w.supported_jobs,
	          w.supported_models, w.min_payout_usd_hr, w.throttled,
	          s.id AS supplier_id_s, s.status AS supplier_status,
	          s.reputation, s.data_country
	     FROM workers w
	     JOIN suppliers s ON s.id = w.supplier_id
	    WHERE w.id = $1
	 ),
	 -- PATCH (Control plane hot path 8->9, docs/internal/CREED_AND_PATH_TO_TEN.md
	 -- "Get the correlated-subquery cost out of the transactional hot path" —
	 -- the O(queue x fleet) cost entry 61 root-caused). eligible_jobs enforces
	 -- EVERY per-job-only predicate (the whole hard filter EXCEPT the two genuinely
	 -- per-task conditions: t.status/visible_at and the claimed_by branch) and
	 -- computes cheaper_class_online ONCE PER CANDIDATE JOB. It is MATERIALIZED on
	 -- purpose: without the barrier Postgres inlines this CTE back into the
	 -- tasks->jobs fan-out and re-evaluates the cheaper_class_online workers-scan
	 -- once per TASK (measured at a 12k-task/600-worker backlog: SubPlan seq-scanning
	 -- workers 12,001 times), which is exactly the pre-existing cost entry 61
	 -- reported. With MATERIALIZED it runs once per candidate JOB (~240 vs 12,000
	 -- at that scale), a >10x cut in the fleet scans. cheaper_class_online depends
	 -- only on j.* and $3 (the claiming worker's cost rank) — never on t — so
	 -- computing it per job is provably identical to the old per-task value (proven
	 -- by TestClaimCheaperClassOnlinePerJobEquivalence: the two orderings produce
	 -- byte-identical full ordered task lists at every queue position).
	 eligible_jobs AS MATERIALIZED (
	   SELECT j.id AS job_id, j.tier, j.job_type, j.model_ref,
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
	     -- SUFFICIENT class", never starvation). SKIP LOCKED + the hard filter are
	     -- untouched — this only re-orders tasks the claiming worker already passes.
	     EXISTS (
	       SELECT 1 FROM workers w2
	         JOIN suppliers s2 ON s2.id = w2.supplier_id
	       WHERE w2.id <> me.worker_id
	         AND w2.last_seen_at IS NOT NULL
	         AND w2.last_seen_at > now() - interval '60 seconds'
	         AND s2.status = 'active'
	         AND NOT COALESCE(w2.throttled, false)
	         AND (`+hwClassCostRankSQL("w2.hw_class")+`) < $3
	         AND COALESCE(j.min_memory_gb,0) <= COALESCE(w2.effective_memory_gb, w2.memory_gb, 0)
	         AND (j.hw_classes IS NULL OR w2.hw_class = ANY(j.hw_classes))
	         AND COALESCE(w2.supported_jobs,'{}') @> ARRAY[j.job_type]
	         AND (COALESCE(j.model_ref,'') = '' OR COALESCE(w2.supported_models,'{}') @> ARRAY[j.model_ref])
	     ) AS cheaper_class_online,
	     -- Dispatch-interleave fairness (Scheduling & Matching Engine 6.5->7,
	     -- docs/internal/CREED_AND_PATH_TO_TEN.md): how many of THIS job's own
	     -- tasks have already been dispatched (running or complete). Ordered
	     -- ASC in the claim's ORDER BY, just above the final oldest-first
	     -- tiebreak: a job that has already had many tasks served steps back so a
	     -- smaller/newer job's tasks interleave instead of waiting out a giant
	     -- job's entire multi-thousand-task backlog first, purely on age.
	     --
	     -- PATCH (Control plane hot path 8->9): this is a per-JOB count (it depends
	     -- only on t.job_id, identical for every task of the job), so it lives HERE
	     -- in eligible_jobs — computed ONCE per candidate job — not re-counted per
	     -- task in the next CTE below. At a 12k-task/240-job backlog that is 240
	     -- count subqueries instead of 12,000, the second-largest per-queue cost
	     -- after cheaper_class_online (measured: it is what kept the loaded:near-
	     -- empty ratio high once the fleet scan was already per-job). Selection
	     -- only — it re-orders tasks the worker already passes; eligibility unchanged.
	     (SELECT count(*) FROM tasks jt
	        WHERE jt.job_id = j.id AND jt.status IN ('running','complete')
	     ) AS job_dispatched_count,
	     -- Throughput tiebreak (selection only, determinism-SAFE): THIS worker's most
	     -- recent measured tokens/sec FOR THIS JOB's job type, from the benchmark the
	     -- agent reports (maintained in worker_tps_cache by UpsertWorker, read as a
	     -- plain indexed lookup). PATCH (Control plane hot path 8->9): job_type is a
	     -- per-JOB attribute and $1 is the constant claiming worker, so this is per
	     -- job — it moves here (looked up once per candidate job) rather than a
	     -- LEFT JOIN re-evaluated for every one of the 12k candidate tasks below.
	     (SELECT COALESCE(wtc.tps, 0) FROM worker_tps_cache wtc
	        WHERE wtc.worker_id = $1 AND wtc.job_type = j.job_type
	     ) AS worker_tps,
	     -- Warm-model tiebreak (P-warmtiebreak, "Scheduling & matching engine" 8->9):
	     -- is THIS JOB's model already loaded warm on the claiming worker ($1)? A
	     -- worker with the model warm wins a tie over one that would pay a cold load.
	     -- PATCH (Control plane hot path 8->9): model_ref is per-JOB and $1 is
	     -- constant, so this too is per job — computed once per candidate job, not
	     -- per candidate task.
	     (COALESCE(j.model_ref,'') <> '' AND EXISTS (
	       SELECT 1 FROM worker_model_state wms
	         WHERE wms.worker_id = $1 AND wms.model_id = j.model_ref
	           AND wms.last_seen_warm > now() - interval '60 seconds'
	     )) AS warm_for_task
	   FROM jobs j, me
	   WHERE j.status NOT IN ('cancelled','failed')
	     -- Only jobs that ACTUALLY have a claimable task right now reach here. A job
	     -- with no queued/retrying-and-visible task this worker could take can never
	     -- produce a next-CTE row (the tasks join below filters it out anyway), so
	     -- pre-restricting eligible_jobs to jobs-with-work keeps the result set
	     -- identical while skipping the per-job cheaper_class_online fleet scan for
	     -- every finished job (the jobs table keeps every completed job forever —
	     -- without this guard the claim would re-price the whole hardware fleet against
	     -- every historical job on every claim). The guard admits a task that is either
	     -- unclaimed OR pinned-to-THIS-worker-and-unstarted, covering BOTH claim
	     -- branches (the spliced claimed_by predicate below) so the pinned/hedge
	     -- branch is never starved of its job. Index-served by
	     -- tasks_ready_unclaimed_idx (unclaimed) / tasks_pkey.
	     AND EXISTS (
	           SELECT 1 FROM tasks tt
	            WHERE tt.job_id = j.id
	              AND tt.status IN ('queued','retrying')
	              AND (tt.claimed_by IS NULL
	                   OR (tt.claimed_by = $1 AND tt.started_at IS NULL))
	              AND COALESCE(tt.visible_at, tt.created_at) <= now())
	     AND me.supplier_status = 'active'
	     -- SAFE memory: effective allocatable (after the supplier's reserved
	     -- headroom) once the worker has heartbeated it, else total memory_gb
	     -- (a just-registered worker is unchanged — no regression).
	     AND COALESCE(j.min_memory_gb,0) <= COALESCE(me.effective_memory_gb, me.memory_gb, 0)
	     -- never hand work to a worker pausing for memory pressure.
	     AND NOT COALESCE(me.throttled, false)
	     AND (j.hw_classes IS NULL OR me.hw_class = ANY(j.hw_classes))
	     AND COALESCE(me.supported_jobs,'{}') @> ARRAY[j.job_type]
	     AND (COALESCE(j.model_ref,'') = '' OR COALESCE(me.supported_models,'{}') @> ARRAY[j.model_ref])
	     AND (j.data_residency IS NULL OR me.data_country = ANY(j.data_residency))
	     -- Elite-supplier gate (DEEP_RESEARCH_V2 §6.4 anti-defection): a high
	     -- min_reputation job is claimable only by a supplier who earned that
	     -- reputation on the platform (0 = any supplier; the unchanged path).
	     AND COALESCE(j.min_reputation,0) <= me.reputation
	     -- Private Deployment (research §3): a private_pool job is claimable only by a
	     -- supplier the buyer bound to their dedicated fleet (private_pool_members).
	     AND (NOT COALESCE(j.private_pool,false) OR EXISTS (
	           SELECT 1 FROM private_pool_members m
	            WHERE m.buyer_id = j.buyer_id AND m.supplier_id = me.supplier_id_s))
	     AND (j.tier <> 'trusted' OR $2 >= 2)
	     AND (COALESCE(j.offered_rate_usd_hr,1e9) >= COALESCE(me.min_payout_usd_hr,0))
	     -- Budget Governor (Plane C §12 / Plane D §14 D8): when the job has a hard
	     -- spend cap, NEVER dispatch a new task whose projected charge would breach
	     -- it. Projected = already-charged on this job's tasks (buyer_charge debits,
	     -- same ledger shape failJobAndSettleOnce settles from) + the per-task estimate for
	     -- every IN-FLIGHT (claimed, running, not-yet-committed) task of this job
	     -- (each will charge at commit, so it is exposure-in-flight) + ONE more for
	     -- the candidate. Counting in-flight work is what makes the cap hold under
	     -- the agent's bounded concurrency ([2,4] permits): without it, several
	     -- tasks could be claimed+running but uncommitted (contributing $0 charged)
	     -- and each then charge past the cap. A capped+exhausted job's tasks stay
	     -- queued (the cap PREVENTS dispatch — it never refunds); jobs with no cap
	     -- are unaffected (j.max_usd IS NULL). This is a per-JOB predicate, so it too
	     -- moves here (evaluated once per job, not once per task).
	     AND (j.max_usd IS NULL OR (
	           (SELECT COALESCE(SUM(-le.amount_usd),0) FROM ledger_entries le
	            WHERE le.kind = 'buyer_charge'
	              AND le.task_id IN (SELECT id FROM tasks WHERE job_id = j.id))
	           + (SELECT count(*) FROM tasks it
	                WHERE it.job_id = j.id AND it.status = 'running')
	             * (COALESCE(j.estimated_usd,0) / NULLIF(j.task_count,0))
	           + COALESCE(j.estimated_usd,0) / NULLIF(j.task_count,0)
	         ) <= j.max_usd)
	 ),
	 next AS (
	   -- Every ORDER BY signal (cheaper_class_online, worker_tps, warm_for_task,
	   -- job_dispatched_count, tier) is PER-JOB — computed once per candidate job in
	   -- eligible_jobs above and carried on ej. The ONLY genuinely per-task columns
	   -- are t.id, t.created_at, and t.claimed_by. So this CTE is a lean tasks scan
	   -- joined to the (small) eligible_jobs set: no per-task subquery, no per-task
	   -- fleet scan, no per-task benchmark lookup. PATCH (Control plane hot path
	   -- 8->9): moving worker_tps / warm_for_task / job_dispatched_count up into
	   -- eligible_jobs (all per-job) collapsed the remaining O(queue) work here to
	   -- just the tasks scan + the LIMIT-1 sort, which is what flattens the
	   -- loaded:near-empty claim-latency ratio (measured: entry 61's ~190x, and
	   -- ~21x with only cheaper_class_online hoisted, down to a small multiple once
	   -- all four per-job signals are hoisted).
	   SELECT t.id,
	     ej.cheaper_class_online,
	     ej.worker_tps,
	     ej.warm_for_task,
	     ej.job_dispatched_count
	   FROM tasks t
	     -- Only the per-JOB-eligible jobs survive to here (eligible_jobs above), so
	     -- this join IS the whole hard filter except the two per-task conditions
	     -- below. Every ordering signal + tier rides along on ej, already computed
	     -- once per job.
	     JOIN eligible_jobs ej ON ej.job_id = t.job_id
	   WHERE t.status IN ('queued','retrying')
	     AND COALESCE(t.visible_at, t.created_at) <= now()
	     -- claimable when unclaimed, OR pre-claimed (pinned) to THIS worker and
	     -- not yet started (a tiebreak/hedge dispatch pinned to a chosen peer).
	     -- PATCH (P-splitclaim, docs/CREED_AND_PATH_TO_TEN.md "Control plane hot
	     -- path" 6->7): this used to be one OR'd condition
	     -- (t.claimed_by IS NULL OR (t.claimed_by = $1 AND t.started_at IS NULL)),
	     -- which the query planner cannot serve from tasks_ready_unclaimed_idx
	     -- (a partial index WHERE claimed_by IS NULL only) because the second
	     -- OR-branch needs rows the index does not contain. The token below is
	     -- substituted by the Go caller with ONE of two plain, non-parameterized
	     -- literals — the
	     -- pinned branch tried first, then the plain "claimed_by IS NULL" branch —
	     -- so the common (unclaimed) case is a direct, index-servable predicate
	     -- instead of a planner-defeating OR. These are the ONLY genuinely
	     -- per-task conditions; every other filter is per-job (eligible_jobs above).
	     AND (%s)
	     -- Verification-requeue worker exclusion (Scheduling & Matching Engine 8->9,
	     -- docs/internal/CREED_AND_PATH_TO_TEN.md): a task that just failed a honeypot
	     -- was requeued with excluded_worker = the worker that failed it, in force
	     -- until excluded_until. Skip that worker for the window so a DIFFERENT worker
	     -- gets first crack; once the window elapses (or for any other worker) the
	     -- task is claimable normally, so a thin/single-worker fleet is never
	     -- permanently starved of the retry. Genuinely per-task (references $1) — it
	     -- belongs here, not in eligible_jobs.
	     AND (t.excluded_worker IS NULL
	          OR t.excluded_worker <> $1
	          OR t.excluded_until IS NULL
	          OR t.excluded_until <= now())
	   -- Pinned-to-this-worker jumps the line, then priority tier, THEN the
	   -- hardware-matched routing preference: within a tier, prefer tasks NO cheaper
	   -- eligible class is online for (cheaper_class_online = false sorts first), so
	   -- an expensive worker defers work a cheaper idle class could take instead of
	   -- tying itself up on it; THEN the throughput tiebreak (this worker's measured
	   -- tokens/sec for the task's job type); THEN warm_for_task (PATCH
	   -- P-warmtiebreak — replaces the old constant-per-query $4::real bw_gbps
	   -- no-op with a real per-row "is the task's model already loaded on this
	   -- worker" check, so a warm worker wins a tie over one that would pay a
	   -- cold load); THEN job_dispatched_count ASC (PATCH P-fairness, Scheduling &
	   -- Matching Engine 6.5->7): a job that has already had many of its OWN tasks
	   -- served steps back so a smaller/newer job's tasks interleave, instead of a
	   -- single multi-thousand-task job monopolizing every worker purely by being
	   -- older; finally oldest-first as the last tiebreak. These preference terms
	   -- all sit BELOW pin + priority on purpose — a hedge/tiebreak pinned here, and
	   -- a priority job, are still served promptly even by an expensive, slower,
	   -- cold, or already-well-served-job worker. Every term below pin+priority is
	   -- selection only (no output change): it only re-orders tasks the worker
	   -- already passes; the hard filter above and SKIP LOCKED are unchanged. NB:
	   -- (t.claimed_by = $1) is a constant within each branch (the WHERE above pins
	   -- it), so it sorts consistently exactly as before the eligible_jobs split.
	   ORDER BY (t.claimed_by = $1) DESC, (ej.tier = 'priority') DESC,
	            cheaper_class_online ASC, worker_tps DESC, warm_for_task DESC,
	            job_dispatched_count ASC, t.created_at ASC
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
		claimedByPredicate)
}

func (s *Store) ClaimTask(ctx context.Context, w WorkerAuth) (*ClaimedTask, error) {
	// PATCH (Scalability headroom 4.5->5, docs/internal/CREED_AND_PATH_TO_TEN.md
	// "Measure instead of estimate"): every ClaimTask call — the exact hot path a
	// poller (real or synthetic) exercises — is timed end-to-end (Begin through
	// Commit/Rollback, including a no-work result) into the process-wide
	// cx_claim_duration histogram, so a real load test against /v1/worker/poll
	// produces a REAL p50/p90/p99 for this call, not an estimate derived from
	// reading the query. Recorded via defer so every return path (error, no-work,
	// claimed) is captured exactly once.
	claimStart := time.Now()
	defer func() { claimDuration.observe(time.Since(claimStart)) }()

	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)

	// Reject unregistered workers loudly rather than silently claiming nothing.
	// We also read the worker's hw_class here: it feeds its cost rank for the
	// hardware-matched routing preference below. (bw_gbps used to be read
	// alongside it for the claim's tiebreak too, but that term was a no-op — the
	// SAME worker's bw_gbps bound once per query execution, identical for every
	// candidate row — and has been replaced by a real per-row warm-model check;
	// see the P-warmtiebreak note on the claim query's ORDER BY below.)
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
	//
	// PATCH (P-completedtasks, docs/internal/CREED_AND_PATH_TO_TEN.md "Control
	// plane hot path" 7->8): this used to be a `count(*)` over ALL of this
	// supplier's tasks, re-scanned on EVERY single claim — an O(supplier's
	// lifetime completed tasks) cost paid on the hottest transactional path in
	// the system, for a number that only actually changes once per real commit.
	// `suppliers.completed_tasks` is now a maintained running column
	// (incremented in CommitTask), so this is a plain O(1) column read.
	var rep float32
	var jobsDone uint64
	if err := tx.QueryRow(ctx,
		`SELECT s.reputation, s.completed_tasks FROM suppliers s WHERE s.id = $1`,
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
	// claimTaskQuery renders the full claim CTE with claimedByPredicate spliced into
	// the one condition split out above (P-splitclaim). Called twice below: the
	// pinned branch first (cheap, rare), then the plain unclaimed branch, so the
	// common case is a simple index-servable predicate instead of an OR.
	//
	// PATCH (Control plane hot path 4.5->5, docs/internal/CREED_AND_PATH_TO_TEN.md
	// "Make the benchmark measure the real query"): this used to be an inline
	// closure whose rendered SQL text existed nowhere but inside this one call —
	// so nothing outside ClaimTask could ever prove a benchmark was running the
	// SAME string. It is now a thin alias for the package-level ClaimTaskSQL
	// (defined just above this function), the ONE place this text is written.
	// `control print-claim-sql` (main.go) calls ClaimTaskSQL directly and prints
	// its return value verbatim, so scripts/bench-local.sh can shell out to the
	// real binary and EXPLAIN ANALYZE the literal bytes ClaimTask itself
	// executes — not a hand-copied stand-in that can silently drift from this
	// function.
	claimTaskQuery := ClaimTaskSQL
	scanClaim := func(claimedByPredicate string) error {
		return tx.QueryRow(ctx, claimTaskQuery(claimedByPredicate),
			w.WorkerID, int(tier), selfCostRank,
		).Scan(&c.TaskID, &c.JobID, &c.JobType, &c.ModelRef, &c.InputRef, &c.ResultKey,
			&c.OutputRef, &c.Tier, &c.VerifPolicy, &c.JobTypeSpec, &c.OfferedRateUsdHr,
			&c.ChunkIndex, &c.IsHoneypot)
	}
	// Pinned branch first: a tiebreak/hedge dispatch already chose this worker for
	// a specific task (claimed_by = $1, not yet started) — cheap and rare. Only if
	// nothing is pinned do we fall through to the general, now index-servable,
	// claimed_by IS NULL branch.
	err = scanClaim("t.claimed_by = $1 AND t.started_at IS NULL")
	if errors.Is(err, pgx.ErrNoRows) {
		err = scanClaim("t.claimed_by IS NULL")
	}
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
	// PATCH (Control plane hot path 7->8, docs/internal/CREED_AND_PATH_TO_TEN.md
	// "Move markBudgetStoppedJobs off the claim path onto its own ticker"): this
	// used to run markBudgetStoppedJobs — an UPDATE...WHERE EXISTS scan over
	// EVERY capped job in the system, each with its own correlated
	// budget-projected-spend subquery — INSIDE this transaction, on EVERY single
	// claim (claimed or not). A capped job's budget state going stale by up to
	// one ticker interval (see budgetStopInterval, control/workers.go) is a
	// display/event-timing detail — the cap ITSELF is still enforced synchronously,
	// every claim, by the unrelated WHERE (j.max_usd IS NULL OR ...) predicate
	// already baked into ClaimTaskSQL's hard filter above; a job over budget is
	// NEVER dispatched here regardless of when its budget_state row last flipped.
	// This sweep only pays for the visible "paused_for_budget" state transition +
	// the one-time budget_stopped event, not for the dispatch guarantee.
	if err := tx.Commit(ctx); err != nil {
		return nil, err
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
	// the tier math (reputationTier) stays the single source of truth. Reads the
	// maintained suppliers.completed_tasks column (Control Plane Hot Path 7->8,
	// docs/internal/CREED_AND_PATH_TO_TEN.md) instead of re-deriving it with a
	// count(*) scan — this admin diagnostic used to carry its own separate copy
	// of the exact same expensive query ClaimTask had; fixing one and not the
	// other would have left them silently able to drift apart.
	var rep float32
	var jobsDone uint64
	if err := s.pool.QueryRow(ctx,
		`SELECT s.reputation, s.completed_tasks
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
		               j.model_ref, j.offered_rate_usd_hr, j.tier AS job_tier, j.min_reputation,
		               j.private_pool, j.buyer_id
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
		          WHEN COALESCE(c.private_pool,false) AND NOT EXISTS (SELECT 1 FROM private_pool_members m WHERE m.buyer_id = c.buyer_id AND m.supplier_id = s.id) THEN 'private_pool_excluded'
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
		   count(*) FILTER (WHERE reason = 'payout_floor' OR reason = 'tier_gate' OR reason = 'reputation_too_low' OR reason = 'private_pool_excluded'),
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
// failJobAndSettleOnce settles from) PLUS one more task's estimated cost
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

// SweepBudgetStops runs markBudgetStoppedJobs in its OWN short transaction, on its
// own ticker cadence (budgetStopInterval, control/workers.go), instead of inside
// ClaimTask's transaction on every single claim.
//
// PATCH (Control plane hot path 7->8, docs/internal/CREED_AND_PATH_TO_TEN.md "Get
// the correlated-subquery cost out of the transactional hot path"): markBudgetStoppedJobs
// is an UPDATE...WHERE EXISTS scan over EVERY capped job in the whole system, each
// row paying its own correlated budget-projected-spend subquery (a SUM over
// ledger_entries plus a COUNT over in-flight tasks) — real, non-trivial cost that
// used to run inside ClaimTask's transaction on EVERY claim attempt, claimed or
// not, regardless of whether this particular claim touched a capped job at all.
// Moving it to a periodic ticker (a 5-10s cadence, per the rung text) removes that
// cost from the hottest transactional path entirely; the claim's OWN dispatch
// safety (a capped job's task is never claimed past its cap) is unaffected because
// it comes from a separate, still-synchronous predicate baked into ClaimTaskSQL's
// hard filter — this sweep only maintains the VISIBLE budget_state transition +
// the one-time event, at a bounded staleness of at most one ticker interval.
func (s *Store) SweepBudgetStops(ctx context.Context) (int, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback(ctx)
	stopped, err := markBudgetStoppedJobs(ctx, tx)
	if err != nil {
		return 0, err
	}
	if err := tx.Commit(ctx); err != nil {
		return 0, err
	}
	return stopped, nil
}

// markBudgetStoppedJobs flips every CAPPED job that still has a claimable task the
// budget gate is refusing to dispatch into budget_state = 'paused_for_budget' and
// emits a budget_stopped event for each, EXACTLY once (the state transition is the
// guard, so a repeated poll does not re-emit). It mirrors the claim's own
// claimable-task predicate (queued/retrying, visible-now, unclaimed, job not
// terminal) so a job is paused only when there is real work the cap is holding
// back. The cap PREVENTS dispatch; no money moves and nothing is refunded. Returns
// the number of jobs newly paused this call so the caller can advance the
// cx_budget_stops_total counter AFTER the transaction commits (a rolled-back sweep
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
