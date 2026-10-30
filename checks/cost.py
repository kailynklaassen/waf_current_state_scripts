"""Cost Optimization pillar checks (CO-*).

Evidence sources: ``system.billing.usage``, ``system.billing.list_prices``,
``system.compute.clusters``, ``system.compute.warehouses``, ``system.query.history``,
and the Workspace SDK for cluster policies.
"""

from __future__ import annotations

from waf_core import (
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    STATUS_MANUAL,
    STATUS_OPEN,
    Ctx,
    Result,
    fmt_pct,
    manual_result,
    ratio_result,
    ratio_status,
)

from checks import cloud, inventory

PILLAR_ID = "cost-optimization"

#: CO-01-04 reference: a cluster is "up to date" if its DBR major is within this many
#: versions of the newest major actually seen in the workspace fleet. Self-calibrating
#: so it never goes stale; override with an absolute bar via cfg["current_dbr_major"].
DEFAULT_DBR_MAJOR_TOLERANCE = 2

#: CO-03-03 chargeback tag-key tokens (RLIKE, case-insensitive). A billing record is
#: "attributable" if any custom tag key matches one of these. Override in 00_config
#: (cfg["chargeback_tag_patterns"]) to match the tag taxonomy an environment uses.
DEFAULT_CHARGEBACK_TAG_PATTERNS = [
    "cost", "team", "project", "owner", "business", "dept", "department", "bu",
    "chargeback", "env",
]


# ----------------------------------------------------------------------------------
# Choose optimal resources
# ----------------------------------------------------------------------------------


def co_01_01(ctx: Ctx) -> Result:
    """Use performance optimized data formats."""
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
    return ratio_result(
        "CO-01-01",
        delta,
        total,
        f"{scope.label} tables use Delta, which enables file skipping, Z-order and "
        "predictive optimization to cut scan cost",
        complete_at=ctx.complete_at,
        none_found="No tables found in the assessed catalogs.",
        metrics={"scoped": True},
    )


def co_01_02(ctx: Ctx) -> Result:
    """Use job clusters (not all-purpose) for automated workloads.

    Binary by design: any job running on all-purpose compute is a cost anti-pattern
    (all-purpose lists at ~2-4x the job-compute rate), so there is no partial credit -
    any such job -> open with the count; none -> completed.

    Measured from task runs: a task whose compute is a UI- or API-created cluster ran on
    all-purpose compute. Billing cannot show this - it does not record the job on
    all-purpose usage, so a billing-based count is always zero.
    """
    if not (ctx.has_table("system.lakeflow.job_task_run_timeline")
            and ctx.has_table("system.compute.clusters")):
        return Result(
            "CO-01-02", "", "", "", STATUS_OPEN,
            "system.lakeflow.job_task_run_timeline or system.compute.clusters is not "
            "readable, so job compute type cannot be assessed.",
        )
    row = ctx.one(
        f"""
        WITH tasks AS (
          SELECT workspace_id, job_id, job_run_id, explode(compute_ids) AS cluster_id
          FROM system.lakeflow.job_task_run_timeline
          WHERE period_start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
        ),
        all_purpose AS (
          SELECT DISTINCT workspace_id, cluster_id FROM system.compute.clusters
          WHERE upper(coalesce(cluster_source, '')) IN ('UI', 'API')
        )
        SELECT count(DISTINCT t.workspace_id, t.job_id) AS jobs_on_ap,
               count(DISTINCT t.workspace_id, t.job_run_id) AS runs_on_ap,
               (SELECT count(DISTINCT workspace_id, job_id) FROM tasks) AS jobs_ran
        FROM tasks t
        JOIN all_purpose a ON t.workspace_id = a.workspace_id AND t.cluster_id = a.cluster_id
        """
    )
    jobs_on_ap = int(row.get("jobs_on_ap") or 0)
    runs_on_ap = int(row.get("runs_on_ap") or 0)
    jobs_ran = int(row.get("jobs_ran") or 0)
    if jobs_on_ap == 0:
        return Result(
            "CO-01-02", "", "", "", STATUS_COMPLETED,
            f"No jobs ran on all-purpose compute in the last {ctx.lookback_days} days "
            f"({jobs_ran:,} job(s) ran); automated workloads use job or serverless compute.",
            {"jobs_on_all_purpose": 0, "jobs_ran": jobs_ran},
        )
    return Result(
        "CO-01-02", "", "", "", STATUS_OPEN,
        f"{jobs_on_ap:,} of {jobs_ran:,} job(s) that ran in the last {ctx.lookback_days} "
        f"days ran tasks on all-purpose compute ({runs_on_ap:,} run(s)). All-purpose "
        "lists at roughly 2-4x the job-compute rate - move these scheduled workloads "
        "onto job clusters or serverless.",
        {"jobs_on_all_purpose": jobs_on_ap, "runs_on_all_purpose": runs_on_ap,
         "jobs_ran": jobs_ran},
    )


def co_01_03(ctx: Ctx) -> Result:
    """Use SQL warehouse for SQL workloads."""
    if not ctx.has_table("system.query.history"):
        return Result(
            "CO-01-03", "", "", "", STATUS_OPEN,
            "system.query.history is not readable, so SQL routing cannot be assessed.",
        )
    row = ctx.one(
        f"""
        SELECT count(*) AS total,
               count_if(lower(coalesce(compute.type, '')) LIKE '%warehouse%') AS on_warehouse
        FROM system.query.history
        WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
          AND statement_type = 'SELECT'
        """
    )
    total = int(row.get("total") or 0)
    on_wh = int(row.get("on_warehouse") or 0)
    return ratio_result(
        "CO-01-03",
        on_wh,
        total,
        f"SELECT statements in the last {ctx.lookback_days} days run on a SQL warehouse "
        "rather than general-purpose compute",
        complete_at=ctx.complete_at,
        none_found=(
            f"No SELECT statements recorded in the last {ctx.lookback_days} days, so SQL "
            "routing cannot be assessed."
        ),
        extra="SQL warehouses are Photon-enabled and priced for BI concurrency.",
    )


def co_01_04(ctx: Ctx) -> Result:
    """Use up-to-date runtimes for your workloads."""
    if not ctx.has_table("system.compute.clusters"):
        return Result(
            "CO-01-04", "", "", "", STATUS_OPEN,
            "system.compute.clusters is not readable, so runtime versions cannot be "
            "assessed.",
        )
    # Pipelines are excluded: Databricks manages their runtime through the pipeline
    # channel (reported as "dlt:<version>"), so there is no DBR choice to score.
    rows = ctx.sql(
        f"""
        {inventory.workloads_cte(ctx, 'cluster_id, dbr_version')}
        SELECT upper(coalesce(cluster_source, '')) LIKE 'PIPELINE%' AS pipeline,
               try_cast(regexp_extract(coalesce(dbr_version, ''), '^([0-9]+)', 1) AS INT) AS major,
               count(*) AS n
        FROM latest WHERE rn = 1
        GROUP BY 1, 2
        """
    )
    pipelines = sum(int(r["n"]) for r in rows if r["pipeline"])
    by_major: dict[int, int] = {}
    for r in rows:
        if not r["pipeline"] and r["major"] is not None:
            by_major[int(r["major"])] = by_major.get(int(r["major"]), 0) + int(r["n"])
    total = sum(by_major.values())
    unit = (f"classic compute workloads (jobs and all-purpose clusters, each counted "
            f"once) that ran {inventory.window_text(ctx)}")
    pipeline_note = (f" {pipelines:,} pipeline workload(s) are excluded because Databricks "
                     "manages their runtime through the pipeline channel.")
    if total == 0:
        return Result(
            "CO-01-04", "", "", "", STATUS_COMPLETED,
            f"No classic clusters with a parseable runtime active in the last "
            f"{ctx.lookback_days} days; serverless compute is always on a current, "
            "Databricks-managed runtime." + (pipeline_note if pipelines else ""),
            {"clusters": 0, "pipeline_workloads_excluded": pipelines},
        )
    newest = max(by_major)
    oldest = min(by_major)
    # Reference "current" major: an explicit config bar, else self-calibrated to the
    # newest major in the fleet minus a tolerance (so it never goes stale).
    configured = ctx.cfg.get("current_dbr_major")
    threshold = int(configured) if configured else max(newest - DEFAULT_DBR_MAJOR_TOLERANCE, 1)
    current = sum(n for major, n in by_major.items() if major >= threshold)
    ref = (
        f"configured bar DBR {threshold}.x"
        if configured
        else f"DBR {threshold}.x or newer (within {DEFAULT_DBR_MAJOR_TOLERANCE} major "
             f"version(s) of the fleet's newest, DBR {newest}.x)"
    )
    # Context: share of classic-compute DBUs on a current runtime (spend-weighted view).
    dbu_note = ""
    if ctx.has_table("system.billing.usage"):
        try:
            drows = ctx.sql(
                f"""
                WITH c AS (
                  SELECT cluster_id,
                         try_cast(regexp_extract(coalesce(dbr_version, ''), '^([0-9]+)', 1) AS INT) AS major,
                         row_number() OVER (PARTITION BY cluster_id ORDER BY change_time DESC) AS rn
                  FROM system.compute.clusters
                  WHERE change_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
                )
                SELECT c.major, sum(u.usage_quantity) AS dbu
                FROM system.billing.usage u
                JOIN c ON u.usage_metadata.cluster_id = c.cluster_id AND c.rn = 1
                WHERE {inventory.billing_scope(ctx, "u.workspace_id")} AND u.usage_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
                GROUP BY c.major
                """
            )
            dbu_total = sum(float(r["dbu"] or 0) for r in drows if r.get("major") is not None)
            dbu_current = sum(float(r["dbu"] or 0) for r in drows
                              if r.get("major") is not None and int(r["major"]) >= threshold)
            if dbu_total > 0:
                dbu_note = (f" Spend-weighted: {100 * dbu_current / dbu_total:.1f}% of classic "
                            f"compute DBUs ({dbu_current:,.0f} of {dbu_total:,.0f}) ran on DBR "
                            f"{threshold}.x or newer.")
        except Exception:
            dbu_note = ""
    return ratio_result(
        "CO-01-04",
        current,
        total,
        f"{unit} run {ref}",
        complete_at=ctx.complete_at,
        extra=(
            f"{total - current:,} of {total:,} workloads (jobs, all-purpose clusters) "
            f"that ran {inventory.window_text(ctx)} use an outdated runtime "
            f"(DBR {threshold - 1}.x or older). Runtime spread: newest DBR {newest}.x, "
            f"oldest DBR {oldest}.x."
            + (pipeline_note if pipelines else "")
            + dbu_note
            + " Newer runtimes deliver free performance (and therefore cost) improvements."
        ),
        metrics={"oldest_dbr_major": oldest, "newest_dbr_major": newest,
                 "threshold_major": threshold, "threshold_configured": bool(configured),
                 "pipeline_workloads_excluded": pipelines},
    )


def co_01_05(ctx: Ctx) -> Result:
    """Only use GPUs for the right workloads.

    GPU usage is identified from the GPU node families for CLOUD_PROVIDER
    (checks/cloud.py) in system.billing.usage. If there is no GPU spend, there is nothing to misuse ->
    completed. If GPUs ARE in use, whether each workload is the *right* use (deep
    learning / LLM training or inference) cannot be confirmed from telemetry: MLflow
    linkage covers <2% of GPU spend, and GPU-on-a-job is frequently legitimate ML
    training - so this is surfaced as manual-review with the usage facts as evidence,
    rather than scored off an unreliable proxy.
    """
    if not ctx.has_table("system.billing.usage"):
        return Result(
            "CO-01-05", "", "", "", STATUS_OPEN,
            "system.billing.usage is not readable, so GPU spend cannot be assessed.",
        )
    row = ctx.one(
        f"""
        SELECT round(sum(usage_quantity), 2) AS gpu_dbu,
               round(sum(CASE WHEN usage_metadata.job_id IS NOT NULL
                               OR usage_metadata.dlt_pipeline_id IS NOT NULL
                              THEN usage_quantity ELSE 0 END), 2) AS gpu_automated,
               count(DISTINCT workspace_id, usage_metadata.job_id) AS gpu_jobs
        FROM system.billing.usage
        WHERE {inventory.billing_scope(ctx, "workspace_id")} AND usage_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
          AND lower(coalesce(usage_metadata.node_type, '')) RLIKE '{cloud.gpu_regex(ctx)}'
        """
    )
    gpu = float(row.get("gpu_dbu") or 0)
    automated = float(row.get("gpu_automated") or 0)
    gpu_jobs = int(row.get("gpu_jobs") or 0)
    if gpu <= 0:
        return Result(
            "CO-01-05", "", "", "", STATUS_COMPLETED,
            f"No GPU DBUs consumed in {inventory.region_label(ctx)} "
            f"in the last {ctx.lookback_days} days ({ctx.cloud.upper()} "
            "GPU instance families), so there is no GPU spend to place on the wrong "
            "workload.",
            {"gpu_dbus": 0.0, "cloud": ctx.cloud},
        )
    interactive = max(gpu - automated, 0)
    return Result(
        "CO-01-05", "", "", "", STATUS_MANUAL,
        f"GPU compute is in use ({gpu:,.0f} DBU in {inventory.region_label(ctx)} "
        f"in the last {ctx.lookback_days} days: "
        f"{automated:,.0f} on jobs/pipelines across {gpu_jobs} job(s), {interactive:,.0f} "
        "interactive/other). Whether each GPU workload is the right use (deep learning / "
        "LLM training or inference) cannot be confirmed from system tables - MLflow "
        "linkage covers under 2% of GPU spend and GPU-on-a-job is often legitimate ML "
        "training. Review the GPU workloads with their owners to confirm they are ML/DL, "
        "not general compute that belongs on CPU.",
        {"gpu_dbus": round(gpu, 2), "gpu_automated_dbus": round(automated, 2),
         "gpu_interactive_dbus": round(interactive, 2), "gpu_jobs": gpu_jobs,
         "automatable": False},
    )


def co_01_06(ctx: Ctx) -> Result:
    """Use Serverless for your workloads."""
    if not ctx.has_table("system.billing.usage"):
        return Result(
            "CO-01-06", "", "", "", STATUS_OPEN,
            "system.billing.usage is not readable, so serverless adoption cannot be "
            "measured.",
        )
    row = ctx.one(
        f"""
        SELECT
          sum(usage_quantity) AS total,
          sum(CASE WHEN coalesce(product_features.is_serverless, lower(coalesce(sku_name, '')) LIKE '%serverless%')
                   THEN usage_quantity ELSE 0 END) AS serverless
        FROM system.billing.usage
        WHERE {inventory.billing_scope(ctx, "workspace_id")} AND usage_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
        """
    )
    total = float(row.get("total") or 0)
    serverless = float(row.get("serverless") or 0)
    if total <= 0:
        return Result(
            "CO-01-06", "", "", "", STATUS_OPEN,
            f"No billable usage recorded in the last {ctx.lookback_days} days.",
        )
    return ratio_result(
        "CO-01-06",
        int(round(serverless)),
        int(round(total)),
        f"DBUs in {inventory.region_label(ctx)} "
        f"in the last {ctx.lookback_days} days run on serverless compute",
        complete_at=ctx.complete_at,
        extra="Serverless removes idle time and start-up waste from the bill.",
        metrics={"serverless_dbus": round(serverless, 2), "total_dbus": round(total, 2)},
    )


def co_01_07(ctx: Ctx) -> Result:
    """Use the right instance type."""
    if not ctx.has_table("system.compute.clusters"):
        return Result(
            "CO-01-07", "", "", "", STATUS_OPEN,
            "system.compute.clusters is not readable, so instance types cannot be "
            "assessed.",
        )
    rows = ctx.sql(
        f"""
        {inventory.workloads_cte(ctx, 'cluster_id, worker_node_type, driver_node_type')}
        SELECT coalesce(worker_node_type, driver_node_type) AS node_type, count(*) AS n
        FROM latest WHERE rn = 1 AND coalesce(worker_node_type, driver_node_type) IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC
        """
    )
    if not rows:
        return Result(
            "CO-01-07", "", "", "", STATUS_COMPLETED,
            f"No classic clusters active in the last {ctx.lookback_days} days; serverless "
            "compute selects instance types automatically.",
            {"clusters": 0},
        )
    # Latest-generation instances are materially cheaper per unit of work. GPUs are
    # left out: their generations follow the accelerator, and CO-01-05 covers them.
    cpu = [r for r in rows if not cloud.is_gpu(ctx, r["node_type"])]
    gpu_workloads = sum(int(r["n"]) for r in rows) - sum(int(r["n"]) for r in cpu)
    total = sum(int(r["n"]) for r in cpu)
    modern = sum(int(r["n"]) for r in cpu if cloud.is_current_generation(ctx, r["node_type"]))
    mix = ", ".join(f"{r['node_type']}={r['n']}" for r in rows[:8])
    return ratio_result(
        "CO-01-07",
        modern,
        total,
        f"{inventory.workload_unit(ctx)} on CPU instances use a current-generation "
        "instance type",
        complete_at=ctx.complete_at,
        extra=(
            f"Current generation = {cloud.generation_rule(ctx)}. "
            f"{gpu_workloads} GPU workload(s) are excluded (see CO-01-05). "
            f"Top instance types: {mix}. Right-sizing means matching memory/compute/IO "
            "to the workload, which needs a per-job review."
        ),
        metrics={"node_type_mix": {r["node_type"]: r["n"] for r in rows},
                 "gpu_workloads": gpu_workloads, "cloud": ctx.cloud},
    )


def co_01_08(ctx: Ctx) -> Result:
    """Choose the most efficient cluster size."""
    if not ctx.has_table("system.compute.node_timeline"):
        return Result(
            "CO-01-08", "", "", "", STATUS_OPEN,
            "system.compute.node_timeline is not readable, so cluster utilization "
            "cannot be assessed.",
        )
    row = ctx.one(
        """
        SELECT count(DISTINCT cluster_id) AS clusters,
               avg(cpu_user_percent + cpu_system_percent) AS avg_cpu,
               avg(mem_used_percent) AS avg_mem
        FROM system.compute.node_timeline
        WHERE start_time >= current_timestamp() - INTERVAL 30 DAYS
        """
    )
    clusters = int(row.get("clusters") or 0)
    if clusters == 0:
        return Result(
            "CO-01-08", "", "", "", STATUS_OPEN,
            "No node utilization telemetry in the last 30 days, so cluster sizing "
            "efficiency cannot be measured.",
        )
    avg_cpu = float(row.get("avg_cpu") or 0)
    avg_mem = float(row.get("avg_mem") or 0)
    # Sustained low CPU means over-provisioning; very high means starvation.
    if avg_cpu < 20:
        status = STATUS_IN_PROGRESS
        verdict = (
            "Average CPU below 20% indicates over-provisioned clusters paying for idle "
            "cores; reduce worker counts or move to serverless autoscaling."
        )
    elif avg_cpu > 85:
        status = STATUS_IN_PROGRESS
        verdict = (
            "Average CPU above 85% indicates undersized clusters, which lengthens "
            "runtime and can raise total cost."
        )
    else:
        status = STATUS_COMPLETED
        verdict = "Average CPU is in the healthy 20-85% band, indicating reasonable sizing."
    return Result(
        "CO-01-08", "", "", "", status,
        f"Across {clusters} cluster(s) in the last 30 days, average CPU utilization is "
        f"{avg_cpu:.1f}% and average memory {avg_mem:.1f}%. {verdict}",
        {"clusters": clusters, "avg_cpu_pct": round(avg_cpu, 2),
         "avg_mem_pct": round(avg_mem, 2)},
    )


def co_01_09(ctx: Ctx) -> Result:
    """Evaluate performance optimized query engines (Photon)."""
    if not ctx.has_table("system.billing.usage"):
        return Result(
            "CO-01-09", "", "", "", STATUS_OPEN,
            "system.billing.usage is not readable, so Photon adoption cannot be "
            "measured.",
        )
    # Score the authoritative Photon flag, not a sku_name string match, and don't lump
    # SQL-warehouse DBU in as if it were the same engine - report it separately.
    row = ctx.one(
        f"""
        SELECT
          sum(usage_quantity) AS total,
          sum(CASE WHEN product_features.is_photon = true THEN usage_quantity ELSE 0 END) AS photon,
          sum(CASE WHEN upper(coalesce(billing_origin_product, '')) IN ('SQL', 'DATABRICKS_SQL')
                   THEN usage_quantity ELSE 0 END) AS sql_warehouse
        FROM system.billing.usage
        WHERE {inventory.billing_scope(ctx, "workspace_id")} AND usage_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
        """
    )
    total = float(row.get("total") or 0)
    if total <= 0:
        return Result(
            "CO-01-09", "", "", "", STATUS_OPEN,
            f"No billable usage recorded in the last {ctx.lookback_days} days.",
        )
    photon = float(row.get("photon") or 0)
    sql_wh = float(row.get("sql_warehouse") or 0)
    return ratio_result(
        "CO-01-09",
        int(round(photon)),
        int(round(total)),
        f"DBUs in {inventory.region_label(ctx)} "
        f"in the last {ctx.lookback_days} days run on the Photon vectorized engine "
        "(product_features.is_photon)",
        complete_at=ctx.complete_at,
        extra=(
            f"SQL warehouse DBU (Photon-backed BI, counted separately): {sql_wh:,.0f}. "
            "Photon costs more per DBU but usually lowers total cost by finishing sooner; "
            "validate per workload."
        ),
        metrics={"photon_dbus": round(photon, 2), "sql_warehouse_dbus": round(sql_wh, 2),
                 "total_dbus": round(total, 2)},
    )


# ----------------------------------------------------------------------------------
# Dynamically allocate resources
# ----------------------------------------------------------------------------------


def co_02_01(ctx: Ctx) -> Result:
    """Leverage auto-scaling compute."""
    if not ctx.has_table("system.compute.clusters"):
        return Result(
            "CO-02-01", "", "", "", STATUS_OPEN,
            "system.compute.clusters is not readable, so autoscaling cannot be assessed.",
        )
    row = ctx.one(
        f"""
        {inventory.workloads_cte(ctx, 'cluster_id, min_autoscale_workers, max_autoscale_workers')}
        SELECT count(*) AS total,
               count_if(max_autoscale_workers IS NOT NULL
                        AND max_autoscale_workers > coalesce(min_autoscale_workers, 0)) AS autoscaling
        FROM latest WHERE rn = 1
        """
    )
    total = int(row.get("total") or 0)
    autoscaling = int(row.get("autoscaling") or 0)
    wh_scaling = 0
    wh_total = 0
    if ctx.has_table("system.compute.warehouses"):
        wrow = ctx.one(
            """
            WITH latest AS (
              SELECT warehouse_id, delete_time, min_clusters, max_clusters,
                     row_number() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) AS rn
              FROM system.compute.warehouses
            )
            SELECT count(*) AS total, count_if(max_clusters > coalesce(min_clusters, 1)) AS scaling
            FROM latest WHERE rn = 1 AND delete_time IS NULL
            """
        )
        wh_total = int(wrow.get("total") or 0)
        wh_scaling = int(wrow.get("scaling") or 0)

    combined_total = total + wh_total
    combined_scaling = autoscaling + wh_scaling
    if combined_total == 0:
        return Result(
            "CO-02-01", "", "", "", STATUS_OPEN,
            f"No clusters or warehouses active in the last {ctx.lookback_days} days, so "
            "autoscaling cannot be verified.",
        )
    return ratio_result(
        "CO-02-01",
        combined_scaling,
        combined_total,
        "compute resources (classic compute workloads - jobs, pipelines and all-purpose "
        f"clusters, each counted once - that ran {inventory.window_text(ctx)}, plus SQL "
        "warehouses) are configured to scale elastically with load",
        complete_at=ctx.complete_at,
        extra=(
            f"Cluster workloads: {autoscaling}/{total} autoscaling. "
            f"Warehouses: {wh_scaling}/{wh_total} multi-cluster."
        ),
        metrics={"clusters_autoscaling": autoscaling, "clusters_total": total,
                 "warehouses_scaling": wh_scaling, "warehouses_total": wh_total},
    )


def co_02_02(ctx: Ctx) -> Result:
    """Use auto termination."""
    if not ctx.has_table("system.compute.clusters"):
        return Result(
            "CO-02-02", "", "", "", STATUS_OPEN,
            "system.compute.clusters is not readable, so auto-termination cannot be "
            "assessed.",
        )
    row = ctx.one(
        f"""
        {inventory.workloads_cte(ctx, 'cluster_id, auto_termination_minutes, cluster_source')}
        SELECT count(*) AS total,
               count_if(auto_termination_minutes IS NOT NULL
                        AND auto_termination_minutes > 0) AS terminating,
               count_if(auto_termination_minutes > 120) AS generous
        FROM latest WHERE rn = 1
          AND lower(coalesce(cluster_source, '')) NOT IN ('job', 'pipeline')
        """
    )
    total = int(row.get("total") or 0)
    terminating = int(row.get("terminating") or 0)
    generous = int(row.get("generous") or 0)
    wh_total = 0
    wh_stop = 0
    if ctx.has_table("system.compute.warehouses"):
        wrow = ctx.one(
            """
            WITH latest AS (
              SELECT warehouse_id, delete_time, auto_stop_minutes,
                     row_number() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) AS rn
              FROM system.compute.warehouses
            )
            SELECT count(*) AS total,
                   count_if(auto_stop_minutes IS NOT NULL AND auto_stop_minutes > 0) AS stopping
            FROM latest WHERE rn = 1 AND delete_time IS NULL
            """
        )
        wh_total = int(wrow.get("total") or 0)
        wh_stop = int(wrow.get("stopping") or 0)

    combined_total = total + wh_total
    if combined_total == 0:
        return Result(
            "CO-02-02", "", "", "", STATUS_OPEN,
            f"No interactive clusters or warehouses active in the last "
            f"{ctx.lookback_days} days, so auto-termination cannot be verified.",
        )
    return ratio_result(
        "CO-02-02",
        terminating + wh_stop,
        combined_total,
        f"all-purpose clusters that ran {inventory.window_text(ctx)} and current SQL "
        "warehouses have auto-termination / auto-stop set",
        complete_at=ctx.complete_at,
        extra=(
            f"All-purpose clusters: {terminating}/{total} terminate automatically"
            + (
                f", of which {generous} wait longer than 120 minutes."
                if generous
                else "."
            )
            + f" Warehouses: {wh_stop}/{wh_total} auto-stop."
        ),
        metrics={"clusters_with_autotermination": terminating,
                 "clusters_over_120min": generous,
                 "warehouses_with_autostop": wh_stop},
    )


def co_02_03(ctx: Ctx) -> Result:
    """Use compute policies to control costs.

    Scored as two equal halves, so defining policies and actually enforcing them each
    count for 50%:

    * **Policies exist (50%)** - at least one cluster policy is defined.
    * **Enforcement (50%)** - the share of compute workloads created under a policy.

    Blended score = 0.5 * (policies exist) + 0.5 * (governed / total clusters), then the
    standard thresholds. Policies that exist but govern nothing cap at ~50%.
    """
    pol = inventory.policy_evidence(ctx)

    total = governed = 0
    if ctx.has_table("system.compute.clusters"):
        row = ctx.one(
            f"""
            {inventory.workloads_cte(ctx, 'cluster_id, policy_id')}
            SELECT count(*) AS total,
                   count_if(policy_id IS NOT NULL AND policy_id <> '') AS governed
            FROM latest WHERE rn = 1
            """
        )
        total = int(row.get("total") or 0)
        governed = int(row.get("governed") or 0)

    # Policies exist if any custom policy is registered or any active cluster cites one.
    if pol["exist"] is None and not ctx.has_table("system.compute.clusters"):
        return Result(
            "CO-02-03", "", "", "", STATUS_OPEN,
            "Neither the cluster policy inventory (audit log) nor cluster records are "
            "readable, so cost guardrails cannot be assessed.",
            {"policies": None},
        )
    have_policies = bool(pol["exist"]) or governed > 0
    policies_half = 1.0 if have_policies else 0.0
    enforce_half = (governed / total) if total > 0 else 0.0
    score = 0.5 * policies_half + 0.5 * enforce_half
    status, _ = ratio_status(round(score * 10_000), 10_000, ctx.complete_at)

    pol_txt = pol["text"]
    enforce_txt = (f"{governed:,} of {total:,} classic compute workloads (jobs, pipelines, "
                   f"all-purpose clusters) that ran {inventory.window_text(ctx)} run under a policy "
                   f"({enforce_half * 100:.1f}%)" if total > 0
                   else f"no clusters active in the last {ctx.lookback_days} days to "
                        "measure enforcement")
    reason = (
        f"Cost-guardrail score {score * 100:.1f}% - 50% policies exist "
        f"({pol_txt}) + 50% enforcement ({enforce_txt})."
    )
    return Result(
        "CO-02-03", "", "", "", status, reason,
        {**pol["metrics"], "governed_clusters": governed, "total_clusters": total,
         "policies_exist": have_policies, "enforcement_pct": round(enforce_half * 100, 2),
         "blended_pct": round(score * 100, 2)},
    )


# ----------------------------------------------------------------------------------
# Monitor and control cost
# ----------------------------------------------------------------------------------


def co_03_01(ctx: Ctx) -> Result:
    """Monitor costs."""
    if not ctx.has_table("system.billing.usage"):
        return Result(
            "CO-03-01", "", "", "", STATUS_OPEN,
            "system.billing.usage is not readable, so cost monitoring is not possible.",
        )
    days = ctx.count(
        f"""
        SELECT count(DISTINCT usage_date) AS n FROM system.billing.usage
        WHERE {inventory.billing_scope(ctx, "workspace_id")} AND usage_date > current_date() - INTERVAL {ctx.lookback_days} DAYS
        """
    )
    consumers = 0
    queries = 0
    if ctx.has_table("system.query.history"):
        row = ctx.one(
            f"""
            SELECT count(*) AS queries, count(DISTINCT executed_by) AS consumers
            FROM system.query.history
            WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND lower(statement_text) LIKE '%system.billing.usage%'
            """
        )
        queries = int(row.get("queries") or 0)
        consumers = int(row.get("consumers") or 0)
    if days == 0:
        return Result(
            "CO-03-01", "", "", "", STATUS_OPEN,
            f"Billing system tables are enabled but hold no usage in the last "
            f"{ctx.lookback_days} days.",
        )
    if queries == 0:
        return Result(
            "CO-03-01", "", "", "", STATUS_OPEN,
            f"Billing data is available ({days} day(s) of usage in the last "
            f"{ctx.lookback_days}) but nobody queried system.billing.usage in that "
            "window, so cost monitoring is not in use.",
            {"usage_days": days, "billing_queries": 0},
        )
    return Result(
        "CO-03-01", "", "", "", STATUS_COMPLETED,
        f"Costs are actively monitored: {queries:,} query/queries against "
        f"system.billing.usage by {consumers} principal(s) over the last "
        f"{ctx.lookback_days} days, backed by {days} day(s) of usage data in "
        f"{inventory.region_label(ctx)}.",
        {"usage_days": days, "billing_queries": queries, "consumers": consumers},
    )


def co_03_02(ctx: Ctx) -> Result:
    """Tag clusters for cost attribution."""
    if not ctx.has_table("system.billing.usage"):
        return Result(
            "CO-03-02", "", "", "", STATUS_OPEN,
            "system.billing.usage is not readable, so tag coverage cannot be measured.",
        )
    row = ctx.one(
        f"""
        SELECT count(*) AS total,
               count_if(custom_tags IS NOT NULL AND size(map_keys(custom_tags)) > 0) AS tagged
        FROM system.billing.usage
        WHERE {inventory.billing_scope(ctx, "workspace_id")} AND usage_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
        """
    )
    total = int(row.get("total") or 0)
    tagged = int(row.get("tagged") or 0)
    keys = ctx.sql(
        f"""
        SELECT k AS tag_key, count(*) AS n
        FROM system.billing.usage
        LATERAL VIEW explode(map_keys(coalesce(custom_tags, map()))) t AS k
        WHERE {inventory.billing_scope(ctx, "workspace_id")} AND usage_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
        GROUP BY 1 ORDER BY 2 DESC LIMIT 8
        """
    )
    mix = ", ".join(f"{r['tag_key']}={int(r['n'] or 0):,}" for r in keys)
    return ratio_result(
        "CO-03-02",
        tagged,
        total,
        f"billing records in {inventory.region_label(ctx)} "
        f"in the last {ctx.lookback_days} days carry custom tags for "
        "cost attribution",
        complete_at=ctx.complete_at,
        none_found=(
            f"No usage records in the last {ctx.lookback_days} days, so tag coverage "
            "cannot be measured."
        ),
        extra=(
            f"Tag keys in use: {mix}."
            if mix
            else "No tag keys found, so spend cannot be attributed to teams or projects."
        ),
        metrics={"tag_keys": {r["tag_key"]: r["n"] for r in keys}},
    )


def co_03_03(ctx: Ctx) -> Result:
    """Implement observability to track and chargeback cost."""
    # If the environment doesn't practice chargeback (confirmed with the owners and set
    # in config), don't assess tagging for a process that doesn't exist - record it open.
    if ctx.cfg.get("chargeback_enabled", True) is False:
        return Result(
            "CO-03-03", "", "", "", STATUS_OPEN,
            "Chargeback is not practiced in this environment (CHARGEBACK_ENABLED = False "
            "in config), so cost-attribution tagging is not assessed. Introducing a "
            "cost-centre/team tagging standard would enable chargeback.",
            {"chargeback_enabled": False},
        )
    if not ctx.has_table("system.billing.usage"):
        return Result(
            "CO-03-03", "", "", "", STATUS_OPEN,
            "system.billing.usage is not readable, so chargeback readiness cannot be "
            "assessed.",
        )
    # Chargeback needs a stable dimension: a cost-center-like tag, not just any tag.
    # The tag-key tokens are config-driven so they match the environment's taxonomy.
    patterns = ctx.cfg.get("chargeback_tag_patterns") or DEFAULT_CHARGEBACK_TAG_PATTERNS
    tag_regex = "(" + "|".join(p.replace("'", "''") for p in patterns) + ")"
    row = ctx.one(
        f"""
        SELECT count(*) AS total,
               count_if(
                 exists(map_keys(coalesce(custom_tags, map())),
                        k -> lower(k) RLIKE '{tag_regex}')
               ) AS attributable
        FROM system.billing.usage
        WHERE {inventory.billing_scope(ctx, "workspace_id")} AND usage_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
        """
    )
    total = int(row.get("total") or 0)
    attributable = int(row.get("attributable") or 0)
    priced = ctx.has_table("system.billing.list_prices")
    dashboards = 0
    if ctx.has_table("system.query.history"):
        dashboards = ctx.count_safe(
            f"""
            SELECT count(DISTINCT coalesce(query_source.dashboard_id,
                                           query_source.legacy_dashboard_id)) AS n
            FROM system.query.history
            WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND lower(statement_text) LIKE '%system.billing%'
              AND (query_source.dashboard_id IS NOT NULL
                   OR query_source.legacy_dashboard_id IS NOT NULL)
            """
        )
    return ratio_result(
        "CO-03-03",
        attributable,
        total,
        f"billing records in {inventory.region_label(ctx)} "
        f"carry a chargeback dimension (cost centre, team, project, "
        "owner or environment tag)",
        complete_at=ctx.complete_at,
        none_found=(
            f"No usage records in the last {ctx.lookback_days} days to attribute."
        ),
        extra=(
            (
                f"{dashboards} dashboard(s) query billing data, indicating "
                "operationalized cost reporting. "
                if dashboards
                else "No dashboards were observed querying billing data. "
            )
            + (
                "list_prices is available for converting DBUs to currency."
                if priced
                else "system.billing.list_prices is unavailable, so DBUs cannot be "
                "converted to currency."
            )
        ),
        metrics={"billing_dashboards": dashboards, "list_prices_available": priced},
    )


def co_03_04(ctx: Ctx) -> Result:
    """Share cost reports regularly."""
    dashboards = 0
    alerts = 0
    if ctx.has_table("system.query.history"):
        dashboards = ctx.count_safe(
            f"""
            SELECT count(DISTINCT coalesce(query_source.dashboard_id,
                                           query_source.legacy_dashboard_id)) AS n
            FROM system.query.history
            WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND lower(statement_text) LIKE '%system.billing%'
              AND (query_source.dashboard_id IS NOT NULL
                   OR query_source.legacy_dashboard_id IS NOT NULL)
            """
        )
        alerts = ctx.count_safe(
            f"""
            SELECT count(DISTINCT query_source.alert_id) AS n
            FROM system.query.history
            WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND lower(statement_text) LIKE '%system.billing%'
              AND query_source.alert_id IS NOT NULL
            """
        )
    if dashboards == 0 and alerts == 0:
        return Result(
            "CO-03-04", "", "", "", STATUS_OPEN,
            f"No dashboards or alerts queried billing data in the last "
            f"{ctx.lookback_days} days, so there is no evidence that cost reports are "
            "produced or circulated. Reports shared outside Databricks (email, "
            "spreadsheets) would not be visible here - confirm with your team.",
            {"billing_dashboards": 0, "billing_alerts": 0},
        )
    status = STATUS_COMPLETED if alerts > 0 else STATUS_IN_PROGRESS
    return Result(
        "CO-03-04", "", "", "", status,
        f"Cost reporting is operationalized: {dashboards} dashboard(s) and {alerts} "
        f"alert(s) query billing data in the last {ctx.lookback_days} days. "
        + (
            "Alerts indicate automated distribution."
            if alerts
            else "No cost alerts found, so distribution may still be manual."
        ),
        {"billing_dashboards": dashboards, "billing_alerts": alerts},
    )


def co_03_05(ctx: Ctx) -> Result:
    """Monitor and manage Delta Sharing egress costs."""
    # Distinguish "no shares exist" from "we could not look" - only the former
    # justifies calling this complete.
    shares_readable = ctx.has_table("system.information_schema.shares")
    shares = 0
    if shares_readable:
        shares = ctx.count_safe("SELECT count(*) AS n FROM system.information_schema.shares")
    recipients = 0
    recipients_readable = False
    w = ctx.w
    if w is not None:
        try:
            recipients = sum(1 for _ in w.recipients.list())
            recipients_readable = True
        except Exception:
            recipients_readable = False

    if not shares_readable and not recipients_readable:
        return Result(
            "CO-03-05", "", "", "", STATUS_OPEN,
            "Neither the shares system table nor the recipients API is reachable, so "
            "Delta Sharing egress cost cannot be assessed.",
            {"shares_readable": False, "recipients_readable": False},
        )
    if shares == 0 and recipients == 0:
        return Result(
            "CO-03-05", "", "", "", STATUS_COMPLETED,
            "No Delta Shares or recipients exist, so there is no sharing egress cost to "
            "manage. Mark ignore in the WAF tool if Delta Sharing is out of scope.",
            {"shares": 0, "recipients": 0},
        )
    egress = 0
    if ctx.has_table("system.billing.usage"):
        egress = ctx.count_safe(
            f"""
            SELECT count(*) AS n FROM system.billing.usage
            WHERE {inventory.billing_scope(ctx, "workspace_id")} AND usage_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
              AND (lower(coalesce(sku_name, '')) LIKE '%internet%'
                   OR lower(coalesce(sku_name, '')) LIKE '%egress%'
                   OR lower(coalesce(billing_origin_product, '')) LIKE '%sharing%')
            """
        )
    materializations = 0
    if ctx.has_table("system.sharing.materialization_history"):
        materializations = ctx.count_safe(
            "SELECT count(*) AS n FROM system.sharing.materialization_history"
        )
    if egress == 0:
        return Result(
            "CO-03-05", "", "", "", STATUS_OPEN,
            f"Delta Sharing is in use ({shares} share(s), {recipients} recipient(s)) but "
            f"no egress or sharing-related billing records appear in "
            f"{inventory.region_label(ctx)} in the last {ctx.lookback_days} days, so "
            "egress cost is not being observed or tracked.",
            {"shares": shares, "recipients": recipients, "egress_records": 0,
             "materializations": materializations},
        )
    return Result(
        "CO-03-05", "", "", "", STATUS_COMPLETED,
        f"Delta Sharing egress is visible in billing: {egress:,} record(s) in "
        f"{inventory.region_label(ctx)} over the last {ctx.lookback_days} days, across "
        f"{shares} share(s) and {recipients} recipient(s) in this metastore (shares are "
        "metastore-level, so not limited to the production catalogs).",
        {"shares": shares, "recipients": recipients, "egress_records": egress,
         "materializations": materializations},
    )


# ----------------------------------------------------------------------------------
# Design cost-effective workloads
# ----------------------------------------------------------------------------------


def co_04_01(ctx: Ctx) -> Result:
    """Balance always-on and triggered streaming."""
    if not ctx.has_table("system.lakeflow.pipelines"):
        return Result(
            "CO-04-01", "", "", "", STATUS_OPEN,
            "system.lakeflow.pipelines is not readable, so streaming mode cannot be "
            "assessed.",
        )
    row = ctx.one(
        f"""
        WITH {inventory.live_latest_cte("pipelines", "pipeline_id, settings")}
        SELECT count(*) AS total,
               count_if(settings.continuous = true) AS continuous,
               count_if(settings.development = true) AS development
        FROM latest WHERE rn = 1
        """
    )
    total = int(row.get("total") or 0)
    continuous = int(row.get("continuous") or 0)
    development = int(row.get("development") or 0)

    continuous_jobs = 0
    if ctx.has_table("system.lakeflow.jobs"):
        continuous_jobs = ctx.count_safe(
            f"""
            WITH {inventory.live_latest_cte("jobs", "job_id, trigger")}
            SELECT count(*) AS n FROM latest
            WHERE rn = 1 AND trigger.continuous.enabled = true
            """
        )
    if total == 0 and continuous_jobs == 0:
        return Result(
            "CO-04-01", "", "", "", STATUS_OPEN,
            "No pipelines or continuous jobs found, so streaming cost trade-offs cannot "
            "be assessed. Mark ignore in the WAF tool if streaming is out of scope.",
        )
    triggered = total - continuous
    res = ratio_result(
        "CO-04-01",
        triggered,
        total,
        "declarative pipelines run in triggered mode rather than always-on continuous "
        "mode",
        complete_at=ctx.complete_at,
        none_found=(
            f"No declarative pipelines found, though {continuous_jobs} continuous job(s) "
            "exist. Verify always-on compute is justified by latency requirements."
        ),
        extra=(
            f"{continuous} continuous pipeline(s) and {continuous_jobs} continuous job(s) "
            "hold compute open around the clock; that is correct only when latency "
            "genuinely demands it."
            + (
                f" {development} pipeline(s) are in development mode, which does not "
                "auto-terminate compute as aggressively as production mode."
                if development
                else ""
            )
        ),
        metrics={"continuous_pipelines": continuous, "triggered_pipelines": triggered,
                 "continuous_jobs": continuous_jobs, "development_pipelines": development},
    )
    return res


def co_04_02(ctx: Ctx) -> Result:
    """Balance between on-demand and capacity excess instances (spot)."""
    if not ctx.has_table("system.compute.clusters"):
        return Result(
            "CO-04-02", "", "", "", STATUS_OPEN,
            "system.compute.clusters is not readable, so spot usage cannot be assessed.",
        )
    # Spot configuration lives in a cloud-specific attribute struct; CLOUD_PROVIDER picks
    # which availability field to read (any substring match hits attribute key names).
    row = ctx.one(
        f"""
        {inventory.workloads_cte(ctx, 'cluster_id, aws_attributes, azure_attributes, gcp_attributes')}
        SELECT count(*) AS total,
               count_if({cloud.spot_sql(ctx)}) AS spot_aware
        FROM latest WHERE rn = 1
        """
    )
    total = int(row.get("total") or 0)
    spot = int(row.get("spot_aware") or 0)
    if total == 0:
        return Result(
            "CO-04-02", "", "", "", STATUS_COMPLETED,
            f"No classic clusters active in the last {ctx.lookback_days} days; serverless "
            "compute manages the on-demand/spot mix on your behalf.",
            {"clusters": 0},
        )
    return ratio_result(
        "CO-04-02",
        spot,
        total,
        f"{inventory.workload_unit(ctx)} are configured to use spot / preemptible capacity for workers",
        complete_at=ctx.complete_at,
        extra=(
            f"Spot = {cloud.spot_sql(ctx)}. Spot capacity cuts worker cost substantially "
            "but can be reclaimed, so keep the driver and latency-critical production "
            "work on-demand."
        ),
        metrics={"spot_aware_clusters": spot, "cloud": ctx.cloud},
    )


CHECKS = [
    ("CO-01-01", co_01_01),
    ("CO-01-02", co_01_02),
    ("CO-01-03", co_01_03),
    ("CO-01-04", co_01_04),
    ("CO-01-05", co_01_05),
    ("CO-01-06", co_01_06),
    ("CO-01-07", co_01_07),
    ("CO-01-08", co_01_08),
    ("CO-01-09", co_01_09),
    ("CO-02-01", co_02_01),
    ("CO-02-02", co_02_02),
    ("CO-02-03", co_02_03),
    ("CO-03-01", co_03_01),
    ("CO-03-02", co_03_02),
    ("CO-03-03", co_03_03),
    ("CO-03-04", co_03_04),
    ("CO-03-05", co_03_05),
    ("CO-04-01", co_04_01),
    ("CO-04-02", co_04_02),
]
