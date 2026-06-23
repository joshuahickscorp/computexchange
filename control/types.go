package main

import (
	"encoding/json"

	"github.com/google/uuid"
)

// types.go — wire contract (the project "horizon"). These structs mirror the
// Rust agent's serde types in agent/src/types.rs EXACTLY: snake_case JSON,
// snake_case string enums, and a tagged JobType. Do not let the representation
// drift from the agent side; this is the single source of truth on Go's side.

// --- string-enum value domains (validated, not separate Go types) ---
//
// hw_class:  apple_silicon_base | apple_silicon_pro | apple_silicon_max |
//            apple_silicon_ultra | cpu
// tier:      batch | priority | trusted
// job type:  embed | batch_infer | audio_transcribe | image_gen | eval | lora_finetune |
//            batch_classification | json_extraction | rerank
// task status: queued | running | complete | failed | retrying
// job status:  queued | running | verifying | complete | failed | cancelled

// validHWClasses is the closed set of hardware classes (matches HardwareClass).
// apple_silicon_cluster (Plane B, docs/PLANE_B.md) is a co-located Mac cluster
// advertising SUMMED member memory as one high-memory worker; the existing claim
// filter routes summed-memory jobs to it with no scheduler change.
var validHWClasses = map[string]bool{
	"apple_silicon_base": true, "apple_silicon_pro": true,
	"apple_silicon_max": true, "apple_silicon_ultra": true,
	"apple_silicon_cluster": true,
	// NVIDIA/CUDA lane, VRAM-tiered. A DISTINCT family from Apple so within-class
	// verification never compares results across architectures (FP kernels differ).
	"nvidia_24g": true, "nvidia_48g": true,
	"nvidia_80g": true, "nvidia_180g": true,
	"cpu": true,
}

// validTiers is the closed set of service tiers.
var validTiers = map[string]bool{"batch": true, "priority": true, "trusted": true}

// validJobTypes is the closed set of job-type tags. The three Turbo workloads
// (batch_classification | json_extraction | rerank) join the original set; each
// has a real result verifier (see verification.go resultsAgree).
var validJobTypes = map[string]bool{
	"embed": true, "batch_infer": true, "audio_transcribe": true,
	"image_gen": true, "eval": true, "lora_finetune": true,
	"batch_classification": true, "json_extraction": true, "rerank": true,
}

// JobType is the tagged job descriptor. The wire form is the serde-tagged enum
// the Rust side emits: {"type":"embed","batch_size":64},
// {"type":"batch_infer","max_tokens":512,"temperature":0.0}, etc. omitempty
// keeps the irrelevant variant fields off the wire so the shape matches.
type JobType struct {
	Type      string `json:"type"`
	BatchSize int    `json:"batch_size,omitempty"`
	// EmbedBinary (embed only): opt-in compact float32 output (PLANE_D §11 D5 /
	// §21 D15). When true the agent emits a binary embedding artifact instead of
	// the JSON `vectors` array. omitempty keeps it off the wire for every other
	// job type and for JSON-default embed jobs; a zero-value (false) decodes
	// against an older agent that never sends it. Persisted in job_type_spec so it
	// round-trips to the agent on dispatch (manifest.params does not).
	EmbedBinary     bool            `json:"binary,omitempty"`
	MaxTokens       uint32          `json:"max_tokens,omitempty"`
	Temperature     float32         `json:"temperature,omitempty"`
	Language        *string         `json:"language,omitempty"`
	Timestamps      bool            `json:"timestamps,omitempty"`
	Resolution      [2]uint32       `json:"resolution,omitempty"`
	Steps           uint32          `json:"steps,omitempty"`
	Rubric          json.RawMessage `json:"rubric,omitempty"`
	Epochs          uint32          `json:"epochs,omitempty"`
	Lr              float32         `json:"lr,omitempty"`
	CheckpointEvery uint32          `json:"checkpoint_every,omitempty"`
	// Turbo workload params (match the Rust agent's matching side):
	//   batch_classification → Labels (the closed label set the model picks from),
	//   json_extraction      → Schema (the JSON schema each item must conform to),
	//   rerank               → TopK   (cut the ranking to the top-K documents).
	Labels []string        `json:"labels,omitempty"`
	Schema json.RawMessage `json:"schema,omitempty"`
	TopK   uint32          `json:"top_k,omitempty"`
}

// ModelRef references a model. Wire: {"kind":"gguf"|"hf"|"mlx","ref":"..."}.
type ModelRef struct {
	Kind string `json:"kind"`
	Ref  string `json:"ref"`
}

// InputRef is one input object: a URL plus its size in bytes.
type InputRef struct {
	URL   string `json:"url"`
	Bytes uint64 `json:"bytes"`
}

// OutputRef is where the merged result is written.
type OutputRef struct {
	URL string `json:"url"`
}

// JobConstraints narrows which workers may run a job.
type JobConstraints struct {
	MinMemoryGB     float32  `json:"min_memory_gb"`
	HWClasses       []string `json:"hw_classes"` // null = any
	MaxDurationSecs uint32   `json:"max_duration_secs"`
	DataResidency   []string `json:"data_residency"` // null = unrestricted
}

// VerificationPolicy controls honeypot/redundancy rates and payout hold.
type VerificationPolicy struct {
	RedundancyFrac float32 `json:"redundancy_frac"`
	HoneypotFrac   float32 `json:"honeypot_frac"`
	PayoutHoldSecs uint32  `json:"payout_hold_secs"`
}

// JobManifest is the full job description submitted by a buyer.
type JobManifest struct {
	ID           uuid.UUID          `json:"id"`
	JobType      JobType            `json:"job_type"`
	Model        ModelRef           `json:"model"`
	Inputs       []InputRef         `json:"inputs"`
	Output       OutputRef          `json:"output"`
	Params       json.RawMessage    `json:"params"`
	Constraints  JobConstraints     `json:"constraints"`
	Verification VerificationPolicy `json:"verification"`
	Tier         string             `json:"tier"`
}

// BenchResult is one benchmark line for a (model, job_type) pair.
type BenchResult struct {
	ModelID   string  `json:"model_id"`
	JobType   string  `json:"job_type"`
	TPS       float32 `json:"tps"` // tokens/sec
	EPS       float32 `json:"eps"` // embeddings/sec
	P99MS     uint32  `json:"p99_ms"`
	ThermalOK bool    `json:"thermal_ok"`
}

// WorkerCapability is what a worker advertises on registration. MinPayoutUsdHr is
// the operator's reservation price ($/hr): the claim's hard filter excludes any
// job whose offered_rate_usd_hr is below it, so a worker never runs work that
// pays under its floor (mirrors the Rust agent's matching side).
type WorkerCapability struct {
	WorkerID        uuid.UUID     `json:"worker_id"`
	SupplierID      uuid.UUID     `json:"supplier_id"`
	HWClass         string        `json:"hw_class"`
	MemoryGB        float32       `json:"memory_gb"`
	MemoryBwGbps    float32       `json:"memory_bw_gbps"`
	SupportedJobs   []string      `json:"supported_jobs"`
	SupportedModels []string      `json:"supported_models"`
	MinPayoutUsdHr  float32       `json:"min_payout_usd_hr"`
	Benchmarks      []BenchResult `json:"benchmarks"`
	AgentVersion    string        `json:"agent_version"`
	OSVersion       string        `json:"os_version"`
}

// TaskDispatch is handed to a worker in response to a poll. result_key is the
// canonical server-side object key the worker PUTs its result to (and echoes in
// the commit); output_url is that same key presigned for upload. The Rust agent
// reads result_key (serde default) and uploads to output_url.
type TaskDispatch struct {
	TaskID           uuid.UUID   `json:"task_id"`
	JobID            uuid.UUID   `json:"job_id"`
	Manifest         JobManifest `json:"manifest"`
	InputURL         string      `json:"input_url"`
	OutputURL        string      `json:"output_url"`
	ResultKey        string      `json:"result_key"`          // canonical result object key (agent echoes it)
	OfferedRateUsdHr float32     `json:"offered_rate_usd_hr"` // $/hr this task pays (matches the worker's min-payout gate)
	Deadline         uint64      `json:"deadline"`
}

// TaskCommit is the worker's result submission.
type TaskCommit struct {
	TaskID        uuid.UUID `json:"task_id"`
	ResultKey     string    `json:"result_key"`
	DurationMS    uint64    `json:"duration_ms"`
	TokensUsed    uint64    `json:"tokens_used"`
	HardwareTempC *float32  `json:"hardware_temp_c"`
}

// Heartbeat is the periodic liveness + telemetry signal (~30s). The resource
// fields (AvailableMemoryGB … Throttled) are the supplier-throttling delta: the
// agent reports its live effective memory and whether it is throttled, and the
// claim's hard filter uses both so a memory-pressured worker is never dispatched
// work. They are optional on the wire (a pre-throttling agent omits them → zero
// values; EffectiveMemoryGB 0 makes the claim fall back to total memory_gb).
//
// LoadedModels is the warm-routing delta (docs/PLANE_D.md §9 D3): the model ids
// currently WARM in the agent's pool. HeartbeatWorker upserts a worker_model_state
// row per id, and the scheduler gives a small re-rank bonus to a worker that has the
// job's model warm (warm only re-ranks — the claim hard filter is unchanged). It is
// optional on the wire (omitempty / a pre-warm agent omits it → nil), so older peers
// still decode; the agent reports real warm ids only.
type Heartbeat struct {
	WorkerID           uuid.UUID  `json:"worker_id"`
	Timestamp          uint64     `json:"timestamp"`
	CPUPct             float32    `json:"cpu_pct"`
	GPUPct             float32    `json:"gpu_pct"`
	GPUTempC           *float32   `json:"gpu_temp_c"`
	CurrentTask        *uuid.UUID `json:"current_task"`
	AvailableMemoryGB  float32    `json:"available_memory_gb"`
	EffectiveMemoryGB  float32    `json:"effective_memory_gb"`
	ReservedHeadroomGB float32    `json:"reserved_headroom_gb"`
	Throttled          bool       `json:"throttled"`
	LoadedModels       []string   `json:"loaded_models,omitempty"` // model ids warm in the agent's pool (warm-routing re-rank)
}

// Earnings is returned by GET /v1/worker/earnings.
type Earnings struct {
	BalanceUSD  float64 `json:"balance_usd"`
	LifetimeUSD float64 `json:"lifetime_usd"`
}

// --- control-plane-local response types (not part of the agent contract) ---

// JobSubmitResponse is the 202 body for POST /v1/jobs. ETASecs is a queue-depth
// + throughput estimate of seconds to completion (also persisted to jobs.eta_secs).
type JobSubmitResponse struct {
	JobID               uuid.UUID `json:"job_id"`
	TaskCount           int       `json:"task_count"`
	EstimatedUSD        float64   `json:"estimated_usd"`
	ETASecs             int       `json:"eta_secs"`
	EstimatedCompletion string    `json:"estimated_completion"` // RFC3339
}

// JobStatus is the GET /v1/jobs/{id} body. ETASecs is the submit-time
// queue-depth/throughput estimate persisted at creation (jobs.eta_secs).
type JobStatus struct {
	JobID        uuid.UUID `json:"job_id"`
	Status       string    `json:"status"`
	JobType      string    `json:"job_type"`
	Tier         string    `json:"tier"`
	TaskCount    int       `json:"task_count"`
	TasksDone    int       `json:"tasks_done"`
	EstimatedUSD float64   `json:"estimated_usd"`
	ActualUSD    float64   `json:"actual_usd"`
	ETASecs      int       `json:"eta_secs"`
	CreatedAt    string    `json:"created_at"`
	// MaxUSD is the buyer's hard spend cap (Budget Governor); 0 when none was set.
	// BudgetState is the governor state machine
	// (tracking|near_limit|paused_for_budget|cancelled_by_budget) — the buyer-facing
	// signal that Computexchange STOPS before a cap (Plane C §12 / Plane D §14 D8).
	MaxUSD      float64 `json:"max_usd"`
	BudgetState string  `json:"budget_state"`
}

// JobResults is the GET /v1/jobs/{id}/results body. results_url is a real
// time-limited presigned GET for the merged job output when one exists;
// result_urls is the per-task list of presigned result URLs (the V1 outputs are
// per-task, since the control plane does not merge). Both are real signed URLs
// minted by storage.PresignGet, never a fabricated stub.
type JobResults struct {
	JobID      uuid.UUID `json:"job_id"`
	Status     string    `json:"status"`
	ResultsURL string    `json:"results_url,omitempty"` // presigned merged output (if any)
	ResultURLs []string  `json:"result_urls"`           // presigned per-task results
}

// ModelInfo is one entry in GET /v1/models.
type ModelInfo struct {
	ID            string  `json:"id"`
	Kind          string  `json:"kind"`
	MinMemoryGB   float32 `json:"min_memory_gb"`
	PricePer1KUSD float64 `json:"price_per_1k_usd"`
	JobType       string  `json:"job_type"`
}

// PriceEstimate is the GET /v1/price-estimate body.
type PriceEstimate struct {
	Model         string  `json:"model"`
	Units         uint64  `json:"units"`
	PricePer1KUSD float64 `json:"price_per_1k_usd"`
	EstimateUSD   float64 `json:"estimate_usd"`
	Tier          string  `json:"tier"`
}

// APIError is the uniform JSON error body.
type APIError struct {
	Error string `json:"error"`
}
