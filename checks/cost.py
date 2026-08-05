"""Cost Optimization pillar checks (CO-*).

Evidence sources: ``system.billing.usage``, ``system.billing.list_prices``,
``system.compute.clusters``, ``system.compute.warehouses``, ``system.query.history``,
and the Workspace SDK for cluster policies.
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
    ratio_result,
)

PILLAR_ID = "cost-optimization"

#: Runtimes at or above this major version count as "up to date" for CO-01-04.
CURRENT_DBR_MAJOR = 14


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
    """Use job clusters (not all-purpose) for automated workloads."""
    if not ctx.has_table("system.billing.usage"):
        return Result(
            "CO-01-02", "", "", "", STATUS_OPEN,
            "system.billing.usage is not readable, so compute-type spend cannot be split.",
        )
    row = ctx.one(
        f"""
        SELECT
          sum(usage_quantity) AS total,
          sum(CASE WHEN lower(coalesce(sku_name, '')) LIKE '%all_purpose%'
                   THEN usage_quantity ELSE 0 END) AS all_purpose,
          sum(CASE WHEN lower(coalesce(sku_name, '')) LIKE '%jobs%'
                   THEN usage_quantity ELSE 0 END) AS jobs
        FROM system.billing.usage
        WHERE usage_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
          AND lower(coalesce(billing_origin_product, '')) NOT IN ('sql', 'databricks_sql')
        """
    )
    total = float(row.get("total") or 0)
    all_purpose = float(row.get("all_purpose") or 0)
    if total <= 0:
        return Result(
            "CO-01-02", "", "", "", STATUS_OPEN,
            f"No non-SQL compute usage recorded in the last {ctx.lookback_days} days.",
        )
    non_ap = total - all_purpose
    return ratio_result(
        "CO-01-02",
        int(round(non_ap)),
        int(round(total)),
        f"non-SQL DBUs in the last {ctx.lookback_days} days run on job or serverless "
        "compute rather than the more expensive all-purpose SKU",
        complete_at=ctx.complete_at,
        extra=(
            f"All-purpose consumption: {all_purpose:,.0f} DBU "
            f"({all_purpose / total * 100:.1f}%). All-purpose lists at roughly 2-4x the "
            "job-compute rate, so scheduled work belongs on job clusters."
        ),
        metrics={"all_purpose_dbus": round(all_purpose, 2),
                 "total_dbus": round(total, 2)},
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
    row = ctx.one(
        f"""
        WITH latest AS (
          SELECT cluster_id, dbr_version,
                 row_number() OVER (PARTITION BY cluster_id ORDER BY change_time DESC) AS rn
          FROM system.compute.clusters
          WHERE delete_time IS NULL
            AND change_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
        ),
        parsed AS (
          SELECT cluster_id, dbr_version,
                 try_cast(regexp_extract(coalesce(dbr_version, ''), '^([0-9]+)', 1) AS INT) AS major
          FROM latest WHERE rn = 1
        )
        SELECT count(*) AS total,
               count_if(major >= {CURRENT_DBR_MAJOR}) AS current_dbr,
               min(major) AS oldest
        FROM parsed
        """
    )
    total = int(row.get("total") or 0)
    current = int(row.get("current_dbr") or 0)
    oldest = row.get("oldest")
    if total == 0:
        return Result(
            "CO-01-04", "", "", "", STATUS_COMPLETED,
            f"No classic clusters active in the last {ctx.lookback_days} days; serverless "
            "compute is always on a current, Databricks-managed runtime.",
            {"clusters": 0},
        )
    return ratio_result(
        "CO-01-04",
        current,
        total,
        f"active clusters run Databricks Runtime {CURRENT_DBR_MAJOR}.x or newer",
        complete_at=ctx.complete_at,
        extra=(
            f"Oldest major version in use: DBR {oldest}.x. Newer runtimes deliver "
            "free performance (and therefore cost) improvements."
            if oldest is not None
            else ""
        ),
        metrics={"oldest_dbr_major": oldest, "threshold_major": CURRENT_DBR_MAJOR},
    )


def co_01_05(ctx: Ctx) -> Result:
    """Only use GPUs for the right workloads."""
    if not ctx.has_table("system.billing.usage"):
        return Result(
            "CO-01-05", "", "", "", STATUS_OPEN,
            "system.billing.usage is not readable, so GPU spend cannot be assessed.",
        )
    row = ctx.one(
        f"""
        SELECT
          sum(usage_quantity) AS total,
          sum(CASE WHEN lower(coalesce(sku_name, '')) RLIKE '(gpu|ml.*gpu)'
                   THEN usage_quantity ELSE 0 END) AS gpu
        FROM system.billing.usage
        WHERE usage_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
        """
    )
    total = float(row.get("total") or 0)
    gpu = float(row.get("gpu") or 0)
    if total <= 0:
        return Result(
            "CO-01-05", "", "", "", STATUS_OPEN,
            f"No billable usage recorded in the last {ctx.lookback_days} days.",
        )
    if gpu == 0:
        return Result(
            "CO-01-05", "", "", "", STATUS_COMPLETED,
            f"No GPU DBUs consumed in the last {ctx.lookback_days} days, so there is no "
            "GPU spend on non-GPU workloads.",
            {"gpu_dbus": 0.0, "total_dbus": round(total, 2)},
        )
    ml_runs = 0
    if ctx.has_table("system.mlflow.runs_latest"):
        ml_runs = ctx.count_safe(
            f"""
            SELECT count(*) AS n FROM system.mlflow.runs_latest
            WHERE delete_time IS NULL
              AND start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
            """
        )
    share = gpu / total
    # GPU spend is only justified when there is ML activity to justify it.
    if ml_runs > 0:
        status = STATUS_COMPLETED
        verdict = (
            f"{ml_runs:,} MLflow run(s) in the same window justify GPU use."
        )
    else:
        status = STATUS_IN_PROGRESS
        verdict = (
            "No MLflow runs were recorded in the same window, so GPU spend may not be "
            "tied to deep-learning workloads. Review whether these workloads need GPUs."
        )
    return Result(
        "CO-01-05", "", "", "", status,
        f"GPU consumption is {fmt_pct(share)} of DBUs in the last {ctx.lookback_days} "
        f"days ({gpu:,.0f} of {total:,.0f}). {verdict}",
        {"gpu_dbus": round(gpu, 2), "total_dbus": round(total, 2),
         "gpu_share_pct": round(share * 100, 2), "mlflow_runs": ml_runs},
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
        WHERE usage_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
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
        f"DBUs in the last {ctx.lookback_days} days run on serverless compute",
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
        WITH latest AS (
          SELECT cluster_id, worker_node_type, driver_node_type,
                 row_number() OVER (PARTITION BY cluster_id ORDER BY change_time DESC) AS rn
          FROM system.compute.clusters
          WHERE delete_time IS NULL
            AND change_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
        )
        SELECT coalesce(worker_node_type, 'unknown') AS node_type, count(*) AS n
        FROM latest WHERE rn = 1
        GROUP BY 1 ORDER BY 2 DESC LIMIT 8
        """
    )
    if not rows:
        return Result(
            "CO-01-07", "", "", "", STATUS_COMPLETED,
            f"No classic clusters active in the last {ctx.lookback_days} days; serverless "
            "compute selects instance types automatically.",
            {"clusters": 0},
        )
    total = sum(int(r["n"]) for r in rows)
    # Latest-generation families are materially cheaper per unit of work.
    modern = sum(
        int(r["n"])
        for r in rows
        if any(tok in str(r["node_type"]).lower() for tok in
               ("d3", "d4", "e4", "d5", "e5", "m5", "m6", "r5", "r6", "c5", "c6",
                "i3en", "i4", "n2", "e2", "c3", "m7", "r7", "c7"))
    )
    mix = ", ".join(f"{r['node_type']}={r['n']}" for r in rows)
    return ratio_result(
        "CO-01-07",
        modern,
        total,
        "active clusters use a current-generation instance family",
        complete_at=ctx.complete_at,
        extra=(
            f"Instance mix: {mix}. Right-sizing means matching memory/compute/IO to the "
            "workload, which needs a per-job review."
        ),
        metrics={"node_type_mix": {r["node_type"]: r["n"] for r in rows}},
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
    row = ctx.one(
        f"""
        SELECT
          sum(usage_quantity) AS total,
          sum(CASE WHEN product_features.is_photon = true
                        OR lower(coalesce(sku_name, '')) LIKE '%photon%'
                        OR lower(coalesce(sku_name, '')) LIKE '%sql%'
                   THEN usage_quantity ELSE 0 END) AS vectorized
        FROM system.billing.usage
        WHERE usage_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
        """
    )
    total = float(row.get("total") or 0)
    if total <= 0:
        return Result(
            "CO-01-09", "", "", "", STATUS_OPEN,
            f"No billable usage recorded in the last {ctx.lookback_days} days.",
        )
    vectorized = float(row.get("vectorized") or 0)
    return ratio_result(
        "CO-01-09",
        int(round(vectorized)),
        int(round(total)),
        f"DBUs in the last {ctx.lookback_days} days run on a vectorized engine "
        "(Photon or SQL warehouse)",
        complete_at=ctx.complete_at,
        extra=(
            "Photon costs more per DBU but usually lowers total cost by finishing sooner; "
            "validate per workload."
        ),
        metrics={"vectorized_dbus": round(vectorized, 2), "total_dbus": round(total, 2)},
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
        WITH latest AS (
          SELECT cluster_id, min_autoscale_workers, max_autoscale_workers,
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
    wh_scaling = 0
    wh_total = 0
    if ctx.has_table("system.compute.warehouses"):
        wrow = ctx.one(
            """
            WITH latest AS (
              SELECT warehouse_id, min_clusters, max_clusters,
                     row_number() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) AS rn
              FROM system.compute.warehouses WHERE delete_time IS NULL
            )
            SELECT count(*) AS total, count_if(max_clusters > coalesce(min_clusters, 1)) AS scaling
            FROM latest WHERE rn = 1
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
        "compute resources (clusters and SQL warehouses) are configured to scale "
        "elastically with load",
        complete_at=ctx.complete_at,
        extra=(
            f"Clusters: {autoscaling}/{total} autoscaling. "
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
        WITH latest AS (
          SELECT cluster_id, auto_termination_minutes, cluster_source,
                 row_number() OVER (PARTITION BY cluster_id ORDER BY change_time DESC) AS rn
          FROM system.compute.clusters
          WHERE delete_time IS NULL
            AND change_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
        )
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
              SELECT warehouse_id, auto_stop_minutes,
                     row_number() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) AS rn
              FROM system.compute.warehouses WHERE delete_time IS NULL
            )
            SELECT count(*) AS total,
                   count_if(auto_stop_minutes IS NOT NULL AND auto_stop_minutes > 0) AS stopping
            FROM latest WHERE rn = 1
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
        "interactive clusters and SQL warehouses have auto-termination / auto-stop set",
        complete_at=ctx.complete_at,
        extra=(
            f"Clusters: {terminating}/{total} terminate automatically"
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
    """Use compute policies to control costs."""
    policies = None
    w = ctx.w
    if w is not None:
        try:
            policies = sum(1 for _ in w.cluster_policies.list())
        except Exception:
            policies = None
    if not ctx.has_table("system.compute.clusters"):
        if policies:
            return Result(
                "CO-02-03", "", "", "", STATUS_IN_PROGRESS,
                f"{policies} cluster policy/policies exist, but cluster records are not "
                "readable so policy enforcement coverage cannot be measured.",
                {"policies": policies},
            )
        return Result(
            "CO-02-03", "", "", "", STATUS_OPEN,
            "Neither cluster policies nor cluster records are readable, so cost "
            "guardrails cannot be assessed.",
        )
    row = ctx.one(
        f"""
        WITH latest AS (
          SELECT cluster_id, policy_id, cluster_source,
                 row_number() OVER (PARTITION BY cluster_id ORDER BY change_time DESC) AS rn
          FROM system.compute.clusters
          WHERE delete_time IS NULL
            AND change_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
        )
        SELECT count(*) AS total,
               count_if(policy_id IS NOT NULL AND policy_id <> '') AS governed
        FROM latest WHERE rn = 1
        """
    )
    total = int(row.get("total") or 0)
    governed = int(row.get("governed") or 0)
    if total == 0:
        if policies:
            return Result(
                "CO-02-03", "", "", "", STATUS_COMPLETED,
                f"{policies} cluster policy/policies are defined and no ungoverned "
                f"clusters were created in the last {ctx.lookback_days} days.",
                {"policies": policies, "clusters": 0},
            )
        return Result(
            "CO-02-03", "", "", "", STATUS_OPEN,
            f"No cluster policies found and no clusters active in the last "
            f"{ctx.lookback_days} days.",
            {"policies": 0},
        )
    return ratio_result(
        "CO-02-03",
        governed,
        total,
        "active clusters were created under a cluster policy that constrains size, "
        "runtime and auto-termination",
        complete_at=ctx.complete_at,
        extra=(
            f"{policies} policy/policies defined in the workspace."
            if policies is not None
            else "Policy inventory unavailable via the SDK."
        ),
        metrics={"policies": policies, "governed_clusters": governed},
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
        WHERE usage_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
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
            "CO-03-01", "", "", "", STATUS_IN_PROGRESS,
            f"Billing data is available ({days} day(s) of usage in the last "
            f"{ctx.lookback_days}) but nobody queried system.billing.usage in that "
            "window, so cost data is collected and not actively monitored.",
            {"usage_days": days, "billing_queries": 0},
        )
    return Result(
        "CO-03-01", "", "", "", STATUS_COMPLETED,
        f"Costs are actively monitored: {queries:,} query/queries against "
        f"system.billing.usage by {consumers} principal(s) over the last "
        f"{ctx.lookback_days} days, backed by {days} day(s) of usage data.",
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
        WHERE usage_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
        """
    )
    total = int(row.get("total") or 0)
    tagged = int(row.get("tagged") or 0)
    keys = ctx.sql(
        f"""
        SELECT k AS tag_key, count(*) AS n
        FROM system.billing.usage
        LATERAL VIEW explode(map_keys(coalesce(custom_tags, map()))) t AS k
        WHERE usage_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
        GROUP BY 1 ORDER BY 2 DESC LIMIT 8
        """
    )
    mix = ", ".join(f"{r['tag_key']}={int(r['n'] or 0):,}" for r in keys)
    return ratio_result(
        "CO-03-02",
        tagged,
        total,
        f"billing records in the last {ctx.lookback_days} days carry custom tags for "
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
    if not ctx.has_table("system.billing.usage"):
        return Result(
            "CO-03-03", "", "", "", STATUS_OPEN,
            "system.billing.usage is not readable, so chargeback readiness cannot be "
            "assessed.",
        )
    # Chargeback needs a stable dimension: a cost-center-like tag, not just any tag.
    row = ctx.one(
        f"""
        SELECT count(*) AS total,
               count_if(
                 exists(map_keys(coalesce(custom_tags, map())),
                        k -> lower(k) RLIKE '(cost|team|project|owner|business|dept|department|bu|chargeback|env)')
               ) AS attributable
        FROM system.billing.usage
        WHERE usage_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
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
        "billing records carry a chargeback dimension (cost centre, team, project, "
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
            "spreadsheets) would not be visible here - confirm with the customer.",
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
            WHERE usage_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
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
            "CO-03-05", "", "", "", STATUS_IN_PROGRESS,
            f"Delta Sharing is in use ({shares} share(s), {recipients} recipient(s)) but "
            f"no egress or sharing-related billing records appear in the last "
            f"{ctx.lookback_days} days, so egress cost is not being tracked. Cloud egress "
            "is often billed by the cloud provider rather than Databricks.",
            {"shares": shares, "recipients": recipients, "egress_records": 0,
             "materializations": materializations},
        )
    return Result(
        "CO-03-05", "", "", "", STATUS_COMPLETED,
        f"Delta Sharing egress is visible in billing: {egress:,} record(s) over the last "
        f"{ctx.lookback_days} days across {shares} share(s) and {recipients} recipient(s).",
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
        """
        WITH latest AS (
          SELECT pipeline_id, settings,
                 row_number() OVER (PARTITION BY pipeline_id ORDER BY change_time DESC) AS rn
          FROM system.lakeflow.pipelines WHERE delete_time IS NULL
        )
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
            """
            WITH latest AS (
              SELECT job_id, trigger,
                     row_number() OVER (PARTITION BY job_id ORDER BY change_time DESC) AS rn
              FROM system.lakeflow.jobs WHERE delete_time IS NULL
            )
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
    # Spot configuration lives in cloud-specific attribute structs; probe whichever
    # this workspace's cloud populates.
    row = ctx.one(
        f"""
        WITH latest AS (
          SELECT cluster_id, aws_attributes, azure_attributes, gcp_attributes,
                 row_number() OVER (PARTITION BY cluster_id ORDER BY change_time DESC) AS rn
          FROM system.compute.clusters
          WHERE delete_time IS NULL
            AND change_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
        )
        SELECT count(*) AS total,
               count_if(
                 coalesce(try_cast(aws_attributes.first_on_demand AS INT), 999) < 999
                 OR lower(coalesce(to_json(azure_attributes), '')) LIKE '%spot%'
                 OR lower(coalesce(to_json(gcp_attributes), '')) LIKE '%preemptible%'
               ) AS spot_aware
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
        "active clusters are configured to use spot / preemptible capacity for workers",
        complete_at=ctx.complete_at,
        extra=(
            "Spot capacity cuts worker cost substantially but can be reclaimed, so keep "
            "the driver and latency-critical production work on-demand."
        ),
        metrics={"spot_aware_clusters": spot},
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
