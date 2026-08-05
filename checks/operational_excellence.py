"""Operational Excellence pillar checks (OE-*).

Evidence sources: ``system.lakeflow.*``, ``system.access.audit``, ``system.mlflow.*``,
``system.serving.*``, ``system.information_schema.*``, and the Workspace SDK for
Git folders, cluster policies and model registry.
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

PILLAR_ID = "operational-excellence"


def _sdk_count(ctx: Ctx, attr: str, sub: str = "list") -> int | None:
    """Count items from an SDK collection, or ``None`` when unavailable."""
    w = ctx.w
    if w is None:
        return None
    try:
        api = getattr(w, attr)
        return sum(1 for _ in getattr(api, sub)())
    except Exception:
        return None


# ----------------------------------------------------------------------------------
# Optimize build and release processes
# ----------------------------------------------------------------------------------


def oe_01_01(ctx: Ctx) -> Result:
    """Create a dedicated Lakehouse operations team."""
    return manual_result(
        "OE-01-01",
        "team structure and on-call ownership are organizational facts with no platform "
        "representation.",
        "who owns platform operations, the on-call rota, and how escalations reach them.",
    )


def oe_01_02(ctx: Ctx) -> Result:
    """Use enterprise source code management (SCM)."""
    repos = _sdk_count(ctx, "repos")
    git_creds = _sdk_count(ctx, "git_credentials")
    # Git-backed job deployments are the strongest signal that SCM is really in the loop.
    git_jobs = 0
    total_jobs = 0
    if ctx.has_table("system.lakeflow.jobs"):
        row = ctx.one(
            """
            WITH latest AS (
              SELECT job_id, deployment,
                     row_number() OVER (PARTITION BY job_id ORDER BY change_time DESC) AS rn
              FROM system.lakeflow.jobs WHERE delete_time IS NULL
            )
            SELECT count(*) AS total,
                   count_if(deployment IS NOT NULL AND deployment.kind IS NOT NULL) AS git_backed
            FROM latest WHERE rn = 1
            """
        )
        total_jobs = int(row.get("total") or 0)
        git_jobs = int(row.get("git_backed") or 0)

    if not repos and not git_creds and git_jobs == 0:
        return Result(
            "OE-01-02", "", "", "", STATUS_OPEN,
            "No Git folders, Git credentials, or Git-backed job deployments found, so "
            "there is no evidence that workspace code is under source control. Code may "
            "live in an external repo that never touches this workspace - confirm with "
            "the customer.",
            {"repos": repos, "git_credentials": git_creds},
        )
    if total_jobs:
        return ratio_result(
            "OE-01-02",
            git_jobs,
            total_jobs,
            "active jobs are deployed from a Git source or bundle rather than "
            "hand-edited in the workspace",
            complete_at=ctx.complete_at,
            extra=(
                f"{repos if repos is not None else 'unknown'} Git folder(s) and "
                f"{git_creds if git_creds is not None else 'unknown'} Git credential(s) "
                "configured."
            ),
            metrics={"repos": repos, "git_credentials": git_creds,
                     "git_backed_jobs": git_jobs},
        )
    return Result(
        "OE-01-02", "", "", "", STATUS_IN_PROGRESS,
        f"Source control is partially in evidence: {repos} Git folder(s) and "
        f"{git_creds} Git credential(s) exist, but no jobs were found to confirm "
        "deployments originate from Git.",
        {"repos": repos, "git_credentials": git_creds},
    )


def oe_01_03(ctx: Ctx) -> Result:
    """Standardize DevOps processes (CI/CD)."""
    if not ctx.has_table("system.lakeflow.jobs"):
        return Result(
            "OE-01-03", "", "", "", STATUS_OPEN,
            "system.lakeflow.jobs is not readable, so deployment automation cannot be "
            "assessed.",
        )
    row = ctx.one(
        """
        WITH latest AS (
          SELECT job_id, deployment, run_as, creator_user_name,
                 row_number() OVER (PARTITION BY job_id ORDER BY change_time DESC) AS rn
          FROM system.lakeflow.jobs WHERE delete_time IS NULL
        )
        SELECT count(*) AS total,
               count_if(deployment.kind IS NOT NULL) AS bundle_deployed
        FROM latest WHERE rn = 1
        """
    )
    total = int(row.get("total") or 0)
    bundles = int(row.get("bundle_deployed") or 0)
    return ratio_result(
        "OE-01-03",
        bundles,
        total,
        "active jobs are deployed declaratively (Databricks Asset Bundles or an "
        "equivalent deployment kind), which is the observable trace of a CI/CD pipeline",
        complete_at=ctx.complete_at,
        none_found=(
            "No active jobs found, so there is no deployment activity to assess for "
            "CI/CD practice."
        ),
        extra=(
            "Pipeline definitions living in external CI (GitHub Actions, Azure DevOps) "
            "are not visible from the workspace - confirm the release process with the "
            "customer."
        ),
        metrics={"bundle_deployed_jobs": bundles},
    )


def oe_01_04(ctx: Ctx) -> Result:
    """Standardize MLOps processes across enterprise."""
    uc_models = _sdk_count(ctx, "registered_models")
    experiments = 0
    runs = 0
    if ctx.has_table("system.mlflow.experiments_latest"):
        experiments = ctx.count_safe(
            "SELECT count(*) AS n FROM system.mlflow.experiments_latest "
            "WHERE delete_time IS NULL"
        )
    if ctx.has_table("system.mlflow.runs_latest"):
        runs = ctx.count_safe(
            f"""
            SELECT count(*) AS n FROM system.mlflow.runs_latest
            WHERE delete_time IS NULL
              AND start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
            """
        )
    if not uc_models and experiments == 0 and runs == 0:
        return Result(
            "OE-01-04", "", "", "", STATUS_OPEN,
            "No registered models, MLflow experiments, or recent runs found, so no MLOps "
            "practice is observable. Mark ignore in the WAF tool if ML is out of scope.",
            {"uc_models": uc_models, "experiments": experiments, "runs": runs},
        )
    models = uc_models or 0
    if models == 0:
        return Result(
            "OE-01-04", "", "", "", STATUS_IN_PROGRESS,
            f"ML activity exists ({experiments} experiment(s), {runs:,} run(s) in the "
            f"last {ctx.lookback_days} days) but no models are registered in Unity "
            "Catalog, so promotion between environments is not standardized.",
            {"uc_models": 0, "experiments": experiments, "runs": runs},
        )
    return Result(
        "OE-01-04", "", "", "", STATUS_COMPLETED,
        f"MLOps is standardized on Unity Catalog: {models} registered model(s), "
        f"{experiments} experiment(s), and {runs:,} tracked run(s) in the last "
        f"{ctx.lookback_days} days.",
        {"uc_models": models, "experiments": experiments, "runs": runs},
    )


def oe_01_05(ctx: Ctx) -> Result:
    """Define environment isolation strategy."""
    sys_cats = ctx.cfg.get("system_catalogs", [])
    from waf_core import _like_clause  # noqa: PLC0415

    not_system = _like_clause("catalog_name", sys_cats, negate=True)
    rows = ctx.sql(
        f"""
        SELECT catalog_name FROM system.information_schema.catalogs
        WHERE {not_system} ORDER BY 1
        """
    )
    names = [r["catalog_name"].lower() for r in rows]
    envs = {
        "prod": [n for n in names if any(t in n for t in ("prod", "prd"))],
        "dev": [n for n in names if any(t in n for t in ("dev", "sandbox"))],
        "test": [n for n in names if any(t in n for t in ("test", "qa", "stag", "uat"))],
    }
    found = {k: v for k, v in envs.items() if v}
    workspaces = 0
    if ctx.has_table("system.access.workspaces_latest"):
        workspaces = ctx.count_safe(
            "SELECT count(DISTINCT workspace_id) AS n FROM system.access.workspaces_latest"
        )
    if not found:
        return Result(
            "OE-01-05", "", "", "", STATUS_OPEN,
            f"No environment separation detectable by naming convention across "
            f"{len(names)} catalog(s) and {workspaces} workspace(s). Isolation may be "
            "implemented per-workspace or under a different naming standard - confirm "
            "with the customer.",
            {"catalogs": len(names), "workspaces": workspaces},
        )
    detail = "; ".join(f"{k}: {len(v)} catalog(s)" for k, v in found.items())
    # Separating prod from at least one lower environment is the bar.
    status = (
        STATUS_COMPLETED
        if "prod" in found and len(found) >= 2
        else STATUS_IN_PROGRESS
    )
    return Result(
        "OE-01-05", "", "", "", status,
        f"{len(found)} of 3 environment tiers are distinguishable by catalog naming "
        f"({detail}) across {len(names)} catalog(s) and {workspaces} workspace(s). "
        + (
            "Production is separated from lower environments."
            if status == STATUS_COMPLETED
            else "Production is not clearly separated from lower environments."
        ),
        {"environments": {k: len(v) for k, v in found.items()},
         "catalogs": len(names), "workspaces": workspaces},
    )


def oe_01_06(ctx: Ctx) -> Result:
    """Streamline the usage and management of various LLM providers."""
    if not ctx.has_table("system.serving.served_entities"):
        return Result(
            "OE-01-06", "", "", "", STATUS_OPEN,
            "system.serving.served_entities is not readable, so LLM provider management "
            "cannot be assessed.",
        )
    row = ctx.one(
        """
        SELECT count(DISTINCT endpoint_id) AS endpoints,
               count_if(external_model_config IS NOT NULL) AS external,
               count_if(foundation_model_config IS NOT NULL) AS foundation,
               count(DISTINCT external_model_config.provider) AS providers
        FROM system.serving.served_entities
        WHERE endpoint_delete_time IS NULL
        """
    )
    endpoints = int(row.get("endpoints") or 0)
    external = int(row.get("external") or 0)
    foundation = int(row.get("foundation") or 0)
    providers = int(row.get("providers") or 0)
    gateway = 0
    if ctx.has_table("system.ai_gateway.usage"):
        gateway = ctx.count_safe(
            f"""
            SELECT count(*) AS n FROM system.ai_gateway.usage
            WHERE request_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
            """
        )
    if endpoints == 0:
        return Result(
            "OE-01-06", "", "", "", STATUS_OPEN,
            "No serving endpoints found, so no LLM providers are managed through "
            "Databricks. Mark ignore in the WAF tool if GenAI is out of scope.",
            {"endpoints": 0},
        )
    if external + foundation == 0:
        return Result(
            "OE-01-06", "", "", "", STATUS_IN_PROGRESS,
            f"{endpoints} serving endpoint(s) exist but none are foundation-model or "
            "external-model endpoints, so LLM access is not centralized behind the "
            "gateway.",
            {"endpoints": endpoints},
        )
    return Result(
        "OE-01-06", "", "", "", STATUS_COMPLETED,
        f"LLM access is centralized through Databricks: {foundation} foundation-model "
        f"and {external} external-model served entity/entities across {providers} "
        f"external provider(s)"
        + (
            f", with {gateway:,} AI Gateway request(s) logged in the last "
            f"{ctx.lookback_days} days."
            if gateway
            else ". No AI Gateway usage records were found, so per-request governance "
            "may not be enabled."
        ),
        {"endpoints": endpoints, "external": external, "foundation": foundation,
         "providers": providers, "gateway_requests": gateway},
    )


def oe_01_07(ctx: Ctx) -> Result:
    """Define catalog strategy for your enterprise using Unity Catalog."""
    sys_cats = ctx.cfg.get("system_catalogs", [])
    from waf_core import _like_clause  # noqa: PLC0415

    not_system = _like_clause("catalog_name", sys_cats, negate=True)
    row = ctx.one(
        f"""
        SELECT count(*) AS total,
               count_if(comment IS NOT NULL AND trim(comment) <> '') AS documented,
               count_if(catalog_owner IS NOT NULL AND trim(catalog_owner) <> '') AS owned
        FROM system.information_schema.catalogs
        WHERE {not_system}
        """
    )
    total = int(row.get("total") or 0)
    documented = int(row.get("documented") or 0)
    hive_tables = ctx.count_safe(
        """
        SELECT count(*) AS n FROM system.information_schema.tables
        WHERE lower(table_catalog) = 'hive_metastore'
        """
    )
    return ratio_result(
        "OE-01-07",
        documented,
        total,
        "non-system catalogs carry a description, indicating a deliberate catalog "
        "strategy rather than ad-hoc creation",
        complete_at=ctx.complete_at,
        none_found=(
            "No non-system catalogs found, so no Unity Catalog strategy is in evidence."
        ),
        extra=(
            f"{row.get('owned')} of {total} catalog(s) have an explicit owner."
            + (
                f" {hive_tables:,} table(s) still live in the legacy hive_metastore."
                if hive_tables
                else " No legacy hive_metastore tables remain."
            )
        ),
        metrics={"catalogs": total, "owned": row.get("owned"),
                 "hive_metastore_tables": hive_tables},
    )


def oe_01_08(ctx: Ctx) -> Result:
    """Compare LLM outputs on set prompts."""
    evals = 0
    if ctx.has_table("system.mlflow.runs_latest"):
        evals = ctx.count_safe(
            f"""
            SELECT count(*) AS n FROM system.mlflow.runs_latest
            WHERE delete_time IS NULL
              AND start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND (lower(coalesce(run_name, '')) RLIKE '(eval|judge|benchmark)'
                   OR lower(coalesce(to_json(tags), '')) RLIKE '(evaluation|mlflow.evaluate|judge)')
            """
        )
    metrics_rows = 0
    if ctx.has_table("system.mlflow.run_metrics_history"):
        metrics_rows = ctx.count_safe(
            f"""
            SELECT count(*) AS n FROM system.mlflow.run_metrics_history
            WHERE metric_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND lower(coalesce(metric_name, '')) RLIKE
                  '(toxicity|relevance|faithful|correctness|groundedness|answer_|rouge|bleu|judge)'
            """
        )
    if evals == 0 and metrics_rows == 0:
        return Result(
            "OE-01-08", "", "", "", STATUS_OPEN,
            f"No LLM evaluation runs or GenAI quality metrics found in the last "
            f"{ctx.lookback_days} days, so model outputs are not being compared "
            "systematically. Mark ignore in the WAF tool if GenAI is out of scope.",
            {"evaluation_runs": 0, "quality_metrics": 0},
        )
    status = STATUS_COMPLETED if metrics_rows > 0 else STATUS_IN_PROGRESS
    return Result(
        "OE-01-08", "", "", "", status,
        f"LLM output comparison is in evidence: {evals:,} evaluation-tagged run(s) and "
        f"{metrics_rows:,} GenAI quality metric record(s) in the last "
        f"{ctx.lookback_days} days. "
        + (
            "Scored metrics indicate a repeatable evaluation harness."
            if metrics_rows
            else "Evaluation runs were found but no quality metrics were logged, so "
            "comparisons may be manual."
        ),
        {"evaluation_runs": evals, "quality_metrics": metrics_rows},
    )


def oe_01_09(ctx: Ctx) -> Result:
    """Build models with all representative, accurate and relevant data sources."""
    return manual_result(
        "OE-01-09",
        "whether training data is representative and covers the relevant sources is a "
        "data-science judgement that telemetry cannot evaluate.",
        "the feature/training data inventory, how source coverage was validated, and "
        "how bias and drift are reviewed.",
    )


# ----------------------------------------------------------------------------------
# Automate deployments and workloads
# ----------------------------------------------------------------------------------


def oe_02_01(ctx: Ctx) -> Result:
    """Use Infrastructure as Code for deployments and maintenance."""
    if not ctx.has_table("system.lakeflow.jobs"):
        return Result(
            "OE-02-01", "", "", "", STATUS_OPEN,
            "system.lakeflow.jobs is not readable, so IaC adoption cannot be assessed.",
        )
    row = ctx.one(
        """
        WITH latest AS (
          SELECT job_id, deployment,
                 row_number() OVER (PARTITION BY job_id ORDER BY change_time DESC) AS rn
          FROM system.lakeflow.jobs WHERE delete_time IS NULL
        )
        SELECT count(*) AS total,
               count_if(deployment.kind IS NOT NULL) AS iac
        FROM latest WHERE rn = 1
        """
    )
    total = int(row.get("total") or 0)
    iac = int(row.get("iac") or 0)
    terraform = 0
    if ctx.has_table("system.access.audit"):
        terraform = ctx.count_safe(
            f"""
            SELECT count(*) AS n FROM system.access.audit
            WHERE event_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
              AND lower(coalesce(user_agent, '')) RLIKE '(terraform|pulumi|cdk)'
            """
        )
    return ratio_result(
        "OE-02-01",
        iac,
        total,
        "active jobs carry a declarative deployment record (Asset Bundles or equivalent), "
        "indicating they are managed as code rather than clicked together",
        complete_at=ctx.complete_at,
        none_found="No active jobs found, so IaC adoption cannot be measured.",
        extra=(
            f"{terraform:,} audited API call(s) came from Terraform/Pulumi/CDK user "
            f"agents in the last {ctx.lookback_days} days."
            if terraform
            else "No Terraform/Pulumi/CDK API activity was observed in the audit log."
        ),
        metrics={"iac_jobs": iac, "terraform_api_calls": terraform},
    )


def oe_02_02(ctx: Ctx) -> Result:
    """Standardize compute configurations."""
    policies = _sdk_count(ctx, "cluster_policies")
    if not ctx.has_table("system.compute.clusters"):
        return Result(
            "OE-02-02", "", "", "", STATUS_OPEN,
            "system.compute.clusters is not readable, so compute standardization cannot "
            "be assessed.",
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
               count_if(policy_id IS NOT NULL AND policy_id <> '') AS policy_backed
        FROM latest WHERE rn = 1
        """
    )
    total = int(row.get("total") or 0)
    governed = int(row.get("policy_backed") or 0)
    if total == 0:
        return presence_result(
            "OE-02-02",
            policies,
            "cluster policy/policies defining standard compute configurations",
            found_note=(
                f"No classic clusters were active in the last {ctx.lookback_days} days, "
                "so policy enforcement could not be measured directly."
            ),
            missing_note=(
                "No cluster policies and no active clusters; compute standards cannot be "
                "confirmed."
            ),
            metrics={"policies": policies},
        )
    return ratio_result(
        "OE-02-02",
        governed,
        total,
        "active clusters were created from a cluster policy, so compute configuration is "
        "standardized rather than per-user",
        complete_at=ctx.complete_at,
        extra=(
            f"{policies} policy/policies defined."
            if policies is not None
            else "Policy inventory unavailable via the SDK."
        ),
        metrics={"policies": policies, "governed_clusters": governed},
    )


def oe_02_03(ctx: Ctx) -> Result:
    """Use automated workflows for jobs."""
    if not ctx.has_table("system.lakeflow.jobs"):
        return Result(
            "OE-02-03", "", "", "", STATUS_OPEN,
            "system.lakeflow.jobs is not readable, so job scheduling cannot be assessed.",
        )
    row = ctx.one(
        """
        WITH latest AS (
          SELECT job_id, trigger, trigger_type, paused,
                 row_number() OVER (PARTITION BY job_id ORDER BY change_time DESC) AS rn
          FROM system.lakeflow.jobs WHERE delete_time IS NULL
        )
        SELECT count(*) AS total,
               count_if(trigger IS NOT NULL
                        AND (trigger.schedule IS NOT NULL
                             OR trigger.file_arrival IS NOT NULL
                             OR trigger.periodic IS NOT NULL
                             OR trigger.table_update IS NOT NULL
                             OR trigger.continuous IS NOT NULL)) AS triggered,
               count_if(coalesce(paused, false)) AS paused
        FROM latest WHERE rn = 1
        """
    )
    total = int(row.get("total") or 0)
    triggered = int(row.get("triggered") or 0)
    paused = int(row.get("paused") or 0)
    return ratio_result(
        "OE-02-03",
        triggered,
        total,
        "active jobs have an automated trigger (schedule, file arrival, table update or "
        "continuous) rather than relying on manual runs",
        complete_at=ctx.complete_at,
        none_found="No active jobs found, so workflow automation cannot be assessed.",
        extra=(
            f"{paused} job(s) are currently paused."
            if paused
            else "No jobs are paused."
        ),
        metrics={"triggered_jobs": triggered, "paused_jobs": paused},
    )


def oe_02_04(ctx: Ctx) -> Result:
    """Use automated and event driven file ingestion."""
    file_arrival = 0
    if ctx.has_table("system.lakeflow.jobs"):
        file_arrival = ctx.count_safe(
            """
            WITH latest AS (
              SELECT job_id, trigger,
                     row_number() OVER (PARTITION BY job_id ORDER BY change_time DESC) AS rn
              FROM system.lakeflow.jobs WHERE delete_time IS NULL
            )
            SELECT count(*) AS n FROM latest
            WHERE rn = 1 AND trigger.file_arrival IS NOT NULL
            """
        )
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
    rescued = 0
    scope = ctx.scope
    rescued = ctx.count_safe(
        f"""
        SELECT count(DISTINCT concat_ws('.', table_catalog, table_schema, table_name)) AS n
        FROM system.information_schema.columns
        WHERE {scope.predicate()} AND lower(column_name) = '_rescued_data'
        """
    )
    connectors = 0
    if ctx.has_table("system.lakeflow.pipelines"):
        connectors = ctx.count_safe(
            """
            SELECT count(*) AS n FROM system.lakeflow.pipelines
            WHERE delete_time IS NULL
              AND lower(coalesce(pipeline_type, '')) RLIKE '(ingestion|managed)'
            """
        )
    signals = []
    if file_arrival:
        signals.append(f"{file_arrival} job(s) with file-arrival triggers")
    if autoloader:
        signals.append(f"{autoloader:,} Auto Loader / read_files statement(s)")
    if rescued:
        signals.append(f"{rescued} table(s) with Auto Loader rescued-data columns")
    if connectors:
        signals.append(f"{connectors} managed ingestion pipeline(s)")
    if not signals:
        return Result(
            "OE-02-04", "", "", "", STATUS_OPEN,
            f"No file-arrival triggers, Auto Loader usage, or managed ingestion "
            f"pipelines found in the last {ctx.lookback_days} days, so file ingestion is "
            "likely scheduled or manual rather than event driven.",
            {"scoped": True},
        )
    status = STATUS_COMPLETED if (file_arrival or autoloader) else STATUS_IN_PROGRESS
    return Result(
        "OE-02-04", "", "", "", status,
        "Event-driven ingestion is in use: " + "; ".join(signals) + ".",
        {"file_arrival_jobs": file_arrival, "autoloader_statements": autoloader,
         "rescued_data_tables": rescued, "ingestion_pipelines": connectors,
         "scoped": True},
    )


def oe_02_05(ctx: Ctx) -> Result:
    """Use ETL frameworks for data pipelines."""
    pipelines = 0
    if ctx.has_table("system.lakeflow.pipelines"):
        pipelines = ctx.count_safe(
            "SELECT count(*) AS n FROM system.lakeflow.pipelines WHERE delete_time IS NULL"
        )
    jobs = 0
    if ctx.has_table("system.lakeflow.jobs"):
        jobs = ctx.count_safe(
            "SELECT count(DISTINCT job_id) AS n FROM system.lakeflow.jobs "
            "WHERE delete_time IS NULL"
        )
    total = pipelines + jobs
    if total == 0:
        return Result(
            "OE-02-05", "", "", "", STATUS_OPEN,
            "No pipelines or jobs found, so no ETL framework is in evidence.",
        )
    return ratio_result(
        "OE-02-05",
        pipelines,
        total,
        "data workloads are declarative Lakeflow pipelines (which manage dependencies, "
        "checkpoints and retries) rather than hand-orchestrated jobs",
        complete_at=0.30,
        extra=(
            f"{pipelines} declarative pipeline(s) alongside {jobs} job(s). A low ratio is "
            "acceptable when jobs wrap a bespoke framework, but declarative pipelines "
            "remove most of that maintenance."
        ),
        metrics={"pipelines": pipelines, "jobs": jobs},
    )


def oe_02_06(ctx: Ctx) -> Result:
    """Follow the deploy-code approach for ML workloads."""
    uc_models = _sdk_count(ctx, "registered_models")
    if not uc_models:
        return Result(
            "OE-02-06", "", "", "", STATUS_OPEN,
            "No models registered in Unity Catalog, so the deploy-code pattern cannot be "
            "confirmed. Mark ignore in the WAF tool if ML is out of scope.",
            {"uc_models": uc_models},
        )
    ml_jobs = 0
    if ctx.has_table("system.lakeflow.jobs"):
        ml_jobs = ctx.count_safe(
            f"""
            WITH latest AS (
              SELECT job_id, name, deployment,
                     row_number() OVER (PARTITION BY job_id ORDER BY change_time DESC) AS rn
              FROM system.lakeflow.jobs WHERE delete_time IS NULL
            )
            SELECT count(*) AS n FROM latest
            WHERE rn = 1
              AND lower(coalesce(name, '')) RLIKE '(train|model|ml|feature|score|predict|inference)'
            """
        )
    if ml_jobs == 0:
        return Result(
            "OE-02-06", "", "", "", STATUS_IN_PROGRESS,
            f"{uc_models} model(s) are registered in Unity Catalog but no ML-named "
            "training or scoring jobs were found, so retraining may not be automated as "
            "deployed code.",
            {"uc_models": uc_models, "ml_jobs": 0},
        )
    return Result(
        "OE-02-06", "", "", "", STATUS_COMPLETED,
        f"Deploy-code is in evidence: {ml_jobs} automated ML job(s) (training/scoring) "
        f"alongside {uc_models} UC-registered model(s), so code is promoted and models "
        "are produced per environment.",
        {"uc_models": uc_models, "ml_jobs": ml_jobs},
    )


def oe_02_07(ctx: Ctx) -> Result:
    """Use a model registry to decouple code and model lifecycle."""
    uc_models = _sdk_count(ctx, "registered_models")
    if uc_models is None:
        return Result(
            "OE-02-07", "", "", "", STATUS_OPEN,
            "The registered-models API is not reachable, so registry adoption cannot be "
            "assessed.",
        )
    if uc_models == 0:
        return Result(
            "OE-02-07", "", "", "", STATUS_OPEN,
            "No models registered in Unity Catalog, so there is no registry decoupling "
            "code from model versions. Mark ignore in the WAF tool if ML is out of scope.",
            {"uc_models": 0},
        )
    served = 0
    if ctx.has_table("system.serving.served_entities"):
        served = ctx.count_safe(
            """
            SELECT count(DISTINCT entity_name) AS n FROM system.serving.served_entities
            WHERE endpoint_delete_time IS NULL AND entity_type = 'CUSTOM_MODEL'
            """
        )
    return Result(
        "OE-02-07", "", "", "", STATUS_COMPLETED,
        f"{uc_models} model(s) are registered in Unity Catalog, giving versioned model "
        f"artifacts independent of code releases; {served} distinct model(s) are served "
        "from the registry.",
        {"uc_models": uc_models, "served_models": served},
    )


def oe_02_08(ctx: Ctx) -> Result:
    """Automate ML experiment tracking."""
    if not ctx.has_table("system.mlflow.runs_latest"):
        return Result(
            "OE-02-08", "", "", "", STATUS_OPEN,
            "system.mlflow.runs_latest is not readable, so experiment tracking cannot be "
            "assessed.",
        )
    row = ctx.one(
        f"""
        SELECT count(*) AS runs,
               count_if(params IS NOT NULL
                        AND lower(to_json(params)) NOT IN ('null', '{{}}', '[]')) AS with_params,
               count_if(aggregated_metrics IS NOT NULL
                        AND lower(to_json(aggregated_metrics)) NOT IN ('null', '{{}}', '[]')) AS with_metrics
        FROM system.mlflow.runs_latest
        WHERE delete_time IS NULL
          AND start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
        """
    )
    runs = int(row.get("runs") or 0)
    if runs == 0:
        return Result(
            "OE-02-08", "", "", "", STATUS_OPEN,
            f"No MLflow runs in the last {ctx.lookback_days} days, so experiment tracking "
            "is not in use. Mark ignore in the WAF tool if ML is out of scope.",
            {"runs": 0},
        )
    with_metrics = int(row.get("with_metrics") or 0)
    return ratio_result(
        "OE-02-08",
        with_metrics,
        runs,
        f"MLflow runs in the last {ctx.lookback_days} days logged metrics, showing "
        "tracking is automatic rather than ad-hoc",
        complete_at=ctx.complete_at,
        extra=f"{row.get('with_params')} run(s) also logged parameters.",
        metrics={"runs": runs, "runs_with_metrics": with_metrics,
                 "runs_with_params": row.get("with_params")},
    )


def oe_02_09(ctx: Ctx) -> Result:
    """Reuse the same infrastructure to manage ML pipelines."""
    if not ctx.has_table("system.lakeflow.jobs"):
        return Result(
            "OE-02-09", "", "", "", STATUS_OPEN,
            "system.lakeflow.jobs is not readable, so ML orchestration cannot be "
            "assessed.",
        )
    ml_jobs = ctx.count_safe(
        """
        WITH latest AS (
          SELECT job_id, name,
                 row_number() OVER (PARTITION BY job_id ORDER BY change_time DESC) AS rn
          FROM system.lakeflow.jobs WHERE delete_time IS NULL
        )
        SELECT count(*) AS n FROM latest
        WHERE rn = 1
          AND lower(coalesce(name, '')) RLIKE '(train|model|ml|feature|score|predict|inference)'
        """
    )
    total_jobs = ctx.count_safe(
        "SELECT count(DISTINCT job_id) AS n FROM system.lakeflow.jobs "
        "WHERE delete_time IS NULL"
    )
    runs = 0
    if ctx.has_table("system.mlflow.runs_latest"):
        runs = ctx.count_safe(
            f"""
            SELECT count(*) AS n FROM system.mlflow.runs_latest
            WHERE delete_time IS NULL
              AND start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
            """
        )
    if runs == 0 and ml_jobs == 0:
        return Result(
            "OE-02-09", "", "", "", STATUS_OPEN,
            "No ML jobs or MLflow runs found, so ML infrastructure reuse cannot be "
            "assessed. Mark ignore in the WAF tool if ML is out of scope.",
            {"ml_jobs": 0, "runs": 0},
        )
    if ml_jobs == 0:
        return Result(
            "OE-02-09", "", "", "", STATUS_IN_PROGRESS,
            f"{runs:,} MLflow run(s) exist but no ML workloads run as Lakeflow Jobs, so "
            "ML is likely executed interactively rather than on the same orchestration "
            "infrastructure as data pipelines.",
            {"ml_jobs": 0, "runs": runs, "total_jobs": total_jobs},
        )
    return Result(
        "OE-02-09", "", "", "", STATUS_COMPLETED,
        f"ML runs on the same orchestration stack as data workloads: {ml_jobs} of "
        f"{total_jobs} job(s) are ML-related, backed by {runs:,} tracked run(s) in the "
        f"last {ctx.lookback_days} days.",
        {"ml_jobs": ml_jobs, "total_jobs": total_jobs, "runs": runs},
    )


def oe_02_10(ctx: Ctx) -> Result:
    """Utilize declarative management for complex data and ML pipelines."""
    pipelines = 0
    serverless_pipelines = 0
    if ctx.has_table("system.lakeflow.pipelines"):
        row = ctx.one(
            """
            WITH latest AS (
              SELECT pipeline_id, settings,
                     row_number() OVER (PARTITION BY pipeline_id ORDER BY change_time DESC) AS rn
              FROM system.lakeflow.pipelines WHERE delete_time IS NULL
            )
            SELECT count(*) AS total,
                   count_if(settings.serverless = true) AS serverless
            FROM latest WHERE rn = 1
            """
        )
        pipelines = int(row.get("total") or 0)
        serverless_pipelines = int(row.get("serverless") or 0)
    multi_task = 0
    total_jobs = 0
    if ctx.has_table("system.lakeflow.job_tasks"):
        multi_task = ctx.count_safe(
            """
            SELECT count(DISTINCT job_id) AS n FROM system.lakeflow.job_tasks
            WHERE delete_time IS NULL
              AND depends_on_keys IS NOT NULL AND size(depends_on_keys) > 0
            """
        )
    if ctx.has_table("system.lakeflow.jobs"):
        total_jobs = ctx.count_safe(
            "SELECT count(DISTINCT job_id) AS n FROM system.lakeflow.jobs "
            "WHERE delete_time IS NULL"
        )
    declarative = pipelines + multi_task
    total = pipelines + total_jobs
    if total == 0:
        return Result(
            "OE-02-10", "", "", "", STATUS_OPEN,
            "No pipelines or jobs found, so declarative management cannot be assessed.",
        )
    return ratio_result(
        "OE-02-10",
        min(declarative, total),
        total,
        "workloads are managed declaratively (Lakeflow pipelines or jobs with declared "
        "task dependency graphs) rather than as imperative scripts",
        complete_at=ctx.complete_at,
        extra=(
            f"{pipelines} pipeline(s) ({serverless_pipelines} serverless) and "
            f"{multi_task} of {total_jobs} job(s) with task dependencies."
        ),
        metrics={"pipelines": pipelines, "serverless_pipelines": serverless_pipelines,
                 "multi_task_jobs": multi_task, "jobs": total_jobs},
    )


def oe_02_11(ctx: Ctx) -> Result:
    """Automate LLM evaluation."""
    eval_jobs = 0
    if ctx.has_table("system.lakeflow.jobs"):
        eval_jobs = ctx.count_safe(
            """
            WITH latest AS (
              SELECT job_id, name,
                     row_number() OVER (PARTITION BY job_id ORDER BY change_time DESC) AS rn
              FROM system.lakeflow.jobs WHERE delete_time IS NULL
            )
            SELECT count(*) AS n FROM latest
            WHERE rn = 1 AND lower(coalesce(name, '')) RLIKE '(eval|judge|benchmark|quality)'
            """
        )
    metrics_rows = 0
    if ctx.has_table("system.mlflow.run_metrics_history"):
        metrics_rows = ctx.count_safe(
            f"""
            SELECT count(*) AS n FROM system.mlflow.run_metrics_history
            WHERE metric_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND lower(coalesce(metric_name, '')) RLIKE
                  '(toxicity|relevance|faithful|correctness|groundedness|answer_|judge)'
            """
        )
    endpoints = 0
    if ctx.has_table("system.serving.served_entities"):
        endpoints = ctx.count_safe(
            """
            SELECT count(DISTINCT endpoint_id) AS n FROM system.serving.served_entities
            WHERE endpoint_delete_time IS NULL
              AND (foundation_model_config IS NOT NULL OR external_model_config IS NOT NULL)
            """
        )
    if endpoints == 0 and eval_jobs == 0 and metrics_rows == 0:
        return Result(
            "OE-02-11", "", "", "", STATUS_OPEN,
            "No LLM endpoints, evaluation jobs, or GenAI quality metrics found, so there "
            "is no automated LLM evaluation. Mark ignore in the WAF tool if GenAI is out "
            "of scope.",
            {"eval_jobs": 0, "quality_metrics": 0, "llm_endpoints": 0},
        )
    if eval_jobs == 0:
        return Result(
            "OE-02-11", "", "", "", STATUS_IN_PROGRESS,
            f"{endpoints} LLM endpoint(s) are in use and {metrics_rows:,} quality metric "
            f"record(s) exist, but no scheduled evaluation job was found, so LLM "
            "evaluation is not automated.",
            {"eval_jobs": 0, "quality_metrics": metrics_rows, "llm_endpoints": endpoints},
        )
    return Result(
        "OE-02-11", "", "", "", STATUS_COMPLETED,
        f"LLM evaluation is automated: {eval_jobs} scheduled evaluation job(s) and "
        f"{metrics_rows:,} quality metric record(s) in the last {ctx.lookback_days} days "
        f"across {endpoints} LLM endpoint(s).",
        {"eval_jobs": eval_jobs, "quality_metrics": metrics_rows,
         "llm_endpoints": endpoints},
    )


# ----------------------------------------------------------------------------------
# Set up Monitoring, Alerting and Logging
# ----------------------------------------------------------------------------------


def oe_03_01(ctx: Ctx) -> Result:
    """Establish monitoring processes."""
    available = []
    for fqn, label in (
        ("system.access.audit", "audit logs"),
        ("system.billing.usage", "billing usage"),
        ("system.query.history", "query history"),
        ("system.lakeflow.job_run_timeline", "job runs"),
        ("system.compute.node_timeline", "node metrics"),
    ):
        if ctx.has_table(fqn):
            available.append(label)
    total_expected = 5
    if not available:
        return Result(
            "OE-03-01", "", "", "", STATUS_OPEN,
            "No system-table telemetry is readable, so no monitoring foundation exists.",
        )
    # Enabled telemetry is necessary but not sufficient; someone must query it.
    consumers = 0
    if ctx.has_table("system.query.history"):
        consumers = ctx.count_safe(
            f"""
            SELECT count(DISTINCT executed_by) AS n FROM system.query.history
            WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND lower(statement_text) LIKE '%system.%'
              AND lower(statement_text) NOT LIKE '%information_schema%'
            """
        )
    res = ratio_result(
        "OE-03-01",
        len(available),
        total_expected,
        "core observability system schemas are enabled and readable",
        complete_at=ctx.complete_at,
        extra=(
            f"Available: {', '.join(available)}. "
            + (
                f"{consumers} distinct principal(s) queried system tables in the last "
                f"{ctx.lookback_days} days."
                if consumers
                else "No principals queried system tables in the lookback window, so "
                "telemetry is collected but not reviewed."
            )
        ),
        metrics={"available_schemas": available, "system_table_consumers": consumers},
    )
    if res.status == STATUS_COMPLETED and consumers == 0:
        res.status = STATUS_IN_PROGRESS
        res.reason += " Downgraded to in-progress: nobody is consuming the telemetry."
    return res


def oe_03_02(ctx: Ctx) -> Result:
    """Use native and external tools for platform monitoring."""
    dashboards = 0
    alerts = 0
    if ctx.has_table("system.query.history"):
        dashboards = ctx.count_safe(
            f"""
            SELECT count(DISTINCT coalesce(query_source.dashboard_id,
                                           query_source.legacy_dashboard_id)) AS n
            FROM system.query.history
            WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND lower(statement_text) LIKE '%system.%'
              AND (query_source.dashboard_id IS NOT NULL
                   OR query_source.legacy_dashboard_id IS NOT NULL)
            """
        )
        alerts = ctx.count_safe(
            f"""
            SELECT count(DISTINCT query_source.alert_id) AS n FROM system.query.history
            WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND query_source.alert_id IS NOT NULL
            """
        )
    external = 0
    if ctx.has_table("system.access.audit"):
        external = ctx.count_safe(
            f"""
            SELECT count(DISTINCT user_agent) AS n FROM system.access.audit
            WHERE event_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
              AND lower(coalesce(user_agent, '')) RLIKE
                  '(datadog|splunk|grafana|prometheus|newrelic|dynatrace|elastic)'
            """
        )
    if dashboards == 0 and alerts == 0 and external == 0:
        return Result(
            "OE-03-02", "", "", "", STATUS_OPEN,
            f"No monitoring dashboards, alerts, or external observability tool activity "
            f"detected in the last {ctx.lookback_days} days, so platform monitoring is "
            "not operationalized.",
            {"dashboards": 0, "alerts": 0, "external_agents": 0},
        )
    tools = sum(1 for x in (dashboards, alerts, external) if x)
    status = STATUS_COMPLETED if tools >= 2 else STATUS_IN_PROGRESS
    return Result(
        "OE-03-02", "", "", "", status,
        f"{tools} of 3 monitoring channel(s) in use: {dashboards} dashboard(s) over "
        f"system tables, {alerts} alert(s), and {external} external observability "
        f"agent(s) seen in the audit log over the last {ctx.lookback_days} days.",
        {"dashboards": dashboards, "alerts": alerts, "external_agents": external},
    )


def oe_03_03(ctx: Ctx) -> Result:
    """Establish an incident response strategy."""
    return manual_result(
        "OE-03-03",
        "incident response runbooks, severity definitions and escalation paths are "
        "process artifacts with no platform footprint.",
        "the incident response runbook, severity matrix, on-call rota, and the date of "
        "the last incident review.",
    )


def oe_03_04(ctx: Ctx) -> Result:
    """Triggering actions in response to a specific event."""
    alerts = 0
    if ctx.has_table("system.query.history"):
        alerts = ctx.count_safe(
            f"""
            SELECT count(DISTINCT query_source.alert_id) AS n FROM system.query.history
            WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND query_source.alert_id IS NOT NULL
            """
        )
    health_rules = 0
    total_jobs = 0
    event_triggers = 0
    if ctx.has_table("system.lakeflow.jobs"):
        row = ctx.one(
            """
            WITH latest AS (
              SELECT job_id, health_rules, trigger,
                     row_number() OVER (PARTITION BY job_id ORDER BY change_time DESC) AS rn
              FROM system.lakeflow.jobs WHERE delete_time IS NULL
            )
            SELECT count(*) AS total,
                   count_if(health_rules IS NOT NULL
                            AND lower(to_json(health_rules)) NOT IN ('null', '[]', '{}')) AS with_rules,
                   count_if(trigger.file_arrival IS NOT NULL
                            OR trigger.table_update IS NOT NULL) AS event_driven
            FROM latest WHERE rn = 1
            """
        )
        total_jobs = int(row.get("total") or 0)
        health_rules = int(row.get("with_rules") or 0)
        event_triggers = int(row.get("event_driven") or 0)
    if alerts == 0 and health_rules == 0 and event_triggers == 0:
        return Result(
            "OE-03-04", "", "", "", STATUS_OPEN,
            f"No SQL alerts, job health rules, or event-driven triggers found across "
            f"{total_jobs} job(s), so nothing is configured to act automatically on an "
            "event.",
            {"alerts": 0, "jobs_with_health_rules": 0, "event_driven_jobs": 0},
        )
    channels = sum(1 for x in (alerts, health_rules, event_triggers) if x)
    status = STATUS_COMPLETED if channels >= 2 else STATUS_IN_PROGRESS
    return Result(
        "OE-03-04", "", "", "", status,
        f"{channels} of 3 event-response mechanism(s) in place: {alerts} SQL alert(s), "
        f"{health_rules} of {total_jobs} job(s) with health rules, and {event_triggers} "
        "job(s) with file-arrival or table-update triggers.",
        {"alerts": alerts, "jobs_with_health_rules": health_rules,
         "event_driven_jobs": event_triggers, "jobs": total_jobs},
    )


# ----------------------------------------------------------------------------------
# Manage capacity and quotas
# ----------------------------------------------------------------------------------


def oe_04_01(ctx: Ctx) -> Result:
    """Manage service limits and quotas."""
    throttles = 0
    if ctx.has_table("system.access.audit"):
        throttles = ctx.count_safe(
            f"""
            SELECT count(*) AS n FROM system.access.audit
            WHERE event_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
              AND (lower(coalesce(response.error_message, '')) RLIKE
                     '(quota|limit exceeded|too many requests|throttl|resource_exhausted)'
                   OR try_cast(response.status_code AS INT) = 429)
            """
        )
    queued = 0
    if ctx.has_table("system.query.history"):
        queued = ctx.count_safe(
            f"""
            SELECT count(*) AS n FROM system.query.history
            WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND coalesce(waiting_at_capacity_duration_ms, 0) > 0
            """
        )
    upsize_failures = 0
    if ctx.has_table("system.compute.warehouse_events"):
        upsize_failures = ctx.count_safe(
            f"""
            SELECT count(*) AS n FROM system.compute.warehouse_events
            WHERE event_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND lower(coalesce(event_type, '')) RLIKE '(scaled_up_failed|failed)'
            """
        )
    total_signals = throttles + queued + upsize_failures
    if total_signals == 0:
        return Result(
            "OE-04-01", "", "", "", STATUS_COMPLETED,
            f"No quota errors, capacity queuing, or scale-up failures observed in the "
            f"last {ctx.lookback_days} days, so current limits are not constraining "
            "workloads. Note this shows headroom today, not that limits are documented "
            "and tracked.",
            {"throttle_events": 0, "queued_queries": 0, "warehouse_failures": 0},
        )
    return Result(
        "OE-04-01", "", "", "", STATUS_IN_PROGRESS,
        f"Capacity pressure detected in the last {ctx.lookback_days} days: "
        f"{throttles:,} quota/throttling error(s), {queued:,} query/queries queued at "
        f"capacity, and {upsize_failures:,} warehouse scale-up failure(s). Review "
        "service limits and request increases where needed.",
        {"throttle_events": throttles, "queued_queries": queued,
         "warehouse_failures": upsize_failures},
    )


def oe_04_02(ctx: Ctx) -> Result:
    """Invest in capacity planning."""
    if not ctx.has_table("system.billing.usage"):
        return Result(
            "OE-04-02", "", "", "", STATUS_OPEN,
            "system.billing.usage is not readable, so capacity trends cannot be analyzed.",
        )
    # A meaningful trend needs a reasonable history; compare recent vs prior period.
    row = ctx.one(
        f"""
        SELECT
          count(DISTINCT usage_date) AS days,
          sum(CASE WHEN usage_date >= current_date() - INTERVAL 30 DAYS
                   THEN usage_quantity ELSE 0 END) AS recent,
          sum(CASE WHEN usage_date < current_date() - INTERVAL 30 DAYS
                        AND usage_date >= current_date() - INTERVAL 60 DAYS
                   THEN usage_quantity ELSE 0 END) AS prior
        FROM system.billing.usage
        WHERE usage_date >= current_date() - INTERVAL 60 DAYS
        """
    )
    days = int(row.get("days") or 0)
    recent = float(row.get("recent") or 0)
    prior = float(row.get("prior") or 0)
    forecasting = 0
    if ctx.has_table("system.query.history"):
        forecasting = ctx.count_safe(
            f"""
            SELECT count(*) AS n FROM system.query.history
            WHERE start_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS
              AND lower(statement_text) LIKE '%system.billing.usage%'
              AND (lower(statement_text) RLIKE '(forecast|trend|projection|budget)'
                   OR lower(statement_text) LIKE '%group by%')
            """
        )
    if days == 0:
        return Result(
            "OE-04-02", "", "", "", STATUS_OPEN,
            "No usage history in the last 60 days, so capacity planning has no baseline.",
        )
    if prior > 0:
        growth = (recent - prior) / prior * 100
        trend = f"Consumption changed {growth:+.1f}% versus the prior 30 days"
    else:
        trend = f"Only {days} day(s) of usage history available"
    if forecasting == 0:
        return Result(
            "OE-04-02", "", "", "", STATUS_IN_PROGRESS,
            f"{days} day(s) of usage history are available for planning ({trend}), but no "
            "trend or forecast queries against billing data were observed, so capacity "
            "planning does not appear to be an active practice.",
            {"usage_days": days, "recent_dbus": round(recent, 2),
             "prior_dbus": round(prior, 2), "forecast_queries": 0},
        )
    return Result(
        "OE-04-02", "", "", "", STATUS_COMPLETED,
        f"Capacity planning is active: {forecasting:,} trend/forecast query/queries "
        f"against billing data over {days} day(s) of history. {trend}.",
        {"usage_days": days, "recent_dbus": round(recent, 2),
         "prior_dbus": round(prior, 2), "forecast_queries": forecasting},
    )


CHECKS = [
    ("OE-01-01", oe_01_01),
    ("OE-01-02", oe_01_02),
    ("OE-01-03", oe_01_03),
    ("OE-01-04", oe_01_04),
    ("OE-01-05", oe_01_05),
    ("OE-01-06", oe_01_06),
    ("OE-01-07", oe_01_07),
    ("OE-01-08", oe_01_08),
    ("OE-01-09", oe_01_09),
    ("OE-02-01", oe_02_01),
    ("OE-02-02", oe_02_02),
    ("OE-02-03", oe_02_03),
    ("OE-02-04", oe_02_04),
    ("OE-02-05", oe_02_05),
    ("OE-02-06", oe_02_06),
    ("OE-02-07", oe_02_07),
    ("OE-02-08", oe_02_08),
    ("OE-02-09", oe_02_09),
    ("OE-02-10", oe_02_10),
    ("OE-02-11", oe_02_11),
    ("OE-03-01", oe_03_01),
    ("OE-03-02", oe_03_02),
    ("OE-03-03", oe_03_03),
    ("OE-03-04", oe_03_04),
    ("OE-04-01", oe_04_01),
    ("OE-04-02", oe_04_02),
]
