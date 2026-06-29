//  EarningsHistory.swift
//  A small, locally-persisted rolling history of the lifetime-earnings figure the
//  app observes in status.json, so the trust panel can draw an earnings sparkline.
//
//  Honesty note (BLACKHOLE): this is the app's OWN observed series · every point is
//  a real `lifetime_usd` value the app actually read from status.json at a real
//  time, never interpolated or invented. When there is too little history to draw a
//  trend the UI says so rather than faking a line. The agent (with the worker
//  token) remains the source of truth for the underlying numbers; this is only a
//  client-side record of what was seen, for visualization.

import Foundation
import Combine

/// One observed sample: the lifetime-earnings total seen at a moment in time.
struct EarningsSample: Codable, Equatable {
    let at: TimeInterval
    let lifetimeUsd: Double
}

/// Records and persists the rolling earnings series. Samples are appended at most
/// once per `minInterval` and capped at `maxSamples` (oldest dropped), so the file
/// stays tiny. Persisted to UserDefaults under its own key.
@MainActor
final class EarningsHistory: ObservableObject {
    @Published private(set) var samples: [EarningsSample] = []

    static let defaultsKey = "cx.earningsHistory"
    /// Minimum spacing between recorded samples (10 min) · enough to show a daily
    /// trend without bloating the store from the 3s status poll.
    private let minInterval: TimeInterval = 600
    private let maxSamples = 288   // ~2 days at 10-min spacing

    init() {
        if let data = UserDefaults.standard.data(forKey: Self.defaultsKey),
           let decoded = try? JSONDecoder().decode([EarningsSample].self, from: data) {
            samples = decoded
        }
    }

    /// Record a fresh observation of lifetime earnings. No-ops if it arrives sooner
    /// than `minInterval` after the last sample (we keep the LATEST value within a
    /// window by replacing the trailing sample, so the sparkline stays current).
    func record(lifetimeUsd: Double, at now: TimeInterval = Date().timeIntervalSince1970) {
        if let last = samples.last {
            if now - last.at < minInterval {
                // Within the window: update the trailing point in place rather than
                // appending, so we don't oversample but still reflect the newest read.
                samples[samples.count - 1] = EarningsSample(at: now, lifetimeUsd: lifetimeUsd)
                persist()
                return
            }
        }
        samples.append(EarningsSample(at: now, lifetimeUsd: lifetimeUsd))
        if samples.count > maxSamples {
            samples.removeFirst(samples.count - maxSamples)
        }
        persist()
    }

    /// The lifetime totals as a plain `[Double]`, oldest→newest, for plotting.
    var series: [Double] { samples.map(\.lifetimeUsd) }

    /// True only when there are at least two distinct points to draw a line between.
    var hasTrend: Bool { samples.count >= 2 }

    private func persist() {
        if let data = try? JSONEncoder().encode(samples) {
            UserDefaults.standard.set(data, forKey: Self.defaultsKey)
        }
    }
}
