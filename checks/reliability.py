"""Reliability pillar checks (R-*).

Evidence sources: ``system.information_schema.*``, ``system.lakeflow.*``,
``system.compute.*``, ``system.query.history``, ``system.serving.*``.
"""

from __future__ import annotations

from waf_core import (
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    STATUS_OPEN,
    Ctx,
    Result,
    manual_result,
    ratio_result,
)

PILLAR_ID = "reliability"


# ----------------------------------------------------------------------------------
# Design for failure
# ----------------------------------------------------------------------------------


def r_01_01(ctx: Ctx) -> Result:
    """Use a data format that supports ACID transactions."""
    scope = ctx.scope
    row = ctx.one(
        f"""
        SELECT count(*) AS total,
               count_if(lower(coalesce(data_source_format, '')) IN ('delta', 'deltasharing')) AS acid
        FROM system.information_schema.tables
        WHERE {scope.predicate()} AND table_type <> 'VIEW'
        """
    )
    total = int(row.get("total") or 0)
    acid = int(row.get("acid") or 0)
    non_acid = ctx.sql(
        f"""
        SELECT coalesce(data_source_format, 'UNKNOWN') AS fmt, count(*) AS n
        FROM system.information_schema.tables
        WHERE {scope.predicate()} AND table_type <> 'VIEW'
          AND lower(coalesce(data_source_format, '')) NOT IN ('delta', 'deltasharing')
        GROUP BY 1 ORDER BY 2 DESC LIMIT 5
        """
    )
    extra = (
        "Non-ACID formats present: "
        + ", ".join(f"{r['fmt']}={r['n']}" for r in non_acid)
        + "."
        if non_acid
        else "No non-ACID table formats found."
    )
    return ratio_result(
        "R-01-01",
        acid,
        total,
        f"{scope.label} tables use an ACID-capable format (Delta)",
        complete_at=ctx.complete_at,
        none_found="No tables found in the assessed catalogs.",
        extra=extra,
        metrics={"scoped": True},
    )


def r_01_02(ctx: Ctx) -> Result:
    """Use a resilient distributed data engine for all workloads.

    Photon and current DBRs are the observable proxy: workloads running on
    supported, vectorized engines rather than ad-hoc or legacy runtimes.
    """
    if not ctx.has_table("system.compute.clusters"):
        return Result(
            "R-01-02", "", "", "", STATUS_OPEN,
            "system.compute.clusters is not readable, so the compute engine mix "
            "cannot be assessed.",
        )
    row = ctx.one(
        f"""
        WITH latest AS (
          SELECT cluster_id, dbr_version,
                 row_number() OVER (PARTITION BY cluster_id ORDER BY change_time DESC) AS rn
          FROM system.compute.clusters
          WHERE delete_time IS NULL
            AND change_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
        )
        SELECT count(*) AS total,
               count_if(dbr_version IS NOT NULL AND dbr_version <> '') AS known
        FROM latest WHERE rn = 1
        """
    )
    total = int(row.get("total") or 0)
    # Serverless workloads carry no cluster record; count them as resilient managed compute.
    serverless_jobs = 0
    if ctx.has_table("system.billing.usage"):
        serverless_jobs = ctx.count(
            f"""
            SELECT count(DISTINCT usage_metadata.job_id) AS n
            FROM system.billing.usage
            WHERE usage_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
              AND lower(coalesce(sku_name, '')) LIKE '%serverless%'
              AND usage_metadata.job_id IS NOT NULL
            """
        )
    if total == 0 and serverless_jobs == 0:
        return Result(
            "R-01-02", "", "", "", STATUS_OPEN,
            f"No clusters or serverless job usage observed in the last "
            f"{ctx.lookback_days} days, so no evidence of a managed distributed engine.",
            {"lookback_days": ctx.lookback_days},
        )
    return Result(
        "R-01-02", "", "", "", STATUS_COMPLETED,
        f"Workloads run on managed Spark: {total} active cluster(s) and "
        f"{serverless_jobs} job(s) on serverless compute in the last "
        f"{ctx.lookback_days} days. Databricks Runtime provides distributed "
        "fault tolerance (task retries, speculative execution) by default.",
        {"clusters": total, "serverless_jobs": serverless_jobs},
    )


def r_01_03(ctx: Ctx) -> Result:
    """Automatically rescue invalid or nonconforming data."""
    scope = ctx.scope
    rescued_cols = ctx.count(
        f"""
        SELECT count(DISTINCT concat_ws('.', table_catalog, table_schema, table_name)) AS n
        FROM system.information_schema.columns
        WHERE {scope.predicate()}
          AND lower(column_name) IN ('_rescued_data', 'rescued_data')
        """
    )
    quarantine = ctx.count(
        f"""
        SELECT count(*) AS n FROM system.information_schema.tables
        WHERE {scope.predicate()}
          AND (lower(table_name) LIKE '%quarantine%'
               OR lower(table_name) LIKE '%reject%'
               OR lower(table_name) LIKE '%_bad%'
               OR lower(table_name) LIKE '%invalid%'
               OR lower(table_name) LIKE '%_dlq%')
        """
    )
    total = ctx.count(
        f"""
        SELECT count(*) AS n FROM system.information_schema.tables
        WHERE {scope.predicate()} AND table_type <> 'VIEW'
        """
    )
    found = rescued_cols + quarantine
    if found == 0:
        return Result(
            "R-01-03", "", "", "", STATUS_OPEN,
            f"No rescued-data columns (_rescued_data) and no quarantine/reject tables "
            f"found across {total:,} table(s) in {scope.label}, so malformed records are "
            "likely dropped or failing pipelines rather than being captured.",
            {"scoped": True, "tables_scanned": total},
        )
    return Result(
        "R-01-03", "", "", "", STATUS_IN_PROGRESS if found < 3 else STATUS_COMPLETED,
        f"Bad-record handling is in place on {found} asset(s) in {scope.label}: "
        f"{rescued_cols} table(s) with a rescued-data column and {quarantine} "
        f"quarantine/reject table(s), out of {total:,} table(s).",
        {"rescued_data_tables": rescued_cols, "quarantine_tables": quarantine,
         "scoped": True},
    )


def r_01_04(ctx: Ctx) -> Result:
    """Configure jobs for automatic retries and termination."""
    if not ctx.has_table("system.lakeflow.jobs"):
        return Result(
            "R-01-04", "", "", "", STATUS_OPEN,
            "system.lakeflow.jobs is not readable, so job resiliency settings cannot "
            "be assessed.",
        )
    total = ctx.count(
        """
        SELECT count(DISTINCT job_id) AS n FROM system.lakeflow.jobs
        WHERE delete_time IS NULL
        """
    )
    if total == 0:
        return Result(
            "R-01-04", "", "", "", STATUS_OPEN,
            "No active jobs found, so retry and timeout configuration cannot be verified.",
        )
    with_timeout = ctx.count(
        """
        WITH latest AS (
          SELECT job_id, timeout_seconds,
                 row_number() OVER (PARTITION BY job_id ORDER BY change_time DESC) AS rn
          FROM system.lakeflow.jobs WHERE delete_time IS NULL
        )
        SELECT count(*) AS n FROM latest
        WHERE rn = 1 AND timeout_seconds IS NOT NULL AND timeout_seconds > 0
        """
    )
    # Retries are observable from run history: a repaired/retried run reuses job_run_id.
    retried = 0
    if ctx.has_table("system.lakeflow.job_run_timeline"):
        retried = ctx.count(
            f"""
            SELECT count(DISTINCT job_id) AS n FROM system.lakeflow.job_run_timeline
            WHERE period_start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND lower(coalesce(run_type, '')) LIKE '%retry%'
            """
        )
    res = ratio_result(
        "R-01-04",
        with_timeout,
        total,
        "active jobs declare a timeout so a hung run cannot run indefinitely",
        complete_at=ctx.complete_at,
        extra=(
            f"{retried} job(s) show retry activity in the last {ctx.lookback_days} days."
            if retried
            else "No retry activity observed in the lookback window; confirm max_retries "
            "is set on tasks."
        ),
        metrics={"jobs_with_timeout": with_timeout, "jobs_with_retries": retried},
    )
    return res


def r_01_05(ctx: Ctx) -> Result:
    """Use a scalable and production-grade model serving infrastructure."""
    if not ctx.has_table("system.serving.served_entities"):
        return Result(
            "R-01-05", "", "", "", STATUS_OPEN,
            "system.serving.served_entities is not readable, so model serving cannot "
            "be assessed.",
        )
    row = ctx.one(
        """
        SELECT count(DISTINCT endpoint_id) AS endpoints,
               count(DISTINCT served_entity_id) AS entities
        FROM system.serving.served_entities
        WHERE endpoint_delete_time IS NULL
        """
    )
    endpoints = int(row.get("endpoints") or 0)
    if endpoints == 0:
        return Result(
            "R-01-05", "", "", "", STATUS_OPEN,
            "No active Model Serving endpoints found. If models are served, they run "
            "outside Databricks managed serving and resiliency is unverified.",
            {"endpoints": 0},
        )
    served = 0
    if ctx.has_table("system.serving.endpoint_usage"):
        served = ctx.count_safe(
            f"""
            SELECT count(*) AS n FROM system.serving.endpoint_usage
            WHERE request_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
            """
        )
    return Result(
        "R-01-05", "", "", "", STATUS_COMPLETED,
        f"{endpoints} managed Model Serving endpoint(s) active with "
        f"{row.get('entities')} served entity/entities; managed serving provides "
        f"autoscaling and health management. {served:,} request record(s) logged in the "
        f"last {ctx.lookback_days} days.",
        {"endpoints": endpoints, "requests": served},
    )


def r_01_06(ctx: Ctx) -> Result:
    """Use managed services for your workloads (serverless / managed ingestion)."""
    if not ctx.has_table("system.billing.usage"):
        return Result(
            "R-01-06", "", "", "", STATUS_OPEN,
            "system.billing.usage is not readable, so managed-service adoption cannot "
            "be measured.",
        )
    row = ctx.one(
        f"""
        SELECT
          sum(usage_quantity) AS total,
          sum(CASE WHEN lower(coalesce(sku_name, '')) LIKE '%serverless%'
                   THEN usage_quantity ELSE 0 END) AS serverless
        FROM system.billing.usage
        WHERE usage_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
        """
    )
    total = float(row.get("total") or 0)
    serverless = float(row.get("serverless") or 0)
    if total <= 0:
        return Result(
            "R-01-06", "", "", "", STATUS_OPEN,
            f"No billable usage recorded in the last {ctx.lookback_days} days.",
        )
    return ratio_result(
        "R-01-06",
        int(round(serverless)),
        int(round(total)),
        f"DBU consumption in the last {ctx.lookback_days} days is on serverless / "
        "managed compute",
        complete_at=ctx.complete_at,
        extra="Managed compute removes cluster tuning and patching as a failure source.",
        metrics={"serverless_dbus": round(serverless, 2), "total_dbus": round(total, 2)},
    )


# ----------------------------------------------------------------------------------
# Manage data quality
# ----------------------------------------------------------------------------------


def r_02_01(ctx: Ctx) -> Result:
    """Use a layered storage architecture (medallion)."""
    scope = ctx.scope
    row = ctx.one(
        f"""
        SELECT
          count(DISTINCT CASE WHEN lower(table_schema) RLIKE '(bronze|raw|landing|staging|stg)'
                              OR lower(table_name) RLIKE '^(bronze|raw)_' THEN table_schema END) AS bronze,
          count(DISTINCT CASE WHEN lower(table_schema) RLIKE '(silver|cleansed|conformed|refined|curated)'
                              OR lower(table_name) RLIKE '^(silver)_' THEN table_schema END) AS silver,
          count(DISTINCT CASE WHEN lower(table_schema) RLIKE '(gold|mart|presentation|serving|semantic|reporting)'
                              OR lower(table_name) RLIKE '^(gold)_' THEN table_schema END) AS gold,
          count(DISTINCT table_schema) AS schemas
        FROM system.information_schema.tables
        WHERE {scope.predicate()}
        """
    )
    layers = [k for k in ("bronze", "silver", "gold") if int(row.get(k) or 0) > 0]
    schemas = int(row.get("schemas") or 0)
    detail = ", ".join(f"{k}={row.get(k)}" for k in ("bronze", "silver", "gold"))

    if not layers:
        return Result(
            "R-02-01", "", "", "", STATUS_OPEN,
            f"No medallion-style layering detected by naming convention across "
            f"{schemas} schema(s) in {scope.label}. Layering may exist under a different "
            "naming standard - confirm with the customer.",
            {"schemas": schemas, "scoped": True},
        )
    if len(layers) >= 3:
        status = STATUS_COMPLETED
    else:
        status = STATUS_IN_PROGRESS
    return Result(
        "R-02-01", "", "", "", status,
        f"{len(layers)} of 3 medallion layers detected by naming convention "
        f"({', '.join(layers)}) across {schemas} schema(s) in {scope.label}. "
        f"Schema counts: {detail}.",
        {"layers_found": layers, "schemas": schemas, "scoped": True},
    )


def r_02_02(ctx: Ctx) -> Result:
    """Improve data integrity by reducing data redundancy."""
    scope = ctx.scope
    row = ctx.one(
        f"""
        WITH t AS (
          SELECT lower(table_name) AS tn,
                 concat_ws('.', table_catalog, table_schema, table_name) AS fqn
          FROM system.information_schema.tables
          WHERE {scope.predicate()} AND table_type <> 'VIEW'
        )
        SELECT
          count(*) AS total,
          count_if(tn RLIKE '(_copy|_bak|_backup|_old|_v[0-9]+$|_tmp|_temp|_dup|_new)$') AS suspect
        FROM t
        """
    )
    total = int(row.get("total") or 0)
    suspect = int(row.get("suspect") or 0)
    views = ctx.count(
        f"""
        SELECT count(*) AS n FROM system.information_schema.tables
        WHERE {scope.predicate()} AND table_type = 'VIEW'
        """
    )
    if total == 0:
        return Result(
            "R-02-02", "", "", "", STATUS_OPEN,
            "No tables found in the assessed catalogs, so redundancy cannot be measured.",
            {"scoped": True},
        )
    clean = total - suspect
    return ratio_result(
        "R-02-02",
        clean,
        total,
        f"{scope.label} tables are free of copy/backup/versioned-duplicate naming "
        "patterns",
        complete_at=ctx.complete_at,
        extra=(
            f"{suspect} likely-duplicate table(s) detected; {views} view(s) exist, which "
            "reduce physical copies by serving logical projections."
        ),
        metrics={"suspect_duplicates": suspect, "views": views, "scoped": True},
    )


def r_02_03(ctx: Ctx) -> Result:
    """Actively manage schemas (schema enforcement / evolution)."""
    if not ctx.has_table("system.access.audit"):
        return Result(
            "R-02-03", "", "", "", STATUS_OPEN,
            "system.access.audit is not readable, so schema change management cannot "
            "be observed.",
        )
    row = ctx.one(
        f"""
        SELECT
          count_if(action_name IN ('createTable', 'commandSubmit') ) AS creates,
          count_if(lower(coalesce(request_params.statement, '')) LIKE '%alter table%'
                   OR action_name = 'updateTable') AS alters
        FROM system.access.audit
        WHERE event_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
          AND service_name IN ('unityCatalog', 'notebook', 'databrickssql')
        """
    )
    alters = int(row.get("alters") or 0)
    scope = ctx.scope
    typed = ctx.one(
        f"""
        SELECT count(*) AS total,
               count_if(is_nullable = 'NO') AS not_null
        FROM system.information_schema.columns
        WHERE {scope.predicate()}
        """
    )
    total_cols = int(typed.get("total") or 0)
    not_null = int(typed.get("not_null") or 0)

    if total_cols == 0:
        return Result(
            "R-02-03", "", "", "", STATUS_OPEN,
            "No columns found in the assessed catalogs, so schema management cannot "
            "be assessed.",
            {"scoped": True},
        )
    pct = not_null / total_cols * 100
    status = STATUS_COMPLETED if pct >= 20 else STATUS_IN_PROGRESS
    return Result(
        "R-02-03", "", "", "", status,
        f"Schemas are actively managed: {not_null:,} of {total_cols:,} column(s) "
        f"({pct:.1f}%) declare NOT NULL in {scope.label}, and {alters:,} schema-altering "
        f"operation(s) were audited in the last {ctx.lookback_days} days. Delta enforces "
        "schema on write by default.",
        {"not_null_columns": not_null, "total_columns": total_cols,
         "alter_events": alters, "scoped": True},
    )


def r_02_04(ctx: Ctx) -> Result:
    """Use constraints and data expectations."""
    scope = ctx.scope
    total = ctx.count(
        f"""
        SELECT count(*) AS n FROM system.information_schema.tables
        WHERE {scope.predicate()} AND table_type <> 'VIEW'
        """
    )
    constrained = ctx.count(
        f"""
        SELECT count(DISTINCT concat_ws('.', table_catalog, table_schema, table_name)) AS n
        FROM system.information_schema.table_constraints
        WHERE {scope.predicate()}
        """
    )
    kinds = ctx.sql(
        f"""
        SELECT constraint_type, count(*) AS n
        FROM system.information_schema.table_constraints
        WHERE {scope.predicate()}
        GROUP BY 1 ORDER BY 2 DESC
        """
    )
    expectations = 0
    if ctx.has_table("system.lakeflow.pipelines"):
        expectations = ctx.count(
            """
            SELECT count(*) AS n FROM system.lakeflow.pipelines
            WHERE delete_time IS NULL
              AND lower(coalesce(to_json(settings), '')) LIKE '%expect%'
            """
        )
    mix = ", ".join(f"{r['constraint_type']}={r['n']}" for r in kinds)
    return ratio_result(
        "R-02-04",
        constrained,
        total,
        f"{scope.label} tables declare at least one constraint (PK/FK/CHECK/NOT NULL)",
        complete_at=ctx.complete_at,
        none_found="No tables found in the assessed catalogs.",
        extra=(
            (f"Constraint mix: {mix}. " if mix else "No constraints declared. ")
            + (
                f"{expectations} pipeline(s) declare data expectations."
                if expectations
                else "No pipeline expectations found."
            )
        ),
        metrics={"constraint_mix": {r["constraint_type"]: r["n"] for r in kinds},
                 "pipelines_with_expectations": expectations, "scoped": True},
    )


def r_02_05(ctx: Ctx) -> Result:
    """Take a data-centric approach to machine learning."""
    if not ctx.has_table("system.mlflow.runs_latest"):
        return Result(
            "R-02-05", "", "", "", STATUS_OPEN,
            "system.mlflow.runs_latest is not readable, so ML practice cannot be "
            "assessed.",
        )
    runs = ctx.count(
        f"""
        SELECT count(*) AS n FROM system.mlflow.runs_latest
        WHERE delete_time IS NULL
          AND start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
        """
    )
    if runs == 0:
        return Result(
            "R-02-05", "", "", "", STATUS_OPEN,
            f"No MLflow runs in the last {ctx.lookback_days} days, so there is no ML "
            "activity to assess. Mark ignore in the WAF tool if ML is out of scope.",
            {"runs": 0},
        )
    feature_tables = 0
    if ctx.has_table("system.information_schema.table_tags"):
        feature_tables = ctx.count(
            """
            SELECT count(DISTINCT concat_ws('.', catalog_name, schema_name, table_name)) AS n
            FROM system.information_schema.table_tags
            WHERE lower(tag_name) LIKE '%feature%'
            """
        )
    lineage_backed = 0
    if ctx.has_table("system.access.table_lineage"):
        lineage_backed = ctx.count(
            f"""
            SELECT count(DISTINCT entity_id) AS n FROM system.access.table_lineage
            WHERE event_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
              AND lower(coalesce(entity_type, '')) IN ('notebook', 'job', 'pipeline')
            """
        )
    status = STATUS_COMPLETED if feature_tables > 0 else STATUS_IN_PROGRESS
    return Result(
        "R-02-05", "", "", "", status,
        f"{runs:,} MLflow run(s) tracked in the last {ctx.lookback_days} days with "
        f"{feature_tables} governed feature table(s) and {lineage_backed} lineage-emitting "
        "entity/entities feeding ML. "
        + (
            "Feature data is managed as a governed asset."
            if feature_tables
            else "No feature tables identified, so training data may not be a managed, "
            "versioned asset."
        ),
        {"runs": runs, "feature_tables": feature_tables, "scoped": True},
    )


# ----------------------------------------------------------------------------------
# Design for autoscaling
# ----------------------------------------------------------------------------------


def r_03_01(ctx: Ctx) -> Result:
    """Enable autoscaling for ETL workloads."""
    if not ctx.has_table("system.compute.clusters"):
        return Result(
            "R-03-01", "", "", "", STATUS_OPEN,
            "system.compute.clusters is not readable, so cluster autoscaling cannot "
            "be assessed.",
        )
    row = ctx.one(
        f"""
        WITH latest AS (
          SELECT cluster_id, min_autoscale_workers, max_autoscale_workers, worker_count,
                 row_number() OVER (PARTITION BY cluster_id ORDER BY change_time DESC) AS rn
          FROM system.compute.clusters
          WHERE delete_time IS NULL
            AND change_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
        )
        SELECT count(*) AS total,
               count_if(max_autoscale_workers IS NOT NULL
                        AND max_autoscale_workers > coalesce(min_autoscale_workers, 0)) AS autoscaling
        FROM latest WHERE rn = 1
        """
    )
    total = int(row.get("total") or 0)
    autoscaling = int(row.get("autoscaling") or 0)
    serverless_jobs = 0
    if ctx.has_table("system.billing.usage"):
        serverless_jobs = ctx.count(
            f"""
            SELECT count(DISTINCT usage_metadata.job_id) AS n
            FROM system.billing.usage
            WHERE usage_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
              AND lower(coalesce(sku_name, '')) LIKE '%serverless%'
              AND usage_metadata.job_id IS NOT NULL
            """
        )
    if total == 0:
        if serverless_jobs:
            return Result(
                "R-03-01", "", "", "", STATUS_COMPLETED,
                f"No classic clusters to configure; {serverless_jobs} job(s) run on "
                "serverless compute, which scales automatically.",
                {"serverless_jobs": serverless_jobs},
            )
        return Result(
            "R-03-01", "", "", "", STATUS_OPEN,
            f"No clusters or serverless jobs observed in the last {ctx.lookback_days} "
            "days, so autoscaling cannot be verified.",
        )
    return ratio_result(
        "R-03-01",
        autoscaling,
        total,
        "active clusters have autoscaling enabled",
        complete_at=ctx.complete_at,
        extra=(
            f"{serverless_jobs} job(s) additionally run on serverless compute, which "
            "autoscales by design."
            if serverless_jobs
            else "Fixed-size clusters cannot absorb load spikes."
        ),
        metrics={"serverless_jobs": serverless_jobs},
    )


def r_03_02(ctx: Ctx) -> Result:
    """Use autoscaling for SQL Warehouses."""
    if not ctx.has_table("system.compute.warehouses"):
        return Result(
            "R-03-02", "", "", "", STATUS_OPEN,
            "system.compute.warehouses is not readable, so warehouse scaling cannot "
            "be assessed.",
        )
    row = ctx.one(
        """
        WITH latest AS (
          SELECT warehouse_id, min_clusters, max_clusters,
                 row_number() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) AS rn
          FROM system.compute.warehouses WHERE delete_time IS NULL
        )
        SELECT count(*) AS total,
               count_if(max_clusters > coalesce(min_clusters, 1)) AS scaling
        FROM latest WHERE rn = 1
        """
    )
    total = int(row.get("total") or 0)
    scaling = int(row.get("scaling") or 0)
    if total == 0:
        return Result(
            "R-03-02", "", "", "", STATUS_OPEN,
            "No SQL warehouses found, so warehouse autoscaling cannot be verified.",
        )
    return ratio_result(
        "R-03-02",
        scaling,
        total,
        "SQL warehouses are configured to scale out (max_clusters > min_clusters)",
        complete_at=ctx.complete_at,
        extra="Multi-cluster load balancing protects concurrency under peak query load.",
    )


# ----------------------------------------------------------------------------------
# Test recovery procedures
# ----------------------------------------------------------------------------------


def r_04_01(ctx: Ctx) -> Result:
    """Recover from Structured Streaming query failures (checkpointing)."""
    streaming_pipelines = 0
    if ctx.has_table("system.lakeflow.pipelines"):
        streaming_pipelines = ctx.count(
            """
            SELECT count(*) AS n FROM system.lakeflow.pipelines
            WHERE delete_time IS NULL
              AND lower(coalesce(pipeline_type, '')) NOT LIKE '%materialized%'
            """
        )
    streaming_queries = 0
    if ctx.has_table("system.query.history"):
        streaming_queries = ctx.count(
            f"""
            SELECT count(*) AS n FROM system.query.history
            WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND (lower(statement_text) LIKE '%readstream%'
                   OR lower(statement_text) LIKE '%writestream%'
                   OR lower(statement_text) LIKE '%stream(%')
            """
        )
    scope = ctx.scope
    streaming_tables = ctx.count(
        f"""
        SELECT count(*) AS n FROM system.information_schema.tables
        WHERE {scope.predicate()}
          AND lower(coalesce(table_type, '')) LIKE '%streaming%'
        """
    )
    total = streaming_pipelines + streaming_queries + streaming_tables
    if total == 0:
        return Result(
            "R-04-01", "", "", "", STATUS_OPEN,
            f"No streaming workloads detected in the last {ctx.lookback_days} days "
            "(no streaming pipelines, streaming tables, or readStream/writeStream "
            "queries). Mark ignore in the WAF tool if streaming is out of scope.",
            {"scoped": True},
        )
    status = STATUS_COMPLETED if streaming_pipelines or streaming_tables else STATUS_IN_PROGRESS
    return Result(
        "R-04-01", "", "", "", status,
        f"Streaming present: {streaming_pipelines} declarative pipeline(s), "
        f"{streaming_tables} streaming table(s), and {streaming_queries:,} "
        f"streaming statement(s) in the last {ctx.lookback_days} days. "
        + (
            "Declarative pipelines and streaming tables manage checkpoints and restart "
            "recovery automatically."
            if streaming_pipelines or streaming_tables
            else "Hand-rolled streaming jobs were found but no managed pipelines - verify "
            "checkpoint locations are durable and unique per query."
        ),
        {"pipelines": streaming_pipelines, "streaming_tables": streaming_tables,
         "streaming_statements": streaming_queries, "scoped": True},
    )


def r_04_02(ctx: Ctx) -> Result:
    """Recover ETL jobs using data time travel capabilities."""
    scope = ctx.scope
    row = ctx.one(
        f"""
        SELECT count(*) AS total,
               count_if(lower(coalesce(data_source_format, '')) = 'delta') AS delta
        FROM system.information_schema.tables
        WHERE {scope.predicate()} AND table_type <> 'VIEW'
        """
    )
    total = int(row.get("total") or 0)
    delta = int(row.get("delta") or 0)
    tt_usage = 0
    if ctx.has_table("system.query.history"):
        tt_usage = ctx.count(
            f"""
            SELECT count(*) AS n FROM system.query.history
            WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND (lower(statement_text) LIKE '%version as of%'
                   OR lower(statement_text) LIKE '%timestamp as of%'
                   OR lower(statement_text) LIKE '%restore table%')
            """
        )
    # custom_max_retention_hours is not present on every workspace/cloud, so probe.
    retention = 0
    if ctx.has_column("system.information_schema.catalogs", "custom_max_retention_hours"):
        retention = ctx.count_safe(
            f"""
            SELECT count(*) AS n FROM system.information_schema.catalogs
            WHERE {scope.catalog_predicate('catalog_name')}
              AND custom_max_retention_hours IS NOT NULL
            """
        )
    res = ratio_result(
        "R-04-02",
        delta,
        total,
        f"{scope.label} tables are Delta and therefore support time travel / RESTORE",
        complete_at=ctx.complete_at,
        none_found="No tables found in the assessed catalogs.",
        extra=(
            f"{tt_usage:,} time-travel or RESTORE statement(s) observed in the last "
            f"{ctx.lookback_days} days"
            + (
                f"; {retention} catalog(s) set a custom retention window."
                if retention
                else ", and no custom retention windows are configured."
            )
            if tt_usage
            else "No time-travel or RESTORE statements observed, so the capability exists "
            "but recovery has not been exercised."
        ),
        metrics={"time_travel_statements": tt_usage, "catalogs_with_retention": retention,
                 "scoped": True},
    )
    if res.status == STATUS_COMPLETED and tt_usage == 0:
        res.status = STATUS_IN_PROGRESS
        res.reason += " Downgraded to in-progress: capability is available but untested."
    return res


def r_04_03(ctx: Ctx) -> Result:
    """Leverage a job automation framework with built-in recovery."""
    if not ctx.has_table("system.lakeflow.jobs"):
        return Result(
            "R-04-03", "", "", "", STATUS_OPEN,
            "system.lakeflow.jobs is not readable, so job orchestration cannot be "
            "assessed.",
        )
    total = ctx.count(
        "SELECT count(DISTINCT job_id) AS n FROM system.lakeflow.jobs WHERE delete_time IS NULL"
    )
    if total == 0:
        return Result(
            "R-04-03", "", "", "", STATUS_OPEN,
            "No active jobs found, so no orchestration framework is in evidence.",
        )
    dependent = 0
    if ctx.has_table("system.lakeflow.job_tasks"):
        dependent = ctx.count(
            """
            SELECT count(DISTINCT job_id) AS n FROM system.lakeflow.job_tasks
            WHERE delete_time IS NULL
              AND depends_on_keys IS NOT NULL AND size(depends_on_keys) > 0
            """
        )
    repairs = 0
    if ctx.has_table("system.lakeflow.job_run_timeline"):
        repairs = ctx.count(
            f"""
            SELECT count(*) AS n FROM system.lakeflow.job_run_timeline
            WHERE period_start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND lower(coalesce(run_type, '')) RLIKE '(repair|retry)'
            """
        )
    return ratio_result(
        "R-04-03",
        dependent,
        total,
        "active jobs use multi-task dependency graphs (Lakeflow Jobs orchestration with "
        "task-level retry and repair-run recovery)",
        complete_at=ctx.complete_at,
        extra=(
            f"{repairs:,} repair/retry run(s) in the last {ctx.lookback_days} days show "
            "recovery is exercised."
            if repairs
            else "No repair or retry runs observed in the lookback window."
        ),
        metrics={"jobs": total, "multi_task_jobs": dependent, "repair_runs": repairs},
    )


def r_04_04(ctx: Ctx) -> Result:
    """Configure a disaster recovery pattern."""
    replication = 0
    if ctx.has_table("system.replication.states"):
        try:
            replication = ctx.count("SELECT count(*) AS n FROM system.replication.states")
        except Exception:
            replication = 0
    shares = 0
    if ctx.has_table("system.information_schema.shares"):
        shares = ctx.count("SELECT count(*) AS n FROM system.information_schema.shares")
    workspaces = 0
    if ctx.has_table("system.access.workspaces_latest"):
        workspaces = ctx.count(
            "SELECT count(DISTINCT workspace_id) AS n FROM system.access.workspaces_latest"
        )
    deep_clones = 0
    if ctx.has_table("system.storage.table_metrics_history"):
        deep_clones = ctx.count(
            """
            SELECT count(DISTINCT table_id) AS n
            FROM system.storage.table_metrics_history
            WHERE snapshot_date >= current_date() - INTERVAL 30 DAYS
              AND clone_details IS NOT NULL
            """
        )
    if replication == 0 and deep_clones == 0:
        return Result(
            "R-04-04", "", "", "", STATUS_OPEN,
            f"No UC metastore replication and no table clones found across "
            f"{workspaces} workspace(s), so no disaster recovery pattern is observable. "
            "DR runbooks and cross-region failover are often external - confirm with "
            "the customer.",
            {"workspaces": workspaces, "shares": shares},
        )
    return Result(
        "R-04-04", "", "", "", STATUS_IN_PROGRESS if replication == 0 else STATUS_COMPLETED,
        f"DR signals found: {replication} replication state record(s), {deep_clones} "
        f"cloned table(s) in the last 30 days, {shares} Delta Share(s), across "
        f"{workspaces} workspace(s). Verify the documented RTO/RPO and last failover test.",
        {"replication_states": replication, "clones": deep_clones,
         "shares": shares, "workspaces": workspaces},
    )


# ----------------------------------------------------------------------------------
# Monitor platform events
# ----------------------------------------------------------------------------------


def r_05_01(ctx: Ctx) -> Result:
    """Monitor data platform events."""
    if not ctx.has_table("system.lakeflow.job_run_timeline"):
        return Result(
            "R-05-01", "", "", "", STATUS_OPEN,
            "system.lakeflow.job_run_timeline is not readable, so run monitoring "
            "cannot be assessed.",
        )
    row = ctx.one(
        f"""
        SELECT count(*) AS runs,
               count_if(result_state = 'SUCCEEDED') AS ok,
               count_if(result_state IN ('FAILED', 'TIMEDOUT', 'INTERNAL_ERROR')) AS failed
        FROM system.lakeflow.job_run_timeline
        WHERE period_start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
          AND result_state IS NOT NULL
        """
    )
    runs = int(row.get("runs") or 0)
    failed = int(row.get("failed") or 0)
    ok = int(row.get("ok") or 0)

    alerting = 0
    if ctx.has_table("system.lakeflow.jobs"):
        alerting = ctx.count(
            """
            WITH latest AS (
              SELECT job_id, health_rules,
                     row_number() OVER (PARTITION BY job_id ORDER BY change_time DESC) AS rn
              FROM system.lakeflow.jobs WHERE delete_time IS NULL
            )
            SELECT count(*) AS n FROM latest
            WHERE rn = 1 AND health_rules IS NOT NULL
              AND lower(to_json(health_rules)) NOT IN ('null', '{}', '[]')
            """
        )
    if runs == 0:
        return Result(
            "R-05-01", "", "", "", STATUS_OPEN,
            f"No job runs recorded in the last {ctx.lookback_days} days, so platform "
            "event monitoring cannot be verified.",
        )
    success_rate = ok / runs * 100
    total_jobs = ctx.count(
        "SELECT count(DISTINCT job_id) AS n FROM system.lakeflow.jobs WHERE delete_time IS NULL"
    )
    res = ratio_result(
        "R-05-01",
        alerting,
        total_jobs,
        "active jobs define health rules / notification thresholds so failures surface "
        "automatically",
        complete_at=ctx.complete_at,
        none_found=(
            "Job run telemetry is available but no active jobs were found to carry "
            "health rules."
        ),
        extra=(
            f"Observed reliability: {runs:,} run(s), {success_rate:.1f}% succeeded, "
            f"{failed:,} failed in the last {ctx.lookback_days} days."
        ),
        metrics={"runs": runs, "failed": failed, "success_rate_pct": round(success_rate, 2),
                 "jobs_with_health_rules": alerting},
    )
    return res


def r_05_02(ctx: Ctx) -> Result:
    """Monitor cloud events."""
    signals = {}
    if ctx.has_table("system.compute.node_timeline"):
        signals["node_timeline_rows"] = ctx.count(
            f"""
            SELECT count(*) AS n FROM system.compute.node_timeline
            WHERE start_time >= current_timestamp() - INTERVAL 7 DAYS
            """
        )
    if ctx.has_table("system.compute.instance_events"):
        signals["instance_events"] = ctx.count(
            f"""
            SELECT count(*) AS n FROM system.compute.instance_events
            WHERE event_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
            """
        )
    if ctx.has_table("system.access.outbound_network"):
        signals["outbound_network_events"] = ctx.count(
            f"""
            SELECT count(*) AS n FROM system.access.outbound_network
            WHERE event_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
            """
        )
    available = {k: v for k, v in signals.items() if v}
    if not available:
        return Result(
            "R-05-02", "", "", "", STATUS_OPEN,
            "No cloud-infrastructure telemetry found (compute node metrics, instance "
            "events, or network events), so cloud-level events are not being monitored "
            "in Databricks. Cloud-native monitoring may exist outside the platform - "
            "confirm with the customer.",
            {"signals": signals},
        )
    detail = ", ".join(f"{k}={v:,}" for k, v in available.items())
    status = STATUS_COMPLETED if len(available) >= 2 else STATUS_IN_PROGRESS
    return Result(
        "R-05-02", "", "", "", status,
        f"{len(available)} of {len(signals)} cloud telemetry stream(s) are populated "
        f"({detail}), giving visibility into infrastructure-level events. Confirm these "
        "feed an alerting destination.",
        {"signals": signals},
    )


CHECKS = [
    ("R-01-01", r_01_01),
    ("R-01-02", r_01_02),
    ("R-01-03", r_01_03),
    ("R-01-04", r_01_04),
    ("R-01-05", r_01_05),
    ("R-01-06", r_01_06),
    ("R-02-01", r_02_01),
    ("R-02-02", r_02_02),
    ("R-02-03", r_02_03),
    ("R-02-04", r_02_04),
    ("R-02-05", r_02_05),
    ("R-03-01", r_03_01),
    ("R-03-02", r_03_02),
    ("R-04-01", r_04_01),
    ("R-04-02", r_04_02),
    ("R-04-03", r_04_03),
    ("R-04-04", r_04_04),
    ("R-05-01", r_05_01),
    ("R-05-02", r_05_02),
]
