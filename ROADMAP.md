# Roadmap

Computexchange is a task-priced, verified spot market for batch AI inference on
idle Apple Silicon Macs. The core engine (queue, scheduling, on-device
inference, verification, ledger) works end to end today. The work ahead is the
buyer-facing product, real multi-machine supply, and the licensed payout rail.

## Now (works today)
- The full job lifecycle: submit, queue (Postgres FOR UPDATE SKIP LOCKED),
  claim, run real on-device inference on Metal, verify, and settle the ledger.
- Verification: honeypots, within-class redundancy, 3-way majority vote,
  reputation, and payout holds.
- An OpenAI-compatible Batch API, a CLI, a Python SDK, and a menu-bar agent.

## Next
- A real buyer console and the Compute Autopilot pipeline builder (the backend
  seams exist; the visual product is a build).
- A routing-intelligence dashboard that surfaces the per-job-type timing data
  the control plane already collects.
- Cross-machine validation on a real Mac Studio and a second physical Mac
  (byte-identical output rates, sustained thermal load).

## Later
- The licensed payout transfer rail (Stripe Connect or Trolley). The ledger and
  the hold-to-release lifecycle are real today; only the final transfer call is
  stubbed.
- A co-located Mac Studio cluster for large-model inference, offered as a
  reserved tier.
- The enterprise and privacy tier (private-pool routing already exists; the
  compliance attestations are external work).
