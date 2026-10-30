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
import re
import sys
import traceback
import uuid
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

#: Why a table could not be read: the runner lacks a grant, or it does not exist here
#: (a system schema not enabled, or a feature not in use - Databricks reports both as
#: TABLE_OR_VIEW_NOT_FOUND).
ACCESS_DENIED = "denied"
ACCESS_MISSING = "missing"


def classify_table_error(message: str) -> str:
    """:data:`ACCESS_DENIED` for permission errors, else :data:`ACCESS_MISSING`."""
    m = (message or "").upper()
    if any(t in m for t in ("INSUFFICIENT_PERMISSIONS", "PERMISSION_DENIED",
                            "DOES NOT HAVE", "NOT AUTHORIZED", "ACCESS DENIED")):
        return ACCESS_DENIED
    return ACCESS_MISSING


#: Values accepted for ``cfg["cloud_provider"]`` (``CLOUD_PROVIDER`` in 00_config).
CLOUD_PROVIDERS = ("azure", "aws", "gcp")


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

    Returns ``(status, fraction)``:

    - Nothing to measure (``denominator`` 0/None, or ``numerator`` None) -> ``open``
      with fraction ``None`` - "we could not find anything" is an open item, not a pass.
    - A measured zero (0 of N covered) -> ``open`` with fraction ``0.0``. No adoption of
      the control is observed, so it is an open item, not partial progress.
    - Above zero but below ``complete_at`` -> ``in-progress`` (some, but not enough).
    - At or above ``complete_at`` -> ``completed``.
    """
    if not denominator:
        return STATUS_OPEN, None
    if numerator is None:
        return STATUS_OPEN, None
    frac = float(numerator) / float(denominator)
    if frac <= 0:
        return STATUS_OPEN, frac
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
    elif frac <= 0:
        reason = (
            f"0.0% of {subject} (0 of {denominator:,}) - no adoption of the control "
            "is observed."
        )
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
        reason = f"{reason} Gather from your Databricks environment's owners: {evidence_to_gather}"
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


def _like_match(value: str, pattern: str) -> bool:
    """Python twin of ``lower(value) LIKE lower(pattern)`` (``%`` any run, ``_`` one char)."""
    rx = "".join(".*" if ch == "%" else "." if ch == "_" else re.escape(ch)
                 for ch in pattern.lower())
    return re.fullmatch(rx, (value or "").lower(), re.S) is not None


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

    def contains(self, catalog: str, schema: str | None = None) -> bool:
        """Python twin of :meth:`predicate`, for objects listed through the SDK."""
        if catalog not in self.catalogs:
            return False
        return not (schema and any(_like_match(schema, p)
                                   for p in self.exclude_schema_patterns))

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

#: Columns carried into the table-lineage snapshot - the union of what the lineage
#: checks read, kept under their original names so a check only swaps the table name.
_TABLE_LINEAGE_PROJECTION = (
    "source_table_catalog, source_table_schema, source_table_name, "
    "source_table_full_name, source_type, target_table_catalog, "
    "target_table_schema, target_table_name, target_table_full_name, "
    "entity_type, entity_id, event_date"
)

#: Columns carried into the column-lineage snapshot.
_COLUMN_LINEAGE_PROJECTION = (
    "source_table_full_name, source_column_name, target_table_full_name, "
    "target_column_name, event_date"
)


def lineage_edges_fqn(cfg: dict[str, Any]) -> str:
    return f"{cfg['catalog']}.{cfg['schema']}.{cfg.get('lineage_table', 'waf_lineage_edges')}"


def lineage_column_edges_fqn(cfg: dict[str, Any]) -> str:
    return (f"{cfg['catalog']}.{cfg['schema']}."
            f"{cfg.get('lineage_column_table', 'waf_lineage_column_edges')}")


def lineage_closure_fqn(cfg: dict[str, Any]) -> str:
    return (f"{cfg['catalog']}.{cfg['schema']}."
            f"{cfg.get('lineage_closure_table', 'waf_lineage_closure')}")


def build_lineage_tables(ctx: "Ctx", max_hops: int = 32) -> dict[str, Any]:
    """Materialize the lineage snapshot and its transitive closure in the configured
    schema, following github.com/kailynklaassen/databricks_lineage.

    Call this once as a setup step (``00_config``) rather than lazily inside a check:
    the lineage-heavy checks then read small persisted tables instead of re-scanning
    ``system.access.table_lineage`` / ``column_lineage`` each time.

    Builds three tables (lookback-windowed):

    - ``waf_lineage_edges`` - deduped direct edges, projected to the columns the checks
      read (original names), so a check only swaps the table it reads from.
    - ``waf_lineage_column_edges`` - column-level direct edges.
    - ``waf_lineage_closure`` - the transitive closure of ``(trg_tbl, src_tbl)`` pairs,
      computed with the iterative BFS "frontier + anti-join" loop from the repo (each
      hop is materialized, so known pairs are never re-derived and cycles can't loop).

    Returns a summary dict of row counts (and ``skipped``/``error`` keys when it no-ops),
    and is safe to call when lineage can't be materialized - it degrades to a no-op and
    the checks fall back to the system tables.
    """
    cfg = ctx.cfg
    if not cfg.get("materialize_lineage", True):
        return {"skipped": "MATERIALIZE_LINEAGE is False"}
    if not cfg.get("persist_results", True):
        return {"skipped": "PERSIST_RESULTS is False (read-only run)"}
    if not cfg.get("catalog") or not cfg.get("schema"):
        return {"skipped": "CATALOG/SCHEMA not set"}
    if not ctx.has_table("system.access.table_lineage"):
        return {"skipped": "system.access.table_lineage not readable"}

    spark = ctx.spark
    schema = f"{cfg['catalog']}.{cfg['schema']}"
    lookback = ctx.lookback_days
    out: dict[str, Any] = {}
    try:
        spark.sql(f"CREATE SCHEMA IF NOT EXISTS {schema}")

        # 1. Direct table-lineage edges (rich projection for the checks).
        edges = lineage_edges_fqn(cfg)
        spark.sql(
            f"CREATE OR REPLACE TABLE {edges} AS "
            f"SELECT DISTINCT {_TABLE_LINEAGE_PROJECTION} FROM system.access.table_lineage "
            f"WHERE event_date >= current_date() - INTERVAL {lookback} DAYS"
        )
        out["edges"] = spark.table(edges).count()

        # 2. Transitive closure via iterative BFS (frontier + anti-join), per the repo.
        closure = lineage_closure_fqn(cfg)
        pairs = f"{schema}.waf_lineage_edge_pairs"
        frontier = f"{schema}.waf_lineage_frontier"
        spark.sql(
            f"CREATE OR REPLACE TABLE {pairs} AS "
            f"SELECT DISTINCT target_table_full_name AS trg_tbl, "
            f"source_table_full_name AS src_tbl FROM {edges} "
            f"WHERE source_type <> 'PATH' "
            f"AND source_table_full_name IS NOT NULL AND target_table_full_name IS NOT NULL "
            f"AND source_table_full_name <> target_table_full_name"
        )
        spark.sql(f"CREATE OR REPLACE TABLE {closure} AS SELECT trg_tbl, src_tbl FROM {pairs}")
        spark.sql(f"CREATE OR REPLACE TABLE {frontier} AS SELECT trg_tbl, src_tbl FROM {pairs}")
        total = spark.table(closure).count()
        hops = 1
        for _ in range(2, max_hops + 1):
            # New edges one hop further out, minus pairs already in the closure.
            spark.sql(
                f"CREATE OR REPLACE TABLE {frontier} AS "
                f"SELECT DISTINCT f.trg_tbl, e.src_tbl "
                f"FROM {frontier} f JOIN {pairs} e ON f.src_tbl = e.trg_tbl "
                f"WHERE f.trg_tbl <> e.src_tbl "
                f"AND NOT EXISTS (SELECT 1 FROM {closure} c "
                f"                WHERE c.trg_tbl = f.trg_tbl AND c.src_tbl = e.src_tbl)"
            )
            new_edges = spark.table(frontier).count()
            if new_edges == 0:
                break
            spark.sql(f"INSERT INTO {closure} SELECT trg_tbl, src_tbl FROM {frontier}")
            total += new_edges
            hops += 1
        spark.sql(f"DROP TABLE IF EXISTS {frontier}")
        spark.sql(f"DROP TABLE IF EXISTS {pairs}")
        out["closure"] = total
        out["hops"] = hops

        # 3. Column-level edges.
        if ctx.has_table("system.access.column_lineage"):
            col = lineage_column_edges_fqn(cfg)
            spark.sql(
                f"CREATE OR REPLACE TABLE {col} AS "
                f"SELECT DISTINCT {_COLUMN_LINEAGE_PROJECTION} FROM system.access.column_lineage "
                f"WHERE event_date >= current_date() - INTERVAL {lookback} DAYS"
            )
            out["column_edges"] = spark.table(col).count()
    except Exception as exc:  # noqa: BLE001 - setup must never abort the assessment
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}

    # Reset cached resolutions so the checks pick up the freshly built tables.
    ctx._lineage_tbl = ctx._lineage_col_tbl = ctx._lineage_closure = None
    return out


class Ctx:
    """Runtime context: config, Spark, SDK client, resolved scope, result collection."""

    def __init__(self, cfg: dict[str, Any], spark: Any = None, w: Any = None):
        self.cfg = cfg
        self._spark = spark
        self._w = w
        self._scope: Scope | None = None
        self._table_cache: dict[str, bool] = {}
        # fqn -> ACCESS_DENIED / ACCESS_MISSING for every table that could not be read,
        # plus the subset probed by the check currently running (see run_checks).
        self.unreadable: dict[str, str] = {}
        self._check_unreadable: dict[str, str] = {}
        self._runner: str | None = None
        self._lineage_tbl: str | None = None
        self._lineage_col_tbl: str | None = None
        self._lineage_closure: str | None = None
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

    @property
    def cloud(self) -> str:
        """``aws`` / ``azure`` / ``gcp`` from ``cfg["cloud_provider"]`` - selects the
        cloud-specific instance, GPU and spot rules in cost checks."""
        cloud = str(self.cfg.get("cloud_provider") or "azure").strip().lower()
        if cloud not in CLOUD_PROVIDERS:
            raise ValueError(f"cloud_provider must be one of {CLOUD_PROVIDERS}, got {cloud!r}")
        return cloud

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
        """Whether a (system) table exists and is readable. Cached; failures are recorded
        with their cause so results can say which grant is missing."""
        if fqn not in self._table_cache:
            ok = False
            try:
                self.spark.sql(f"SELECT 1 FROM {fqn} LIMIT 1").collect()
                ok = True
            except Exception as exc:  # noqa: BLE001
                self.unreadable[fqn] = classify_table_error(str(exc))
            self._table_cache[fqn] = ok
        if not self._table_cache[fqn] and fqn in self.unreadable:
            self._check_unreadable[fqn] = self.unreadable[fqn]
        return self._table_cache[fqn]

    @property
    def runner(self) -> str:
        """The principal running the assessment (``current_user()``)."""
        if self._runner is None:
            try:
                self._runner = str(self.one("SELECT current_user() AS u").get("u") or "")
            except Exception:  # noqa: BLE001
                self._runner = ""
        return self._runner or "the runner"

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

    # -- lineage snapshot --------------------------------------------------------------

    def lineage_table(self) -> str:
        """Table to query for table-level lineage.

        Returns the materialized snapshot (``<catalog>.<schema>.waf_lineage_edges``) when
        it exists - built once up front by :func:`build_lineage_tables` from ``00_config``
        so lineage-heavy checks read a small local table instead of re-scanning the large
        ``system.access.table_lineage``. Falls back to the system table when the snapshot
        was not materialized (read-only run, no write access, or materialization off), so
        checks are correct either way. Resolved once and cached for the run.
        """
        if self._lineage_tbl is None:
            self._lineage_tbl = self._resolve_lineage(
                lineage_edges_fqn, "system.access.table_lineage"
            )
        return self._lineage_tbl

    def column_lineage_table(self) -> str:
        """Column-level analogue of :meth:`lineage_table`
        (``<catalog>.<schema>.waf_lineage_column_edges`` -> ``system.access.column_lineage``)."""
        if self._lineage_col_tbl is None:
            self._lineage_col_tbl = self._resolve_lineage(
                lineage_column_edges_fqn, "system.access.column_lineage"
            )
        return self._lineage_col_tbl

    def lineage_closure_table(self) -> str:
        """The transitive table-lineage closure (``waf_lineage_closure``, columns
        ``trg_tbl``/``src_tbl``) when materialized, for multi-hop "all upstream sources"
        questions. Returns ``""`` when it was not built - callers must handle that."""
        if self._lineage_closure is None:
            cfg = self.cfg
            fqn = ""
            if cfg.get("materialize_lineage", True) and cfg.get("catalog") and cfg.get("schema"):
                cand = lineage_closure_fqn(cfg)
                if self.has_table(cand):
                    fqn = cand
            self._lineage_closure = fqn
        return self._lineage_closure

    def _resolve_lineage(self, fqn_fn: Callable[[dict], str], system_fqn: str) -> str:
        """Prefer the materialized snapshot if present; else the system table."""
        cfg = self.cfg
        if cfg.get("materialize_lineage", True) and cfg.get("catalog") and cfg.get("schema"):
            fqn = fqn_fn(cfg)
            if self.has_table(fqn):
                return fqn
        return system_fqn

    def _attach_catalog_breakdown(self, fn: Callable[["Ctx"], Result], res: Result) -> None:
        """Record ``metrics["by_catalog"] = {catalog: [numerator, denominator]}`` for a
        production-scoped ratio, by re-running the same check once per in-scope catalog.

        Runs in different workspaces can see different catalogs (workspace bindings), so a
        combined report must merge catalog by catalog rather than add or pick runs. Re-using
        the check itself keeps the per-catalog logic identical to the headline. Skipped for
        non-ratio results, the all-catalogs fallback, and scopes over
        ``per_catalog_max_catalogs`` (default 50). Disable with ``per_catalog_breakdown``.
        """
        m = res.metrics
        if (not m.get("scoped") or "numerator" not in m or m.get("denominator") is None
                or not self.cfg.get("per_catalog_breakdown", True)):
            return
        scope = self.scope
        cats = list(scope.catalogs)
        if scope.mode != "prod" or not cats or len(cats) > int(
                self.cfg.get("per_catalog_max_catalogs", 50)):
            return
        if len(cats) == 1:
            m["by_catalog"] = {cats[0]: [m["numerator"], m["denominator"]]}
            return
        by: dict[str, list] = {}
        try:
            for cat in cats:
                self._scope = Scope(scope.mode, [cat], scope.include_patterns,
                                    scope.exclude_schema_patterns, scope.system_catalogs)
                try:
                    sub = fn(self)
                except Exception:  # noqa: BLE001 - breakdown is best-effort
                    continue
                if "numerator" in sub.metrics:
                    by[cat] = [sub.metrics["numerator"], sub.metrics["denominator"]]
        finally:
            self._scope = scope
        m["by_catalog"] = by

    def visibility_note(self) -> str:
        """Caveat naming catalogs bound only to other workspaces (``""`` if none/unknown).

        Imported lazily: ``checks.inventory`` depends on this module."""
        if not self.cfg.get("detect_hidden_catalogs", True):
            return ""
        try:
            from checks import inventory  # noqa: PLC0415

            return inventory.visibility_note(self)
        except Exception:
            return ""

    def access_note(self, unreadable: dict[str, str]) -> str:
        """``[ACCESS: ...]`` caveat naming each unreadable table and how to fix it."""
        if not unreadable:
            return ""
        parts = []
        for fqn, kind in sorted(unreadable.items()):
            if kind == ACCESS_DENIED:
                schema = fqn.rsplit(".", 1)[0]
                parts.append(f"{fqn} - {self.runner} lacks the grant (GRANT USE CATALOG ON "
                             f"CATALOG {schema.split('.')[0]}, USE SCHEMA and SELECT ON SCHEMA "
                             f"{schema} TO `{self.runner}`)")
            else:
                parts.append(f"{fqn} - not found (system schema not enabled, or the "
                             "feature is not in use here)")
        return "[ACCESS: could not read " + "; ".join(parts) + "] "

    def _apply_access_gaps(self, res: Result, unreadable: dict[str, str]) -> None:
        """An unreadable table means the result may describe the runner's access rather
        than the environment. If the check could not read its own source (it returned
        "not readable"), or a permission gap left it at ``open``, mark it manual-review;
        either way name the gap. A table that simply does not exist only adds the note."""
        if not unreadable:
            return
        denied = any(k == ACCESS_DENIED for k in unreadable.values())
        own_source = res.status == STATUS_OPEN and "not readable" in res.reason
        if res.status == STATUS_OPEN and (denied or own_source):
            res.status = STATUS_MANUAL
            res.metrics["access_gap"] = True
        res.metrics["unreadable_tables"] = unreadable
        res.reason = self.access_note(unreadable) + res.reason

    def access_scope_note(self) -> str:
        """Caveat naming in-scope catalogs the runner has no privileges on (``""`` if
        none/unknown). Imported lazily: ``checks.inventory`` depends on this module."""
        if not self.cfg.get("detect_hidden_catalogs", True):
            return ""
        try:
            from checks import inventory  # noqa: PLC0415

            return inventory.access_scope_note(self)
        except Exception:
            return ""

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
            self._check_unreadable = {}
            try:
                res = fn(self) if fn else error_result(qid, RuntimeError("no check defined"))
            except Exception as exc:  # noqa: BLE001 - one bad check must not stop the pillar
                if self.cfg.get("debug"):
                    traceback.print_exc()
                res = error_result(qid, exc)
            self._apply_access_gaps(res, dict(self._check_unreadable))
            if fn is not None:
                self._attach_catalog_breakdown(fn, res)
            meta = catalog_meta.get(qid, {})
            res.qid = qid
            res.pillar = pillar
            res.principle = meta.get("principle", "")
            res.title = meta.get("title", "")
            # Prefix the scope caveat onto anything measured against catalog data.
            if self.scope.mode == "all" and res.metrics.get("scoped"):
                res.reason = self.scope.note + res.reason
            # ...and the binding caveat when catalogs are invisible from this workspace.
            if res.metrics.get("scoped"):
                vis = self.visibility_note()
                if vis:
                    res.reason = vis + res.reason
                # In-scope catalogs hidden by the runner's privileges: the ratio covers
                # only part of the scope, so it cannot be trusted as the scored status.
                gap = self.access_scope_note()
                if gap:
                    if res.status != STATUS_MANUAL:
                        res.metrics["status_before_access_gap"] = res.status
                        res.status = STATUS_MANUAL
                    res.metrics["access_gap"] = True
                    res.reason = gap + res.reason
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
    export: dict | None = None,
) -> str:
    """Markdown report combining every pillar.

    ``export`` (``checks.inventory.run_export``) adds the appendices that make the report
    self-contained for combining several workspaces' reports and building an HTML report:
    per-workspace limits, per-catalog breakdowns, catalog visibility, and the combine /
    HTML instructions."""
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
    if export:
        out += [
            f"- **Workspace ID:** {export.get('workspace_id') or 'unknown'}",
            f"- **Metastore:** {export.get('metastore') or 'unknown'}",
            f"- **Scope mode:** {export.get('scope', {}).get('mode', '')}",
            f"- **Activity window:** {export.get('window', '')} "
            f"({export.get('lookback_days', '')} days)",
            f"- **Completion threshold:** {float(export.get('complete_at', 0.8)) * 100:.0f}%",
        ]
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
    if export:
        out += render_appendices(export)
    return "\n".join(out)


def _md(v: Any) -> str:
    return str(v).replace("|", "\\|")


def render_appendices(export: dict) -> list[str]:
    """Appendices A-E of the markdown report (see :func:`render_final_report`)."""
    res = {x["question_id"]: x for x in export.get("results", [])}
    out: list[str] = ["", "---", "", "## Appendix A - Per-workspace service limits (OE-04-01)", ""]
    q = (res.get("OE-04-01") or {}).get("metrics") or {}
    per_ws = q.get("per_workspace") or {}
    out.append(
        f"Status rule: any asset >= {q.get('open_at_pct', 80):g}% of its limit -> open; else "
        f"average >= {q.get('complete_below_pct', 60):g}% -> in-progress; else completed. "
        "Per-workspace limits use the fullest workspace; every workspace is listed below.")
    for label, block in per_ws.items():
        out += ["", f"**{label}** (limit {int(block['limit']):,} per workspace)", "",
                "| Workspace | Count | Utilization |", "| --- | ---: | ---: |"]
        for ws, n, pct in block.get("workspaces", []):
            out.append(f"| {_md(ws)} | {int(n):,} | {pct:.1f}% |")
    assets = q.get("assets") or {}
    if assets:
        out += ["", "**All assessed limits** (used for the status)", "",
                "| Asset | Count | Limit | Utilization |", "| --- | ---: | ---: | ---: |"]
        for label, a in sorted(assets.items(), key=lambda kv: -kv[1]["utilization_pct"]):
            out.append(f"| {_md(label)} | {int(a['count']):,} | {int(a['limit']):,} "
                       f"| {a['utilization_pct']:.1f}% |")

    out += ["", "## Appendix B - Per-catalog breakdown of catalog-scoped checks", "",
            "Production-scoped ratio checks, re-run once per in-scope catalog. A question "
            "listed here is **catalog-scoped** (see Appendix D).", "",
            "| Question | Catalog | Numerator | Denominator | Threshold |",
            "| --- | --- | ---: | ---: | ---: |"]
    for qid in sorted(res):
        m = res[qid].get("metrics") or {}
        for cat, (n, d) in sorted((m.get("by_catalog") or {}).items()):
            out.append(f"| {qid} | {_md(cat)} | {int(n or 0):,} | {int(d or 0):,} "
                       f"| {m.get('complete_at_pct', 80):g}% |")

    inv = export.get("catalog_inventory") or {}
    hidden = export.get("hidden_catalogs") or []
    out += ["", "## Appendix C - Catalog visibility from this workspace", "",
            f"{len(inv):,} non-system catalog(s) visible here; {len(hidden):,} bound only to "
            "other workspaces (invisible to `information_schema` here).", "",
            "| Catalog | Has description | Schemas | Tables | Fullest schema: tables "
            "| Fullest schema: functions | Fullest schema: volumes | Metric views |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for cat in sorted(inv):
        v = inv[cat]
        out.append("| " + " | ".join([_md(cat)] + [str(int(v.get(k) or 0)) for k in (
            "has_comment", "schemas", "tables", "max_tables_per_schema",
            "max_functions_per_schema", "max_volumes_per_schema", "metric_views")]) + " |")
    if hidden:
        out += ["", "**Bound only to other workspaces:** " + ", ".join(_md(h) for h in hidden)]
    priv = export.get("privilege_hidden_catalogs") or []
    if priv:
        out += ["", f"**In scope but not visible to {_md(str(export.get('runner') or 'the runner'))} "
                f"(no privileges; catalog-scoped results marked manual-review):** "
                + ", ".join(_md(h) for h in priv)]
    unread = export.get("unreadable_tables") or {}
    if unread:
        out += ["", "**System tables the runner could not read:** " + ", ".join(
            f"{_md(t)} ({'missing grant' if k == ACCESS_DENIED else 'not found'})"
            for t, k in unread.items())]
    out += ["", COMBINE_INSTRUCTIONS, "", HTML_REPORT_INSTRUCTIONS]
    return out


#: Appendix D of every markdown report: how to merge several workspaces' reports.
COMBINE_INSTRUCTIONS = """## Appendix D - Combining reports from several workspaces

Run the assessment in each workspace that needs to be covered, then merge the markdown
reports with these rules. They are deterministic: two people (or an LLM) following them
on the same files get the same result.

1. **Group the reports by metastore** (header `Metastore`, `cloud:region:id`). Only
   combine reports from the same metastore. Most system tables are regional, so reports
   from different regions measure different workspaces.
2. **Pick the base report**: the most recent report whose `Scope mode` is `prod`
   (else the most recent report). Use its question list, titles and wording.
3. **Classify each question** and combine it accordingly:
   - **Catalog-scoped** - the question appears in Appendix B. Take the union of catalogs
     across all reports' Appendix B rows for that question. A catalog in several reports
     is counted **once**, using the most recent report's row. Sum the numerators and
     denominators over the union, then recompute: denominator 0 -> `open`; numerator 0 ->
     `open`; share >= the row's threshold -> `completed`; otherwise `in-progress`. In the
     base rationale, replace the first percentage, the `(n of d)` pair and the threshold
     verdict, and change the scope label to `production (K catalog(s) across N workspace
     run(s): ...)`.
   - **Metastore counts** - recompute from the union of all reports' Appendix C tables
     (a catalog in several reports counted once, most recent row wins):
     OE-01-07 = catalogs with a description / catalogs (threshold 80%); OE-04-01
     `Catalogs (per metastore)` = number of catalogs, `Tables (per metastore)` = sum of
     tables, the `fullest ...` rows = the maximum. Then re-apply the OE-04-01 status rule
     in Appendix A to the updated `All assessed limits` table.
   - **Account-level** - every other question. System tables and the audit log already
     cover every workspace in the region, and per-workspace objects (cluster policies,
     Git folders, secret scopes, Apps, warehouses, jobs, identities) are counted for every
     workspace in each run. Take the base report's status and rationale unchanged. The
     reports should agree up to the minutes or hours between runs; a different status
     is worth a note but not a merge.
4. **Visibility caveat**: hidden after merge = union of all `Bound only to other
   workspaces` lists minus the union of Appendix C catalogs. Remove any `[VISIBILITY: ...]`
   prefix from the base rationales; if the merged hidden list is non-empty, prefix
   catalog-scoped rationales with `[VISIBILITY: N catalog(s) are bound only to workspaces
   that were not assessed (first five names)]`.
5. **Score**: `completed / (questions - manual-review)` overall and per pillar.
6. **WAF Assessment Tool JSON** (optional): one `{question_id: status}` map per pillar
   slug, with `manual-review` written as `open` (the tool has no such state).
7. **Record the method** in the combined report: the reports used (workspace, run id,
   scope), the base report, and which questions were catalog-merged."""


#: Appendix E of every markdown report: the HTML report template, account-neutral.
HTML_REPORT_INSTRUCTIONS = """## Appendix E - Building the HTML report

A single self-contained HTML file (inline CSS/JS, no external assets) named
`waf_unified_report.html`, presented as scrollable slides. Use only names that appear in
the reports (workspace names, catalogs); add no other organization-specific wording.

**Status vocabulary** (always icon + label, never color alone): completed `#0ca30c` with a
check mark; in-progress `#fab219` with a half circle (dark text on it); open `#d03b3b` with
a cross; manual-review `#a9a8a0` with a question mark. Provide light and dark themes
(`prefers-color-scheme`) and print styles (one slide per page; expand every toggle).

**Slides, in order**
1. **Sticky navigation** - Overview, How results were combined, one link per pillar,
   Appendix, plus an "Expand all scoring logic" button.
2. **Overview** - title `Assessment across N workspace(s)`, subtitle with cloud,
   generation date and question count; four KPI tiles (automated maturity score with
   `x of y scored questions`, in-progress count, open count, manual-review count); a
   stacked status bar with counts and a legend; a pillar table: pillar (link), score,
   a horizontal bar of the completed share, a mini stacked status bar, question count.
   Footnote: `Score = completed / (questions - manual review)`.
3. **How results were combined** - the workspaces assessed (name, run id, scope), the
   three question classes from Appendix D and how each was combined, the base report,
   a consistency note (how many statuses agree across reports), and the visibility and
   region caveats.
4. **One slide per pillar** - eyebrow `Pillar i of n`, pillar name, large score with
   `x of y scored questions complete`; a stacked status bar and legend; a table with
   columns ID, Question (title plus principle), Status (badge), Measure, and
   `Where you are & why` (the rationale). **Measure** shows the percentage, a thin meter
   with a tick at the threshold, and `n of d` - only when the percentage is the
   `(n of d)` ratio in the rationale; otherwise an em dash. Tag rows `merged by catalog`
   (Appendix B questions) or `combined across workspaces` where step 3 changed them.
5. **"How this was scored" toggle** under every question, built from the check's source
   (`checks/<pillar module>.py`, function named after the question id, e.g. `co_01_04`
   for CO-01-04): one sentence on what is measured; numerator and denominator; a data
   sources table (system table or API, the columns used, the filter); a thresholds and
   messages table (status, condition, message template) with the row matching the
   result highlighted; the config settings and defaults it depends on; notes; and the
   SQL, collapsed.
6. **Appendix** - Appendix A's per-workspace limit tables and Appendix B's per-catalog
   breakdown, combined across reports.

**Charts**: thin marks, 2px gaps between stacked segments, hover tooltips with exact
counts, no dual axes, one hue per status."""


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
# WAF Assessment Tool JSON export
# --------------------------------------------------------------------------------------

#: The WAF Assessment Tool's category taxonomy. These are tool metadata, independent
#: of the assessed questions, carried through verbatim so an exported assessment
#: re-imports cleanly at databricks-solutions.github.io/waf-assessment-tool.
WAF_CATEGORIES = [
    "ETL & DS tools",
    "BI Tools",
    "Orchestration",
    "Ingest & Transform",
    "Advanced Analytics, ML & Gen AI",
    "Data Warehouse",
    "Collaboration",
    "AI Engine",
    "Data & AI Governance",
    "Cloud Platform",
]

#: Maps a WAF question-id prefix to the Assessment Tool's pillar slug. The prefix is
#: the canonical pillar marker carried in every question id (e.g. ``DG-01-02``), so it
#: survives regardless of how a pillar is labelled on a :class:`Result`. Insertion
#: order is the order pillars appear in the exported JSON.
WAF_PILLAR_SLUG_BY_PREFIX = {
    "DG": "data-ai-governance",
    "IU": "interoperability-usability",
    "OE": "operational-excellence",
    "SCP": "security-compliance-privacy",
    "R": "reliability",
    "PE": "performance-efficiency",
    "CO": "cost-optimization",
}

#: The Assessment Tool recognizes only three states. ``manual-review`` is emitted as
#: ``open`` ("not yet answered") so a human resolves it inside the tool.
_WAF_JSON_STATUS = {
    STATUS_OPEN: "open",
    STATUS_IN_PROGRESS: "in-progress",
    STATUS_COMPLETED: "completed",
    STATUS_MANUAL: "open",
}


def _pillar_slug(question_id: str) -> str | None:
    """Pillar slug for a question id, derived from its prefix (``DG-01-02`` -> ...)."""
    prefix = question_id.split("-", 1)[0]
    return WAF_PILLAR_SLUG_BY_PREFIX.get(prefix)


def _iso_z(dt: datetime) -> str:
    """Format a datetime as the tool's ``2026-07-23T14:48:46.678Z`` (UTC, millis)."""
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def render_waf_json(
    rows: Sequence[dict | Result],
    *,
    name: str = "",
    cloud_provider: str = "aws",
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    assessment_id: str = "",
) -> str:
    """Serialize results into the WAF Assessment Tool's import/export JSON.

    The schema mirrors a saved assessment from
    databricks-solutions.github.io/waf-assessment-tool, so results load straight back
    into the tool: ``data.pillars`` maps each pillar slug to a ``{question_id: status}``
    object. Only questions present in ``rows`` are emitted; ``manual-review`` is written
    as ``open`` because the tool has no such state. Pillars and questions are ordered
    deterministically (pillar order follows :data:`WAF_PILLAR_SLUG_BY_PREFIX`).
    """
    rows = [_as_dict(r) for r in rows]
    now = datetime.now(timezone.utc)
    created = created_at or now
    updated = updated_at or now

    by_slug: dict[str, dict[str, str]] = {}
    for r in rows:
        qid = r["question_id"]
        slug = _pillar_slug(qid)
        if not slug:
            continue
        by_slug.setdefault(slug, {})[qid] = _WAF_JSON_STATUS.get(r["status"], "open")

    # Deterministic order: pillars by the canonical prefix order, questions by id.
    pillars = {
        slug: {qid: by_slug[slug][qid] for qid in sorted(by_slug[slug])}
        for slug in WAF_PILLAR_SLUG_BY_PREFIX.values()
        if slug in by_slug
    }

    if not assessment_id:
        assessment_id = f"assessment-{int(now.timestamp() * 1000)}-{uuid.uuid4().hex[:9]}"

    payload = {
        "id": assessment_id,
        "name": name,
        "description": "",
        "categories": list(WAF_CATEGORIES),
        "cloudProvider": cloud_provider,
        "createdAt": _iso_z(created),
        "updatedAt": _iso_z(updated),
        "data": {"pillars": pillars, "metadata": {}},
        "modified": True,
    }
    return json.dumps(payload, indent=2)


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
