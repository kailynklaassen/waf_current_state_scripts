#!/usr/bin/env python3
"""Execute every pillar check against a real workspace via the Databricks CLI.

This is a *syntax and permissions* smoke test, run from a laptop without a cluster.
It swaps ``Ctx.sql`` for an implementation backed by
``databricks experimental aitools tools query``, so every SQL statement in every
check is really parsed and executed by Databricks SQL. Statuses produced here
reflect the test workspace, not a customer, so only failures matter.

Usage:
    python tools/smoke_test.py --profile <PROFILE> [--pillar governance] [-v]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import waf_core as wc  # noqa: E402
import waf_questions as wq  # noqa: E402

PILLAR_MODULES = {
    "governance": ("checks.governance", "data-ai-governance"),
    "interoperability": ("checks.interoperability", "interoperability-usability"),
    "operational_excellence": ("checks.operational_excellence", "operational-excellence"),
    "security": ("checks.security", "security-compliance-privacy"),
    "reliability": ("checks.reliability", "reliability"),
    "performance": ("checks.performance", "performance-efficiency"),
    "cost": ("checks.cost", "cost-optimization"),
}


class CliCtx(wc.Ctx):
    """A :class:`Ctx` whose SQL runs through the Databricks CLI instead of Spark."""

    def __init__(self, cfg, profile: str, verbose: bool = False):
        super().__init__(cfg)
        self.profile = profile
        self.verbose = verbose
        self.queries: list[str] = []
        self.failures: list[tuple[str, str]] = []

    def sql(self, query: str) -> list[dict]:
        one_line = " ".join(query.split())
        self.queries.append(one_line)
        if self.verbose:
            print(f"    SQL> {one_line[:220]}")
        proc = subprocess.run(
            [
                "databricks", "experimental", "aitools", "tools", "query",
                query, "--profile", self.profile,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        # The aitools CLI prints result JSON on stderr, so both streams must be
        # considered when locating the payload.
        out = proc.stdout or ""
        err = proc.stderr or ""
        combined = out + "\n" + err
        if proc.returncode != 0 or "Error:" in combined:
            self.failures.append((one_line, combined.strip()[:500]))
            raise RuntimeError(combined.strip()[:400])
        match = re.search(r"\[.*\]", combined, re.S)
        if not match:
            return []
        try:
            rows = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            self.failures.append((one_line, f"unparseable output: {exc}"))
            raise
        return rows

    @property
    def w(self):
        """Real SDK client against the chosen profile."""
        if self._w is None:
            try:
                from databricks.sdk import WorkspaceClient

                self._w = WorkspaceClient(profile=self.profile)
            except Exception as exc:
                print(f"  (SDK unavailable: {exc})")
                return None
        return self._w

    def has_table(self, fqn: str) -> bool:
        if fqn in self._table_cache:
            return self._table_cache[fqn]
        try:
            self.sql(f"SELECT 1 AS one FROM {fqn} LIMIT 1")
            ok = True
        except Exception:
            ok = False
            if self.failures:
                self.failures.pop()  # probing a missing table is not a code defect
        self._table_cache[fqn] = ok
        return ok

    def has_column(self, fqn: str, column: str) -> bool:
        key = f"{fqn}::{column}"
        if key in self._table_cache:
            return self._table_cache[key]
        try:
            self.sql(f"SELECT {column} FROM {fqn} LIMIT 1")
            ok = True
        except Exception:
            ok = False
            if self.failures:
                self.failures.pop()  # probing an absent column is not a code defect
        self._table_cache[key] = ok
        return ok

    def count_safe(self, query: str, key: str = "n", default: int = 0) -> int:
        try:
            return self.count(query, key)
        except Exception:
            if self.failures:
                self.failures.pop()
            return default


def default_cfg() -> dict:
    return {
        "catalog": "main",
        "schema": "waf_assessment",
        "results_table": "waf_assessment_results",
        "prod_catalog_patterns": ["prod%", "main", "%_prod", "%_production"],
        "prod_schema_exclude_patterns": ["%dev%", "%test%", "%sandbox%", "%tmp%", "%scratch%"],
        "system_catalogs": ["system", "__databricks_internal%", "samples"],
        "lookback_days": 90,
        "complete_at": 0.80,
        "debug": True,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--pillar", action="append", help="repeatable; default all")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    import importlib

    targets = args.pillar or list(PILLAR_MODULES)
    ctx = CliCtx(default_cfg(), args.profile, args.verbose)

    print(f"Resolving scope via profile {args.profile} ...")
    t0 = time.time()
    scope = ctx.scope
    print(f"  mode={scope.mode} catalogs={scope.catalogs} ({time.time() - t0:.1f}s)\n")

    all_results: list[wc.Result] = []
    errors = 0
    for name in targets:
        if name not in PILLAR_MODULES:
            print(f"!! unknown pillar {name}")
            return 2
        mod_name, pillar_id = PILLAR_MODULES[name]
        try:
            mod = importlib.import_module(mod_name)
        except ModuleNotFoundError:
            print(f"-- {name}: module {mod_name} not written yet, skipping\n")
            continue
        pillar_name = wq.PILLAR_NAMES[pillar_id]
        print(f"=== {pillar_name} ({len(mod.CHECKS)} checks) ===")
        t0 = time.time()
        res = ctx.run_checks(pillar_name, wq.pillar_meta(pillar_id), mod.CHECKS)
        all_results.extend(res)

        expected = set(wq.pillar_meta(pillar_id))
        got = {r.qid for r in res}
        if expected - got:
            print(f"  !! MISSING checks: {sorted(expected - got)}")
            errors += 1
        if got - expected:
            print(f"  !! UNKNOWN ids: {sorted(got - expected)}")
            errors += 1
        errs = [r for r in res if r.metrics.get("error")]
        if errs:
            print(f"  !! {len(errs)} check(s) raised:")
            for r in errs:
                print(f"     {r.qid}: {r.reason[:300]}")
            errors += len(errs)
        print(f"  ({time.time() - t0:.1f}s)\n")

    print("=" * 90)
    counts = wc.summarize(all_results)
    print(f"Checks run: {len(all_results)}  {counts}")
    print(f"SQL statements executed: {len(ctx.queries)}")
    if ctx.failures:
        print(f"\n!! {len(ctx.failures)} SQL FAILURE(S):")
        for q, e in ctx.failures:
            print(f"\n  QUERY: {q[:300]}\n  ERROR: {e}")
    print("=" * 90)
    if errors or ctx.failures:
        print(f"RESULT: FAIL ({errors} check error(s), {len(ctx.failures)} SQL failure(s))")
        return 1
    print("RESULT: PASS - all checks executed cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
