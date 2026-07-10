#!/usr/bin/env python3
"""Tests for CX-native speculation ladder helpers."""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import run_cx_native_speculation_ladder as ladder  # noqa: E402
import run_token_spec_decode_ladder as token_ladder  # noqa: E402


class CxNativeSpeculationLadderTest(unittest.TestCase):
    def test_adaptive_width_chooses_largest_confident_span_once(self):
        predictor = token_ladder.NGramDraft(ctx=2)
        predictor.prime([1, 2, 1, 2, 1, 2])

        width, confidence, proposal = ladder.choose_adaptive_width(
            predictor=predictor,
            remaining=8,
            widths=[8, 4],
            threshold=0.99,
        )

        self.assertEqual(width, 8)
        self.assertEqual(proposal, [1, 2, 1, 2, 1, 2, 1, 2])
        self.assertEqual(confidence, 1.0)

    def test_adaptive_width_rejects_after_low_first_confidence(self):
        predictor = token_ladder.NGramDraft(ctx=2)
        predictor.prime([1, 2, 3, 4])

        width, confidence, proposal = ladder.choose_adaptive_width(
            predictor=predictor,
            remaining=8,
            widths=[8, 4],
            threshold=0.9,
        )

        self.assertEqual(width, 0)
        self.assertEqual(proposal, [])
        self.assertLess(confidence, 0.9)

    def test_best_rows_by_scenario_prefers_grow_over_raw_speed(self):
        rows = [
            {
                "branch_action": "prune",
                "speedup_x": 100.0,
                "meta": {"scenario": "code"},
            },
            {
                "branch_action": "grow",
                "speedup_x": 3.0,
                "meta": {"scenario": "code"},
            },
        ]

        best = ladder.best_rows_by_scenario(rows)

        self.assertEqual(best["code"]["branch_action"], "grow")

    def test_prefix_accept_adaptive_branch_is_exact_and_token_accounted(self):
        row = ladder.run_token_prefix_accept_adaptive_branch(
            scenario="code",
            n_tokens=512,
            prefix_frac=0.25,
            ctx=8,
            widths=[32, 16, 8, 4, 2],
            verifier_work=0,
            confidence_threshold=0.55,
            fallback_tokens=2,
        )

        self.assertTrue(row["exact"])
        self.assertEqual(row["quality_gate"], True)
        self.assertEqual(row["meta"]["strategy"], "prefix_accept_adaptive")
        self.assertEqual(row["output_tokens"], row["meta"]["generated_tokens"])
        self.assertGreaterEqual(row["accepted_fraction"], 0.0)
        self.assertLessEqual(row["accepted_fraction"], 1.0)

    def test_prefix_accept_policy_grows_fast_low_draft_acceptance_rows(self):
        row = {
            "exact": True,
            "quality_gate": True,
            "speedup_x": 4.0,
            "accepted_fraction": 0.8,
            "target_call_reduction_x": 4.0,
            "draft_token_acceptance": 0.03,
        }

        self.assertEqual(ladder.decide_prefix_accept_row(row), "grow")

    def test_context_copy_predictor_prefers_recent_verified_match(self):
        predictor = ladder.ContextCopyNGramDraft(ctx=3, min_match=3)
        predictor.prime([1, 2, 3, 4, 1, 2, 3, 5])

        token, confidence, meta = predictor.predict_next_with_confidence([1, 2, 3])

        self.assertEqual(token, 5)
        self.assertEqual(meta["source"], "copy_match")
        self.assertEqual(meta["match_len"], 3)
        self.assertGreaterEqual(confidence, 0.7)

    def test_prefix_copy_adaptive_branch_is_exact_and_reports_source(self):
        row = ladder.run_token_prefix_accept_adaptive_branch(
            scenario="json",
            n_tokens=512,
            prefix_frac=0.25,
            ctx=8,
            widths=[32, 16, 8, 4, 2],
            verifier_work=0,
            confidence_threshold=0.55,
            fallback_tokens=2,
            proposal_source="copy_match",
            copy_min_match=3,
        )

        self.assertTrue(row["exact"])
        self.assertEqual(row["quality_gate"], True)
        self.assertEqual(row["meta"]["strategy"], "prefix_accept_copy_match")
        self.assertEqual(row["meta"]["proposal_source"], "copy_match")
        self.assertIn("copy_match", row["meta"]["proposal_sources"])
        self.assertEqual(row["output_tokens"], row["meta"]["generated_tokens"])

    def test_copy_runway_selector_requires_source_runway(self):
        predictor = ladder.ContextCopyNGramDraft(ctx=4, min_match=2, max_candidates=16)
        predictor.prime([1, 2, 3, 4, 10, 11, 1, 2, 3, 4])

        width, confidence, proposal, source_meta = ladder.choose_copy_runway_width_with_sources(
            predictor=predictor,
            remaining=8,
            widths=[8, 4],
            threshold=0.55,
        )

        self.assertEqual(width, 4)
        self.assertEqual(proposal, [10, 11, 1, 2])
        self.assertGreaterEqual(confidence, 0.55)
        self.assertEqual({m["source"] for m in source_meta}, {"copy_runway"})
        self.assertEqual(source_meta[0]["match_len"], 4)

    def test_prefix_copy_runway_branch_is_exact_and_reports_source(self):
        row = ladder.run_token_prefix_accept_adaptive_branch(
            scenario="repeat",
            n_tokens=512,
            prefix_frac=0.25,
            ctx=8,
            widths=[32, 16, 8, 4, 2],
            verifier_work=0,
            confidence_threshold=0.55,
            fallback_tokens=2,
            proposal_source="copy_runway",
            copy_min_match=3,
            copy_max_candidates=32,
        )

        self.assertTrue(row["exact"])
        self.assertEqual(row["quality_gate"], True)
        self.assertEqual(row["meta"]["strategy"], "prefix_accept_copy_runway")
        self.assertEqual(row["meta"]["proposal_source"], "copy_runway")
        self.assertIn("copy_runway", row["meta"]["proposal_sources"])
        self.assertEqual(row["output_tokens"], row["meta"]["generated_tokens"])

    def test_json_template_predicts_next_verified_row(self):
        predictor = ladder.JsonTemplateDraft(ctx=8)
        rows = [
            b'{"id":000000,"status":"retry","value":0}\n',
            b'{"id":000001,"status":"ok","value":17}\n',
            b'{"id":000002,"status":"ok","value":34}\n',
            b'{"id":000003,"status":"ok","value":51}\n',
            b'{"id":000004,"status":"ok","value":68}\n',
            b'{"id":000005,"status":"retry","value":85}\n',
        ]
        predictor.prime(list(b"".join(rows)))

        width, _confidence, proposal, source_meta = ladder.choose_json_template_width_with_sources(
            predictor=predictor,
            remaining=64,
            widths=[32, 16, 8],
            threshold=0.55,
        )

        self.assertEqual(width, 32)
        self.assertTrue(bytes(proposal).startswith(b'{"id":000006,"status":"ok"'))
        self.assertEqual({m["source"] for m in source_meta}, {"json_template"})

    def test_json_template_bulk_predicts_across_rows(self):
        predictor = ladder.JsonTemplateDraft(ctx=8)
        rows = [
            b'{"id":000000,"status":"retry","value":0}\n',
            b'{"id":000001,"status":"ok","value":17}\n',
            b'{"id":000002,"status":"ok","value":34}\n',
            b'{"id":000003,"status":"ok","value":51}\n',
            b'{"id":000004,"status":"ok","value":68}\n',
            b'{"id":000005,"status":"retry","value":85}\n',
        ]
        predictor.prime(list(b"".join(rows)))

        proposal, source_meta = predictor.predict_many_with_sources(96, stop_below=0.55)

        self.assertIn(b'{"id":000007,"status":"ok","value":119}', bytes(proposal))
        self.assertEqual({m["source"] for m in source_meta}, {"json_template"})

    def test_json_template_learns_id_cycle_after_reset(self):
        parsed_rows = [
            ladder.parse_json_template_row('{"id":000000,"status":"retry","value":0}'),
            ladder.parse_json_template_row('{"id":000001,"status":"ok","value":17}'),
            ladder.parse_json_template_row('{"id":000002,"status":"ok","value":34}'),
            ladder.parse_json_template_row('{"id":000003,"status":"ok","value":51}'),
            ladder.parse_json_template_row('{"id":000004,"status":"ok","value":68}'),
            ladder.parse_json_template_row('{"id":000005,"status":"retry","value":85}'),
            ladder.parse_json_template_row('{"id":000000,"status":"retry","value":0}'),
        ]
        params = ladder.infer_json_template_params([row for row in parsed_rows if row is not None])
        next_row = ladder.next_json_template_row(parsed_rows[-1], params)

        self.assertEqual(params["id_mod"], 6)
        self.assertEqual(next_row, '{"id":000001,"status":"ok","value":17}\n')

    def test_json_template_falls_back_on_divergent_partial_line(self):
        predictor = ladder.JsonTemplateDraft(ctx=8)
        rows = [
            b'{"id":000000,"status":"retry","value":0}\n',
            b'{"id":000001,"status":"ok","value":17}\n',
            b'{"id":000002,"status":"ok","value":34}\n',
            b'{"id":000003,"status":"ok","value":51}\n',
            b'{"id":000004,"status":"ok","value":68}\n',
        ]
        predictor.prime(list(b"".join(rows)))
        predictor.observe(ord("x"))

        proposal, source_meta = predictor.predict_many_with_sources(8, stop_below=0.55)

        self.assertLessEqual(len(proposal), 8)
        self.assertNotIn("json_template", {m["source"] for m in source_meta})

    def test_prefix_json_template_branch_is_exact_and_reports_source(self):
        row = ladder.run_token_prefix_accept_adaptive_branch(
            scenario="json",
            n_tokens=512,
            prefix_frac=0.25,
            ctx=8,
            widths=[64, 32, 16, 8, 4, 2],
            verifier_work=0,
            confidence_threshold=0.55,
            fallback_tokens=2,
            proposal_source="json_template",
        )

        self.assertTrue(row["exact"])
        self.assertEqual(row["quality_gate"], True)
        self.assertEqual(row["meta"]["strategy"], "prefix_accept_json_template")
        self.assertEqual(row["meta"]["proposal_source"], "json_template")
        self.assertIn("json_template", row["meta"]["proposal_sources"])
        self.assertEqual(row["output_tokens"], row["meta"]["generated_tokens"])


if __name__ == "__main__":
    unittest.main()
