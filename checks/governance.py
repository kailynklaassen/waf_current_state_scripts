"""Data & AI Governance pillar checks (DG-*).

Evidence sources: ``system.information_schema.*``, ``system.access.*``,
``system.data_quality_monitoring.*``, and the Workspace SDK for UC-registered models.
"""

from __future__ import annotations

from waf_core import (
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    STATUS_OPEN,
    Ctx,
    Result,
    fmt_pct,
    manual_result,
    presence_result,
    ratio_result,
    ratio_status,
)


def _admin_identities(ctx: Ctx) -> set[str] | None:
    """Lowercased emails of workspace ``admins`` group members, or ``None`` if the SDK
    cannot resolve them. Used to tell admin lineage usage from any user's."""
    w = ctx.w
    if w is None:
        return None
    try:
        admins: set[str] = set()
        for g in w.groups.list(filter="displayName eq 'admins'"):
            for m in (g.members or []):
                disp = (getattr(m, "display", None) or "").strip().lower()
                if disp:
                    admins.add(disp)
        return admins or None
    except Exception:
        return None

PILLAR_ID = "data-ai-governance"


# ----------------------------------------------------------------------------------
# Unify data and AI management
# ----------------------------------------------------------------------------------


def dg_01_01(ctx: Ctx) -> Result:
    """Establish data governance process."""
    return manual_result(
        "DG-01-01",
        "a governance operating model is an organizational process, not a platform "
        "setting, so no telemetry can confirm it exists.",
        "the data governance charter, named data owners/stewards, and the policy "
        "review cadence.",
    )


def dg_01_02(ctx: Ctx) -> Result:
    """Manage metadata for all data assets in one place.

    Measures whether Unity Catalog metadata is actually being maintained: do
    production tables carry table-level comments and an owner? Weighted toward
    production because that is where governance matters most.
    """
    scope = ctx.scope
    row = ctx.one(
        f"""
        SELECT
          count(*) AS total,
          count_if(comment IS NOT NULL AND trim(comment) <> '') AS documented,
          count_if(table_owner IS NOT NULL AND trim(table_owner) <> '') AS owned
        FROM system.information_schema.tables
        WHERE {scope.predicate()}
          AND table_type <> 'VIEW'
        """
    )
    total = int(row.get("total") or 0)
    documented = int(row.get("documented") or 0)
    owned = int(row.get("owned") or 0)

    res = ratio_result(
        "DG-01-02",
        documented,
        total,
        f"{scope.label} tables are registered in Unity Catalog with a table-level "
        "description maintained",
        complete_at=ctx.complete_at,
        none_found=(
            "No tables found in the assessed catalogs, so there is no evidence that "
            "metadata is centrally managed in Unity Catalog."
        ),
        extra=(
            f"Ownership is set on {fmt_pct(owned / total) if total else 'n/a'} of them."
            if total
            else ""
        ),
        metrics={"owned": owned, "scoped": True},
    )
    return res


def dg_01_03(ctx: Ctx) -> Result:
    """Track data and AI lineage to drive visibility of the data.

    Scored as two equally-weighted halves, so a workspace that captures lineage but
    never consults it only reaches 50%:

    * **Tracking (50%)** - lineage is captured automatically for every Unity Catalog
      asset, so "is lineage tracked?" is the structural question "is the table in UC?".
      Component = UC tables / (UC + hive) tables. Tables stranded in hive_metastore get
      no automatic lineage.
    * **Usage (50%)** - is lineage actually consulted? Component = the share of weeks in
      the lookback window in which an admin or a service principal (e.g. an SP running a
      governance job) queried the lineage system tables - i.e. "at least once a week"
      prorated. Browsing the Catalog Explorer graph is not visible here; this measures
      lineage queried from SQL / jobs.

    Blended score = 0.5 * tracking + 0.5 * usage, then the standard thresholds.
    """
    scope = ctx.scope
    uc_tables = ctx.count(
        f"""
        SELECT count(*) AS n FROM system.information_schema.tables
        WHERE {scope.predicate()} AND table_type <> 'VIEW'
        """
    )
    hive_tables = ctx.count_safe(
        """
        SELECT count(*) AS n FROM system.information_schema.tables
        WHERE lower(table_catalog) = 'hive_metastore' AND table_type <> 'VIEW'
        """
    )
    total = uc_tables + hive_tables
    if total == 0:
        return Result(
            "DG-01-03", "", "", "", STATUS_OPEN,
            "No tables found in Unity Catalog or hive_metastore, so lineage coverage "
            "cannot be assessed.",
            {"scoped": True},
        )
    tracking = uc_tables / total

    # -- Usage half: weekly cadence of admin / SP queries against the lineage tables. --
    total_weeks = max(1, -(-ctx.lookback_days // 7))  # ceil(days / 7)
    admins = _admin_identities(ctx)
    admin_known = admins is not None
    # A query qualifies when the principal is a service principal (jobs show a non-email
    # identity) or a known workspace admin. When admins can't be resolved we fall back to
    # any principal and say so, rather than silently dropping the usage half.
    if admin_known and admins:
        quoted = ", ".join("'" + a.replace("'", "''") + "'" for a in sorted(admins))
        qualifier = f"(executed_by NOT LIKE '%@%' OR lower(executed_by) IN ({quoted}))"
    elif admin_known:  # resolved, but the admins group is empty -> only SPs qualify
        qualifier = "executed_by NOT LIKE '%@%'"
    else:  # SDK unavailable -> count any principal, caveat in the rationale
        qualifier = "true"

    active_weeks = queries = principals = sp_queries = 0
    history_available = ctx.has_table("system.query.history")
    if history_available:
        row = ctx.one(
            f"""
            SELECT count(DISTINCT date_trunc('WEEK', start_time)) AS active_weeks,
                   count(*) AS queries,
                   count(DISTINCT executed_by) AS principals,
                   count_if(executed_by NOT LIKE '%@%') AS sp_queries
            FROM system.query.history
            WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND lower(statement_text) LIKE '%system.access.%lineage%'
              AND {qualifier}
            """
        )
        active_weeks = int(row.get("active_weeks") or 0)
        queries = int(row.get("queries") or 0)
        principals = int(row.get("principals") or 0)
        sp_queries = int(row.get("sp_queries") or 0)
    usage = min(1.0, active_weeks / total_weeks)

    score = 0.5 * tracking + 0.5 * usage
    status, _ = ratio_status(round(score * 10_000), 10_000, ctx.complete_at)

    verdict = (
        f"at or above the {fmt_pct(ctx.complete_at)} threshold"
        if status == STATUS_COMPLETED
        else f"below the {fmt_pct(ctx.complete_at)} threshold"
    )
    who = "an admin or service principal" if admin_known else "any principal"
    reason = (
        f"{fmt_pct(score)} lineage maturity - {verdict}. Tracking (50%): "
        f"{fmt_pct(tracking)} of {scope.label} tables are UC-managed and so get automatic "
        f"lineage ({uc_tables:,} of {total:,}). Usage (50%): {fmt_pct(usage)} - the "
        f"lineage system tables were queried by {who} in {active_weeks} of {total_weeks} "
        f"week(s) in the window ({queries:,} query/queries, {sp_queries:,} from service "
        "principals)."
    )
    if hive_tables:
        reason += (
            f" {hive_tables:,} table(s) remain in hive_metastore with no automatic "
            "lineage; migrate them to UC to capture provenance."
        )
    if not history_available:
        reason += (
            " system.query.history is not readable, so the usage half could not be "
            "measured and is counted as zero."
        )
    elif not admin_known:
        reason += (
            " The admins group could not be resolved from the SDK, so usage counts "
            "queries from any principal (not just admins/SPs)."
        )
    if active_weeks == 0 and history_available:
        reason += (
            " To earn the usage half, consult lineage from SQL or a job (impact "
            "analysis, orphaned-table / PII-propagation reports on "
            "system.access.table_lineage), not only the Catalog Explorer graph."
        )

    return Result(
        "DG-01-03", "", "", "", status, reason,
        {
            "uc_tables": uc_tables,
            "hive_metastore_tables": hive_tables,
            "tracking_pct": round(tracking * 100, 2),
            "usage_pct": round(usage * 100, 2),
            "blended_pct": round(score * 100, 2),
            "lineage_active_weeks": active_weeks,
            "lineage_total_weeks": total_weeks,
            "lineage_queries": queries,
            "lineage_sp_queries": sp_queries,
            "lineage_query_principals": principals,
            "admins_resolved": admin_known,
            "scoped": True,
        },
    )


def dg_01_04(ctx: Ctx) -> Result:
    """Add consistent descriptions to your metadata (column-level comments)."""
    scope = ctx.scope
    row = ctx.one(
        f"""
        SELECT count(*) AS total,
               count_if(comment IS NOT NULL AND trim(comment) <> '') AS documented
        FROM system.information_schema.columns
        WHERE {scope.predicate()}
        """
    )
    total = int(row.get("total") or 0)
    documented = int(row.get("documented") or 0)
    return ratio_result(
        "DG-01-04",
        documented,
        total,
        f"columns in {scope.label} carry a description",
        complete_at=ctx.complete_at,
        none_found="No columns found in the assessed catalogs.",
        extra="AI-generated comments can bulk-populate these in Catalog Explorer.",
        metrics={"scoped": True},
    )


def dg_01_05(ctx: Ctx) -> Result:
    """Allow easy data discovery for data consumers.

    Discovery relies on more than comments: tags and certification make assets
    findable. Scored on tag coverage across production tables.
    """
    scope = ctx.scope
    total = ctx.count(
        f"""
        SELECT count(*) AS n FROM system.information_schema.tables
        WHERE {scope.predicate()} AND table_type <> 'VIEW'
        """
    )
    tagged = 0
    tag_names = 0
    if ctx.has_table("system.information_schema.table_tags"):
        tagged = ctx.count(
            f"""
            SELECT count(DISTINCT concat_ws('.', catalog_name, schema_name, table_name)) AS n
            FROM system.information_schema.table_tags
            WHERE {scope.predicate('catalog_name', 'schema_name')}
            """
        )
        tag_names = ctx.count(
            f"""
            SELECT count(DISTINCT tag_name) AS n
            FROM system.information_schema.table_tags
            WHERE {scope.predicate('catalog_name', 'schema_name')}
            """
        )
    tagged = min(tagged, total) if total else tagged
    return ratio_result(
        "DG-01-05",
        tagged,
        total,
        f"{scope.label} tables carry at least one governed tag to aid discovery",
        complete_at=ctx.complete_at,
        none_found=(
            "No tables in scope, so data discoverability cannot be assessed."
        ),
        extra=(
            f"{tag_names} distinct tag key(s) in use."
            if tag_names
            else "No tags are applied; consumers must rely on names alone."
        ),
        metrics={"distinct_tag_names": tag_names, "scoped": True},
    )


def dg_01_06(ctx: Ctx) -> Result:
    """Govern AI assets together with data (models and functions in Unity Catalog)."""
    scope = ctx.scope
    uc_models = None
    w = ctx.w
    if w is not None:
        try:
            uc_models = sum(1 for _ in w.registered_models.list())
        except Exception:
            uc_models = None

    uc_functions = ctx.count(
        f"""
        SELECT count(*) AS n FROM system.information_schema.routines
        WHERE {scope.predicate('routine_catalog', 'routine_schema')}
        """
    )
    ws_experiments = 0
    if ctx.has_table("system.mlflow.experiments_latest"):
        ws_experiments = ctx.count(
            "SELECT count(*) AS n FROM system.mlflow.experiments_latest "
            "WHERE delete_time IS NULL"
        )

    if uc_models is None and uc_functions == 0 and ws_experiments == 0:
        return Result(
            "DG-01-06", "", "", "", STATUS_OPEN,
            "No UC-registered models, UC functions, or MLflow experiments found, so "
            "there is no evidence that AI assets are governed alongside data.",
            {"scoped": True},
        )

    models_n = uc_models or 0
    ai_assets = models_n + uc_functions
    if ai_assets == 0:
        return Result(
            "DG-01-06", "", "", "", STATUS_OPEN,
            f"{ws_experiments} MLflow experiment(s) exist but no models or functions are "
            "registered in Unity Catalog, so AI assets are governed outside UC.",
            {"experiments": ws_experiments, "scoped": True},
        )

    status = STATUS_COMPLETED if models_n > 0 else STATUS_OPEN
    reason = (
        f"{models_n} model(s) registered in Unity Catalog and {uc_functions} UC "
        f"function(s) governed alongside data ({ws_experiments} MLflow experiment(s) "
        "tracked)."
    )
    if models_n == 0:
        reason = (
            f"No adoption of UC model governance was observed (0 UC-registered models); "
            f"{uc_functions} UC function(s) and {ws_experiments} MLflow experiment(s) exist, "
            "but models should be registered in Unity Catalog to govern AI assets alongside data."
        )
    return Result(
        "DG-01-06", "", "", "", status, reason,
        {"uc_models": models_n, "uc_functions": uc_functions,
         "experiments": ws_experiments, "scoped": True},
    )


# ----------------------------------------------------------------------------------
# Unify data and AI security
# ----------------------------------------------------------------------------------


def dg_02_01(ctx: Ctx) -> Result:
    """Centralize access control for all data and AI assets.

    Two signals: grants are made to groups rather than individual users, and
    legacy Hive metastore tables are not still in play.
    """
    scope = ctx.scope
    row = ctx.one(
        f"""
        SELECT count(*) AS total,
               count_if(grantee ILIKE '%@%') AS user_grants
        FROM system.information_schema.table_privileges
        WHERE {scope.predicate()}
        """
    )
    total = int(row.get("total") or 0)
    user_grants = int(row.get("user_grants") or 0)
    group_grants = total - user_grants

    hive_refs = 0
    if ctx.has_table("system.access.table_lineage"):
        hive_refs = ctx.count(
            f"""
            SELECT count(*) AS n FROM {ctx.lineage_table()}
            WHERE event_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
              AND (lower(source_table_catalog) = 'hive_metastore'
                   OR lower(target_table_catalog) = 'hive_metastore')
            """
        )

    if total == 0:
        return Result(
            "DG-02-01", "", "", "", STATUS_OPEN,
            "No table privileges found in the assessed catalogs, so centralized "
            "Unity Catalog access control is not in evidence.",
            {"scoped": True},
        )

    extra = (
        f"{hive_refs:,} lineage event(s) still reference hive_metastore in the last "
        f"{ctx.lookback_days} days, indicating access control is not fully centralized "
        "in Unity Catalog."
        if hive_refs
        else "No hive_metastore activity observed, so access control is centralized in UC."
    )
    res = ratio_result(
        "DG-02-01",
        group_grants,
        total,
        "table grants in scope are made to groups or service principals rather than "
        "individual users",
        complete_at=ctx.complete_at,
        extra=extra,
        metrics={"user_grants": user_grants, "hive_lineage_events": hive_refs,
                 "scoped": True},
    )
    # Live Hive usage caps this at in-progress: control is demonstrably split.
    if hive_refs and res.status == STATUS_COMPLETED:
        res.status = STATUS_IN_PROGRESS
        res.reason = (
            res.reason
            + " Downgraded to in-progress because governance spans two metastores."
        )
    return res


def dg_02_02(ctx: Ctx) -> Result:
    """Configure audit logging."""
    if not ctx.has_table("system.access.audit"):
        return Result(
            "DG-02-02", "", "", "", STATUS_OPEN,
            "system.access.audit is not readable, so the audit log system schema is "
            "not enabled (or access has not been granted to the assessor).",
        )
    row = ctx.one(
        f"""
        SELECT count(*) AS events,
               count(DISTINCT event_date) AS days,
               max(event_date) AS last_day
        FROM system.access.audit
        WHERE event_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
        """
    )
    events = int(row.get("events") or 0)
    days = int(row.get("days") or 0)
    if events == 0:
        return Result(
            "DG-02-02", "", "", "", STATUS_OPEN,
            f"The audit system table is enabled but recorded no events in the last "
            f"{ctx.lookback_days} days.",
            {"lookback_days": ctx.lookback_days},
        )
    return ratio_result(
        "DG-02-02",
        days,
        ctx.lookback_days,
        f"days in the last {ctx.lookback_days} have audit coverage",
        complete_at=ctx.complete_at,
        extra=f"{events:,} audit events captured; most recent {row.get('last_day')}.",
        metrics={"events": events},
    )


def dg_02_03(ctx: Ctx) -> Result:
    """Audit data platform events (are the audit logs actually being used?)."""
    if not ctx.has_table("system.access.audit"):
        return Result(
            "DG-02-03", "", "", "", STATUS_OPEN,
            "system.access.audit is not readable, so platform events are not being "
            "audited.",
        )
    consumers = 0
    queries = 0
    if ctx.has_table("system.query.history"):
        row = ctx.one(
            f"""
            SELECT count(*) AS queries, count(DISTINCT executed_by) AS consumers
            FROM system.query.history
            WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND lower(statement_text) LIKE '%system.access.audit%'
            """
        )
        queries = int(row.get("queries") or 0)
        consumers = int(row.get("consumers") or 0)

    unique_actions = ctx.count(
        f"""
        SELECT count(DISTINCT action_name) AS n FROM system.access.audit
        WHERE event_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
        """
    )
    if queries == 0:
        return Result(
            "DG-02-03", "", "", "", STATUS_OPEN,
            f"No adoption of audit log review was observed (0 queries against system.access.audit in "
            f"the last {ctx.lookback_days} days). Audit data is available ({unique_actions} distinct "
            "action types collected) but no evidence that the logs are actively reviewed.",
            {"unique_actions": unique_actions, "audit_queries": 0},
        )
    return Result(
        "DG-02-03", "", "", "", STATUS_COMPLETED,
        f"Audit logs are actively reviewed: {queries:,} query/queries against "
        f"system.access.audit by {consumers} distinct principal(s) in the last "
        f"{ctx.lookback_days} days, across {unique_actions} audited action types.",
        {"unique_actions": unique_actions, "audit_queries": queries,
         "audit_consumers": consumers},
    )


# ----------------------------------------------------------------------------------
# Establish data quality standards
# ----------------------------------------------------------------------------------


def dg_03_01(ctx: Ctx) -> Result:
    """Define and document data quality standards."""
    return manual_result(
        "DG-03-01",
        "written data quality standards and their acceptance thresholds live in "
        "documentation, not in platform metadata.",
        "the data quality standard/SLA document, owning team, and how exceptions are "
        "escalated.",
    )


def dg_03_02(ctx: Ctx) -> Result:
    """Use data quality tools for profiling, cleansing, validating, and monitoring."""
    scope = ctx.scope
    monitored = 0
    if ctx.has_table("system.data_quality_monitoring.table_results"):
        monitored = ctx.count(
            f"""
            SELECT count(DISTINCT concat_ws('.', catalog_name, schema_name, table_name)) AS n
            FROM system.data_quality_monitoring.table_results
            WHERE {scope.predicate('catalog_name', 'schema_name')}
            """
        )
    # Pipeline expectations are declared in pipeline source, which is not exposed in
    # system tables. Declarative pipelines are the vehicle for expectations, so count
    # them as a capability signal rather than claiming expectations were found.
    pipelines = 0
    if ctx.has_table("system.lakeflow.pipelines"):
        pipelines = ctx.count_safe(
            "SELECT count(*) AS n FROM system.lakeflow.pipelines WHERE delete_time IS NULL"
        )
    constrained = ctx.count(
        f"""
        SELECT count(DISTINCT concat_ws('.', table_catalog, table_schema, table_name)) AS n
        FROM system.information_schema.table_constraints
        WHERE {scope.predicate()}
        """
    )
    total = ctx.count(
        f"""
        SELECT count(*) AS n FROM system.information_schema.tables
        WHERE {scope.predicate()} AND table_type <> 'VIEW'
        """
    )
    covered = min(monitored + constrained, total) if total else 0
    signals = []
    if monitored:
        signals.append(f"{monitored} table(s) under Lakehouse Monitoring")
    if constrained:
        signals.append(f"{constrained} table(s) with declared constraints")
    if pipelines:
        signals.append(
            f"{pipelines} declarative pipeline(s) that can carry expectations "
            "(expectation definitions live in pipeline source, not system tables)"
        )

    if not signals:
        return Result(
            "DG-03-02", "", "", "", STATUS_OPEN,
            "No Lakehouse Monitoring results, table constraints, or declarative "
            "pipelines found, so no automated data quality tooling is in use.",
            {"scoped": True},
        )
    return ratio_result(
        "DG-03-02",
        covered,
        total,
        f"{scope.label} tables are covered by a data quality control (monitor or constraint)",
        complete_at=ctx.complete_at,
        extra="Signals found: " + "; ".join(signals) + ".",
        metrics={"monitored": monitored, "constrained": constrained,
                 "declarative_pipelines": pipelines, "scoped": True},
    )


def dg_03_03(ctx: Ctx) -> Result:
    """Implement and enforce standardized data formats and definitions."""
    scope = ctx.scope
    row = ctx.one(
        f"""
        SELECT count(*) AS total,
               count_if(lower(coalesce(data_source_format, '')) IN ('delta', 'deltasharing')) AS delta
        FROM system.information_schema.tables
        WHERE {scope.predicate()} AND table_type <> 'VIEW'
        """
    )
    total = int(row.get("total") or 0)
    delta = int(row.get("delta") or 0)
    fmt_rows = ctx.sql(
        f"""
        SELECT coalesce(data_source_format, 'UNKNOWN') AS fmt, count(*) AS n
        FROM system.information_schema.tables
        WHERE {scope.predicate()} AND table_type <> 'VIEW'
        GROUP BY 1 ORDER BY 2 DESC LIMIT 5
        """
    )
    mix = ", ".join(f"{r['fmt']}={r['n']}" for r in fmt_rows)
    return ratio_result(
        "DG-03-03",
        delta,
        total,
        f"{scope.label} tables use the standardized Delta format",
        complete_at=ctx.complete_at,
        none_found="No tables found in the assessed catalogs.",
        extra=f"Format mix: {mix}." if mix else "",
        metrics={"format_mix": {r["fmt"]: r["n"] for r in fmt_rows}, "scoped": True},
    )


CHECKS = [
    ("DG-01-01", dg_01_01),
    ("DG-01-02", dg_01_02),
    ("DG-01-03", dg_01_03),
    ("DG-01-04", dg_01_04),
    ("DG-01-05", dg_01_05),
    ("DG-01-06", dg_01_06),
    ("DG-02-01", dg_02_01),
    ("DG-02-02", dg_02_02),
    ("DG-02-03", dg_02_03),
    ("DG-03-01", dg_03_01),
    ("DG-03-02", dg_03_02),
    ("DG-03-03", dg_03_03),
]
