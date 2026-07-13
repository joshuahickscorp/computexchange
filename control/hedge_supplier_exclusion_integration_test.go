//go:build integration

package main

import (
	"context"
	"fmt"
	"testing"

	"github.com/google/uuid"
)

func TestHedgePeerExcludesSupplierAlreadyRepresentedOnChunk(t *testing.T) {
	reset(t)
	ctx := context.Background()
	fixture := newVerificationProcessorFixture(t)
	represented := newAdversarialWorker(t, ctx, "represented-hedge-supplier")
	independent := newAdversarialWorker(t, ctx, "independent-hedge-supplier")

	// Model a redundancy task already executing this chunk on `represented`.
	// The hedge selector used to exclude only the primary supplier, so it could
	// choose this supplier again and hand it two task rows in a later N-way vote.
	redundancyTask := uuid.New()
	resultKey := fmt.Sprintf("jobs/%s/redundancy/%s/result.json", fixture.Dispatch.JobID, redundancyTask)
	if _, err := itPool.Exec(ctx, `
		INSERT INTO tasks
		 (id,job_id,status,is_honeypot,is_redundancy,retry_count,input_ref,result_key,
		  chunk_index,claimed_by,claimed_at,visible_at,
		  economic_buyer_charge_usd,economic_supplier_payout_usd)
		SELECT $1,t.job_id,'queued',false,true,0,t.input_ref,$2,
		       COALESCE(t.chunk_index,0),$3,now(),now(),
		       t.economic_buyer_charge_usd,t.economic_supplier_payout_usd
		  FROM tasks t WHERE t.id=$4`,
		redundancyTask, resultKey, represented.workerID, fixture.Dispatch.TaskID); err != nil {
		t.Fatalf("insert represented redundancy task: %v", err)
	}
	if err := itStore.StartTask(ctx, redundancyTask, represented.workerID); err != nil {
		t.Fatalf("start represented redundancy task: %v", err)
	}

	wk := &Workers{store: itStore}
	suppliers, err := wk.representedChunkSuppliers(ctx, fixture.Dispatch.JobID, 0)
	if err != nil {
		t.Fatalf("representedChunkSuppliers: %v", err)
	}
	seen := make(map[uuid.UUID]bool, len(suppliers))
	for _, supplierID := range suppliers {
		seen[supplierID] = true
	}
	if !seen[demoSupplierUUID] || !seen[represented.supplierID] {
		t.Fatalf("represented suppliers = %v, want primary %s and redundancy %s",
			suppliers, demoSupplierUUID, represented.supplierID)
	}

	peer, err := itStore.SelectRedundancyPeerExcluding(ctx, "embed", "all-minilm-l6-v2", 0,
		demoWorkerUUID, nil, suppliers)
	if err != nil {
		t.Fatalf("select hedge peer with represented suppliers excluded: %v", err)
	}
	if peer != independent.workerID {
		t.Fatalf("hedge peer = %s, want only independent supplier worker %s (represented worker %s)",
			peer, independent.workerID, represented.workerID)
	}
}
