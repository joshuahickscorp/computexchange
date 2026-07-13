package main

import "github.com/google/uuid"

// receipt.go — the ClearingReceipt projection (backlog P0 items 13-15).
//
// One buyer-facing artifact that assembles the facets a "cleared trade" needs, each
// from machinery that already exists: the QUOTE + ACTUALS + SETTLEMENT amounts (the
// InvoiceView / JobInvoice), the VERIFICATION counts + label + DISPUTE status
// (JobVerification), and the verification CLASS(es) that produced the results
// (JobVerificationClasses). Nothing is invented: every figure is a real read, so the
// receipt never overstates what happened.

// ClearingReceipt is the single projection returned by GET /v1/jobs/{id}/receipt.
type ClearingReceipt struct {
	JobID  uuid.UUID `json:"job_id"`
	Status string    `json:"status"`
	// Invoice carries the quote (quoted_usd), the actuals (actual/charged), and the
	// settlement amounts (supplier credit + platform take).
	Invoice *InvoiceView `json:"invoice"`
	// Verification carries the honeypot/redundancy counts, the honest label
	// (fully-verified / sampled-verified / honeypot-checked /
	// no-independent-peer / cross-class-skip / unverified),
	// and the latest dispute status.
	Verification Verification `json:"verification"`
	// Classes is the distinct set of (engine|build_hash) verification classes that
	// produced this job's results — the "cleared under" provenance.
	Classes []string `json:"verification_classes"`
	// Tasks is the per-task drilldown (item 15): each task's worker class + comparison
	// event, and NEVER the hidden honeypot answer (TaskReceipt has no answer field).
	Tasks []TaskReceipt `json:"tasks"`
	// Routing is the SUBSTRATE-ROUTING decision recorded at submit (Speed Lane
	// road-to-ten rubric dimension 5, control/routing.go + quote.go): the
	// "we ran it on X because Y" row the product promises — projected verbatim
	// from the persisted jobs.routing_* columns, never re-decided. Present only
	// for jobs that carried a routing block (GENERATIVE with records > 0 — the
	// A100 sweep's generative-decode honesty boundary); omitted for every other
	// shape. gpu_modeled_secs is ALWAYS [MODELED] (see QuoteRouting.Basis).
	Routing *QuoteRouting `json:"routing,omitempty"`
}

// TaskReceipt is one task's verification drilldown (item 15). It deliberately carries NO
// result bytes and NO honeypot known_answer: a drilldown must show HOW a task was checked
// (its verification class + comparison event) without leaking the hidden probe answer.
type TaskReceipt struct {
	ChunkIndex       int    `json:"chunk_index"`
	Status           string `json:"status"`
	IsHoneypot       bool   `json:"is_honeypot"`
	RuntimeCellID    string `json:"runtime_cell_id,omitempty"`
	RuntimeID        string `json:"runtime_id,omitempty"`
	RuntimeMatrixSHA string `json:"runtime_matrix_sha256,omitempty"`
	ModelKind        string `json:"model_kind,omitempty"`
	WorkerClass      string `json:"worker_class"`      // engine|build_hash (classKey); "" if unknown
	VerificationKind string `json:"verification_kind"` // latest comparison event kind; "" if none
	Verdict          string `json:"verdict"`           // durable current task verdict; "" until verified
}

// taskReceiptRow builds a TaskReceipt from a row's fields. Pure — the WorkerClass is the
// classKey, and there is no path for the known_answer to enter (it is never queried).
func taskReceiptRow(chunkIndex int, status string, isHoneypot bool, engine, build, kind, verdict string) TaskReceipt {
	return taskReceiptRowWithRuntime(chunkIndex, status, isHoneypot, engine, build, kind, verdict, "", "", "", "")
}

// taskReceiptRowWithRuntime adds the server-frozen dispatch authority. Empty
// values identify a legacy/unclaimed task and are omitted rather than invented.
func taskReceiptRowWithRuntime(chunkIndex int, status string, isHoneypot bool, engine, build, kind, verdict, cellID, runtimeID, matrixSHA, modelKind string) TaskReceipt {
	return TaskReceipt{
		ChunkIndex:       chunkIndex,
		Status:           status,
		IsHoneypot:       isHoneypot,
		RuntimeCellID:    cellID,
		RuntimeID:        runtimeID,
		RuntimeMatrixSHA: matrixSHA,
		ModelKind:        modelKind,
		WorkerClass:      classKey(engine, build),
		VerificationKind: kind,
		Verdict:          verdict,
	}
}

// assembleClearingReceipt joins the already-read facets into one projection (item 13).
// Pure: the I/O is the caller's; this guarantees every required facet (quote, actuals,
// verification, class, dispute, settlement, and the substrate-routing decision) is
// present in one place and is unit-tested. routing is nil for a job that carried no
// routing block (a non-generative or empty-input job — the honesty boundary the quote
// and submit paths both enforce), in which case the receipt omits the block entirely.
func assembleClearingReceipt(jobID uuid.UUID, status string, inv *InvoiceView, verif Verification, classes []string, tasks []TaskReceipt, routing *QuoteRouting) ClearingReceipt {
	return ClearingReceipt{
		JobID:        jobID,
		Status:       status,
		Invoice:      inv,
		Verification: verif,
		Classes:      classes,
		Tasks:        tasks,
		Routing:      routing,
	}
}

// receiptRouting rebuilds the routing block from the persisted jobs.routing_*
// columns carried on the invoice view (rubric dimension 5). Returns nil when the
// job carried no routing block (RoutingSubstrate == "" — every routing column was
// NULL), so the receipt omits it exactly as the submit response did. Pure — the
// I/O (the JobInvoice read) is the caller's, keeping assembleClearingReceipt
// unit-testable. The GPU figure stays [MODELED] via the shared quoteRoutingBasis.
func receiptRouting(inv *InvoiceView) *QuoteRouting {
	if inv == nil || inv.RoutingSubstrate == "" {
		return nil
	}
	return &QuoteRouting{
		Substrate:      inv.RoutingSubstrate,
		Reason:         inv.RoutingReason,
		FleetETASecs:   inv.RoutingFleetETASecs,
		GPUModeledSecs: inv.RoutingGPUModeledSecs,
		Basis:          quoteRoutingBasis,
	}
}

// PipelineReceipt aggregates each stage's receipt for a multi-stage pipeline (item 14).
// The aggregation is HONEST: AllVerified is true only when EVERY stage that ran is
// verified (a single unverified stage means the pipeline is not fully verified), and the
// total is the real sum of stage charges — no inflation, no hidden gap.
type PipelineReceipt struct {
	PipelineID      uuid.UUID              `json:"pipeline_id"`
	Status          string                 `json:"status"`
	Stages          []PipelineStageReceipt `json:"stages"`
	TotalChargedUSD float64                `json:"total_charged_usd"`
	AllVerified     bool                   `json:"all_verified"`
}

// PipelineStageReceipt is one stage's receipt summary inside a PipelineReceipt.
type PipelineStageReceipt struct {
	Index             int     `json:"index"`
	Op                string  `json:"op"`
	JobID             string  `json:"job_id,omitempty"`
	Status            string  `json:"status"`
	VerificationLabel string  `json:"verification_label"`
	ChargedUSD        float64 `json:"charged_usd"`
}

// assemblePipelineReceipt aggregates stage receipts honestly (item 14): the total is the
// sum of stage charges; AllVerified is true only if EVERY stage is "verified". Pure.
func assemblePipelineReceipt(pipelineID uuid.UUID, status string, stages []PipelineStageReceipt) PipelineReceipt {
	total := 0.0
	allVerified := len(stages) > 0
	for _, st := range stages {
		total += st.ChargedUSD
		if st.VerificationLabel != "verified" {
			allVerified = false
		}
	}
	return PipelineReceipt{
		PipelineID:      pipelineID,
		Status:          status,
		Stages:          stages,
		TotalChargedUSD: total,
		AllVerified:     allVerified,
	}
}
