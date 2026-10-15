"""Structural tests for the notebooks and per-pillar check coverage.

These run offline (no workspace, no Spark). They catch the failure modes that would
otherwise only surface after uploading to a workspace: malformed notebook JSON, a
pillar whose CHECKS list has drifted from the published question bank, or a check
function that is not wired up.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import ast
import json
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import waf_questions as wq  # noqa: E402

NOTEBOOK_DIR = os.path.join(REPO, "notebooks")
CHECKS_DIR = os.path.join(REPO, "checks")

#: notebook -> (check module, pillar id)
PILLAR_NOTEBOOKS = {
    "01_governance.ipynb": ("checks.governance", "data-ai-governance"),
    "02_interoperability.ipynb": ("checks.interoperability", "interoperability-usability"),
    "03_operational_excellence.ipynb": ("checks.operational_excellence", "operational-excellence"),
    "04_reliability.ipynb": ("checks.reliability", "reliability"),
    "05_performance.ipynb": ("checks.performance", "performance-efficiency"),
    "06_cost.ipynb": ("checks.cost", "cost-optimization"),
}

IMPLEMENTED_PILLARS = {pid for _, pid in PILLAR_NOTEBOOKS.values()}


def load_nb(name):
    with open(os.path.join(NOTEBOOK_DIR, name)) as fh:
        return json.load(fh)


def code_text(nb):
    return "\n".join(
        "".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"
    )


class TestNotebookStructure(unittest.TestCase):
    def all_notebooks(self):
        return sorted(f for f in os.listdir(NOTEBOOK_DIR) if f.endswith(".ipynb"))

    def test_expected_notebooks_exist(self):
        found = set(self.all_notebooks())
        expected = {"00_config.ipynb", "99_run_all.ipynb"} | set(PILLAR_NOTEBOOKS)
        self.assertTrue(expected <= found, f"missing: {sorted(expected - found)}")

    def test_notebooks_are_valid_json_with_required_keys(self):
        for name in self.all_notebooks():
            with self.subTest(notebook=name):
                nb = load_nb(name)
                self.assertIn("cells", nb)
                self.assertEqual(nb["nbformat"], 4)
                self.assertIn("metadata", nb)

    def test_cells_have_valid_shape(self):
        for name in self.all_notebooks():
            nb = load_nb(name)
            for i, cell in enumerate(nb["cells"]):
                with self.subTest(notebook=name, cell=i):
                    self.assertIn(cell["cell_type"], ("code", "markdown"))
                    self.assertIsInstance(cell["source"], list)
                    if cell["cell_type"] == "code":
                        self.assertIn("outputs", cell)
                        self.assertIn("execution_count", cell)

    def test_code_cells_are_syntactically_valid_python(self):
        """A syntax error here would only surface after upload."""
        for name in self.all_notebooks():
            nb = load_nb(name)
            for i, cell in enumerate(nb["cells"]):
                if cell["cell_type"] != "code":
                    continue
                src = "".join(cell["source"])
                if src.lstrip().startswith(("%", "!")):
                    continue  # magic commands are not Python
                with self.subTest(notebook=name, cell=i):
                    try:
                        ast.parse(src)
                    except SyntaxError as exc:
                        self.fail(f"{name} cell {i}: {exc}\n{src[:400]}")

    def test_notebooks_have_no_committed_output(self):
        for name in self.all_notebooks():
            nb = load_nb(name)
            for i, cell in enumerate(nb["cells"]):
                if cell["cell_type"] == "code":
                    with self.subTest(notebook=name, cell=i):
                        self.assertEqual(cell["outputs"], [])
                        self.assertIsNone(cell["execution_count"])

    def test_pillar_notebooks_run_config_first(self):
        for name in PILLAR_NOTEBOOKS:
            with self.subTest(notebook=name):
                self.assertIn("%run ./00_config", code_text(load_nb(name)))

    def test_driver_runs_config_first(self):
        self.assertIn("%run ./00_config", code_text(load_nb("99_run_all.ipynb")))

    def test_pillar_notebooks_reference_correct_module_and_pillar(self):
        for name, (module, pid) in PILLAR_NOTEBOOKS.items():
            with self.subTest(notebook=name):
                text = code_text(load_nb(name))
                self.assertIn(module, text)
                self.assertIn(pid, text)


class TestConfigNotebook(unittest.TestCase):
    def setUp(self):
        self.text = code_text(load_nb("00_config.ipynb"))

    def test_required_values_default_to_empty_so_user_must_fill_them(self):
        """Shipping a real catalog name would silently write to someone else's catalog."""
        self.assertIn('CATALOG = ""', self.text)
        self.assertIn('SCHEMA = ""', self.text)

    def test_validation_raises_when_required_values_missing(self):
        self.assertIn("raise ValueError", self.text)

    def test_documents_the_80_percent_threshold(self):
        self.assertIn("COMPLETE_AT = 0.80", self.text)

    def test_exposes_scope_and_lookback_knobs(self):
        for knob in ("PROD_CATALOG_PATTERNS", "PROD_SCHEMA_EXCLUDE_PATTERNS",
                     "LOOKBACK_DAYS", "SYSTEM_CATALOGS"):
            self.assertIn(knob, self.text)

    def test_defines_run_pillar_helper(self):
        self.assertIn("def run_pillar", self.text)

    def test_run_pillar_validates_question_coverage(self):
        self.assertIn("AssertionError", self.text)


class TestDriverNotebook(unittest.TestCase):
    def setUp(self):
        self.text = code_text(load_nb("99_run_all.ipynb"))

    def test_runs_every_implemented_pillar(self):
        for module, pid in PILLAR_NOTEBOOKS.values():
            with self.subTest(pillar=pid):
                self.assertIn(module, self.text)

    def test_unimplemented_security_pillar_is_declared_not_silently_dropped(self):
        self.assertIn("security-compliance-privacy", self.text)
        self.assertIn("NOT_IMPLEMENTED", self.text)

    def test_builds_final_report_and_exports(self):
        self.assertIn("render_final_report", self.text)
        self.assertIn("render_csv", self.text)

    def test_one_failing_pillar_does_not_abort_the_run(self):
        self.assertIn("except Exception", self.text)
        self.assertIn("pillar_errors", self.text)


class TestPillarCheckCoverage(unittest.TestCase):
    """Every implemented pillar must cover exactly its published question ids."""

    def modules(self):
        import importlib

        for module, pid in sorted(PILLAR_NOTEBOOKS.values()):
            yield importlib.import_module(module), pid

    def test_checks_match_question_bank_exactly(self):
        for mod, pid in self.modules():
            with self.subTest(pillar=pid):
                expected = set(wq.pillar_meta(pid))
                got = {qid for qid, _ in mod.CHECKS}
                self.assertEqual(
                    got, expected,
                    f"{pid}: missing={sorted(expected - got)} "
                    f"unexpected={sorted(got - expected)}",
                )

    def test_no_duplicate_question_ids(self):
        for mod, pid in self.modules():
            with self.subTest(pillar=pid):
                ids = [qid for qid, _ in mod.CHECKS]
                self.assertEqual(len(ids), len(set(ids)))

    def test_every_check_is_callable(self):
        for mod, pid in self.modules():
            for qid, fn in mod.CHECKS:
                with self.subTest(pillar=pid, qid=qid):
                    self.assertTrue(callable(fn), f"{qid} is not callable")

    def test_every_check_has_a_docstring(self):
        for mod, pid in self.modules():
            for qid, fn in mod.CHECKS:
                with self.subTest(pillar=pid, qid=qid):
                    self.assertTrue((fn.__doc__ or "").strip(), f"{qid} lacks a docstring")

    def test_pillar_id_constant_matches(self):
        for mod, pid in self.modules():
            with self.subTest(pillar=pid):
                self.assertEqual(mod.PILLAR_ID, pid)

    def test_implemented_pillars_cover_113_questions(self):
        """6 of 7 pillars = 113 of 151 questions; security (38) is not implemented."""
        total = sum(len(wq.pillar_meta(pid)) for pid in IMPLEMENTED_PILLARS)
        self.assertEqual(total, 113)
        self.assertEqual(len(wq.QUESTIONS) - total, 38)

    def test_performance_skips_nonexistent_pe_02_13(self):
        """The published bank has no PE-02-13; inventing one would be wrong."""
        self.assertNotIn("PE-02-13", wq.QUESTIONS)
        import checks.performance as perf

        self.assertNotIn("PE-02-13", {qid for qid, _ in perf.CHECKS})


class TestCheckModulesAreImportableWithoutSpark(unittest.TestCase):
    """Check modules must not touch Spark at import time."""

    def test_import_does_not_require_spark(self):
        import importlib

        for name in sorted(os.listdir(CHECKS_DIR)):
            if not name.endswith(".py") or name == "__init__.py":
                continue
            with self.subTest(module=name):
                importlib.import_module(f"checks.{name[:-3]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
