package main

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/google/uuid"
)

// Item 13: the ClearingReceipt projection returns ALL of quote, actuals, verification,
// class, dispute, settlement, AND the per-task drilldown in one place.
func TestAssembleClearingReceipt(t *testing.T) {
	jobID := uuid.New()
	quoted := 9.5
	inv := &InvoiceView{
		JobID: jobID, Status: "complete",
		EstimatedUSD: 9.0, ActualUSD: 8.0, ChargedUSD: 8.0,
		SupplierPaidUSD: 7.76, PlatformTakeUSD: 0.24, QuotedUSD: &quoted,
	}
	verif := Verification{RedundancyMatched: 2, Checked: 2, Label: "verified", DisputeStatus: "resolved"}
	classes := []string{"candle|abc123"}
	tasks := []TaskReceipt{
		taskReceiptRow(0, "complete", false, "candle", "abc123", "redundancy_match"),
		taskReceiptRow(0, "complete", true, "candle", "abc123", "honeypot_pass"),
	}

	routing := &QuoteRouting{
		Substrate: "gpu_recommend", Reason: "gpu recommended: ... [modeled] ...",
		FleetETASecs: 900, GPUModeledSecs: 1.43, Basis: quoteRoutingBasis,
	}
	rc := assembleClearingReceipt(jobID, "complete", inv, verif, classes, tasks, routing)

	if rc.Invoice == nil || rc.Invoice.QuotedUSD == nil || *rc.Invoice.QuotedUSD != 9.5 {
		t.Fatal("receipt must carry the QUOTE")
	}
	// Rubric dimension 5: the receipt carries the persisted substrate-routing
	// decision verbatim — the "we ran it on X because Y" product row.
	if rc.Routing == nil || rc.Routing.Substrate != "gpu_recommend" ||
		rc.Routing.FleetETASecs != 900 || rc.Routing.GPUModeledSecs != 1.43 {
		t.Fatalf("receipt must carry the ROUTING decision; got %+v", rc.Routing)
	}
	if rc.Routing.Basis != quoteRoutingBasis {
		t.Fatalf("routing basis must be the shared [MODELED] citation; got %q", rc.Routing.Basis)
	}
	if rc.Invoice.ActualUSD != 8.0 {
		t.Fatal("receipt must carry ACTUALS")
	}
	if rc.Invoice.SupplierPaidUSD == 0 || rc.Invoice.PlatformTakeUSD == 0 {
		t.Fatal("receipt must carry SETTLEMENT amounts")
	}
	if rc.Verification.Label != "verified" || rc.Verification.DisputeStatus != "resolved" {
		t.Fatal("receipt must carry VERIFICATION + DISPUTE")
	}
	if len(rc.Classes) != 1 || rc.Classes[0] != "candle|abc123" {
		t.Fatal("receipt must carry the verification CLASS")
	}
	if len(rc.Tasks) != 2 || rc.Tasks[0].WorkerClass != "candle|abc123" || rc.Tasks[0].VerificationKind != "redundancy_match" {
		t.Fatalf("receipt must carry the per-task drilldown with worker class + event; got %+v", rc.Tasks)
	}
}

// Item 15 security property: the per-task drilldown NEVER leaks the hidden honeypot
// answer. A honeypot TaskReceipt shows it was a probe + its pass/fail, but its JSON
// contains no answer/result field.
func TestTaskReceiptNeverLeaksHoneypotAnswer(t *testing.T) {
	tr := taskReceiptRow(3, "complete", true, "candle", "h1", "honeypot_pass")
	if !tr.IsHoneypot || tr.VerificationKind != "honeypot_pass" || tr.WorkerClass != "candle|h1" {
		t.Fatalf("honeypot task receipt should show the probe + class + outcome; got %+v", tr)
	}
	b, _ := json.Marshal(tr)
	lower := strings.ToLower(string(b))
	if strings.Contains(lower, "answer") || strings.Contains(lower, "result") {
		t.Fatalf("a task drilldown must NOT expose any answer/result field; got %s", b)
	}
}

// Rubric dimension 5: receiptRouting projects the persisted jobs.routing_*
// columns (carried on the invoice view) into the receipt's routing block, and
// respects the honesty boundary — an empty RoutingSubstrate (every routing column
// was NULL: a non-generative or empty-input job) yields NO block.
func TestReceiptRouting(t *testing.T) {
	// A job that carried a routing block: projected verbatim, with the shared
	// [MODELED] basis attached.
	inv := &InvoiceView{
		RoutingSubstrate: "fleet", RoutingReason: "running on the fleet: ...",
		RoutingFleetETASecs: 42, RoutingGPUModeledSecs: 0.19,
	}
	r := receiptRouting(inv)
	if r == nil || r.Substrate != "fleet" || r.FleetETASecs != 42 || r.GPUModeledSecs != 0.19 {
		t.Fatalf("routing must project the persisted columns verbatim; got %+v", r)
	}
	if r.Basis != quoteRoutingBasis {
		t.Fatalf("routing basis must be the shared [MODELED] citation; got %q", r.Basis)
	}
	// The honesty boundary: no routing block persisted → no block on the receipt.
	if got := receiptRouting(&InvoiceView{}); got != nil {
		t.Fatalf("a job with no routing block must project no routing; got %+v", got)
	}
	if got := receiptRouting(nil); got != nil {
		t.Fatalf("a nil invoice must project no routing; got %+v", got)
	}
}

// Item 14: the pipeline receipt aggregates stage receipts HONESTLY — the total is the
// real sum of stage charges, and all_verified is true only when EVERY stage is verified.
func TestAssemblePipelineReceipt(t *testing.T) {
	pid := uuid.New()
	all := []PipelineStageReceipt{
		{Index: 0, Op: "embed", Status: "complete", VerificationLabel: "verified", ChargedUSD: 1.0},
		{Index: 1, Op: "batch_classification", Status: "complete", VerificationLabel: "verified", ChargedUSD: 2.5},
	}
	r := assemblePipelineReceipt(pid, "complete", all)
	if r.TotalChargedUSD != 3.5 {
		t.Fatalf("total = %v, want 3.5", r.TotalChargedUSD)
	}
	if !r.AllVerified {
		t.Fatal("all stages verified -> all_verified must be true")
	}
	mixed := []PipelineStageReceipt{
		{VerificationLabel: "verified", ChargedUSD: 1.0},
		{VerificationLabel: "no-independent-peer", ChargedUSD: 1.0},
	}
	if assemblePipelineReceipt(pid, "complete", mixed).AllVerified {
		t.Fatal("a single unverified stage must make all_verified false")
	}
	if assemblePipelineReceipt(pid, "queued", nil).AllVerified {
		t.Fatal("an empty pipeline is not 'all verified'")
	}
}
