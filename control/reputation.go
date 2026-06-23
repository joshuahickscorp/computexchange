package main

// reputation.go — pure reputation math. Ported verbatim from the action plan
// (update_reputation / reputation_tier). No I/O, no dependencies: the store
// persists the result, these functions only compute it.

// ReputationEvent enumerates the scoring events a worker can trigger.
type ReputationEvent string

const (
	EventTaskSuccess      ReputationEvent = "task_success"
	EventHoneypotPass     ReputationEvent = "honeypot_pass"
	EventRedundancyMatch  ReputationEvent = "redundancy_match"
	EventMismatch         ReputationEvent = "mismatch"
	EventHoneypotFail     ReputationEvent = "honeypot_fail"
	EventTimeout          ReputationEvent = "timeout"
	EventThermalThrottle  ReputationEvent = "thermal_throttle"
	EventResultCorrupt    ReputationEvent = "result_corrupt"
	EventSpoofingDetected ReputationEvent = "spoofing_detected"
)

// reputationDelta is the exact per-event delta from the action plan.
func reputationDelta(event ReputationEvent) float32 {
	switch event {
	case EventTaskSuccess:
		return 0.001
	case EventHoneypotPass:
		return 0.002
	case EventRedundancyMatch:
		return 0.001
	case EventMismatch:
		return -0.100
	case EventHoneypotFail:
		return -0.150
	case EventTimeout:
		return -0.020
	case EventThermalThrottle:
		return -0.005
	case EventResultCorrupt:
		return -0.200
	case EventSpoofingDetected:
		return -1.000 // instant ban threshold
	default:
		return 0.0
	}
}

// updateReputation applies an event's delta to the current score, clamped to
// [0.0, 1.0]. Direct port of update_reputation(current, event).
func updateReputation(current float32, event ReputationEvent) float32 {
	v := current + reputationDelta(event)
	if v < 0.0 {
		return 0.0
	}
	if v > 1.0 {
		return 1.0
	}
	return v
}

// reputationTier computes the tier gate from reputation + lifetime job count.
// Computed on query, never stored. Direct port of reputation_tier(rep, jobs).
func reputationTier(rep float32, jobsCompleted uint64) uint8 {
	switch {
	case rep >= 0.90 && jobsCompleted >= 5000:
		return 3
	case rep >= 0.80 && jobsCompleted >= 500:
		return 2
	case rep >= 0.60 && jobsCompleted >= 100:
		return 1
	default:
		return 0
	}
}
