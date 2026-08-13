"""Interoperability & Usability pillar checks (IU-*).

Evidence sources: ``system.information_schema.*`` (connections, shares, volumes,
external locations), ``system.lakeflow.*``, ``system.access.*``, ``system.billing.usage``,
and the Workspace SDK for connections, recipients and cluster policies.
"""

from __future__ import annotations

from waf_core import (
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    STATUS_OPEN,
    Ctx,
    Result,
    manual_result,
    presence_result,
    ratio_result,
)

PILLAR_ID = "interoperability-usability"


# ----------------------------------------------------------------------------------
# Define standards for integration
# ----------------------------------------------------------------------------------


def iu_01_01(ctx: Ctx) -> Result:
    """Use standard and reusable integration patterns for external integration."""
    connections = 0
    if ctx.has_table("system.information_schema.connections"):
        connections = ctx.count_safe(
            "SELECT count(*) AS n FROM system.information_schema.connections"
        )
    ext_locations = 0
    if ctx.has_table("system.information_schema.external_locations"):
        ext_locations = ctx.count_safe(
            "SELECT count(*) AS n FROM system.information_schema.external_locations"
        )
    credentials = 0
    if ctx.has_table("system.information_schema.storage_credentials"):
        credentials = ctx.count_safe(
            "SELECT count(*) AS n FROM system.information_schema.storage_credentials"
        )
    # Ad-hoc credential handling in query text is the anti-pattern to catch.
    adhoc = 0
    if ctx.has_table("system.query.history"):
        adhoc = ctx.count_safe(
            f"""
            SELECT count(*) AS n FROM system.query.history
            WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND (lower(statement_text) RLIKE 'jdbc:(mysql|postgres|sqlserver|oracle)'
                   OR lower(statement_text) LIKE '%fs.azure.account.key%'
                   OR lower(statement_text) LIKE '%aws_secret_access_key%')
            """
        )
    governed = connections + ext_locations
    if governed == 0:
        return Result(
            "IU-01-01", "", "", "", STATUS_OPEN,
            "No Unity Catalog connections or external locations found, so external "
            "integrations are not using governed, reusable patterns."
            + (
                f" {adhoc:,} statement(s) embed raw JDBC URLs or cloud keys inline, which "
                "is the pattern to replace."
                if adhoc
                else ""
            ),
            {"connections": 0, "external_locations": 0, "adhoc_statements": adhoc},
        )
    status = STATUS_COMPLETED if adhoc == 0 else STATUS_IN_PROGRESS
    return Result(
        "IU-01-01", "", "", "", status,
        f"Governed integration objects are in place: {connections} Lakehouse Federation "
        f"connection(s), {ext_locations} external location(s) and {credentials} storage "
        f"credential(s). "
        + (
            f"However {adhoc:,} statement(s) in the last {ctx.lookback_days} days still "
            "embed raw JDBC URLs or inline cloud keys, so the standard is not applied "
            "everywhere."
            if adhoc
            else "No inline JDBC URLs or hard-coded cloud keys were observed in query "
            "history."
        ),
        {"connections": connections, "external_locations": ext_locations,
         "storage_credentials": credentials, "adhoc_statements": adhoc},
    )


def iu_01_02(ctx: Ctx) -> Result:
    """Use optimized connectors to ingest data sources into the lakehouse."""
    autoloader = 0
    if ctx.has_table("system.query.history"):
        autoloader = ctx.count_safe(
            f"""
            SELECT count(*) AS n FROM system.query.history
            WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND (lower(statement_text) LIKE '%cloudfiles%'
                   OR lower(statement_text) LIKE '%read_files(%')
            """
        )
    managed_ingest = 0
    if ctx.has_table("system.lakeflow.pipelines"):
        managed_ingest = ctx.count_safe(
            """
            SELECT count(*) AS n FROM system.lakeflow.pipelines
            WHERE delete_time IS NULL
              AND lower(coalesce(pipeline_type, '')) RLIKE '(ingestion|managed)'
            """
        )
    federation = 0
    if ctx.has_table("system.information_schema.connections"):
        federation = ctx.count_safe(
            "SELECT count(*) AS n FROM system.information_schema.connections"
        )
    zerobus = 0
    if ctx.has_table("system.lakeflow.zerobus_stream"):
        zerobus = ctx.count_safe(
            "SELECT count(*) AS n FROM system.lakeflow.zerobus_stream"
        )
    signals = []
    if autoloader:
        signals.append(f"{autoloader:,} Auto Loader statement(s)")
    if managed_ingest:
        signals.append(f"{managed_ingest} Lakeflow Connect / managed ingestion pipeline(s)")
    if federation:
        signals.append(f"{federation} federation connection(s)")
    if zerobus:
        signals.append(f"{zerobus} Zerobus stream(s)")
    if not signals:
        return Result(
            "IU-01-02", "", "", "", STATUS_OPEN,
            f"No Auto Loader usage, managed ingestion connectors, federation connections, "
            f"or streaming ingest found in the last {ctx.lookback_days} days, so ingestion "
            "is not using optimized native connectors.",
            {"autoloader": 0, "managed_ingestion": 0, "federation": 0},
        )
    # Two or more distinct optimized mechanisms indicates a deliberate standard.
    status = STATUS_COMPLETED if len(signals) >= 2 else STATUS_IN_PROGRESS
    return Result(
        "IU-01-02", "", "", "", status,
        f"{len(signals)} optimized ingestion mechanism(s) in use: "
        + "; ".join(signals)
        + ".",
        {"autoloader": autoloader, "managed_ingestion": managed_ingest,
         "federation": federation, "zerobus": zerobus},
    )


def iu_01_03(ctx: Ctx) -> Result:
    """Use certified partner tools."""
    if not ctx.has_table("system.query.history"):
        return Result(
            "IU-01-03", "", "", "", STATUS_OPEN,
            "system.query.history is not readable, so client tooling cannot be "
            "identified.",
        )
    rows = ctx.sql(
        f"""
        SELECT coalesce(client_application, 'unknown') AS app, count(*) AS n
        FROM system.query.history
        WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
          AND client_application IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC LIMIT 10
        """
    )
    if not rows:
        return Result(
            "IU-01-03", "", "", "", STATUS_OPEN,
            f"No client application information recorded in the last "
            f"{ctx.lookback_days} days, so partner tool usage cannot be identified.",
        )
    known_partners = (
        "tableau", "power bi", "powerbi", "looker", "qlik", "sigma", "thoughtspot",
        "dbt", "fivetran", "informatica", "talend", "matillion", "alteryx", "sap",
        "collibra", "alation", "atlan", "hightouch", "census", "airbyte", "excel",
        "datadog", "hex", "streamlit", "mode", "preset", "superset",
    )
    partner_rows = [
        r for r in rows if any(p in str(r["app"]).lower() for p in known_partners)
    ]
    total = sum(int(r["n"] or 0) for r in rows)
    partner_queries = sum(int(r["n"] or 0) for r in partner_rows)
    mix = ", ".join(f"{r['app']}={int(r['n'] or 0):,}" for r in rows[:6])
    if not partner_rows:
        return Result(
            "IU-01-03", "", "", "", STATUS_IN_PROGRESS,
            f"No recognized certified partner tools appear in {total:,} attributed "
            f"query/queries over the last {ctx.lookback_days} days. Top clients: {mix}. "
            "Access may be via native drivers or an unrecognized client - confirm the "
            "BI/ETL toolchain with the customer.",
            {"client_mix": {r["app"]: int(r["n"] or 0) for r in rows}},
        )
    return Result(
        "IU-01-03", "", "", "", STATUS_COMPLETED,
        f"{len(partner_rows)} certified partner tool(s) are connecting, accounting for "
        f"{partner_queries:,} of {total:,} attributed query/queries "
        f"({partner_queries / total * 100:.1f}%) in the last {ctx.lookback_days} days. "
        f"Top clients: {mix}.",
        {"partner_tools": [r["app"] for r in partner_rows],
         "client_mix": {r["app"]: int(r["n"] or 0) for r in rows}},
    )


def iu_01_04(ctx: Ctx) -> Result:
    """Reduce complexity of data engineering pipelines."""
    pipelines = 0
    if ctx.has_table("system.lakeflow.pipelines"):
        pipelines = ctx.count_safe(
            "SELECT count(*) AS n FROM system.lakeflow.pipelines WHERE delete_time IS NULL"
        )
    jobs = 0
    complex_jobs = 0
    if ctx.has_table("system.lakeflow.jobs"):
        jobs = ctx.count_safe(
            "SELECT count(DISTINCT job_id) AS n FROM system.lakeflow.jobs "
            "WHERE delete_time IS NULL"
        )
    if ctx.has_table("system.lakeflow.job_tasks"):
        complex_jobs = ctx.count_safe(
            """
            WITH counts AS (
              SELECT job_id, count(*) AS tasks
              FROM system.lakeflow.job_tasks
              WHERE delete_time IS NULL
              GROUP BY job_id
            )
            SELECT count(*) AS n FROM counts WHERE tasks > 20
            """
        )
    total = pipelines + jobs
    if total == 0:
        return Result(
            "IU-01-04", "", "", "", STATUS_OPEN,
            "No pipelines or jobs found, so pipeline complexity cannot be assessed.",
        )
    simple = total - complex_jobs
    return ratio_result(
        "IU-01-04",
        simple,
        total,
        "data workloads avoid excessive orchestration complexity (more than 20 tasks in "
        "a single job)",
        complete_at=ctx.complete_at,
        extra=(
            f"{pipelines} declarative pipeline(s) handle dependencies automatically; "
            f"{complex_jobs} job(s) exceed 20 tasks and are candidates for conversion to "
            "declarative pipelines."
        ),
        metrics={"pipelines": pipelines, "jobs": jobs, "complex_jobs": complex_jobs},
    )


# ----------------------------------------------------------------------------------
# Utilize open interfaces and data formats
# ----------------------------------------------------------------------------------


def iu_02_01(ctx: Ctx) -> Result:
    """Use open data formats."""
    scope = ctx.scope
    row = ctx.one(
        f"""
        SELECT count(*) AS total,
               count_if(lower(coalesce(data_source_format, '')) IN
                        ('delta', 'deltasharing', 'parquet', 'iceberg', 'avro', 'orc', 'csv', 'json', 'text')) AS open_fmt,
               count_if(lower(coalesce(data_source_format, '')) IN ('delta', 'deltasharing', 'iceberg')) AS open_table_fmt
        FROM system.information_schema.tables
        WHERE {scope.predicate()} AND table_type <> 'VIEW'
        """
    )
    total = int(row.get("total") or 0)
    open_fmt = int(row.get("open_fmt") or 0)
    open_table = int(row.get("open_table_fmt") or 0)
    proprietary = ctx.sql(
        f"""
        SELECT coalesce(data_source_format, 'UNKNOWN') AS fmt, count(*) AS n
        FROM system.information_schema.tables
        WHERE {scope.predicate()} AND table_type <> 'VIEW'
          AND lower(coalesce(data_source_format, '')) NOT IN
              ('delta', 'deltasharing', 'parquet', 'iceberg', 'avro', 'orc', 'csv', 'json', 'text')
        GROUP BY 1 ORDER BY 2 DESC LIMIT 5
        """
    )
    return ratio_result(
        "IU-02-01",
        open_fmt,
        total,
        f"{scope.label} tables use an open format (Delta, Iceberg, Parquet, Avro, ORC or "
        "text), avoiding vendor lock-in",
        complete_at=ctx.complete_at,
        none_found="No tables found in the assessed catalogs.",
        extra=(
            f"{open_table} table(s) use an open *table* format (Delta or Iceberg) with "
            "ACID guarantees."
            + (
                " Non-open formats: "
                + ", ".join(f"{r['fmt']}={int(r['n'] or 0)}" for r in proprietary)
                + "."
                if proprietary
                else ""
            )
        ),
        metrics={"open_table_formats": open_table, "scoped": True},
    )


def iu_02_02(ctx: Ctx) -> Result:
    """Enable secure data sharing for all data and AI assets."""
    shares = 0
    if ctx.has_table("system.information_schema.shares"):
        shares = ctx.count_safe("SELECT count(*) AS n FROM system.information_schema.shares")
    recipients = 0
    w = ctx.w
    if w is not None:
        try:
            recipients = sum(1 for _ in w.recipients.list())
        except Exception:
            recipients = 0
    shared_tables = 0
    if ctx.has_table("system.information_schema.table_share_usage"):
        shared_tables = ctx.count_safe(
            "SELECT count(*) AS n FROM system.information_schema.table_share_usage"
        )
    ip_limited = 0
    if ctx.has_table("system.information_schema.recipient_allowed_ip_ranges"):
        ip_limited = ctx.count_safe(
            "SELECT count(DISTINCT recipient_name) AS n "
            "FROM system.information_schema.recipient_allowed_ip_ranges"
        )
    if shares == 0 and recipients == 0:
        return Result(
            "IU-02-02", "", "", "", STATUS_OPEN,
            "No Delta Shares or recipients configured, so governed data sharing is not "
            "enabled. Mark ignore in the WAF tool if external sharing is out of scope.",
            {"shares": 0, "recipients": 0},
        )
    status = STATUS_COMPLETED if shares > 0 and shared_tables > 0 else STATUS_IN_PROGRESS
    return Result(
        "IU-02-02", "", "", "", status,
        f"Delta Sharing is configured: {shares} share(s), {recipients} recipient(s), and "
        f"{shared_tables} shared table reference(s). "
        + (
            f"{ip_limited} recipient(s) are restricted by IP allowlist."
            if ip_limited
            else "No recipient IP allowlists are configured, so access is not "
            "network-restricted."
        ),
        {"shares": shares, "recipients": recipients, "shared_tables": shared_tables,
         "ip_restricted_recipients": ip_limited},
    )


def iu_02_03(ctx: Ctx) -> Result:
    """Use open standards for your AI workflows."""
    runs = 0
    if ctx.has_table("system.mlflow.runs_latest"):
        runs = ctx.count_safe(
            f"""
            SELECT count(*) AS n FROM system.mlflow.runs_latest
            WHERE delete_time IS NULL
              AND start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
            """
        )
    experiments = 0
    if ctx.has_table("system.mlflow.experiments_latest"):
        experiments = ctx.count_safe(
            "SELECT count(*) AS n FROM system.mlflow.experiments_latest "
            "WHERE delete_time IS NULL"
        )
    endpoints = 0
    if ctx.has_table("system.serving.served_entities"):
        endpoints = ctx.count_safe(
            "SELECT count(DISTINCT endpoint_id) AS n FROM system.serving.served_entities "
            "WHERE endpoint_delete_time IS NULL"
        )
    if runs == 0 and experiments == 0 and endpoints == 0:
        return Result(
            "IU-02-03", "", "", "", STATUS_OPEN,
            "No MLflow experiments, runs, or serving endpoints found, so no AI workflow "
            "standard is observable. Mark ignore in the WAF tool if AI/ML is out of scope.",
            {"runs": 0, "experiments": 0, "endpoints": 0},
        )
    # MLflow is the open standard here (open source, portable model packaging).
    status = STATUS_COMPLETED if (runs > 0 or experiments > 0) else STATUS_IN_PROGRESS
    return Result(
        "IU-02-03", "", "", "", status,
        f"AI workflows use MLflow, an open standard: {experiments} experiment(s), "
        f"{runs:,} run(s) in the last {ctx.lookback_days} days, and {endpoints} serving "
        "endpoint(s) exposing OpenAI-compatible REST interfaces. "
        + (
            "Models packaged with MLflow remain portable outside Databricks."
            if runs or experiments
            else "Serving endpoints exist but no MLflow tracking was found, so model "
            "packaging may not be standardized."
        ),
        {"runs": runs, "experiments": experiments, "endpoints": endpoints},
    )


# ----------------------------------------------------------------------------------
# Simplify new use case implementation
# ----------------------------------------------------------------------------------


def iu_03_01(ctx: Ctx) -> Result:
    """Provide a self-service experience across the platform."""
    policies = None
    w = ctx.w
    if w is not None:
        try:
            policies = sum(1 for _ in w.cluster_policies.list())
        except Exception:
            policies = None
    # Self-service is visible as many distinct principals doing work themselves.
    users = 0
    if ctx.has_table("system.query.history"):
        users = ctx.count_safe(
            f"""
            SELECT count(DISTINCT executed_by) AS n FROM system.query.history
            WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
            """
        )
    catalogs_readable = 0
    if ctx.has_table("system.information_schema.schema_privileges"):
        catalogs_readable = ctx.count_safe(
            """
            SELECT count(DISTINCT grantee) AS n
            FROM system.information_schema.schema_privileges
            WHERE lower(privilege_type) IN ('select', 'use_schema', 'usage')
            """
        )
    if users == 0:
        return Result(
            "IU-03-01", "", "", "", STATUS_OPEN,
            f"No query activity in the last {ctx.lookback_days} days, so self-service "
            "adoption cannot be assessed.",
            {"active_principals": 0, "policies": policies},
        )
    signals = []
    if policies:
        signals.append(f"{policies} cluster policy/policies letting users self-provision compute safely")
    if catalogs_readable:
        signals.append(f"{catalogs_readable} principal(s) hold schema-level read grants")
    status = STATUS_COMPLETED if len(signals) >= 2 else STATUS_IN_PROGRESS
    return Result(
        "IU-03-01", "", "", "", status,
        f"{users} distinct principal(s) ran queries in the last {ctx.lookback_days} days. "
        + (
            "Self-service enablers found: " + "; ".join(signals) + "."
            if signals
            else "No cluster policies or broad schema grants were found, so users likely "
            "depend on a central team to provision access and compute."
        ),
        {"active_principals": users, "policies": policies,
         "principals_with_grants": catalogs_readable},
    )


def iu_03_02(ctx: Ctx) -> Result:
    """Use serverless services."""
    if not ctx.has_table("system.billing.usage"):
        return Result(
            "IU-03-02", "", "", "", STATUS_OPEN,
            "system.billing.usage is not readable, so serverless adoption cannot be "
            "measured.",
        )
    row = ctx.one(
        f"""
        SELECT sum(usage_quantity) AS total,
               sum(CASE WHEN coalesce(product_features.is_serverless,
                                      lower(coalesce(sku_name, '')) LIKE '%serverless%')
                        THEN usage_quantity ELSE 0 END) AS serverless,
               count(DISTINCT CASE WHEN coalesce(product_features.is_serverless, false)
                                   THEN billing_origin_product END) AS serverless_products
        FROM system.billing.usage
        WHERE usage_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
        """
    )
    total = float(row.get("total") or 0)
    serverless = float(row.get("serverless") or 0)
    if total <= 0:
        return Result(
            "IU-03-02", "", "", "", STATUS_OPEN,
            f"No billable usage recorded in the last {ctx.lookback_days} days.",
        )
    return ratio_result(
        "IU-03-02",
        int(round(serverless)),
        int(round(total)),
        f"DBUs in the last {ctx.lookback_days} days run on serverless services, removing "
        "infrastructure setup as a barrier to new use cases",
        complete_at=ctx.complete_at,
        extra=(
            f"Serverless is used across {int(row.get('serverless_products') or 0)} "
            "distinct product area(s)."
        ),
        metrics={"serverless_dbus": round(serverless, 2), "total_dbus": round(total, 2),
                 "serverless_products": row.get("serverless_products")},
    )


def iu_03_03(ctx: Ctx) -> Result:
    """Use pre-defined compute templates."""
    policies = None
    w = ctx.w
    if w is not None:
        try:
            policies = sum(1 for _ in w.cluster_policies.list())
        except Exception:
            policies = None
    if not ctx.has_table("system.compute.clusters"):
        return presence_result(
            "IU-03-03",
            policies,
            "cluster policy/policies acting as pre-defined compute templates",
            missing_note="Without policies, users must size compute from scratch.",
            metrics={"policies": policies},
        )
    row = ctx.one(
        f"""
        WITH latest AS (
          SELECT cluster_id, policy_id,
                 row_number() OVER (PARTITION BY cluster_id ORDER BY change_time DESC) AS rn
          FROM system.compute.clusters
          WHERE delete_time IS NULL
            AND change_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
        )
        SELECT count(*) AS total,
               count_if(policy_id IS NOT NULL AND policy_id <> '') AS from_template
        FROM latest WHERE rn = 1
        """
    )
    total = int(row.get("total") or 0)
    templated = int(row.get("from_template") or 0)
    if total == 0:
        return presence_result(
            "IU-03-03",
            policies,
            "cluster policy/policies acting as pre-defined compute templates",
            found_note=(
                f"No classic clusters were created in the last {ctx.lookback_days} days, "
                "so template usage could not be measured directly."
            ),
            missing_note="No policies and no clusters; compute templates are not in use.",
            metrics={"policies": policies},
        )
    return ratio_result(
        "IU-03-03",
        templated,
        total,
        "clusters were created from a policy template rather than configured ad hoc",
        complete_at=ctx.complete_at,
        extra=(
            f"{policies} template(s) available."
            if policies is not None
            else "Policy inventory unavailable via the SDK."
        ),
        metrics={"policies": policies, "templated_clusters": templated},
    )


def iu_03_04(ctx: Ctx) -> Result:
    """Use AI capabilities to increase productivity."""
    assistant = 0
    if ctx.has_table("system.access.assistant_events"):
        assistant = ctx.count_safe(
            f"""
            SELECT count(*) AS n FROM system.access.assistant_events
            WHERE event_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
            """
        )
    ai_functions = 0
    if ctx.has_table("system.query.history"):
        ai_functions = ctx.count_safe(
            f"""
            SELECT count(*) AS n FROM system.query.history
            WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND lower(statement_text) RLIKE
                  'ai_(query|classify|extract|gen|summarize|translate|fix_grammar|similarity|forecast|parse_document|analyze_sentiment|mask)\\\\s*\\\\('
            """
        )
    genie = 0
    if ctx.has_table("system.query.history"):
        genie = ctx.count_safe(
            f"""
            SELECT count(*) AS n FROM system.query.history
            WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND query_source.genie_space_id IS NOT NULL
            """
        )
    signals = []
    if assistant:
        signals.append(f"{assistant:,} Databricks Assistant event(s)")
    if ai_functions:
        signals.append(f"{ai_functions:,} AI function call(s) in SQL")
    if genie:
        signals.append(f"{genie:,} Genie query/queries")
    if not signals:
        return Result(
            "IU-03-04", "", "", "", STATUS_OPEN,
            f"No Databricks Assistant activity, AI function usage, or Genie queries in "
            f"the last {ctx.lookback_days} days, so platform AI capabilities are not "
            "being used for productivity.",
            {"assistant_events": 0, "ai_function_calls": 0, "genie_queries": 0},
        )
    status = STATUS_COMPLETED if len(signals) >= 2 else STATUS_IN_PROGRESS
    return Result(
        "IU-03-04", "", "", "", status,
        f"{len(signals)} of 3 AI productivity capability/capabilities in use: "
        + "; ".join(signals)
        + ".",
        {"assistant_events": assistant, "ai_function_calls": ai_functions,
         "genie_queries": genie},
    )


# ----------------------------------------------------------------------------------
# Ensure data consistency and usability
# ----------------------------------------------------------------------------------


def iu_04_01(ctx: Ctx) -> Result:
    """Offer reusable data-as-products that the business can trust."""
    scope = ctx.scope
    row = ctx.one(
        f"""
        SELECT count(*) AS total,
               count_if(comment IS NOT NULL AND trim(comment) <> ''
                        AND table_owner IS NOT NULL AND trim(table_owner) <> '') AS product_ready
        FROM system.information_schema.tables
        WHERE {scope.predicate()} AND table_type <> 'VIEW'
        """
    )
    total = int(row.get("total") or 0)
    ready = int(row.get("product_ready") or 0)
    certified = 0
    if ctx.has_table("system.information_schema.table_tags"):
        certified = ctx.count_safe(
            f"""
            SELECT count(DISTINCT concat_ws('.', catalog_name, schema_name, table_name)) AS n
            FROM system.information_schema.table_tags
            WHERE {scope.predicate('catalog_name', 'schema_name')}
              AND (lower(tag_name) RLIKE '(certif|product|gold|trusted|sla)'
                   OR lower(tag_value) RLIKE '(certif|product|gold|trusted)')
            """
        )
    return ratio_result(
        "IU-04-01",
        ready,
        total,
        f"{scope.label} tables are documented and owned, the minimum bar for being "
        "consumable as a data product",
        complete_at=ctx.complete_at,
        none_found="No tables found in the assessed catalogs.",
        extra=(
            f"{certified} table(s) carry a certification/data-product tag."
            if certified
            else "No certification or data-product tags were found, so consumers cannot "
            "tell which assets are trustworthy."
        ),
        metrics={"certified_tables": certified, "scoped": True},
    )


def iu_04_02(ctx: Ctx) -> Result:
    """Publish data products semantically consistent across the enterprise."""
    scope = ctx.scope
    views = ctx.count_safe(
        f"""
        SELECT count(*) AS n FROM system.information_schema.tables
        WHERE {scope.predicate()} AND table_type = 'VIEW'
        """
    )
    tables = ctx.count_safe(
        f"""
        SELECT count(*) AS n FROM system.information_schema.tables
        WHERE {scope.predicate()} AND table_type <> 'VIEW'
        """
    )
    functions = ctx.count_safe(
        f"""
        SELECT count(*) AS n FROM system.information_schema.routines
        WHERE {scope.predicate('routine_catalog', 'routine_schema')}
        """
    )
    # Metric views are the semantic-consistency primitive; detect via table_type.
    metric_views = ctx.count_safe(
        f"""
        SELECT count(*) AS n FROM system.information_schema.tables
        WHERE {scope.predicate()}
          AND lower(coalesce(table_type, '')) LIKE '%metric%'
        """
    )
    total = views + tables
    if total == 0:
        return Result(
            "IU-04-02", "", "", "", STATUS_OPEN,
            "No tables or views found in the assessed catalogs, so semantic consistency "
            "cannot be assessed.",
            {"scoped": True},
        )
    semantic_layer = views + metric_views + functions
    return ratio_result(
        "IU-04-02",
        min(semantic_layer, total),
        total,
        f"data assets in {scope.label} are exposed through a semantic layer (views, "
        "metric views or governed functions) that encodes shared business definitions "
        "rather than raw tables",
        complete_at=0.30,
        extra=(
            f"{views} view(s), {metric_views} metric view(s) and {functions} governed "
            "function(s) provide consistent definitions over "
            f"{tables} base table(s)."
            + (
                ""
                if metric_views
                else " No metric views found; these are the strongest guarantee of "
                "consistent metric definitions across tools."
            )
        ),
        metrics={"views": views, "metric_views": metric_views, "functions": functions,
                 "tables": tables, "scoped": True},
    )


def iu_04_03(ctx: Ctx) -> Result:
    """Provide a central catalog for discovery and lineage.

    A central catalog with provenance means the tables are governed by Unity
    Catalog, which emits lineage automatically. The gap is tables stranded in
    the legacy hive_metastore. Scored as UC tables / (UC + hive) tables.
    """
    scope = ctx.scope
    uc_tables = ctx.count_safe(
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
            "IU-04-03", "", "", "", STATUS_OPEN,
            "No tables found in Unity Catalog or hive_metastore, so central cataloging "
            "cannot be assessed.",
            {"scoped": True},
        )
    column_lineage = 0
    if ctx.has_table("system.access.column_lineage"):
        column_lineage = ctx.count_safe(
            f"""
            SELECT count(*) AS n FROM system.access.column_lineage
            WHERE event_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
            """
        )
    return ratio_result(
        "IU-04-03",
        uc_tables,
        total,
        f"{scope.label} tables are governed by Unity Catalog (a central catalog with "
        "automatic lineage) rather than stranded in the legacy hive_metastore",
        complete_at=ctx.complete_at,
        none_found=(
            "No tables found in Unity Catalog or hive_metastore, so central cataloging "
            "cannot be assessed."
        ),
        extra=(
            f"{column_lineage:,} column-level lineage event(s) recorded in the last "
            f"{ctx.lookback_days} days."
            + (
                f" {hive_tables:,} table(s) remain outside UC in hive_metastore, so the "
                "catalog is not fully central."
                if hive_tables
                else " No tables remain in the legacy hive_metastore."
            )
        ),
        metrics={"column_lineage_events": column_lineage,
                 "hive_metastore_tables": hive_tables, "uc_tables": uc_tables,
                 "scoped": True},
    )


CHECKS = [
    ("IU-01-01", iu_01_01),
    ("IU-01-02", iu_01_02),
    ("IU-01-03", iu_01_03),
    ("IU-01-04", iu_01_04),
    ("IU-02-01", iu_02_01),
    ("IU-02-02", iu_02_02),
    ("IU-02-03", iu_02_03),
    ("IU-03-01", iu_03_01),
    ("IU-03-02", iu_03_02),
    ("IU-03-03", iu_03_03),
    ("IU-03-04", iu_03_04),
    ("IU-04-01", iu_04_01),
    ("IU-04-02", iu_04_02),
    ("IU-04-03", iu_04_03),
]
