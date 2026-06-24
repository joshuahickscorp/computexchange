package main

import (
	"context"
	"encoding/json"
	"log"
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
	Op    string `json:"op"`
	Model string `json:"model"`
	From  string `json:"from"`
}

// pipelineOps is the workload allowlist the builder offers. createJob remains the authority
// on a job type's validity; this gives a clearer pipeline-level error for an unknown op.
var pipelineOps = map[string]bool{
	"embed":                true,
	"batch_classification": true,
	"audio_transcribe":     true,
	"json_extraction":      true,
}

type pipelineCreateRequest struct {
	Name   string          `json:"name"`
	Stages []pipelineStage `json:"stages"`
	Input  json.RawMessage `json:"input"` // inline JSONL string OR {"s3_key":...}, same as jobSubmit
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
	for i, st := range req.Stages {
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
	}

	spec, _ := json.Marshal(req.Stages)
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
		resp, herr := s.createJob(r.Context(), auth.BuyerID, jobSubmit{
			JobType: JobType{Type: st.Op},
			Model:   ModelRef{Kind: "gguf", Ref: st.Model},
			Tier:    "batch",
			Input:   req.Input,
		})
		if herr != nil {
			writeErr(w, herr.status, "stage "+strconv.Itoa(i)+": "+herr.msg)
			return
		}
		if lerr := s.store.LinkPipelineJob(r.Context(), resp.JobID, pipeID, i); lerr != nil {
			log.Printf("pipeline %s: linking stage %d job %s: %v", pipeID, i, resp.JobID, lerr)
		}
		launched = append(launched, map[string]any{"stage": i, "op": st.Op, "job_id": resp.JobID})
	}

	writeJSON(w, http.StatusAccepted, map[string]any{
		"pipeline_id": pipeID.String(),
		"stages":      len(req.Stages),
		"launched":    launched,
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
	if s.store.PipelineStageSubmitted(ctx, pipeID, next) {
		return
	}
	ref, err := s.store.JobOutputRef(ctx, jobID)
	if err != nil || ref == "" {
		return
	}
	out, err := s.storage.GetObject(ctx, ref)
	if err != nil {
		log.Printf("pipeline %s: fetching stage %d output %q: %v", pipeID, stageIdx, ref, err)
		return
	}
	buyerID, err := s.store.PipelineBuyer(ctx, pipeID)
	if err != nil {
		return
	}
	st := stages[next]
	inputJSON, _ := json.Marshal(string(out)) // a JSON string IS the inline JSONL
	resp, herr := s.createJob(ctx, buyerID, jobSubmit{
		JobType: JobType{Type: st.Op},
		Model:   ModelRef{Kind: "gguf", Ref: st.Model},
		Tier:    "batch",
		Input:   inputJSON,
	})
	if herr != nil {
		log.Printf("pipeline %s: chaining to stage %d (%s) failed: %s", pipeID, next, st.Op, herr.msg)
		_ = s.store.SetPipelineStatus(ctx, pipeID, "failed")
		return
	}
	if lerr := s.store.LinkPipelineJob(ctx, resp.JobID, pipeID, next); lerr != nil {
		log.Printf("pipeline %s: linking chained stage %d job %s: %v", pipeID, next, resp.JobID, lerr)
	}
}
