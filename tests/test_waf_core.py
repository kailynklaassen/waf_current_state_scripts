"""Off-cluster unit tests for the WAF assessment scoring and reporting logic.

Run from the repo root:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import waf_core as wc  # noqa: E402
import waf_questions as wq  # noqa: E402


class TestRatioStatus(unittest.TestCase):
    def test_nothing_found_is_open(self):
        self.assertEqual(wc.ratio_status(0, 0)[0], wc.STATUS_OPEN)
        self.assertEqual(wc.ratio_status(None, None)[0], wc.STATUS_OPEN)
        self.assertEqual(wc.ratio_status(5, None)[0], wc.STATUS_OPEN)

    def test_zero_of_many_is_in_progress_not_open(self):
        # Denominator exists, so we measured something: 0% adoption is in-progress.
        status, frac = wc.ratio_status(0, 100)
        self.assertEqual(status, wc.STATUS_IN_PROGRESS)
        self.assertEqual(frac, 0.0)

    def test_below_threshold_is_in_progress(self):
        self.assertEqual(wc.ratio_status(79, 100)[0], wc.STATUS_IN_PROGRESS)

    def test_at_threshold_is_completed(self):
        # Kailyn's rule: >= 80% is complete, < 80% is in-progress.
        self.assertEqual(wc.ratio_status(80, 100)[0], wc.STATUS_COMPLETED)
        self.assertEqual(wc.ratio_status(100, 100)[0], wc.STATUS_COMPLETED)

    def test_custom_threshold(self):
        self.assertEqual(wc.ratio_status(60, 100, complete_at=0.5)[0], wc.STATUS_COMPLETED)
        self.assertEqual(wc.ratio_status(40, 100, complete_at=0.5)[0], wc.STATUS_IN_PROGRESS)


class TestRatioResult(unittest.TestCase):
    def test_dg_01_02_style_in_progress_reason(self):
        r = wc.ratio_result(
            "DG-01-02", 431, 691, "production tables carry a table-level comment"
        )
        self.assertEqual(r.status, wc.STATUS_IN_PROGRESS)
        self.assertIn("62.4%", r.reason)
        self.assertIn("431 of 691", r.reason)
        self.assertIn("below the 80.0% threshold", r.reason)
        self.assertEqual(r.metrics["pct"], 62.37)

    def test_completed_reason_mentions_threshold(self):
        r = wc.ratio_result("X-01", 90, 100, "widgets are documented")
        self.assertEqual(r.status, wc.STATUS_COMPLETED)
        self.assertIn("90.0%", r.reason)
        self.assertIn("at or above the 80.0% threshold", r.reason)

    def test_empty_denominator_uses_none_found_text(self):
        r = wc.ratio_result("X-02", 0, 0, "tables", none_found="No tables in scope at all.")
        self.assertEqual(r.status, wc.STATUS_OPEN)
        self.assertEqual(r.reason, "No tables in scope at all.")

    def test_extra_is_appended(self):
        r = wc.ratio_result("X-03", 9, 10, "jobs retry", extra="Checked 30 days.")
        self.assertTrue(r.reason.endswith("Checked 30 days."))

    def test_metrics_are_json_serializable(self):
        r = wc.ratio_result("X-04", 1, 2, "things")
        json.dumps(r.metrics)  # must not raise


class TestPresenceResult(unittest.TestCase):
    def test_absent_is_open(self):
        r = wc.presence_result("X-05", 0, "external locations", missing_note="Configure one.")
        self.assertEqual(r.status, wc.STATUS_OPEN)
        self.assertIn("No external locations detected", r.reason)
        self.assertIn("Configure one.", r.reason)

    def test_present_meets_bar(self):
        r = wc.presence_result("X-06", 3, "service principals running jobs")
        self.assertEqual(r.status, wc.STATUS_COMPLETED)
        self.assertIn("Found 3", r.reason)

    def test_present_below_bar_is_in_progress(self):
        r = wc.presence_result("X-07", 2, "monitors", min_expected=5)
        self.assertEqual(r.status, wc.STATUS_IN_PROGRESS)


class TestManualAndError(unittest.TestCase):
    def test_manual_result(self):
        r = wc.manual_result("DG-01-01", "governance is a process, not a setting.", "policy docs")
        self.assertEqual(r.status, wc.STATUS_MANUAL)
        self.assertIn("Not determinable", r.reason)
        self.assertIn("policy docs", r.reason)
        self.assertFalse(r.metrics["automatable"])

    def test_error_result_is_open(self):
        r = wc.error_result("X-08", ValueError("boom\nsecond line"))
        self.assertEqual(r.status, wc.STATUS_OPEN)
        self.assertIn("ValueError", r.reason)
        self.assertNotIn("\n", r.reason)
        self.assertTrue(r.metrics["error"])


class TestScope(unittest.TestCase):
    def _scope(self, mode="prod", cats=("prod_sales", "main")):
        return wc.Scope(
            mode=mode,
            catalogs=list(cats),
            include_patterns=["prod%"],
            exclude_schema_patterns=["%dev%", "%test%"],
            system_catalogs=["system"],
        )

    def test_catalog_predicate_quotes_values(self):
        pred = self._scope().catalog_predicate()
        self.assertIn("'prod_sales'", pred)
        self.assertIn("'main'", pred)

    def test_empty_scope_is_false(self):
        self.assertEqual(self._scope(cats=()).catalog_predicate(), "false")

    def test_predicate_excludes_schemas_and_information_schema(self):
        pred = self._scope().predicate()
        self.assertIn("NOT (", pred)
        self.assertIn("%dev%", pred)
        self.assertIn("information_schema", pred)

    def test_sql_injection_via_catalog_name_is_escaped(self):
        s = self._scope(cats=["it's_prod"])
        self.assertIn("'it''s_prod'", s.catalog_predicate())

    def test_all_mode_emits_scope_note(self):
        self.assertEqual(self._scope().note, "")
        self.assertIn("no catalog matched", self._scope(mode="all").note)


class TestLikeClause(unittest.TestCase):
    def test_positive(self):
        c = wc._like_clause("catalog_name", ["prod%", "main"])
        self.assertIn("OR", c)
        self.assertIn("lower('prod%')", c)

    def test_negated(self):
        self.assertTrue(wc._like_clause("catalog_name", ["system"], negate=True).startswith("NOT"))

    def test_empty_patterns(self):
        self.assertEqual(wc._like_clause("c", []), "true")
        self.assertEqual(wc._like_clause("c", [], negate=True), "false")


class TestReporting(unittest.TestCase):
    def _rows(self):
        return [
            {
                "pillar": "Data & AI Governance",
                "question_id": "DG-01-01",
                "principle": "Unify data and AI management",
                "question": "Establish data governance process",
                "status": wc.STATUS_MANUAL,
                "reason": "Not determinable from system tables or APIs: process question.",
            },
            {
                "pillar": "Data & AI Governance",
                "question_id": "DG-01-02",
                "principle": "Unify data and AI management",
                "question": "Manage metadata for all data assets in one place",
                "status": wc.STATUS_IN_PROGRESS,
                "reason": "62.4% of production tables carry a comment | pipe test.",
            },
            {
                "pillar": "Reliability",
                "question_id": "R-01-01",
                "principle": "Design for failure",
                "question": "Use a data format that supports ACID transactions",
                "status": wc.STATUS_COMPLETED,
                "reason": "98.0% of tables are Delta.",
            },
            {
                "pillar": "Reliability",
                "question_id": "R-01-04",
                "principle": "Design for failure",
                "question": "Configure jobs for automatic retries",
                "status": wc.STATUS_OPEN,
                "reason": "No jobs found.",
            },
        ]

    def test_summarize(self):
        c = wc.summarize(self._rows())
        self.assertEqual(c[wc.STATUS_COMPLETED], 1)
        self.assertEqual(c[wc.STATUS_IN_PROGRESS], 1)
        self.assertEqual(c[wc.STATUS_OPEN], 1)
        self.assertEqual(c[wc.STATUS_MANUAL], 1)

    def test_manual_excluded_from_score(self):
        rpt = wc.render_final_report(self._rows(), run_id="test-run")
        # 1 completed of 3 machine-checkable = 33.3%
        self.assertIn("1/3", rpt)
        self.assertIn("33.3%", rpt)

    def test_final_report_has_expected_sections(self):
        rpt = wc.render_final_report(self._rows(), run_id="r1", scope_label="production")
        for section in ("## Overall summary", "## By pillar", "## Priority findings", "## Full results"):
            self.assertIn(section, rpt)
        self.assertIn("r1", rpt)
        self.assertIn("production", rpt)

    def test_pipes_escaped_in_markdown_table(self):
        rpt = wc.render_final_report(self._rows())
        self.assertIn("pipe test", rpt)
        self.assertIn("\\|", rpt)

    def test_priority_findings_open_before_in_progress(self):
        rpt = wc.render_final_report(self._rows())
        section = rpt.split("## Priority findings")[1].split("## Full results")[0]
        self.assertLess(section.index("R-01-04"), section.index("DG-01-02"))
        self.assertNotIn("DG-01-01", section)  # manual-review is not "actionable"

    def test_pillar_report_renders_all_questions(self):
        rows = [r for r in self._rows() if r["pillar"] == "Reliability"]
        txt = wc.render_pillar_report("Reliability", rows)
        self.assertIn("R-01-01", txt)
        self.assertIn("R-01-04", txt)
        self.assertIn("Automated score: 1/2", txt)

    def test_empty_results_do_not_crash(self):
        self.assertIn("Questions assessed:** 0", wc.render_final_report([]))
        self.assertIn("Reliability", wc.render_pillar_report("Reliability", []))

    def test_csv_export(self):
        csv_txt = wc.render_csv(self._rows())
        self.assertTrue(csv_txt.startswith("pillar,question_id,principle,question,status,reason"))
        self.assertEqual(len(csv_txt.strip().splitlines()), 5)  # header + 4

    def test_result_as_row_roundtrip(self):
        from datetime import datetime, timezone

        r = wc.Result("DG-01-02", "P", "Pr", "T", wc.STATUS_OPEN, "why", {"a": 1})
        row = r.as_row("run1", datetime.now(timezone.utc))
        self.assertEqual(row["question_id"], "DG-01-02")
        self.assertEqual(json.loads(row["metrics_json"])["a"], 1)


class TestQuestionCatalog(unittest.TestCase):
    def test_all_151_questions_present(self):
        self.assertEqual(len(wq.QUESTIONS), 151)

    def test_seven_pillars(self):
        self.assertEqual(len(wq.PILLAR_NAMES), 7)

    def test_expected_counts_per_pillar(self):
        expected = {
            "data-ai-governance": 12,
            "interoperability-usability": 14,
            "operational-excellence": 26,
            "security-compliance-privacy": 38,
            "reliability": 19,
            "performance-efficiency": 23,
            "cost-optimization": 19,
        }
        for pid, n in expected.items():
            self.assertEqual(len(wq.pillar_meta(pid)), n, pid)

    def test_pillar_meta_shape(self):
        meta = wq.pillar_meta("data-ai-governance")
        self.assertIn("DG-01-02", meta)
        self.assertEqual(meta["DG-01-02"]["title"], "Manage metadata for all data assets in one place")
        self.assertTrue(meta["DG-01-02"]["principle"])

    def test_every_question_has_title_and_pillar(self):
        for qid, q in wq.QUESTIONS.items():
            self.assertTrue(q["title"], qid)
            self.assertIn(q["pillar_id"], wq.PILLAR_NAMES, qid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
