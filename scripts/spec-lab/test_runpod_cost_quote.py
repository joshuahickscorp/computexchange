#!/usr/bin/env python3
"""Unit tests for runpod_cost_quote.py — parsing, attribution, derivation.

Run:  python3 -m unittest scripts.spec-lab is not a package, so:
      python3 scripts/spec-lab/test_runpod_cost_quote.py
All ledger fixtures are SYNTHETIC (constructed here); no real ledger, no
network, no RunPod API.
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import runpod_cost_quote as rcq  # noqa: E402


TS = "2026-07-09T{}:00-0400"


def ts(hhmm: str) -> str:
    return TS.format(hhmm)


def preflight_row(event: str, when: str, balance: float, config=None) -> dict:
    return {
        "event": event,
        "ts": ts(when),
        "preflight": {"balance": {"clientBalance": balance},
                      "config": config or {}},
    }


def receipt_row(when: str, gpu="NVIDIA A100 80GB PCIe", cloud="SECURE",
                resolution="1920x1080", repair=False,
                baseline_s=400.0, spec_s=50.0) -> dict:
    return {
        "event": "same_gpu_integrated_production_receipt",
        "ts": ts(when),
        "receipt": {
            "gpu": {"gpu": gpu, "cloud": cloud},
            "baseline_total_s": baseline_s,
            "spec_total_s": spec_s,
            "render_metrics": {
                "resolution": resolution,
                "repair_enabled": repair,
                "per_frame_ref_s": [100.0, 100.0, 100.0, 100.0],
                "mean_keyframe_render_s": 25.0,
            },
        },
    }


class TestDefensiveParsing(unittest.TestCase):
    def test_malformed_lines_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.jsonl"
            p.write_text(
                '{"event": "a_preflight", "ts": "2026-07-09T10:00:00-0400"}\n'
                "\n"
                "not json at all\n"
                '{"truncated": \n'
                "[1, 2, 3]\n"          # valid JSON but not an object -> skipped
                '"just a string"\n'    # ditto
                '{"event": "ok"}\n'
            )
            rows = rcq.parse_jsonl(p)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["event"], "a_preflight")
        self.assertEqual(rows[1]["event"], "ok")

    def test_missing_file_is_empty(self):
        self.assertEqual(rcq.parse_jsonl(Path("/nonexistent/nope.jsonl")), [])

    def test_ts_parse(self):
        dt = rcq.parse_ts("2026-07-09T14:03:40-0400")
        self.assertIsInstance(dt, datetime)
        self.assertEqual(dt.hour, 14)
        self.assertIsNone(rcq.parse_ts("garbage"))
        self.assertIsNone(rcq.parse_ts(None))
        self.assertIsNone(rcq.parse_ts(12345))

    def test_gpu_key(self):
        self.assertEqual(rcq.gpu_key("NVIDIA A100 80GB PCIe"), "A100")
        self.assertEqual(rcq.gpu_key("NVIDIA H100 80GB HBM3"), "H100")
        self.assertEqual(rcq.gpu_key("NVIDIA H200"), "H200")
        self.assertIsNone(rcq.gpu_key(None))
        self.assertIsNone(rcq.gpu_key("  "))


class TestSnapshots(unittest.TestCase):
    def test_only_preflight_rows_count(self):
        # A pruned row embedding a STALE preflight copy must NOT become a
        # snapshot (it would zero out the pruned attempt's measured cost).
        rows = {
            "l.jsonl": [
                preflight_row("bench_preflight", "10:00", 10.0),
                {"event": "bench_provision_pruned", "ts": ts("10:05"),
                 "preflight": {"balance": {"clientBalance": 10.0}}},
                preflight_row("bench_preflight", "10:30", 9.0),
            ]
        }
        snaps = rcq.extract_snapshots(rows)
        self.assertEqual(len(snaps), 2)
        self.assertEqual([b for _, b in snaps], [10.0, 9.0])

    def test_sorted_across_ledgers(self):
        rows = {
            "a.jsonl": [preflight_row("x_preflight", "12:00", 5.0)],
            "b.jsonl": [preflight_row("y_preflight", "11:00", 6.0)],
        }
        snaps = rcq.extract_snapshots(rows)
        self.assertEqual([b for _, b in snaps], [6.0, 5.0])


class TestAttempts(unittest.TestCase):
    def test_pairing_and_outcomes(self):
        rows = [
            preflight_row("bench_preflight", "10:00", 10.0),
            {"event": "bench_provision_pruned", "ts": ts("10:10"), "reason": "capacity"},
            preflight_row("bench_preflight", "10:30", 9.5),   # abandoned by next preflight
            preflight_row("bench_preflight", "11:00", 9.0),
            receipt_row("11:20"),
            preflight_row("bench_preflight", "11:30", 8.0),   # trailing, never closed
        ]
        attempts = rcq.extract_attempts(rows, "l.jsonl")
        self.assertEqual([a["outcome"] for a in attempts],
                         ["pruned", "abandoned", "success", "abandoned"])
        ok = attempts[2]
        self.assertEqual(ok["gpu"], "A100")
        self.assertEqual(ok["cloud"], "SECURE")
        self.assertEqual(ok["balance_before"], 9.0)

    def test_probe_result_is_success(self):
        rows = [
            preflight_row("probe_preflight", "10:00", 4.0),
            {"event": "probe_result", "ts": ts("10:40"),
             "pod": {"gpu": "NVIDIA H100 80GB HBM3", "cloud": "SECURE"},
             "metrics": {"wall_ref_s": 700.0}},
        ]
        attempts = rcq.extract_attempts(rows, "cross_denoiser_probe_ledger.jsonl")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["outcome"], "success")
        self.assertEqual(attempts[0]["gpu"], "H100")
        self.assertEqual(rcq.classify(attempts[0]), "single_frame_probe")


class TestClassification(unittest.TestCase):
    def _one(self, **kw):
        rows = [preflight_row("bench_preflight", "10:00", 10.0), receipt_row("10:20", **kw)]
        a = rcq.extract_attempts(rows, "integrated_spec_render_token_ledger.jsonl")[0]
        return rcq.classify(a)

    def test_classes(self):
        self.assertEqual(self._one(resolution="1920x1080"), "pipeline_1080p")
        self.assertEqual(self._one(resolution="3840x2160"), "pipeline_4k")
        self.assertEqual(self._one(resolution="3840x2160", repair=True), "pipeline_4k_repair")


class TestCostAttribution(unittest.TestCase):
    def test_measured_balance_delta(self):
        rows = {
            "integrated_spec_render_token_ledger.jsonl": [
                preflight_row("bench_preflight", "10:00", 10.0),
                receipt_row("11:00"),
                preflight_row("bench_preflight", "11:05", 8.5),
            ]
        }
        snaps = rcq.extract_snapshots(rows)
        attempts = rcq.extract_attempts(
            rows["integrated_spec_render_token_ledger.jsonl"],
            "integrated_spec_render_token_ledger.jsonl")
        rcq.attribute_costs(attempts, snaps)
        run = attempts[0]
        self.assertEqual(run["outcome"], "success")
        self.assertAlmostEqual(run["cost_usd"], 1.5, places=4)
        self.assertTrue(run["cost_label"].startswith("MEASURED"))
        self.assertAlmostEqual(run["session_hours"], 1.0, places=4)
        self.assertAlmostEqual(run["implied_usd_hr"], 1.5, places=3)

    def test_topup_across_window_rejected(self):
        rows = {
            "l.jsonl": [
                preflight_row("bench_preflight", "10:00", 10.0),
                receipt_row("11:00"),
                preflight_row("bench_preflight", "11:05", 40.0),  # top-up
            ]
        }
        snaps = rcq.extract_snapshots(rows)
        attempts = rcq.extract_attempts(rows["l.jsonl"], "l.jsonl")
        rcq.attribute_costs(attempts, snaps)
        self.assertIsNone(attempts[0]["cost_usd"])

    def test_modeled_fallback_when_no_closing_snapshot(self):
        rows = {
            "l.jsonl": [
                preflight_row("bench_preflight", "10:00", 10.0),
                receipt_row("11:00"),  # no later snapshot anywhere
            ]
        }
        snaps = rcq.extract_snapshots(rows)
        attempts = rcq.extract_attempts(rows["l.jsonl"], "l.jsonl")
        rcq.attribute_costs(attempts, snaps, {"A100": 2.0})
        run = attempts[0]
        self.assertAlmostEqual(run["cost_usd"], 2.0, places=4)  # 1h x $2/hr
        self.assertTrue(run["cost_label"].startswith("MODELED"))
        self.assertIsNone(run["implied_usd_hr"])  # modeled cost never feeds rates

    def test_pruned_attempt_cost_measured_to_next_preflight(self):
        rows = {
            "l.jsonl": [
                preflight_row("bench_preflight", "10:00", 10.0),
                {"event": "bench_provision_pruned", "ts": ts("10:10"),
                 # stale embedded copy — must NOT zero the cost:
                 "preflight": {"balance": {"clientBalance": 10.0}}},
                preflight_row("bench_preflight", "10:30", 9.3),
            ]
        }
        snaps = rcq.extract_snapshots(rows)
        attempts = rcq.extract_attempts(rows["l.jsonl"], "l.jsonl")
        rcq.attribute_costs(attempts, snaps)
        pruned = attempts[0]
        self.assertEqual(pruned["outcome"], "pruned")
        self.assertAlmostEqual(pruned["cost_usd"], 0.7, places=4)


class TestRates(unittest.TestCase):
    def _attempt(self, gpu, implied):
        return {"outcome": "success", "gpu": gpu, "implied_usd_hr": implied}

    def test_median_extraction(self):
        attempts = [self._attempt("A100", 1.40), self._attempt("A100", 1.50),
                    self._attempt("A100", 1.45), self._attempt("H100", 2.90)]
        rates = rcq.derive_rates(attempts)
        self.assertAlmostEqual(rates["A100"]["extracted_median_usd_hr"], 1.45, places=3)
        self.assertEqual(rates["A100"]["basis_usd_hr"], 1.45)
        self.assertIn("extracted-median", rates["A100"]["basis_source"])
        # H100 has n=1 < MIN_RATE_SAMPLES -> documented constant wins
        self.assertEqual(rates["H100"]["basis_usd_hr"],
                         rcq.DOCUMENTED_RATES_USD_HR["H100"])
        self.assertIn("documented", rates["H100"]["basis_source"])

    def test_no_samples_uses_documented(self):
        rates = rcq.derive_rates([])
        for k, v in rcq.DOCUMENTED_RATES_USD_HR.items():
            self.assertEqual(rates[k]["basis_usd_hr"], v)
            self.assertEqual(rates[k]["n_samples"], 0)


class TestEndToEndReport(unittest.TestCase):
    def _mk_ledgers(self, d: Path):
        integrated = [
            preflight_row("production_benchmark_preflight", "10:00", 20.0,
                          {"resolution": "1920x1080"}),
            receipt_row("10:20", resolution="1920x1080"),
            preflight_row("production_benchmark_preflight", "10:25", 19.5,
                          {"resolution": "3840x2160", "repair_enabled": True}),
            receipt_row("11:45", gpu="NVIDIA H100 80GB HBM3",
                        resolution="3840x2160", repair=True,
                        baseline_s=2800.0, spec_s=1100.0),
            preflight_row("production_benchmark_preflight", "11:50", 15.6),
            {"event": "production_benchmark_provision_pruned", "ts": ts("12:00"),
             "reason": "capacity",
             "preflight": {"balance": {"clientBalance": 15.6}}},
        ]
        probe = [
            preflight_row("multi_selector_probe_preflight", "12:10", 15.2),
            {"event": "multi_selector_probe_result", "ts": ts("12:45"),
             "pod": {"gpu": "NVIDIA A100 80GB PCIe", "cloud": "SECURE"},
             "metrics": {}},
            preflight_row("multi_selector_probe_preflight", "13:00", 14.4),
        ]
        (d / "integrated_spec_render_token_ledger.jsonl").write_text(
            "\n".join(json.dumps(r) for r in integrated) + "\nBROKEN LINE\n")
        (d / "multi_selector_probe_ledger.jsonl").write_text(
            "\n".join(json.dumps(r) for r in probe) + "\n")
        (d / "cross_denoiser_probe_ledger.jsonl").write_text("")
        # reference_consistency ledger intentionally ABSENT -> must not crash

    def test_report_builds_and_serializes(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._mk_ledgers(d)
            report = rcq.build_report(d, [3, 4])
            # JSON-serializable end to end
            blob = json.dumps(report)
            self.assertIn("quotes", report)
            back = json.loads(blob)
            self.assertEqual(back["failed_attempt_overhead"]["label"],
                             "MEASURED (balance-delta)")

            # 1080p run: 20.0 - 19.5 = 0.5 measured
            runs = {r["workload_class"]: r for r in report["runs"]
                    if r["outcome"] == "success"}
            self.assertAlmostEqual(runs["pipeline_1080p"]["cost_usd"], 0.5, places=4)
            self.assertAlmostEqual(runs["pipeline_4k_repair"]["cost_usd"], 3.9, places=4)
            self.assertAlmostEqual(runs["single_frame_probe"]["cost_usd"], 0.8, places=4)

            # pruned attempt got a measured cost to the next (probe) preflight
            pruned = [r for r in report["runs"] if r["outcome"] == "pruned"]
            self.assertEqual(len(pruned), 1)
            self.assertAlmostEqual(pruned[0]["cost_usd"], 0.4, places=4)  # 15.6-15.2

            # quote rows exist for every requested class
            workloads = " | ".join(q["workload"] for q in report["quotes"])
            for frag in ("1080p 4-frame pipeline", "strict-delivery w/ repair",
                         "single-frame probe", "cross-arch CUDA half", "scene sweep"):
                self.assertIn(frag, workloads)

            # markdown renders and carries the honesty labels
            md = rcq.render_markdown(report)
            self.assertIn("MEASURED", md)
            self.assertIn("MODELED", md)
            self.assertIn("Quote table", md)

    def test_sweep_formula_monotonic(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._mk_ledgers(d)
            report = rcq.build_report(d, [1, 2, 8])
            sweep = [q for q in report["quotes"] if "scene sweep" in q["workload"]][0]
            if "warm_pod_usd_by_n" in sweep:
                vals = [sweep["warm_pod_usd_by_n"][k] for k in ("1", "2", "8")]
                self.assertTrue(vals[0] < vals[1] < vals[2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
