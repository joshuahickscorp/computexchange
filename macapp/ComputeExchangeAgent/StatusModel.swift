//  StatusModel.swift
//  The status the menu-bar app reads from the agent, plus the operator prefs it
//  writes back. The agent (the Rust `cx-agent` binary) is the source of truth for
//  status; this app is a viewer + a control surface that toggles the agent's
//  config and (re)launches it.
//
//  The status file is the small contract between the two. We expect the agent to
//  atomically write JSON to:
//
//      ~/.compute-exchange/status.json
//
//  on every heartbeat (~30s) and on every task transition. Schema (snake_case to
//  match the agent's serde wire style):
//
//      {
//        "schema_version": 1,
//        "state": "running" | "idle" | "paused" | "offline",
//        "agent_version": "0.1.0",
//        "worker_id": "uuid|null",
//        "current_job": { "job_id": "uuid", "job_type": "embed", "started_at": 1718900000 } | null,
//        "today_earnings_usd": 0.42,        // accrued since local midnight
//        "balance_usd": 12.50,              // GET /v1/worker/earnings balance
//        "lifetime_usd": 130.00,
//        "thermal_state": "nominal" | "fair" | "serious" | "critical",
//        "gpu_temp_c": 61.0 | null,
//        "cpu_pct": 18.0,
//        "model_cache_bytes": 4831838208,   // sum of warm/cached model files on disk
//        "current_task_id": "uuid|null",    // the in-flight task id when running
//        "total_memory_gb": 64.0,           // physical memory
//        "available_memory_gb": 40.0,       // live free + reclaimable memory
//        "reserved_headroom_gb": 8.0,       // GB kept free for the operator
//        "effective_memory_gb": 32.0,       // allocatable for jobs = available − headroom
//        "throttled": false,                // true => paused for memory pressure
//        "throttle_reason": "string|null",  // why work paused (set iff throttled)
//        "active": true,                    // operator master switch (mirrors prefs)
//        "eligible_now": true,              // false during quiet hours / on battery (power-only)
//        "last_heartbeat": 1718900123,      // unix secs; staleness => agent likely down
//        "last_error": "string|null"        // surfaced verbatim; never hidden
//      }
//
//  The Rust agent writes this file (agent/src/status.rs): atomically (temp +
//  rename), on registration, every heartbeat, and each task transition. The app
//  still degrades honestly when the file is absent or stale (see AgentStatusStore).

import Foundation

enum AgentState: String, Codable {
    case running, idle, paused, offline

    var label: String {
        switch self {
        case .running: return "Running a job"
        case .idle:    return "Idle — waiting for work"
        case .paused:  return "Paused"
        case .offline: return "Offline"
        }
    }

    /// SF Symbol used in the menu-bar label.
    var symbol: String {
        switch self {
        case .running: return "bolt.fill"
        case .idle:    return "bolt"
        case .paused:  return "pause.circle"
        case .offline: return "moon.zzz"
        }
    }
}

enum ThermalState: String, Codable {
    case nominal, fair, serious, critical

    var label: String {
        switch self {
        case .nominal:  return "Nominal"
        case .fair:     return "Fair"
        case .serious:  return "Serious — throttling"
        case .critical: return "Critical — backing off"
        }
    }
}

struct CurrentJob: Codable, Equatable {
    let jobId: String
    let jobType: String
    let startedAt: TimeInterval

    enum CodingKeys: String, CodingKey {
        case jobId = "job_id"
        case jobType = "job_type"
        case startedAt = "started_at"
    }
}

/// Decoded `status.json`. Every field optional/defaulted so a partial or
/// older-schema file still decodes (we surface staleness separately).
struct AgentStatus: Codable, Equatable {
    var schemaVersion: Int = 1
    var state: AgentState = .offline
    var agentVersion: String = "unknown"
    var workerId: String?
    var currentJob: CurrentJob?
    var todayEarningsUsd: Double = 0
    var balanceUsd: Double = 0
    var lifetimeUsd: Double = 0
    var thermalState: ThermalState = .nominal
    var gpuTempC: Double?
    var cpuPct: Double = 0
    var modelCacheBytes: Int64 = 0
    var currentTaskId: String?
    // Dynamic provider-throttling surface (all defaulted, so an older agent that
    // omits them still decodes — Swift's synthesized decoder uses the defaults).
    var totalMemoryGb: Double = 0
    var availableMemoryGb: Double = 0
    var reservedHeadroomGb: Double = 0
    var effectiveMemoryGb: Double = 0
    var throttled: Bool = false
    var throttleReason: String?
    var active: Bool = false
    var eligibleNow: Bool = false
    var lastHeartbeat: TimeInterval = 0
    var lastError: String?

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case state
        case agentVersion = "agent_version"
        case workerId = "worker_id"
        case currentJob = "current_job"
        case todayEarningsUsd = "today_earnings_usd"
        case balanceUsd = "balance_usd"
        case lifetimeUsd = "lifetime_usd"
        case thermalState = "thermal_state"
        case gpuTempC = "gpu_temp_c"
        case cpuPct = "cpu_pct"
        case modelCacheBytes = "model_cache_bytes"
        case currentTaskId = "current_task_id"
        case totalMemoryGb = "total_memory_gb"
        case availableMemoryGb = "available_memory_gb"
        case reservedHeadroomGb = "reserved_headroom_gb"
        case effectiveMemoryGb = "effective_memory_gb"
        case throttled
        case throttleReason = "throttle_reason"
        case active
        case eligibleNow = "eligible_now"
        case lastHeartbeat = "last_heartbeat"
        case lastError = "last_error"
    }

    /// Heartbeats older than this mean the agent is probably not running, even if
    /// the file still says "running". We never trust a stale file's liveness.
    static let staleAfter: TimeInterval = 90

    var isStale: Bool {
        guard lastHeartbeat > 0 else { return true }
        return Date().timeIntervalSince1970 - lastHeartbeat > AgentStatus.staleAfter
    }

    var modelCacheHuman: String {
        ByteCountFormatter.string(fromByteCount: modelCacheBytes, countStyle: .file)
    }
}

/// Operator preferences the app writes back to the agent's config. These mirror
/// the fields the Rust `AgentConfig` already parses (agent/src/config.rs):
/// `quiet_hours`, `power_only`, `min_payout_usd_per_hr`, plus a master `active`
/// switch the app owns. Persisted to UserDefaults AND written to the agent config
/// file on change so the next agent launch picks them up.
struct OperatorPrefs: Codable, Equatable {
    var active: Bool = true
    var quietHoursEnabled: Bool = false
    var quietStartHour: Int = 22
    var quietEndHour: Int = 7
    var powerOnly: Bool = true
    var minPayoutUsdPerHr: Double = 0.05
    /// GB of memory reserved for the operator's own use. Maps to the agent's
    /// `memory_headroom_gb`; effective allocatable memory for jobs is
    /// `available − memoryHeadroomGb`, and the agent pauses new work before it
    /// would breach this (dynamic throttling). Mirrors agent/src/config.rs.
    var memoryHeadroomGb: Double = 8.0

    static let defaultsKey = "cx.operatorPrefs"
}
