#!/usr/bin/env python3
"""Tests for the local token speculative-decode ladder."""

import sys
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_token_spec_decode_ladder as ladder  # noqa: E402


class TokenSpecDecodeLadderTest(unittest.TestCase):
    def test_speculative_loop_is_lossless_on_structured_stream(self):
        toks = ladder.repeat_stream(512)
        base = ladder.run_baseline(toks, prefix_len=64, verifier_work=0)
        spec = ladder.run_speculative(toks, prefix_len=64, ctx=4, num_spec_tokens=8, verifier_work=0)
        self.assertEqual(spec["output"], base["output"])
        self.assertGreater(spec["acceptance"], 0.90)
        self.assertLess(spec["target_calls"], base["target_calls"])

    def test_random_stream_prunes_or_parks_with_low_acceptance(self):
        row = ladder.scenario_row(
            scenario="random",
            num_spec_tokens=8,
            n_tokens=1024,
            prefix_frac=0.25,
            ctx=4,
            verifier_work=0,
        )
        self.assertTrue(row["lossless"])
        self.assertLess(row["acceptance"], 0.08)
        self.assertEqual(row["branch_action"], "prune")

    def test_summary_keeps_best_per_scenario(self):
        rows = [
            {"scenario": "repeat", "lossless": True, "speedup_x": 2.0, "branch_action": "grow", "num_spec_tokens": 4, "acceptance": 0.8},
            {"scenario": "repeat", "lossless": True, "speedup_x": 3.0, "branch_action": "grow", "num_spec_tokens": 8, "acceptance": 0.9},
            {"scenario": "random", "lossless": True, "speedup_x": 0.7, "branch_action": "prune", "num_spec_tokens": 8, "acceptance": 0.0},
        ]
        summary = ladder.summarize(rows)
        self.assertEqual(summary["best"]["speedup_x"], 3.0)
        self.assertEqual(summary["by_scenario"]["repeat"]["best_num_spec_tokens"], 8)
        self.assertEqual(summary["prune_rows"], 1)


if __name__ == "__main__":
    unittest.main()
