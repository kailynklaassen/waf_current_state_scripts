"""Every check must behave sanely on a workspace with nothing in it.

Real test workspaces have data, so they never exercise the paths a brand-new or
locked-down workspace takes. These tests drive every check with a stub context and
assert two things:

1. No check raises, and all return a well-formed ``Result`` with a real rationale.
2. **A check never reports ``completed`` when its evidence source was unreadable.**
   Treating "we could not look" as "the control is fine" would report a false pass
   on a customer's assessment, which is the worst failure this tool could have.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import waf_core as wc  # noqa: E402

CHECK_MODULES = [
    "checks.governance",
    "checks.interoperability",
    "checks.operational_excellence",
    "checks.reliability",
    "checks.performance",
    "checks.cost",
]

VALID_STATUSES = {
    wc.STATUS_OPEN,
    wc.STATUS_IN_PROGRESS,
    wc.STATUS_COMPLETED,
    wc.STATUS_MANUAL,
}

#: Checks allowed to report ``completed`` with zero findings, because the *absence*
#: of the thing genuinely is the good outcome - and only when the source was readable.
ABSENCE_IS_GOOD = {
    "PE-02-04",  # no classic clusters -> serverless sizes itself
    "CO-01-04",  # no classic clusters -> runtime is Databricks-managed
    "CO-01-05",  # no GPU spend -> no GPU waste
    "CO-01-07",  # no classic clusters -> instance type is managed
    "CO-02-02",  # no interactive clusters -> nothing to auto-terminate
    "CO-03-05",  # no Delta Shares -> no egress cost to manage
    "CO-04-02",  # no classic clusters -> spot mix is managed
    "OE-04-01",  # no quota pressure observed -> limits are not binding
    "OE-02-02",  # no clusters, policies present
    "IU-03-03",  # no clusters, policies present
    "R-01-02",   # managed compute in use
    "R-03-01",   # serverless autoscales
}


class StubCtx(wc.Ctx):
    """A context that answers every query as empty.

    ``tables_readable=False`` simulates a workspace where system schemas are not
    granted; ``True`` simulates readable-but-empty system tables.
    """

    def __init__(self, tables_readable: bool):
        super().__init__(
            {
                "catalog": "c",
                "schema": "s",
                "lookback_days": 90,
                "complete_at": 0.80,
                "prod_catalog_patterns": ["prod%"],
                "prod_schema_exclude_patterns": [],
                "system_catalogs": ["system"],
            }
        )
        self._tables_readable = tables_readable
        self._scope = wc.Scope("prod", ["prod_x"], ["prod%"], [], ["system"])

    # Every data access returns "nothing found".
    def sql(self, query):
        return []

    def one(self, query):
        return {}

    def count(self, query, key="n"):
        return 0

    def count_safe(self, query, key="n", default=0):
        return default

    def has_table(self, fqn):
        return self._tables_readable

    def has_column(self, fqn, column):
        return self._tables_readable

    @property
    def w(self):
        return None  # SDK unavailable


def all_checks():
    for module_name in CHECK_MODULES:
        mod = importlib.import_module(module_name)
        for qid, fn in mod.CHECKS:
            yield module_name, qid, fn


class TestGreenfieldWorkspace(unittest.TestCase):
    def test_no_check_raises_when_nothing_is_readable(self):
        ctx = StubCtx(tables_readable=False)
        for module_name, qid, fn in all_checks():
            with self.subTest(module=module_name, qid=qid):
                try:
                    fn(ctx)
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"{qid} raised {type(exc).__name__}: {exc}")

    def test_no_check_raises_on_empty_tables(self):
        ctx = StubCtx(tables_readable=True)
        for module_name, qid, fn in all_checks():
            with self.subTest(module=module_name, qid=qid):
                try:
                    fn(ctx)
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"{qid} raised {type(exc).__name__}: {exc}")

    def test_all_statuses_are_valid(self):
        for readable in (False, True):
            ctx = StubCtx(readable)
            for _, qid, fn in all_checks():
                with self.subTest(qid=qid, readable=readable):
                    self.assertIn(fn(ctx).status, VALID_STATUSES)

    def test_every_rationale_is_substantive(self):
        """A bare 'open' with no explanation is useless to whoever reads the report."""
        for readable in (False, True):
            ctx = StubCtx(readable)
            for _, qid, fn in all_checks():
                with self.subTest(qid=qid, readable=readable):
                    reason = fn(ctx).reason
                    self.assertTrue(reason, f"{qid} has an empty rationale")
                    self.assertGreater(
                        len(reason), 25, f"{qid} rationale too terse: {reason!r}"
                    )

    def test_metrics_are_json_serializable(self):
        """Metrics are persisted as JSON, so an unserializable value breaks the write."""
        for readable in (False, True):
            ctx = StubCtx(readable)
            for _, qid, fn in all_checks():
                with self.subTest(qid=qid, readable=readable):
                    json.dumps(fn(ctx).metrics, default=str)

    def test_no_false_pass_when_nothing_is_readable(self):
        """The critical guard: unreadable evidence must never score ``completed``.

        Reporting a control as satisfied because we could not check it would give a
        customer a false clean bill of health.
        """
        ctx = StubCtx(tables_readable=False)
        offenders = []
        for _, qid, fn in all_checks():
            res = fn(ctx)
            if res.status == wc.STATUS_COMPLETED and qid not in ABSENCE_IS_GOOD:
                offenders.append((qid, res.reason[:160]))
        self.assertEqual(
            offenders, [],
            "these checks report 'completed' with no readable evidence:\n"
            + "\n".join(f"  {q}: {r}" for q, r in offenders),
        )

    def test_greenfield_is_mostly_open(self):
        """An empty workspace should read as 'no evidence', not as a passing grade."""
        ctx = StubCtx(tables_readable=False)
        counts = wc.summarize([fn(ctx) for _, _, fn in all_checks()])
        total = sum(counts.values())
        self.assertGreater(
            counts[wc.STATUS_OPEN] / total, 0.85,
            f"expected mostly 'open' on a greenfield workspace, got {counts}",
        )

    def test_manual_review_count_is_stable(self):
        """6 questions are process-only; a change here should be deliberate."""
        ctx = StubCtx(tables_readable=True)
        manual = [qid for _, qid, fn in all_checks()
                  if fn(ctx).status == wc.STATUS_MANUAL]
        self.assertEqual(
            sorted(manual),
            ["DG-01-01", "DG-03-01", "OE-01-01", "OE-01-09", "OE-03-03", "PE-03-01"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
