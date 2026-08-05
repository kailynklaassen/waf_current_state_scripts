"""
Shared helpers for the Databricks Well-Architected Framework (WAF) current-state
assessment notebooks.

This module is intentionally free of ``pyspark`` / ``databricks.sdk`` imports at the
top level so that the scoring and reporting logic can be unit tested off-cluster.
The Spark session and the WorkspaceClient are resolved lazily by ``Ctx``.

Status vocabulary matches the WAF Assessment Tool (databricks-solutions.github.io/
waf-assessment-tool) so results can be transcribed back into the tool:

    open           - no supporting evidence found, or the control is absent
    in-progress    - partially adopted (below the "completed" threshold)
    completed      - adoption at or above the "completed" threshold
    manual-review  - not observable from system tables / APIs; needs a human answer
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

# --------------------------------------------------------------------------------------
# Status vocabulary
# --------------------------------------------------------------------------------------

STATUS_OPEN = "open"
STATUS_IN_PROGRESS = "in-progress"
STATUS_COMPLETED = "completed"
STATUS_MANUAL = "manual-review"

#: Sort order used when rendering reports (worst / most actionable first).
STATUS_SORT = {
    STATUS_OPEN: 0,
    STATUS_IN_PROGRESS: 1,
    STATUS_MANUAL: 2,
    STATUS_COMPLETED: 3,
}

STATUS_ICON = {
    STATUS_OPEN: "[!]",
    STATUS_IN_PROGRESS: "[~]",
    STATUS_COMPLETED: "[x]",
    STATUS_MANUAL: "[?]",
}


# --------------------------------------------------------------------------------------
# Result container
# --------------------------------------------------------------------------------------


@dataclass
class Result:
    """One assessed WAF question."""

    qid: str
    pillar: str
    principle: str
    title: str
    status: str
    reason: str
    metrics: dict[str, Any] = field(default_factory=dict)

    def as_row(self, run_id: str, run_ts: datetime) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "run_timestamp": run_ts,
            "pillar": self.pillar,
            "question_id": self.qid,
            "principle": self.principle,
            "question": self.title,
            "status": self.status,
            "reason": self.reason,
            "metrics_json": json.dumps(self.metrics, default=str),
        }


# --------------------------------------------------------------------------------------
# Scoring helpers
# --------------------------------------------------------------------------------------


def ratio_status(
    numerator: float | int | None,
    denominator: float | int | None,
    complete_at: float = 0.80,
) -> tuple[str, float | None]:
    """Grade a coverage ratio.

    Returns ``(status, fraction)``. When there is nothing to measure
    (``denominator`` is 0/None) the question is ``open`` and the fraction is
    ``None`` - "we could not find anything" is an open item, not a pass.
    """
    if not denominator:
        return STATUS_OPEN, None
    if numerator is None:
        return STATUS_OPEN, None
    frac = float(numerator) / float(denominator)
    return (STATUS_COMPLETED if frac >= complete_at else STATUS_IN_PROGRESS), frac


def fmt_pct(frac: float | None) -> str:
    """Format a 0-1 fraction as a percentage string."""
    if frac is None:
        return "n/a"
    return f"{frac * 100:.1f}%"


def ratio_result(
    qid: str,
    numerator: int | None,
    denominator: int | None,
    subject: str,
    *,
    complete_at: float = 0.80,
    none_found: str | None = None,
    extra: str = "",
    metrics: dict[str, Any] | None = None,
) -> Result:
    """Build a :class:`Result` from a coverage ratio with an auto-generated rationale.

    ``subject`` completes the sentence "<pct> of <subject>", e.g.
    ``"production tables carry a table-level comment"``.
    """
    status, frac = ratio_status(numerator, denominator, complete_at)
    m = dict(metrics or {})
    m.update(
        {
            "numerator": numerator,
            "denominator": denominator,
            "pct": None if frac is None else round(frac * 100, 2),
            "complete_at_pct": round(complete_at * 100, 2),
        }
    )

    if frac is None:
        reason = none_found or f"No {subject} found in scope, so no evidence of adoption."
    else:
        verdict = (
            f"at or above the {fmt_pct(complete_at)} threshold"
            if status == STATUS_COMPLETED
            else f"below the {fmt_pct(complete_at)} threshold"
        )
        reason = (
            f"{fmt_pct(frac)} of {subject} ({numerator:,} of {denominator:,}) - {verdict}."
        )
    if extra:
        reason = f"{reason} {extra.strip()}"
    return Result(qid, "", "", "", status, reason, m)


def presence_result(
    qid: str,
    found: int | None,
    subject: str,
    *,
    min_expected: int = 1,
    found_note: str = "",
    missing_note: str = "",
    metrics: dict[str, Any] | None = None,
) -> Result:
    """Grade a simple "does this exist at all?" control.

    Presence alone is rarely full adoption, so a positive finding is graded
    ``completed`` only when ``found >= min_expected``; anything present but under
    the bar is ``in-progress``.
    """
    m = dict(metrics or {})
    m.update({"found": found, "min_expected": min_expected})
    if not found:
        reason = f"No {subject} detected. {missing_note}".strip()
        return Result(qid, "", "", "", STATUS_OPEN, reason, m)
    status = STATUS_COMPLETED if found >= min_expected else STATUS_IN_PROGRESS
    reason = f"Found {found:,} {subject}. {found_note}".strip()
    return Result(qid, "", "", "", status, reason, m)


def manual_result(qid: str, why: str, evidence_to_gather: str = "") -> Result:
    """A question that cannot be answered from telemetry."""
    reason = f"Not determinable from system tables or APIs: {why}"
    if evidence_to_gather:
        reason = f"{reason} Ask the customer for: {evidence_to_gather}"
    return Result(qid, "", "", "", STATUS_MANUAL, reason, {"automatable": False})


def error_result(qid: str, exc: BaseException) -> Result:
    """A check that blew up. Reported as ``open`` so it stays on the radar."""
    msg = f"{type(exc).__name__}: {exc}".replace("\n", " ")[:600]
    return Result(
        qid,
        "",
        "",
        "",
        STATUS_OPEN,
        f"Check could not complete, treat as unverified: {msg}",
        {"error": True},
    )


# --------------------------------------------------------------------------------------
# Production scope
# --------------------------------------------------------------------------------------


def _like_clause(col: str, patterns: Sequence[str], negate: bool = False) -> str:
    """Build ``(col LIKE p1 OR col LIKE p2 ...)``, lowercase-insensitive."""
    if not patterns:
        return "true" if not negate else "false"
    parts = [f"lower({col}) LIKE lower('{p}')" for p in patterns]
    joined = " OR ".join(parts)
    return f"NOT ({joined})" if negate else f"({joined})"


@dataclass
class Scope:
    """Resolved assessment scope (which catalogs/schemas count as production)."""

    mode: str  # "prod" | "all"
    catalogs: list[str]
    include_patterns: list[str]
    exclude_schema_patterns: list[str]
    system_catalogs: list[str]

    @property
    def note(self) -> str:
        if self.mode == "prod":
            return ""
        return (
            "[SCOPE: no catalog matched the configured production patterns, so ALL "
            "non-system catalogs were assessed] "
        )

    @property
    def label(self) -> str:
        if self.mode == "prod":
            return f"production ({len(self.catalogs)} catalog(s): {', '.join(self.catalogs[:8])})"
        return f"all non-system catalogs ({len(self.catalogs)})"

    def catalog_predicate(self, col: str = "table_catalog") -> str:
        """SQL predicate restricting ``col`` to the in-scope catalogs."""
        if not self.catalogs:
            return "false"
        quoted = ", ".join("'" + c.replace("'", "''") + "'" for c in self.catalogs)
        return f"{col} IN ({quoted})"

    def predicate(self, cat_col: str = "table_catalog", sch_col: str = "table_schema") -> str:
        """Full scope predicate over a catalog column and a schema column."""
        clauses = [self.catalog_predicate(cat_col)]
        if sch_col and self.exclude_schema_patterns:
            clauses.append(_like_clause(sch_col, self.exclude_schema_patterns, negate=True))
        if sch_col:
            clauses.append(f"lower({sch_col}) <> 'information_schema'")
        return " AND ".join(f"({c})" for c in clauses)


# --------------------------------------------------------------------------------------
# Execution context
# --------------------------------------------------------------------------------------


class Ctx:
    """Runtime context: config, Spark, SDK client, resolved scope, result collection."""

    def __init__(self, cfg: dict[str, Any], spark: Any = None, w: Any = None):
        self.cfg = cfg
        self._spark = spark
        self._w = w
        self._scope: Scope | None = None
        self._table_cache: dict[str, bool] = {}
        self.results: list[Result] = []
        self.run_id = cfg.get("run_id") or datetime.now(timezone.utc).strftime(
            "waf-%Y%m%dT%H%M%SZ"
        )
        self.run_ts = datetime.now(timezone.utc)

    # -- lazy resources ---------------------------------------------------------------

    @property
    def spark(self):
        if self._spark is None:
            try:
                from pyspark.sql import SparkSession  # noqa: PLC0415

                self._spark = SparkSession.getActiveSession() or SparkSession.builder.getOrCreate()
            except Exception as exc:  # pragma: no cover - cluster only
                raise RuntimeError("No active Spark session available") from exc
        return self._spark

    @property
    def w(self):
        """Workspace SDK client (``None`` if the SDK is unavailable)."""
        if self._w is None:
            try:
                from databricks.sdk import WorkspaceClient  # noqa: PLC0415

                self._w = WorkspaceClient()
            except Exception:
                return None
        return self._w

    @property
    def lookback_days(self) -> int:
        return int(self.cfg.get("lookback_days", 90))

    @property
    def complete_at(self) -> float:
        return float(self.cfg.get("complete_at", 0.80))

    # -- SQL ---------------------------------------------------------------------------

    def sql(self, query: str) -> list[dict[str, Any]]:
        """Run a query and return rows as dicts."""
        return [r.asDict(recursive=True) for r in self.spark.sql(query).collect()]

    def one(self, query: str) -> dict[str, Any]:
        """Run a query expected to return a single row; ``{}`` when empty."""
        rows = self.sql(query)
        return rows[0] if rows else {}

    def count(self, query: str, key: str = "n") -> int:
        """Run an aggregate query and return an integer column (0 when missing/NULL)."""
        val = self.one(query).get(key)
        return int(val) if val is not None else 0

    def has_table(self, fqn: str) -> bool:
        """Whether a (system) table exists and is readable. Cached."""
        if fqn in self._table_cache:
            return self._table_cache[fqn]
        ok = False
        try:
            self.spark.sql(f"SELECT 1 FROM {fqn} LIMIT 1").collect()
            ok = True
        except Exception:
            ok = False
        self._table_cache[fqn] = ok
        return ok

    def require_tables(self, *fqns: str) -> str | None:
        """Return a human-readable note naming the first unavailable table, else None."""
        for fqn in fqns:
            if not self.has_table(fqn):
                return fqn
        return None

    def has_column(self, fqn: str, column: str) -> bool:
        """Whether a column exists on a table.

        System table schemas vary by cloud, DBR version and rollout stage, so
        checks that read newer columns must probe first rather than assume.
        """
        key = f"{fqn}::{column}"
        if key in self._table_cache:
            return self._table_cache[key]
        ok = False
        try:
            self.spark.sql(f"SELECT {column} FROM {fqn} LIMIT 0").collect()
            ok = True
        except Exception:
            ok = False
        self._table_cache[key] = ok
        return ok

    def count_safe(self, query: str, key: str = "n", default: int = 0) -> int:
        """Like :meth:`count`, but returns ``default`` instead of raising.

        For optional/supplementary signals where an unavailable source should
        soften the rationale rather than fail the whole question.
        """
        try:
            return self.count(query, key)
        except Exception:
            return default

    # -- scope -------------------------------------------------------------------------

    @property
    def scope(self) -> Scope:
        if self._scope is None:
            self._scope = self.resolve_scope()
        return self._scope

    def resolve_scope(self) -> Scope:
        """Resolve production catalogs, falling back to all non-system catalogs."""
        sys_cats = list(self.cfg.get("system_catalogs", []))
        inc = list(self.cfg.get("prod_catalog_patterns", []))
        exc_sch = list(self.cfg.get("prod_schema_exclude_patterns", []))

        not_system = _like_clause("catalog_name", sys_cats, negate=True)
        all_rows = self.sql(
            "SELECT catalog_name FROM system.information_schema.catalogs "
            f"WHERE {not_system} ORDER BY 1"
        )
        all_cats = [r["catalog_name"] for r in all_rows]

        if inc:
            match = _like_clause("catalog_name", inc)
            rows = self.sql(
                "SELECT catalog_name FROM system.information_schema.catalogs "
                f"WHERE {not_system} AND {match} ORDER BY 1"
            )
            prod = [r["catalog_name"] for r in rows]
            if prod:
                return Scope("prod", prod, inc, exc_sch, sys_cats)
        return Scope("all", all_cats, inc, exc_sch, sys_cats)

    # -- result collection -------------------------------------------------------------

    def run_checks(
        self,
        pillar: str,
        catalog_meta: dict[str, dict[str, str]],
        checks: Iterable[tuple[str, Callable[["Ctx"], Result] | None]],
        verbose: bool = True,
    ) -> list[Result]:
        """Execute ``(qid, fn)`` checks, attach question metadata, print progress.

        A ``fn`` of ``None`` is not expected; use a lambda returning
        :func:`manual_result` instead. Exceptions are trapped per-check.
        """
        out: list[Result] = []
        for qid, fn in checks:
            try:
                res = fn(self) if fn else error_result(qid, RuntimeError("no check defined"))
            except Exception as exc:  # noqa: BLE001 - one bad check must not stop the pillar
                if self.cfg.get("debug"):
                    traceback.print_exc()
                res = error_result(qid, exc)
            meta = catalog_meta.get(qid, {})
            res.qid = qid
            res.pillar = pillar
            res.principle = meta.get("principle", "")
            res.title = meta.get("title", "")
            # Prefix the scope caveat onto anything measured against catalog data.
            if self.scope.mode == "all" and res.metrics.get("scoped"):
                res.reason = self.scope.note + res.reason
            out.append(res)
            if verbose:
                print(f"  {STATUS_ICON.get(res.status, '[ ]')} {qid:<10} {res.status:<14} {res.title}")
                print(f"      {res.reason}")
        self.results.extend(out)
        return out


# --------------------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------------------

RESULTS_SCHEMA_DDL = """
    run_id STRING,
    run_timestamp TIMESTAMP,
    pillar STRING,
    question_id STRING,
    principle STRING,
    question STRING,
    status STRING,
    reason STRING,
    metrics_json STRING
"""


def results_table_fqn(cfg: dict[str, Any]) -> str:
    return f"{cfg['catalog']}.{cfg['schema']}.{cfg.get('results_table', 'waf_assessment_results')}"


def ensure_results_table(ctx: Ctx) -> str:
    """Create the catalog/schema/table used to hand results between notebooks."""
    cfg = ctx.cfg
    fqn = results_table_fqn(cfg)
    if cfg.get("create_catalog"):
        ctx.spark.sql(f"CREATE CATALOG IF NOT EXISTS {cfg['catalog']}")
    ctx.spark.sql(f"CREATE SCHEMA IF NOT EXISTS {cfg['catalog']}.{cfg['schema']}")
    ctx.spark.sql(f"CREATE TABLE IF NOT EXISTS {fqn} ({RESULTS_SCHEMA_DDL}) USING DELTA")
    return fqn


def persist_results(ctx: Ctx, results: Sequence[Result]) -> str:
    """Replace this run's rows for the assessed pillar(s) and append fresh ones."""
    if not results:
        return ""
    fqn = ensure_results_table(ctx)
    pillars = sorted({r.pillar for r in results})
    quoted = ", ".join("'" + p.replace("'", "''") + "'" for p in pillars)
    ctx.spark.sql(
        f"DELETE FROM {fqn} WHERE run_id = '{ctx.run_id}' AND pillar IN ({quoted})"
    )

    rows = [r.as_row(ctx.run_id, ctx.run_ts) for r in results]
    df = ctx.spark.createDataFrame(rows, schema=_results_struct())
    df.write.mode("append").saveAsTable(fqn)
    print(f"\nPersisted {len(rows)} result(s) to {fqn} (run_id={ctx.run_id})")
    return fqn


def _results_struct():
    from pyspark.sql.types import (  # noqa: PLC0415
        StringType,
        StructField,
        StructType,
        TimestampType,
    )

    return StructType(
        [
            StructField("run_id", StringType()),
            StructField("run_timestamp", TimestampType()),
            StructField("pillar", StringType()),
            StructField("question_id", StringType()),
            StructField("principle", StringType()),
            StructField("question", StringType()),
            StructField("status", StringType()),
            StructField("reason", StringType()),
            StructField("metrics_json", StringType()),
        ]
    )


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------


def summarize(results: Sequence[Result | dict]) -> dict[str, int]:
    """Count results by status."""
    counts = {s: 0 for s in (STATUS_OPEN, STATUS_IN_PROGRESS, STATUS_COMPLETED, STATUS_MANUAL)}
    for r in results:
        status = r["status"] if isinstance(r, dict) else r.status
        counts[status] = counts.get(status, 0) + 1
    return counts


def _as_dict(r: Result | dict) -> dict[str, Any]:
    if isinstance(r, dict):
        return r
    return {
        "pillar": r.pillar,
        "question_id": r.qid,
        "principle": r.principle,
        "question": r.title,
        "status": r.status,
        "reason": r.reason,
    }


def render_pillar_report(pillar: str, results: Sequence[Result | dict]) -> str:
    """Plain-text report for a single pillar."""
    rows = [_as_dict(r) for r in results]
    counts = summarize(rows)
    scored = counts[STATUS_OPEN] + counts[STATUS_IN_PROGRESS] + counts[STATUS_COMPLETED]
    lines = [
        "=" * 100,
        f"WAF CURRENT-STATE ASSESSMENT - {pillar}",
        "=" * 100,
        f"Questions: {len(rows)}   "
        f"completed: {counts[STATUS_COMPLETED]}   "
        f"in-progress: {counts[STATUS_IN_PROGRESS]}   "
        f"open: {counts[STATUS_OPEN]}   "
        f"manual-review: {counts[STATUS_MANUAL]}",
    ]
    if scored:
        lines.append(
            f"Automated score: {counts[STATUS_COMPLETED]}/{scored} "
            f"({counts[STATUS_COMPLETED] / scored * 100:.1f}%) of machine-checkable questions complete"
        )
    lines.append("=" * 100)
    for r in rows:
        lines.append("")
        lines.append(f"{r['question_id']}  [{r['status']}]  {r['question']}")
        lines.append(f"    Principle: {r['principle']}")
        lines.append(f"    Reason:    {r['reason']}")
    lines.append("")
    return "\n".join(lines)


def render_final_report(
    rows: Sequence[dict],
    run_id: str = "",
    scope_label: str = "",
    workspace: str = "",
) -> str:
    """Markdown report combining every pillar."""
    rows = [_as_dict(r) for r in rows]
    counts = summarize(rows)
    scored = counts[STATUS_OPEN] + counts[STATUS_IN_PROGRESS] + counts[STATUS_COMPLETED]

    out: list[str] = [
        "# Databricks Well-Architected Framework - Current State Assessment",
        "",
        f"- **Run ID:** `{run_id}`",
        f"- **Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
    ]
    if workspace:
        out.append(f"- **Workspace:** {workspace}")
    if scope_label:
        out.append(f"- **Assessment scope:** {scope_label}")
    out += [
        f"- **Questions assessed:** {len(rows)}",
        "",
        "## Overall summary",
        "",
        "| Status | Count | Share |",
        "| --- | ---: | ---: |",
    ]
    for status in (STATUS_COMPLETED, STATUS_IN_PROGRESS, STATUS_OPEN, STATUS_MANUAL):
        n = counts[status]
        share = f"{n / len(rows) * 100:.1f}%" if rows else "n/a"
        out.append(f"| {status} | {n} | {share} |")
    if scored:
        out += [
            "",
            f"**Automated maturity score: {counts[STATUS_COMPLETED]}/{scored} "
            f"({counts[STATUS_COMPLETED] / scored * 100:.1f}%)** of machine-checkable "
            "questions are complete. `manual-review` questions are excluded from the "
            "score because they require a human answer.",
        ]

    # Per-pillar rollup
    pillars: dict[str, list[dict]] = {}
    for r in rows:
        pillars.setdefault(r["pillar"], []).append(r)

    out += [
        "",
        "## By pillar",
        "",
        "| Pillar | Questions | completed | in-progress | open | manual-review | Score |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for pillar, prs in pillars.items():
        c = summarize(prs)
        s = c[STATUS_OPEN] + c[STATUS_IN_PROGRESS] + c[STATUS_COMPLETED]
        score = f"{c[STATUS_COMPLETED] / s * 100:.0f}%" if s else "n/a"
        out.append(
            f"| {pillar} | {len(prs)} | {c[STATUS_COMPLETED]} | {c[STATUS_IN_PROGRESS]} "
            f"| {c[STATUS_OPEN]} | {c[STATUS_MANUAL]} | {score} |"
        )

    # Priority actions: open then in-progress, excluding manual
    actionable = [r for r in rows if r["status"] in (STATUS_OPEN, STATUS_IN_PROGRESS)]
    actionable.sort(key=lambda r: (STATUS_SORT.get(r["status"], 9), r["question_id"]))
    if actionable:
        out += ["", "## Priority findings (open, then in-progress)", ""]
        for r in actionable:
            out.append(f"- **{r['question_id']}** ({r['status']}) - {r['question']}")
            out.append(f"  - {r['reason']}")

    # Full detail
    out += ["", "## Full results", ""]
    for pillar, prs in pillars.items():
        out += [f"### {pillar}", ""]
        by_principle: dict[str, list[dict]] = {}
        for r in prs:
            by_principle.setdefault(r["principle"], []).append(r)
        for principle, qrs in by_principle.items():
            out += [f"**{principle}**", ""]
            out += ["| Question | Status | Rationale |", "| --- | --- | --- |"]
            for r in qrs:
                reason = r["reason"].replace("|", "\\|")
                out.append(
                    f"| `{r['question_id']}` {r['question']} | {r['status']} | {reason} |"
                )
            out.append("")
    return "\n".join(out)


def render_csv(rows: Sequence[dict]) -> str:
    """CSV export of the combined results."""
    import csv  # noqa: PLC0415
    import io  # noqa: PLC0415

    buf = io.StringIO()
    cols = ["pillar", "question_id", "principle", "question", "status", "reason"]
    wtr = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    wtr.writeheader()
    for r in rows:
        wtr.writerow(_as_dict(r))
    return buf.getvalue()


# --------------------------------------------------------------------------------------
# Repo bootstrap (used by the config notebook)
# --------------------------------------------------------------------------------------


def find_repo_root(start: str | None = None, marker: str = "waf_core.py") -> str | None:
    """Walk up from ``start`` looking for the directory containing ``marker``."""
    cur = os.path.abspath(start or os.getcwd())
    for _ in range(8):
        if os.path.exists(os.path.join(cur, marker)):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def bootstrap_sys_path(start: str | None = None) -> str | None:
    """Ensure the repo root is importable, and return it."""
    root = find_repo_root(start)
    if root and root not in sys.path:
        sys.path.insert(0, root)
    return root
