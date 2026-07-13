#!/usr/bin/env python3
"""Emit current Python-lane receipts as JSONL for the strict Rust ingress gate."""

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import cx_render_spec_adapter as render  # noqa: E402
import cx_speculative_core as core  # noqa: E402
import cx_transcode_spec_adapter as transcode  # noqa: E402


def core_receipt() -> dict:
    def baseline(unit):
        time.sleep(0.001)
        return unit.payload * 10

    engine = core.SpeculativeEngine(
        branch_id="strict-ingress-core",
        modality="number",
        draft=lambda unit: core.DraftProposal(unit, unit.payload * 10),
        verify=lambda proposal: core.Verification(True, proposal.draft),
        repair=lambda _proposal, verification: core.RepairResult(verification.truth),
        baseline=baseline,
    )
    _, receipt = engine.run([core.SpecUnit("u0", "number", 7)])
    return receipt.to_dict()


def main() -> None:
    receipts = [
        core_receipt(),
        render.dry_run()[0],
        transcode.simulate(),
    ]
    for receipt in receipts:
        print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
