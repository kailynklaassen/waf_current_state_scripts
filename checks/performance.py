"""Performance Efficiency pillar checks (PE-*).

Evidence sources: ``system.query.history``, ``system.compute.*``,
``system.storage.*``, ``system.billing.usage``, ``system.information_schema.*``.

Note: the published question bank skips ``PE-02-13``; the identifiers here mirror
the source data exactly rather than renumbering.
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

from checks import inventory

PILLAR_ID = "performance-efficiency"


def _has_query_history(ctx: Ctx, qid: str) -> Result | None:
    if ctx.has_table("system.query.history"):
        return None
    return Result(
        qid, "", "", "", STATUS_OPEN,
        "system.query.history is not readable, so query behaviour cannot be assessed.",
    )


# ----------------------------------------------------------------------------------
# Utilize serverless capabilities
# ----------------------------------------------------------------------------------


def pe_01_01(ctx: Ctx) -> Result:
    """Use serverless architecture."""
    if not ctx.has_table("system.billing.usage"):
        return Result(
            "PE-01-01", "", "", "", STATUS_OPEN,
            "system.billing.usage is not readable, so serverless adoption cannot be "
            "measured.",
        )
    row = ctx.one(
        f"""
        SELECT sum(usage_quantity) AS total,
               sum(CASE WHEN coalesce(product_features.is_serverless,
                                      lower(coalesce(sku_name, '')) LIKE '%serverless%')
                        THEN usage_quantity ELSE 0 END) AS serverless
        FROM system.billing.usage
        WHERE {inventory.billing_scope(ctx, "workspace_id")} AND usage_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
        """
    )
    total = float(row.get("total") or 0)
    serverless = float(row.get("serverless") or 0)
    if total <= 0:
        return Result(
            "PE-01-01", "", "", "", STATUS_OPEN,
            f"No billable usage recorded in the last {ctx.lookback_days} days.",
        )
    return ratio_result(
        "PE-01-01",
        int(round(serverless)),
        int(round(total)),
        f"DBUs in {inventory.region_label(ctx)} "
        f"in the last {ctx.lookback_days} days run on serverless compute, which "
        "removes start-up latency and sizing guesswork",
        complete_at=ctx.complete_at,
        metrics={"serverless_dbus": round(serverless, 2), "total_dbus": round(total, 2)},
    )


def pe_01_02(ctx: Ctx) -> Result:
    """Use an enterprise grade model serving service.

    Not just "do endpoints exist" but "does managed serving carry the ML/GenAI load":
    scored as endpoint utilization - the share of active Model Serving endpoints that
    actually served traffic in the window (idle endpoint sprawl is not enterprise-grade
    use). If there are no endpoints at all yet ML/GenAI compute is being consumed,
    inference is running off managed serving -> open.
    """
    if not ctx.has_table("system.serving.served_entities"):
        return Result(
            "PE-01-02", "", "", "", STATUS_OPEN,
            "system.serving.served_entities is not readable, so model serving cannot be "
            "assessed.",
        )
    active = ctx.count_safe(
        "SELECT count(DISTINCT endpoint_id) AS n FROM system.serving.served_entities "
        "WHERE endpoint_delete_time IS NULL"
    )
    # ML/GenAI consumption context (for the no-endpoints guard and the rationale).
    genai_dbu = serving_dbu = 0.0
    if ctx.has_table("system.billing.usage"):
        brow = ctx.one(
            f"""
            SELECT sum(usage_quantity) AS genai,
                   sum(CASE WHEN upper(coalesce(billing_origin_product, '')) = 'MODEL_SERVING'
                            THEN usage_quantity ELSE 0 END) AS serving
            FROM system.billing.usage
            WHERE {inventory.billing_scope(ctx, "workspace_id")} AND usage_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
              AND upper(coalesce(billing_origin_product, '')) IN
                  ('MODEL_SERVING', 'VECTOR_SEARCH', 'AI_FUNCTIONS', 'AGENT_BRICKS',
                   'AI_GATEWAY', 'SUPERVISOR_AGENT', 'AI_RUNTIME')
            """
        )
        genai_dbu = float(brow.get("genai") or 0)
        serving_dbu = float(brow.get("serving") or 0)

    if active == 0:
        if genai_dbu > 0:
            return Result(
                "PE-01-02", "", "", "", STATUS_OPEN,
                f"No active Model Serving endpoints, yet {genai_dbu:,.0f} ML/GenAI DBU "
                f"were consumed in the last {ctx.lookback_days} days - inference is "
                "running off managed serving. Stand up enterprise-grade serving endpoints.",
                {"endpoints": 0, "genai_dbu": round(genai_dbu, 2)},
            )
        return Result(
            "PE-01-02", "", "", "", STATUS_OPEN,
            "No active Model Serving endpoints and no ML/GenAI consumption found. Mark "
            "ignore in the WAF tool if model serving is out of scope.",
            {"endpoints": 0},
        )
    with_traffic = requests = 0
    if ctx.has_table("system.serving.endpoint_usage"):
        trow = ctx.one(
            f"""
            SELECT count(DISTINCT se.endpoint_id) AS endpoints, count(*) AS requests
            FROM system.serving.served_entities se
            JOIN system.serving.endpoint_usage u ON u.served_entity_id = se.served_entity_id
            WHERE se.endpoint_delete_time IS NULL
              AND u.request_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
            """
        )
        with_traffic = int(trow.get("endpoints") or 0)
        requests = int(trow.get("requests") or 0)
    share = f"{serving_dbu / genai_dbu * 100:.0f}%" if genai_dbu > 0 else "n/a"
    return ratio_result(
        "PE-01-02",
        with_traffic,
        active,
        f"active Model Serving endpoints served traffic in the last {ctx.lookback_days} "
        "days (managed serving is actually carrying inference, not idle endpoint sprawl)",
        complete_at=ctx.complete_at,
        extra=(
            f"{requests:,} request(s) served; Model Serving is {share} of ML/GenAI DBU "
            f"in {inventory.region_label(ctx)} ({serving_dbu:,.0f} of {genai_dbu:,.0f})."
        ),
        metrics={"active_endpoints": active, "endpoints_with_traffic": with_traffic,
                 "requests": requests, "serving_dbu": round(serving_dbu, 2),
                 "genai_dbu": round(genai_dbu, 2)},
    )


# ----------------------------------------------------------------------------------
# Design workloads for performance
# ----------------------------------------------------------------------------------


def pe_02_01(ctx: Ctx) -> Result:
    """Understand your data ingestion and access patterns."""
    if (bad := _has_query_history(ctx, "PE-02-01")) is not None:
        return bad
    row = ctx.one(
        f"""
        SELECT count(*) AS total,
               count_if(read_bytes IS NOT NULL) AS measured,
               avg(coalesce(read_io_cache_percent, 0)) AS avg_cache,
               sum(coalesce(read_bytes, 0)) AS bytes_read,
               sum(coalesce(pruned_files, 0)) AS pruned,
               sum(coalesce(read_files, 0)) AS files_read
        FROM system.query.history
        WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
          AND execution_status = 'FINISHED'
        """
    )
    total = int(row.get("total") or 0)
    if total == 0:
        return Result(
            "PE-02-01", "", "", "", STATUS_OPEN,
            f"No completed queries in the last {ctx.lookback_days} days, so access "
            "patterns cannot be characterized.",
        )
    pruned = int(row.get("pruned") or 0)
    files_read = int(row.get("files_read") or 0)
    denom = pruned + files_read
    return ratio_result(
        "PE-02-01",
        pruned,
        denom,
        "files touched by queries were pruned rather than read, which shows access "
        "patterns are understood and the layout matches them",
        complete_at=0.50,
        none_found=(
            f"{total:,} query/queries ran but reported no file-level statistics, so "
            "access patterns cannot be characterized."
        ),
        extra=(
            f"Observed over {total:,} completed query/queries reading "
            f"{float(row.get('bytes_read') or 0) / 1e9:,.1f} GB, average IO cache hit "
            f"{float(row.get('avg_cache') or 0):.1f}%."
        ),
        metrics={"queries": total, "pruned_files": pruned, "read_files": files_read},
    )


def pe_02_02(ctx: Ctx) -> Result:
    """Use parallel computation where it is beneficial."""
    if (bad := _has_query_history(ctx, "PE-02-02")) is not None:
        return bad
    row = ctx.one(
        f"""
        SELECT count(*) AS total,
               count_if(coalesce(read_partitions, 0) > 1) AS parallel,
               avg(coalesce(read_partitions, 0)) AS avg_partitions
        FROM system.query.history
        WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
          AND execution_status = 'FINISHED'
          AND coalesce(read_bytes, 0) > 0
        """
    )
    total = int(row.get("total") or 0)
    parallel = int(row.get("parallel") or 0)
    return ratio_result(
        "PE-02-02",
        parallel,
        total,
        "data-reading queries scan more than one partition, indicating work is spread "
        "across the cluster rather than serialized",
        complete_at=ctx.complete_at,
        none_found=(
            f"No data-reading queries in the last {ctx.lookback_days} days, so "
            "parallelism cannot be assessed."
        ),
        extra=f"Average partitions read per query: {float(row.get('avg_partitions') or 0):.1f}.",
        metrics={"queries": total, "parallel_queries": parallel},
    )


def pe_02_03(ctx: Ctx) -> Result:
    """Analyze the whole chain of execution."""
    if (bad := _has_query_history(ctx, "PE-02-03")) is not None:
        return bad
    row = ctx.one(
        f"""
        SELECT count(*) AS total,
               sum(coalesce(total_duration_ms, 0)) AS total_ms,
               sum(coalesce(waiting_for_compute_duration_ms, 0)) AS wait_compute_ms,
               sum(coalesce(waiting_at_capacity_duration_ms, 0)) AS wait_capacity_ms,
               sum(coalesce(compilation_duration_ms, 0)) AS compile_ms,
               sum(coalesce(execution_duration_ms, 0)) AS exec_ms,
               sum(coalesce(result_fetch_duration_ms, 0)) AS fetch_ms
        FROM system.query.history
        WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
          AND execution_status = 'FINISHED'
        """
    )
    total = int(row.get("total") or 0)
    total_ms = float(row.get("total_ms") or 0)
    if total == 0 or total_ms <= 0:
        return Result(
            "PE-02-03", "", "", "", STATUS_OPEN,
            f"No completed queries with timing data in the last {ctx.lookback_days} days, "
            "so the execution chain cannot be analyzed.",
        )
    exec_ms = float(row.get("exec_ms") or 0)
    overhead = total_ms - exec_ms
    phases = {
        "queue/compute wait": float(row.get("wait_compute_ms") or 0)
        + float(row.get("wait_capacity_ms") or 0),
        "compilation": float(row.get("compile_ms") or 0),
        "result fetch": float(row.get("fetch_ms") or 0),
    }
    worst = max(phases, key=phases.get)
    breakdown = ", ".join(
        f"{k} {v / total_ms * 100:.1f}%" for k, v in phases.items()
    )
    return ratio_result(
        "PE-02-03",
        int(exec_ms),
        int(total_ms),
        "total query wall-clock time is spent in actual execution rather than waiting, "
        "compiling or fetching",
        complete_at=ctx.complete_at,
        extra=(
            f"Non-execution overhead is {overhead / total_ms * 100:.1f}% across "
            f"{total:,} query/queries ({breakdown}); the largest non-execution phase is "
            f"{worst}."
        ),
        metrics={"queries": total, "total_ms": total_ms, "exec_ms": exec_ms,
                 "phase_ms": phases},
    )


def pe_02_04(ctx: Ctx) -> Result:
    """Prefer larger clusters."""
    if not ctx.has_table("system.compute.clusters"):
        return Result(
            "PE-02-04", "", "", "", STATUS_OPEN,
            "system.compute.clusters is not readable, so cluster sizing cannot be "
            "assessed.",
        )
    row = ctx.one(
        f"""
        {inventory.workloads_cte(ctx, 'cluster_id, coalesce(max_autoscale_workers, worker_count, 0) AS workers')}
        SELECT count(*) AS total,
               count_if(workers >= 2) AS multi_worker,
               count_if(workers = 0) AS single_node,
               avg(workers) AS avg_workers
        FROM latest WHERE rn = 1
        """
    )
    total = int(row.get("total") or 0)
    if total == 0:
        return Result(
            "PE-02-04", "", "", "", STATUS_COMPLETED,
            f"No classic clusters active in the last {ctx.lookback_days} days; serverless "
            "compute sizes itself, making manual cluster sizing moot.",
            {"clusters": 0},
        )
    return ratio_result(
        "PE-02-04",
        int(row.get("multi_worker") or 0),
        total,
        f"{inventory.workload_unit(ctx)} have at least two workers, so a single large cluster does the "
        "work instead of many undersized ones",
        complete_at=ctx.complete_at,
        extra=(
            f"Average worker count {float(row.get('avg_workers') or 0):.1f}; "
            f"{row.get('single_node')} single-node cluster(s). Fewer, larger clusters "
            "usually finish sooner for the same DBU spend."
        ),
        metrics={"single_node": row.get("single_node"),
                 "avg_workers": round(float(row.get("avg_workers") or 0), 2)},
    )


def pe_02_05(ctx: Ctx) -> Result:
    """Use native Spark operations (rather than UDFs)."""
    if (bad := _has_query_history(ctx, "PE-02-05")) is not None:
        return bad
    row = ctx.one(
        f"""
        SELECT count(*) AS total,
               count_if(lower(coalesce(statement_text, '')) RLIKE
                        '(create +(or +replace +)?(temporary +)?function|py_udf|pandas_udf)') AS udf_defs
        FROM system.query.history
        WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
        """
    )
    total = int(row.get("total") or 0)
    udfs = int(row.get("udf_defs") or 0)
    scope = ctx.scope
    py_routines = ctx.count_safe(
        f"""
        SELECT count(*) AS n FROM system.information_schema.routines
        WHERE {scope.predicate('routine_catalog', 'routine_schema')}
          AND lower(coalesce(external_language, '')) IN ('python', 'scala', 'java')
        """
    )
    sql_routines = ctx.count_safe(
        f"""
        SELECT count(*) AS n FROM system.information_schema.routines
        WHERE {scope.predicate('routine_catalog', 'routine_schema')}
          AND lower(coalesce(external_language, '')) NOT IN ('python', 'scala', 'java')
        """
    )
    if total == 0:
        return Result(
            "PE-02-05", "", "", "", STATUS_OPEN,
            f"No query activity in the last {ctx.lookback_days} days, so operator use "
            "cannot be assessed.",
            {"scoped": True},
        )
    native = total - udfs
    return ratio_result(
        "PE-02-05",
        native,
        total,
        "statements avoid defining scalar UDFs, which cannot be optimized or vectorized "
        "the way native Spark expressions are",
        complete_at=ctx.complete_at,
        extra=(
            f"{py_routines} non-SQL UC function(s) vs {sql_routines} SQL function(s) "
            "registered in scope. SQL and pandas UDFs vectorize; row-at-a-time Python "
            "UDFs do not."
        ),
        metrics={"udf_definition_statements": udfs, "python_routines": py_routines,
                 "sql_routines": sql_routines, "scoped": True},
    )


def pe_02_06(ctx: Ctx) -> Result:
    """Use native platform engines (Photon)."""
    if not ctx.has_table("system.billing.usage"):
        return Result(
            "PE-02-06", "", "", "", STATUS_OPEN,
            "system.billing.usage is not readable, so Photon adoption cannot be measured.",
        )
    row = ctx.one(
        f"""
        SELECT sum(usage_quantity) AS total,
               sum(CASE WHEN product_features.is_photon = true
                        THEN usage_quantity ELSE 0 END) AS photon,
               sum(CASE WHEN lower(coalesce(sku_name, '')) LIKE '%sql%'
                        THEN usage_quantity ELSE 0 END) AS sql_dbus
        FROM system.billing.usage
        WHERE {inventory.billing_scope(ctx, "workspace_id")} AND usage_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
        """
    )
    total = float(row.get("total") or 0)
    if total <= 0:
        return Result(
            "PE-02-06", "", "", "", STATUS_OPEN,
            f"No billable usage recorded in the last {ctx.lookback_days} days.",
        )
    photon = float(row.get("photon") or 0)
    sql_dbus = float(row.get("sql_dbus") or 0)
    vectorized = max(photon, 0) + max(sql_dbus - photon, 0)
    return ratio_result(
        "PE-02-06",
        int(round(min(vectorized, total))),
        int(round(total)),
        f"DBUs in {inventory.region_label(ctx)} "
        f"in the last {ctx.lookback_days} days run on the Photon vectorized engine "
        "or a Photon-backed SQL warehouse",
        complete_at=ctx.complete_at,
        extra=(
            f"Photon-flagged usage: {photon:,.0f} DBU; SQL warehouse usage: "
            f"{sql_dbus:,.0f} DBU."
        ),
        metrics={"photon_dbus": round(photon, 2), "sql_dbus": round(sql_dbus, 2),
                 "total_dbus": round(total, 2)},
    )


def pe_02_07(ctx: Ctx) -> Result:
    """Understand your hardware and workload type.

    Rolls the query-level performance signals up to the compute resource and counts how
    many resources (jobs / warehouses / clusters) show a recurring hardware-fit problem.
    A resource is flagged when a material share (default 20%) of a meaningful number of
    its queries (default >= 20) spill to disk (memory too small) or queue at capacity
    (compute too small) - the two signals that point at the wrong hardware/size. Shuffle-
    heavy is a query-shape issue, not a hardware one, so it is excluded here.

    Status is the COUNT of flagged resources:  < 5 completed, 5-20 in-progress, > 20 open.
    All thresholds are config-overridable.
    """
    if not ctx.has_table("system.query.history"):
        return Result(
            "PE-02-07", "", "", "", STATUS_OPEN,
            "system.query.history is not readable, so workloads with performance issues "
            "cannot be identified.",
        )
    share = float(ctx.cfg.get("perf_issue_share", 0.20))
    min_q = int(ctx.cfg.get("perf_issue_min_queries", 20))
    inprog_at = int(ctx.cfg.get("perf_issue_inprogress_at", 5))
    open_at = int(ctx.cfg.get("perf_issue_open_at", 20))
    row = ctx.one(
        f"""
        WITH q AS (
          SELECT coalesce(query_source.job_info.job_id, compute.warehouse_id,
                          compute.cluster_id) AS rid,
                 CASE WHEN query_source.job_info.job_id IS NOT NULL THEN 'job'
                      WHEN compute.cluster_id IS NOT NULL THEN 'cluster'
                      ELSE 'warehouse' END AS rtype,
                 CASE WHEN coalesce(spilled_local_bytes, 0) > 0
                           OR coalesce(waiting_at_capacity_duration_ms, 0) > 0
                      THEN 1 ELSE 0 END AS bad
          FROM system.query.history
          WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
            AND execution_status = 'FINISHED'
            AND coalesce(query_source.job_info.job_id, compute.warehouse_id,
                         compute.cluster_id) IS NOT NULL
        ),
        agg AS (SELECT rid, rtype, count(*) AS tot, sum(bad) AS bad FROM q GROUP BY 1, 2),
        flagged AS (SELECT rtype FROM agg WHERE tot >= {min_q} AND bad * 1.0 / tot >= {share})
        SELECT count(*) AS flagged,
               count_if(rtype = 'job') AS jobs,
               count_if(rtype = 'warehouse') AS warehouses,
               count_if(rtype = 'cluster') AS clusters
        FROM flagged
        """
    )
    flagged = int(row.get("flagged") or 0)
    jobs = int(row.get("jobs") or 0)
    warehouses = int(row.get("warehouses") or 0)
    clusters = int(row.get("clusters") or 0)
    if flagged > open_at:
        status = STATUS_OPEN
    elif flagged >= inprog_at:
        status = STATUS_IN_PROGRESS
    else:
        status = STATUS_COMPLETED
    return Result(
        "PE-02-07", "", "", "", status,
        f"{flagged} compute resource(s) show recurring performance issues "
        f"(>= {share * 100:.0f}% of at least {min_q} queries spilling to disk or queuing "
        f"at capacity) in the last {ctx.lookback_days} days: {jobs} job(s), "
        f"{warehouses} warehouse(s), {clusters} cluster(s). Bands: <{inprog_at} completed, "
        f"{inprog_at}-{open_at} in-progress, >{open_at} open. Flagged workloads likely "
        "need a larger or differently-shaped instance type.",
        {"flagged_resources": flagged, "jobs": jobs, "warehouses": warehouses,
         "clusters": clusters, "issue_share": share, "min_queries": min_q},
    )


def pe_02_08(ctx: Ctx) -> Result:
    """Use caching."""
    if (bad := _has_query_history(ctx, "PE-02-08")) is not None:
        return bad
    row = ctx.one(
        f"""
        SELECT count(*) AS total,
               count_if(from_result_cache = true) AS result_cached,
               avg(coalesce(read_io_cache_percent, 0)) AS avg_io_cache
        FROM system.query.history
        WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
          AND execution_status = 'FINISHED'
        """
    )
    total = int(row.get("total") or 0)
    if total == 0:
        return Result(
            "PE-02-08", "", "", "", STATUS_OPEN,
            f"No completed queries in the last {ctx.lookback_days} days, so cache "
            "effectiveness cannot be measured.",
        )
    io_cache = float(row.get("avg_io_cache") or 0)
    result_cached = int(row.get("result_cached") or 0)
    # The disk (IO) cache is the primary performance lever here.
    status = STATUS_COMPLETED if io_cache >= 50 else STATUS_IN_PROGRESS
    return Result(
        "PE-02-08", "", "", "", status,
        f"Average IO cache hit rate is {io_cache:.1f}% across {total:,} completed "
        f"query/queries, and {result_cached:,} query/queries "
        f"({result_cached / total * 100:.1f}%) were served entirely from the result "
        "cache. "
        + (
            "Caching is working well."
            if status == STATUS_COMPLETED
            else "A low IO cache hit rate suggests cold compute, a working set larger "
            "than cache, or too little reuse to benefit."
        ),
        {"queries": total, "avg_io_cache_pct": round(io_cache, 2),
         "result_cache_hits": result_cached},
    )


def pe_02_09(ctx: Ctx) -> Result:
    """Use compaction."""
    if not ctx.has_table("system.storage.predictive_optimization_operations_history"):
        return Result(
            "PE-02-09", "", "", "", STATUS_OPEN,
            "system.storage.predictive_optimization_operations_history is not readable, "
            "so compaction activity cannot be assessed.",
        )
    row = ctx.one(
        f"""
        SELECT count(*) AS ops,
               count(DISTINCT table_id) AS tables,
               count_if(lower(coalesce(operation_type, '')) LIKE '%compact%'
                        OR lower(coalesce(operation_type, '')) LIKE '%optimize%') AS compactions
        FROM system.storage.predictive_optimization_operations_history
        WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
          AND {ctx.scope.predicate("catalog_name", "schema_name")}
        """
    )
    ops = int(row.get("ops") or 0)
    manual_optimize = 0
    if ctx.has_table("system.query.history"):
        manual_optimize = ctx.count_safe(
            f"""
            SELECT count(*) AS n FROM system.query.history
            WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND lower(statement_text) RLIKE '^\\\\s*optimize '
            """
        )
    if ops == 0 and manual_optimize == 0:
        return Result(
            "PE-02-09", "", "", "", STATUS_OPEN,
            f"No predictive optimization operations in {ctx.scope.label} and no manual "
            f"OPTIMIZE statements in this region in the last {ctx.lookback_days} days, so "
            "small files are not being compacted.",
            {"po_operations": 0, "manual_optimize": 0},
        )
    status = STATUS_COMPLETED if ops > 0 else STATUS_IN_PROGRESS
    return Result(
        "PE-02-09", "", "", "", status,
        f"Compaction is running: {ops:,} predictive optimization operation(s) across "
        f"{int(row.get('tables') or 0)} table(s) in {ctx.scope.label} and "
        f"{manual_optimize:,} manual OPTIMIZE statement(s) run in this region (any "
        f"catalog - statement text has no reliable catalog) in the last "
        f"{ctx.lookback_days} days. "
        + (
            "Predictive Optimization handles this automatically."
            if ops
            else "Only manual OPTIMIZE was observed; enabling Predictive Optimization "
            "removes the maintenance burden."
        ),
        {"po_operations": ops, "po_tables": row.get("tables"),
         "manual_optimize": manual_optimize},
    )


def pe_02_10(ctx: Ctx) -> Result:
    """Use data skipping."""
    if (bad := _has_query_history(ctx, "PE-02-10")) is not None:
        return bad
    row = ctx.one(
        f"""
        SELECT sum(coalesce(pruned_files, 0)) AS pruned,
               sum(coalesce(read_files, 0)) AS read_files,
               count(*) AS queries
        FROM system.query.history
        WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
          AND execution_status = 'FINISHED'
          AND coalesce(read_files, 0) + coalesce(pruned_files, 0) > 0
        """
    )
    pruned = int(row.get("pruned") or 0)
    read_files = int(row.get("read_files") or 0)
    total_files = pruned + read_files
    clustered = 0
    if ctx.has_table("system.query.history"):
        clustered = ctx.count_safe(
            f"""
            SELECT count(*) AS n FROM system.query.history
            WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND (lower(statement_text) LIKE '%cluster by%'
                   OR lower(statement_text) LIKE '%zorder%')
            """
        )
    return ratio_result(
        "PE-02-10",
        pruned,
        total_files,
        "candidate files were skipped by the query engine instead of being read",
        complete_at=0.50,
        none_found=(
            f"No queries reported file-level statistics in the last {ctx.lookback_days} "
            "days, so data skipping effectiveness cannot be measured."
        ),
        extra=(
            f"Across {int(row.get('queries') or 0):,} query/queries. {clustered:,} statement(s) "
            "reference CLUSTER BY or ZORDER, which is what makes skipping effective."
        ),
        metrics={"pruned_files": pruned, "read_files": read_files,
                 "clustering_statements": clustered},
    )


def pe_02_11(ctx: Ctx) -> Result:
    """Enable Predictive Optimization on your metastore."""
    if not ctx.has_table("system.storage.table_metrics_history"):
        return Result(
            "PE-02-11", "", "", "", STATUS_OPEN,
            "system.storage.table_metrics_history is not readable, so Predictive "
            "Optimization status cannot be read.",
        )
    scope = ctx.scope
    row = ctx.one(
        f"""
        WITH latest AS (
          SELECT table_id, catalog_name, schema_name, predictive_optimization_enabled,
                 row_number() OVER (PARTITION BY table_id ORDER BY snapshot_date DESC) AS rn
          FROM system.storage.table_metrics_history
          WHERE table_dropped_time IS NULL
            AND snapshot_date >= current_date() - INTERVAL 14 DAYS
            AND {scope.predicate('catalog_name', 'schema_name')}
        )
        SELECT count(*) AS total,
               count_if(lower(coalesce(predictive_optimization_enabled, '')) RLIKE '(enable|inherit|true)') AS enabled
        FROM latest WHERE rn = 1
        """
    )
    total = int(row.get("total") or 0)
    enabled = int(row.get("enabled") or 0)
    return ratio_result(
        "PE-02-11",
        enabled,
        total,
        f"{scope.label} tables have Predictive Optimization enabled or inherited",
        complete_at=ctx.complete_at,
        none_found=(
            "No table storage metrics found in scope, so Predictive Optimization status "
            "is unknown."
        ),
        extra=(
            "Predictive Optimization runs OPTIMIZE, VACUUM and statistics collection "
            "automatically where it will pay off."
        ),
        metrics={"scoped": True},
    )


def pe_02_12(ctx: Ctx) -> Result:
    """Avoid over-partitioning."""
    scope = ctx.scope
    row = ctx.one(
        f"""
        WITH parts AS (
          SELECT table_catalog, table_schema, table_name,
                 count_if(partition_index IS NOT NULL) AS partition_cols
          FROM system.information_schema.columns
          WHERE {scope.predicate()}
          GROUP BY 1, 2, 3
        )
        SELECT count(*) AS total,
               count_if(partition_cols = 0) AS unpartitioned,
               count_if(partition_cols BETWEEN 1 AND 2) AS lightly,
               count_if(partition_cols > 2) AS heavily
        FROM parts
        """
    )
    total = int(row.get("total") or 0)
    if total == 0:
        return Result(
            "PE-02-12", "", "", "", STATUS_OPEN,
            "No tables found in the assessed catalogs, so partitioning cannot be "
            "assessed.",
            {"scoped": True},
        )
    heavily = int(row.get("heavily") or 0)
    healthy = total - heavily
    small_tables = 0
    if ctx.has_table("system.storage.table_metrics_history"):
        small_tables = ctx.count_safe(
            f"""
            WITH latest AS (
              SELECT table_id, active_bytes, catalog_name, schema_name,
                     row_number() OVER (PARTITION BY table_id ORDER BY snapshot_date DESC) AS rn
              FROM system.storage.table_metrics_history
              WHERE table_dropped_time IS NULL
                AND snapshot_date >= current_date() - INTERVAL 14 DAYS
                AND {scope.predicate('catalog_name', 'schema_name')}
            )
            SELECT count(*) AS n FROM latest
            WHERE rn = 1 AND coalesce(active_bytes, 0) BETWEEN 1 AND 1073741824
            """
        )
    return ratio_result(
        "PE-02-12",
        healthy,
        total,
        f"{scope.label} tables avoid heavy partitioning (more than two partition columns)",
        complete_at=ctx.complete_at,
        extra=(
            f"{row.get('unpartitioned')} unpartitioned, {row.get('lightly')} lightly "
            f"partitioned, {heavily} heavily partitioned. "
            + (
                f"{small_tables} table(s) are under 1 GB, where partitioning usually "
                "hurts; prefer liquid clustering."
                if small_tables
                else "Prefer liquid clustering over partitioning for new tables."
            )
        ),
        metrics={"unpartitioned": row.get("unpartitioned"),
                 "lightly_partitioned": row.get("lightly"),
                 "heavily_partitioned": heavily, "small_tables": small_tables,
                 "scoped": True},
    )


def pe_02_14(ctx: Ctx) -> Result:
    """Consider file size tuning."""
    if not ctx.has_table("system.storage.table_metrics_history"):
        return Result(
            "PE-02-14", "", "", "", STATUS_OPEN,
            "system.storage.table_metrics_history is not readable, so file sizes cannot "
            "be assessed.",
        )
    scope = ctx.scope
    row = ctx.one(
        f"""
        WITH latest AS (
          SELECT table_id, active_bytes, active_files, catalog_name, schema_name,
                 row_number() OVER (PARTITION BY table_id ORDER BY snapshot_date DESC) AS rn
          FROM system.storage.table_metrics_history
          WHERE table_dropped_time IS NULL
            AND snapshot_date >= current_date() - INTERVAL 14 DAYS
            AND {scope.predicate('catalog_name', 'schema_name')}
        ),
        sized AS (
          SELECT table_id, active_bytes, active_files,
                 active_bytes / nullif(active_files, 0) AS avg_file_bytes
          FROM latest WHERE rn = 1 AND coalesce(active_files, 0) > 0
        )
        SELECT count(*) AS total,
               count_if(avg_file_bytes >= 16777216) AS healthy,
               count_if(avg_file_bytes < 16777216) AS small_file_tables,
               avg(avg_file_bytes) AS avg_bytes
        FROM sized
        """
    )
    total = int(row.get("total") or 0)
    healthy = int(row.get("healthy") or 0)
    avg_bytes = float(row.get("avg_bytes") or 0)
    return ratio_result(
        "PE-02-14",
        healthy,
        total,
        f"{scope.label} tables average at least 16 MB per data file, avoiding the "
        "small-file problem",
        complete_at=ctx.complete_at,
        none_found=(
            "No table file statistics available in scope, so file size tuning cannot be "
            "assessed."
        ),
        extra=(
            f"Mean average file size across measured tables: {avg_bytes / 1048576:,.1f} MB. "
            f"{row.get('small_file_tables')} table(s) fall below 16 MB."
        ),
        metrics={"small_file_tables": row.get("small_file_tables"),
                 "avg_file_mb": round(avg_bytes / 1048576, 2), "scoped": True},
    )


def pe_02_15(ctx: Ctx) -> Result:
    """Optimize join performance."""
    if (bad := _has_query_history(ctx, "PE-02-15")) is not None:
        return bad
    row = ctx.one(
        f"""
        SELECT count(*) AS joins,
               count_if(coalesce(spilled_local_bytes, 0) > 0) AS spilling,
               sum(coalesce(shuffle_read_bytes, 0)) AS shuffle_bytes,
               sum(coalesce(read_bytes, 0)) AS read_bytes
        FROM system.query.history
        WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
          AND execution_status = 'FINISHED'
          AND lower(coalesce(statement_text, '')) LIKE '% join %'
        """
    )
    joins = int(row.get("joins") or 0)
    if joins == 0:
        return Result(
            "PE-02-15", "", "", "", STATUS_OPEN,
            f"No join queries observed in the last {ctx.lookback_days} days, so join "
            "performance cannot be assessed.",
        )
    spilling = int(row.get("spilling") or 0)
    clean = joins - spilling
    shuffle = float(row.get("shuffle_bytes") or 0)
    read = float(row.get("read_bytes") or 0)
    return ratio_result(
        "PE-02-15",
        clean,
        joins,
        "join queries complete without spilling to local disk",
        complete_at=ctx.complete_at,
        extra=(
            f"{spilling:,} of {joins:,} join query/queries spilled. Shuffle volume is "
            f"{shuffle / 1e9:,.1f} GB against {read / 1e9:,.1f} GB read"
            + (
                f" ({shuffle / read * 100:.0f}% shuffle-to-read ratio); high ratios point "
                "to missing broadcast hints or poor join ordering."
                if read > 0
                else "."
            )
        ),
        metrics={"join_queries": joins, "spilling_queries": spilling,
                 "shuffle_bytes": shuffle, "read_bytes": read},
    )


def pe_02_16(ctx: Ctx) -> Result:
    """Run analyze table to collect table statistics."""
    analyze_stmts = 0
    if ctx.has_table("system.query.history"):
        analyze_stmts = ctx.count_safe(
            f"""
            SELECT count(*) AS n FROM system.query.history
            WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND lower(coalesce(statement_text, '')) RLIKE 'analyze +table'
            """
        )
    po_stats = 0
    if ctx.has_table("system.storage.predictive_optimization_operations_history"):
        po_stats = ctx.count_safe(
            f"""
            SELECT count(*) AS n
            FROM system.storage.predictive_optimization_operations_history
            WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND {ctx.scope.predicate("catalog_name", "schema_name")}
              AND lower(coalesce(operation_type, '')) RLIKE '(analyze|statistic|compute_stats)'
            """
        )
    if analyze_stmts == 0 and po_stats == 0:
        return Result(
            "PE-02-16", "", "", "", STATUS_OPEN,
            f"No statistics-collection operations in {ctx.scope.label} and no ANALYZE "
            f"TABLE statements in this region in the last {ctx.lookback_days} days, so the "
            "optimizer may be planning without "
            "table statistics.",
            {"analyze_statements": 0, "po_statistics_operations": 0},
        )
    status = STATUS_COMPLETED if po_stats > 0 else STATUS_IN_PROGRESS
    return Result(
        "PE-02-16", "", "", "", status,
        f"Statistics are being collected: {po_stats:,} automated statistics operation(s) "
        f"in {ctx.scope.label} and {analyze_stmts:,} ANALYZE TABLE statement(s) run in "
        f"this region (any catalog) in the last {ctx.lookback_days} days. "
        + (
            "Predictive Optimization maintains statistics automatically."
            if po_stats
            else "Only manual ANALYZE was seen; automate it or enable Predictive "
            "Optimization so statistics do not go stale."
        ),
        {"analyze_statements": analyze_stmts, "po_statistics_operations": po_stats},
    )


# ----------------------------------------------------------------------------------
# Run performance testing
# ----------------------------------------------------------------------------------


def pe_03_01(ctx: Ctx) -> Result:
    """Test on data representative of production data."""
    return manual_result(
        "PE-03-01",
        "whether test data is representative of production volumes and distributions is "
        "a property of the testing process, not something telemetry can confirm.",
        "how test/staging datasets are produced, and how their volume and cardinality "
        "compare to production.",
    )


def pe_03_02(ctx: Ctx) -> Result:
    """Take prewarming of resources into account."""
    if (bad := _has_query_history(ctx, "PE-03-02")) is not None:
        return bad
    row = ctx.one(
        f"""
        SELECT count(*) AS total,
               sum(coalesce(waiting_for_compute_duration_ms, 0)) AS wait_ms,
               sum(coalesce(total_duration_ms, 0)) AS total_ms,
               count_if(coalesce(waiting_for_compute_duration_ms, 0) > 10000) AS cold_starts
        FROM system.query.history
        WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
          AND execution_status = 'FINISHED'
        """
    )
    total = int(row.get("total") or 0)
    if total == 0:
        return Result(
            "PE-03-02", "", "", "", STATUS_OPEN,
            f"No completed queries in the last {ctx.lookback_days} days, so start-up "
            "behaviour cannot be assessed.",
        )
    cold = int(row.get("cold_starts") or 0)
    warm = total - cold
    total_ms = float(row.get("total_ms") or 0)
    wait_ms = float(row.get("wait_ms") or 0)
    return ratio_result(
        "PE-03-02",
        warm,
        total,
        "queries start on already-warm compute (under 10s waiting for compute)",
        complete_at=ctx.complete_at,
        extra=(
            f"Compute wait accounts for {wait_ms / total_ms * 100:.1f}% of total query "
            f"time; {cold:,} query/queries waited over 10s. Serverless compute or a "
            "warmed warehouse removes most of this."
            if total_ms > 0
            else f"{cold:,} query/queries waited over 10s for compute."
        ),
        metrics={"cold_start_queries": cold, "wait_ms": wait_ms, "total_ms": total_ms},
    )


def pe_03_03(ctx: Ctx) -> Result:
    """Identify bottlenecks."""
    if (bad := _has_query_history(ctx, "PE-03-03")) is not None:
        return bad
    # A single query can hit several of these at once, so the per-category counts
    # overlap. "unhealthy" is the distinct count of queries tripping ANY bottleneck
    # (the union); taking max() of the categories would ignore problems outside the
    # largest one and overstate health, while summing would double-count overlaps.
    row = ctx.one(
        f"""
        SELECT count(*) AS total,
               count_if(coalesce(spilled_local_bytes, 0) > 0) AS spilling,
               count_if(coalesce(shuffle_read_bytes, 0) > coalesce(read_bytes, 0)
                        AND coalesce(read_bytes, 0) > 0) AS shuffle_heavy,
               count_if(coalesce(waiting_at_capacity_duration_ms, 0) > 0) AS queued,
               count_if(coalesce(spilled_local_bytes, 0) > 0
                        OR (coalesce(shuffle_read_bytes, 0) > coalesce(read_bytes, 0)
                            AND coalesce(read_bytes, 0) > 0)
                        OR coalesce(waiting_at_capacity_duration_ms, 0) > 0) AS unhealthy
        FROM system.query.history
        WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
          AND execution_status = 'FINISHED'
        """
    )
    total = int(row.get("total") or 0)
    if total == 0:
        return Result(
            "PE-03-03", "", "", "", STATUS_OPEN,
            f"No completed queries in the last {ctx.lookback_days} days, so bottlenecks "
            "cannot be identified.",
        )
    spilling = int(row.get("spilling") or 0)
    shuffle_heavy = int(row.get("shuffle_heavy") or 0)
    queued = int(row.get("queued") or 0)
    unhealthy = int(row.get("unhealthy") or 0)
    healthy = total - unhealthy
    issues = []
    if spilling:
        issues.append(f"{spilling:,} spilling to disk (memory pressure)")
    if shuffle_heavy:
        issues.append(f"{shuffle_heavy:,} shuffling more than they read (data movement)")
    if queued:
        issues.append(f"{queued:,} queued at capacity (concurrency limit)")
    return ratio_result(
        "PE-03-03",
        max(healthy, 0),
        total,
        "completed queries show no spill, excessive shuffle, or capacity queuing",
        complete_at=ctx.complete_at,
        extra=(
            "Bottleneck signals: " + "; ".join(issues) + "."
            if issues
            else "No bottleneck signals detected in query telemetry."
        ),
        metrics={"queries": total, "unhealthy": unhealthy, "spilling": spilling,
                 "shuffle_heavy": shuffle_heavy, "queued": queued},
    )


# ----------------------------------------------------------------------------------
# Monitor performance
# ----------------------------------------------------------------------------------


def pe_04_01(ctx: Ctx) -> Result:
    """Monitor query performance."""
    if not ctx.has_table("system.query.history"):
        return Result(
            "PE-04-01", "", "", "", STATUS_OPEN,
            "system.query.history is not readable, so query monitoring is not possible.",
        )
    # Active monitoring is a recurring practice, not a single ad-hoc query. Score the
    # share of weeks that had at least one query against system.query.history.
    total_weeks = max(1, -(-ctx.lookback_days // 7))  # ceil(days / 7)
    row = ctx.one(
        f"""
        SELECT count(DISTINCT date_trunc('WEEK', start_time)) AS active_weeks,
               count(*) AS queries,
               count(DISTINCT executed_by) AS watchers
        FROM system.query.history
        WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
          AND lower(statement_text) LIKE '%system.query.history%'
        """
    )
    active_weeks = int(row.get("active_weeks") or 0)
    queries = int(row.get("queries") or 0)
    watchers = int(row.get("watchers") or 0)
    return ratio_result(
        "PE-04-01",
        active_weeks,
        total_weeks,
        f"weeks in the last {ctx.lookback_days} days had query-performance monitoring (a "
        "query against system.query.history), i.e. monitoring is a recurring practice "
        "rather than a one-off",
        complete_at=ctx.complete_at,
        extra=f"{queries:,} monitoring query/queries by {watchers} distinct principal(s).",
        metrics={"active_weeks": active_weeks, "total_weeks": total_weeks,
                 "monitoring_queries": queries, "watchers": watchers},
    )


def pe_04_02(ctx: Ctx) -> Result:
    """Monitor streaming workloads."""
    streaming = 0
    if ctx.has_table("system.lakeflow.pipelines"):
        streaming = ctx.count_safe(
            inventory.live_count_sql("pipelines", "settings", "settings.continuous = true")
        )
    updates = 0
    if ctx.has_table("system.lakeflow.pipeline_update_timeline"):
        updates = ctx.count_safe(
            f"""
            SELECT count(DISTINCT workspace_id, update_id) AS n
            FROM system.lakeflow.pipeline_update_timeline
            WHERE period_start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
            """
        )
    if streaming == 0 and updates == 0:
        return Result(
            "PE-04-02", "", "", "", STATUS_OPEN,
            f"No continuous pipelines and no pipeline updates in the last "
            f"{ctx.lookback_days} days, so there are no streaming workloads to monitor. "
            "Mark ignore in the WAF tool if streaming is out of scope.",
            {"continuous_pipelines": 0, "pipeline_updates": 0},
        )
    status = STATUS_COMPLETED if updates > 0 else STATUS_IN_PROGRESS
    return Result(
        "PE-04-02", "", "", "", status,
        f"Streaming telemetry is available: {streaming} continuous pipeline(s) and "
        f"{updates:,} pipeline update(s) in the last {ctx.lookback_days} days. "
        "Confirm backlog and throughput metrics are alerted on, not just recorded.",
        {"continuous_pipelines": streaming, "pipeline_updates": updates},
    )


def pe_04_03(ctx: Ctx) -> Result:
    """Monitor job performance."""
    if not ctx.has_table("system.lakeflow.job_run_timeline"):
        return Result(
            "PE-04-03", "", "", "", STATUS_OPEN,
            "system.lakeflow.job_run_timeline is not readable, so job performance "
            "cannot be monitored.",
        )
    row = ctx.one(
        f"""
        SELECT count(*) AS runs,
               count(DISTINCT job_id) AS jobs,
               avg(coalesce(run_duration_seconds, 0)) AS avg_secs,
               avg(coalesce(queue_duration_seconds, 0)) AS avg_queue
        FROM system.lakeflow.job_run_timeline
        WHERE period_start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
          AND result_state IS NOT NULL
        """
    )
    runs = int(row.get("runs") or 0)
    if runs == 0:
        return Result(
            "PE-04-03", "", "", "", STATUS_OPEN,
            f"No job runs in the last {ctx.lookback_days} days, so job performance "
            "cannot be assessed.",
        )
    total_jobs = ctx.count_safe(
        inventory.live_count_sql("jobs")
    )
    with_rules = ctx.count_safe(
        f"""
        WITH {inventory.live_latest_cte("jobs", "job_id, health_rules")}
        SELECT count(*) AS n FROM latest
        WHERE rn = 1 AND health_rules IS NOT NULL
          AND lower(to_json(health_rules)) NOT IN ('null', '{{}}', '[]')
        """
    )
    return ratio_result(
        "PE-04-03",
        with_rules,
        total_jobs,
        "active jobs declare health rules (for example a duration threshold) so slow "
        "runs raise an alert rather than going unnoticed",
        complete_at=ctx.complete_at,
        none_found=(
            f"{runs:,} job run(s) are recorded but no active jobs were found to carry "
            "health rules."
        ),
        extra=(
            f"Observed {runs:,} run(s) across {int(row.get('jobs') or 0)} job(s), averaging "
            f"{float(row.get('avg_secs') or 0):,.0f}s runtime and "
            f"{float(row.get('avg_queue') or 0):,.0f}s queued."
        ),
        metrics={"runs": runs, "jobs_with_health_rules": with_rules},
    )


CHECKS = [
    ("PE-01-01", pe_01_01),
    ("PE-01-02", pe_01_02),
    ("PE-02-01", pe_02_01),
    ("PE-02-02", pe_02_02),
    ("PE-02-03", pe_02_03),
    ("PE-02-04", pe_02_04),
    ("PE-02-05", pe_02_05),
    ("PE-02-06", pe_02_06),
    ("PE-02-07", pe_02_07),
    ("PE-02-08", pe_02_08),
    ("PE-02-09", pe_02_09),
    ("PE-02-10", pe_02_10),
    ("PE-02-11", pe_02_11),
    ("PE-02-12", pe_02_12),
    # PE-02-13 does not exist in the published question bank.
    ("PE-02-14", pe_02_14),
    ("PE-02-15", pe_02_15),
    ("PE-02-16", pe_02_16),
    ("PE-03-01", pe_03_01),
    ("PE-03-02", pe_03_02),
    ("PE-03-03", pe_03_03),
    ("PE-04-01", pe_04_01),
    ("PE-04-02", pe_04_02),
    ("PE-04-03", pe_04_03),
]
