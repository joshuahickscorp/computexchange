package main

import (
	"context"
	"encoding/json"
	"errors"
	"log"
	"math"
	"net/http"
	"strconv"

	"github.com/google/uuid"
)

// pipeline.go — Compute Autopilot: USER-DEFINED multi-step pipelines. Where the Concierge
// (intake.go) AUTO-DETECTS a pipeline from a source, this lets the buyer COMPOSE one
// explicitly in the visual builder: an ordered list of CX workloads, each a job type + a
// model + whether it reads the source input or the previous stage's output. Submit once and
// the whole chain runs: stages whose `from` is "input" launch immediately on the supplied
// data; stages whose `from` is "previous" are submitted by advancePipeline when their
// predecessor completes, fed that predecessor's merged output (jobs/{id}/output.jsonl).
//
// It reuses the proven job machinery wholesale (createJob, the completion hook,
// JobOutputRef -> storage -> inline input), exactly as advanceIntake does, so a pipeline is
// "real": every stage is an ordinary CX job that the scheduler dispatches, verifies, bills,
// and merges. No new execution path, no faked progress.

// pipelineStage is one composed step. From selects the stage's input: "input" (or "") runs
// it on the pipeline's source data; "previous" chains it onto the prior stage's output.
type pipelineStage struct {
	Op             string          `json:"op"`
	Model          string          `json:"model"`
	From           string          `json:"from"`
	LaunchContract *LaunchContract `json:"launch_contract,omitempty"`
}

// pipelineOps is the workload allowlist the builder offers. createJob remains the authority
// on a job type's validity; this gives a clearer pipeline-level error for an unknown op.
var pipelineOps = map[string]bool{
	"embed":                true,
	"batch_classification": true,
	"json_extraction":      true,
}

type pipelineCreateRequest struct {
	Name   string          `json:"name"`
	Stages []pipelineStage `json:"stages"`
	Input  json.RawMessage `json:"input"` // inline JSONL string OR {"s3_key":...}, same as jobSubmit
	// LaunchContract fields (items 1, 3): the budget/verification/routing every stage of
	// the pipeline must carry, exactly as a direct submission would. Stamped onto each
	// stage's jobSubmit so a pipelined job is not a back door around the buyer's cap.
	QuoteID       string             `json:"quote_id,omitempty"`
	MaxUSD        float64            `json:"max_usd,omitempty"`
	MinReputation float32            `json:"min_reputation,omitempty"`
	PrivatePool   bool               `json:"private_pool,omitempty"`
	Verification  VerificationPolicy `json:"verification,omitempty"`
}

// PipelineStageView is one stage's live state for the buyer (the builder polls these).
type PipelineStageView struct {
	Index        int     `json:"index"`
	Op           string  `json:"op"`
	Model        string  `json:"model"`
	From         string  `json:"from"`
	JobID        string  `json:"job_id,omitempty"`
	Status       string  `json:"status"` // pending | queued | running | verifying | complete | failed | cancelled
	TasksDone    int     `json:"tasks_done"`
	TaskCount    int     `json:"task_count"`
	EstimatedUSD float64 `json:"estimated_usd"`
	ActualUSD    float64 `json:"actual_usd"`
}

// PipelineView is the GET /v1/pipelines/{id} body. Status is DERIVED from the stages so it
// is always honest: failed if any stage failed, complete only when every stage completed.
type PipelineView struct {
	ID        string              `json:"id"`
	Name      string              `json:"name"`
	Status    string              `json:"status"`
	CreatedAt string              `json:"created_at"`
	Stages    []PipelineStageView `json:"stages"`
}

// handleCreatePipeline validates the composed stages, persists the pipeline, and launches
// every "from input" stage on the supplied data (typically stage 0). The "from previous"
// stages are left for advancePipeline to submit as their predecessors complete. Input is the
// same polymorphic field as a plain job (inline JSONL string or {"s3_key":...}); createJob
// resolves it, so a single uploaded object feeds the whole chain without re-upload.
func (s *Server) handleCreatePipeline(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxBuyer).(*AuthResult)
	var req pipelineCreateRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid pipeline body")
		return
	}
	if len(req.Stages) == 0 {
		writeErr(w, http.StatusBadRequest, "a pipeline needs at least one stage")
		return
	}
	if len(req.Stages) > 8 {
		writeErr(w, http.StatusBadRequest, "a pipeline is capped at 8 stages")
		return
	}
	if len(req.Input) == 0 {
		writeErr(w, http.StatusBadRequest, "a pipeline needs input (inline JSONL or an s3_key)")
		return
	}
	if math.IsNaN(req.MaxUSD) || math.IsInf(req.MaxUSD, 0) || req.MaxUSD < 0 {
		writeErr(w, http.StatusBadRequest, "max_usd must be a finite non-negative aggregate pipeline cap")
		return
	}
	if req.QuoteID != "" && len(req.Stages) > 1 {
		writeErr(w, http.StatusBadRequest, "one quote_id cannot bind multiple pipeline stages; submit per-stage jobs or omit quote_id")
		return
	}
	for i, st := range req.Stages {
		if st.Op == audioUploadJobType {
			writeErr(w, http.StatusBadRequest, "stage "+strconv.Itoa(i)+": "+audioUploadBoundaryError)
			return
		}
		if !pipelineOps[st.Op] {
			writeErr(w, http.StatusBadRequest, "stage "+strconv.Itoa(i)+": unknown op "+st.Op)
			return
		}
		if st.Model == "" {
			writeErr(w, http.StatusBadRequest, "stage "+strconv.Itoa(i)+": missing model")
			return
		}
		if st.From != "" && st.From != "input" && st.From != "previous" {
			writeErr(w, http.StatusBadRequest, "stage "+strconv.Itoa(i)+`: from must be "input" or "previous"`)
			return
		}
		if i == 0 && st.From == "previous" {
			writeErr(w, http.StatusBadRequest, "stage 0 must read the source input, not a previous stage")
			return
		}
		if err := validateAdvertisedRuntimeJobModel(st.Op, st.Model); err != nil {
			writeErr(w, http.StatusBadRequest, "stage "+strconv.Itoa(i)+": "+err.Error())
			return
		}
	}

	// Price every stage before any write or job creation. The quote weights let one
	// aggregate buyer cap be split into disjoint per-job caps; copying the whole cap
	// to each stage would multiply the buyer's exposure by the stage count.
	schedule, err := LoadEconomicScheduleFromEnv()
	if err != nil {
		writeErr(w, http.StatusServiceUnavailable, "economic schedule unavailable: "+err.Error())
		return
	}
	inputReader, _, err := s.resolveInput(r.Context(), auth.BuyerID, req.Input)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "resolving pipeline input: "+err.Error())
		return
	}
	inputBytes, err := readSynchronousInput(inputReader)
	if err != nil {
		status := http.StatusBadRequest
		if errors.Is(err, errSynchronousInputTooLarge) {
			status = http.StatusRequestEntityTooLarge
		}
		writeErr(w, status, "reading pipeline input: "+err.Error())
		return
	}

	baseContract := LaunchContract{
		QuoteID:       req.QuoteID,
		MinReputation: req.MinReputation,
		PrivatePool:   req.PrivatePool,
		Verification:  req.Verification,
	}
	stageQuotes := make([]Quote, len(req.Stages))
	weights := make([]float64, len(req.Stages))
	for i, st := range req.Stages {
		q := s.buildQuoteWithSchedule(r.Context(), auth.BuyerID, baseContract.applyTo(jobSubmit{
			JobType: JobType{Type: st.Op},
			Model:   generatedRuntimeModelRef(st.Op, st.Model),
			Tier:    "batch",
			Input:   req.Input,
		}), inputBytes, schedule)
		if !q.Economics.Executable {
			writeErr(w, http.StatusConflict, "stage "+strconv.Itoa(i)+" quote is not executable: "+q.Economics.BlockReason)
			return
		}
		stageQuotes[i] = q
		weights[i] = q.Cost.MaxUSD
	}
	requiredMaxUSD := composeQuotes(stageQuotes).TotalCost.MaxUSD
	aggregateMaxUSD, err := resolveAggregateMaxUSD(req.MaxUSD, requiredMaxUSD)
	if err != nil {
		writeErr(w, http.StatusConflict, "pipeline budget unavailable: "+err.Error())
		return
	}
	stageCaps, err := allocateAggregateMaxUSD(aggregateMaxUSD, weights)
	if err != nil {
		writeErr(w, http.StatusConflict, "pipeline budget unavailable: "+err.Error())
		return
	}
	for i := range req.Stages {
		contract := baseContract
		contract.MaxUSD = stageCaps[i]
		req.Stages[i].LaunchContract = &contract
	}

	spec, err := json.Marshal(req.Stages)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "could not persist pipeline launch contract")
		return
	}
	pipeID, err := s.store.CreatePipeline(r.Context(), auth.BuyerID, req.Name, spec)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "could not create pipeline")
		return
	}

	var launched []map[string]any
	for i, st := range req.Stages {
		if st.From == "previous" {
			continue // advancePipeline submits this when stage i-1 completes
		}
		resp, herr := s.createJob(r.Context(), auth.BuyerID, st.LaunchContract.applyTo(jobSubmit{
			JobType: JobType{Type: st.Op},
			Model:   generatedRuntimeModelRef(st.Op, st.Model),
			Tier:    "batch",
			Input:   req.Input,
		}))
		if herr != nil {
			_ = s.store.SetPipelineStatus(r.Context(), pipeID, "failed")
			writeErr(w, herr.status, "stage "+strconv.Itoa(i)+": "+herr.msg)
			return
		}
		if lerr := s.store.LinkPipelineJob(r.Context(), resp.JobID, pipeID, i); lerr != nil {
			log.Printf("pipeline %s: linking stage %d job %s: %v", pipeID, i, resp.JobID, lerr)
			_ = s.store.SetPipelineStatus(r.Context(), pipeID, "failed")
			writeErr(w, http.StatusInternalServerError, "linking pipeline stage: "+lerr.Error())
			return
		}
		launched = append(launched, map[string]any{"stage": i, "op": st.Op, "job_id": resp.JobID})
	}

	writeJSON(w, http.StatusAccepted, map[string]any{
		"pipeline_id":   pipeID.String(),
		"stages":        len(req.Stages),
		"launched":      launched,
		"max_usd":       aggregateMaxUSD,
		"stage_max_usd": stageCaps,
	})
}

// handleGetPipeline returns the pipeline with each stage's live job status (derived overall
// status). The visual builder polls this to animate the chain.
func (s *Server) handleGetPipeline(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxBuyer).(*AuthResult)
	id, err := uuid.Parse(r.PathValue("id"))
	if err != nil {
		writeErr(w, http.StatusBadRequest, "bad pipeline id")
		return
	}
	view, err := s.store.GetPipelineView(r.Context(), auth.BuyerID, id)
	if err != nil {
		writeErr(w, http.StatusNotFound, "pipeline not found")
		return
	}
	writeJSON(w, http.StatusOK, view)
}

// handlePipelineReceipt returns the PipelineReceipt (item 14): each stage's receipt
// summary (status + verification label + charge), aggregated honestly (real total charge;
// all_verified only when every stage is verified). Buyer-scoped via GetPipelineView.
func (s *Server) handlePipelineReceipt(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxBuyer).(*AuthResult)
	id, err := uuid.Parse(r.PathValue("id"))
	if err != nil {
		writeErr(w, http.StatusBadRequest, "bad pipeline id")
		return
	}
	view, err := s.store.GetPipelineView(r.Context(), auth.BuyerID, id)
	if err != nil {
		writeErr(w, http.StatusNotFound, "pipeline not found")
		return
	}
	var stages []PipelineStageReceipt
	for _, st := range view.Stages {
		label := ""
		if st.JobID != "" {
			if jid, perr := uuid.Parse(st.JobID); perr == nil {
				if v, verr := s.store.JobVerification(r.Context(), jid); verr == nil {
					label = v.Label
				}
			}
		}
		stages = append(stages, PipelineStageReceipt{
			Index: st.Index, Op: st.Op, JobID: st.JobID, Status: st.Status,
			VerificationLabel: label, ChargedUSD: st.ActualUSD,
		})
	}
	writeJSON(w, http.StatusOK, assemblePipelineReceipt(id, view.Status, stages))
}

// advancePipeline chains a user-defined pipeline: when a pipeline-linked job completes, it
// submits the next "from previous" stage on that job's merged output, and marks the pipeline
// complete when the last stage finishes. Best-effort + gated on pipeline membership, so it
// can never disturb an ordinary job's lifecycle. Mirrors advanceIntake; called from the same
// completion hook.
func (s *Server) advancePipeline(ctx context.Context, jobID uuid.UUID) {
	pipeID, stageIdx, ok := s.store.PipelineForJob(ctx, jobID)
	if !ok {
		return
	}
	spec, err := s.store.PipelineSpec(ctx, pipeID)
	if err != nil {
		return
	}
	var stages []pipelineStage
	if err := json.Unmarshal(spec, &stages); err != nil {
		return
	}
	next := stageIdx + 1
	// Last stage done -> the pipeline is complete.
	if next >= len(stages) {
		_ = s.store.SetPipelineStatus(ctx, pipeID, "complete")
		return
	}
	// A non-chained next stage was already launched up front; nothing to do here.
	if stages[next].From != "previous" {
		return
	}
	unlock, err := s.store.LockWorkflowStage(ctx, pipelineStageLockNamespace, pipeID, next)
	if err != nil {
		log.Printf("pipeline %s: locking stage %d: %v", pipeID, next, err)
		return
	}
	defer unlock()
	// Re-check only after obtaining the cross-replica lock. Two completion workers
	// may enter concurrently; exactly one may create and link the downstream job.
	if s.store.PipelineStageSubmitted(ctx, pipeID, next) {
		return
	}
	ref, err := s.store.JobOutputRef(ctx, jobID)
	if err != nil || ref == "" {
		return
	}
	outReader, err := s.storage.GetObjectReader(ctx, ref)
	if err != nil {
		log.Printf("pipeline %s: opening stage %d output %q: %v", pipeID, stageIdx, ref, err)
		return
	}
	out, err := readSynchronousInput(outReader)
	if err != nil {
		log.Printf("pipeline %s: reading bounded stage %d output %q: %v", pipeID, stageIdx, ref, err)
		_ = s.store.SetPipelineStatus(ctx, pipeID, "failed")
		return
	}
	buyerID, err := s.store.PipelineBuyer(ctx, pipeID)
	if err != nil {
		return
	}
	st := stages[next]
	if st.LaunchContract == nil || st.LaunchContract.MaxUSD <= 0 {
		log.Printf("pipeline %s: stage %d has no persisted positive launch contract; refusing uncapped chained execution", pipeID, next)
		_ = s.store.SetPipelineStatus(ctx, pipeID, "failed")
		return
	}
	inputJSON, _ := json.Marshal(string(out)) // a JSON string IS the inline JSONL
	resp, herr := s.createJob(ctx, buyerID, st.LaunchContract.applyTo(jobSubmit{
		JobType: JobType{Type: st.Op},
		Model:   generatedRuntimeModelRef(st.Op, st.Model),
		Tier:    "batch",
		Input:   inputJSON,
	}))
	if herr != nil {
		log.Printf("pipeline %s: chaining to stage %d (%s) failed: %s", pipeID, next, st.Op, herr.msg)
		_ = s.store.SetPipelineStatus(ctx, pipeID, "failed")
		return
	}
	if lerr := s.store.LinkPipelineJob(ctx, resp.JobID, pipeID, next); lerr != nil {
		log.Printf("pipeline %s: linking chained stage %d job %s: %v", pipeID, next, resp.JobID, lerr)
	}
}
