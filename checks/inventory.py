"""Account-wide inventory of workspace-scoped objects, read from system tables.

The Workspace SDK only sees the workspace the assessment runs in, so counts built from
it (cluster policies, Git folders, secret scopes, Apps, ...) silently cover one workspace
out of many. This module rebuilds those inventories for *every* workspace in the region
from ``system.access.audit`` (create / delete / touch events), ``system.compute.*``,
``system.lakeflow.*`` and ``system.billing.usage``, grouped by ``workspace_id`` - so one
run of the assessment covers the account.

Lifecycle rule (per ``workspace_id`` + object id): an object is **registered** when it
has a successful (HTTP 200) create or touch event in the inventory window and no later
successful delete. Deleted objects are excluded. An object is **active** when it has a
successful usage event within ``lookback_days``. Failed calls are ignored on purpose -
e.g. a ``getSecret`` against a scope that does not exist logs status 3002
(RESOURCE_DOES_NOT_EXIST) and must not resurrect it.

Known gap: objects created *before* the audit window that were never touched since are
not visible. :func:`coverage` measures that gap in the current workspace by comparing
against the Workspace SDK, which lists everything registered there.

Every function is read-only and memoised on the ``Ctx`` for the run.
"""

from __future__ import annotations

from typing import Any, Callable

from waf_core import Ctx

AUDIT = "system.access.audit"

#: Default audit window for rebuilding inventories (system.access.audit keeps ~1 year).
DEFAULT_INVENTORY_WINDOW_DAYS = 365

_RESULT = "response.result"


def _j(path: str) -> str:
    return f"get_json_object({_RESULT}, '{path}')"


#: Audit lifecycle definitions. ``id_*`` are SQL expressions over an audit row.
#: ``active`` lists the actions that mean "in use" (None = no usage event exists).
LIFECYCLE: dict[str, dict[str, Any]] = {
    "secret_scopes": {
        "label": "secret scopes",
        "service": "secrets",
        "create": ("createScope",), "id_create": "request_params['scope']",
        "delete": ("deleteScope",), "id_delete": "request_params['scope']",
        "touch": ("putSecret", "deleteSecret", "listSecrets", "getSecret", "putAcl",
                  "deleteAcl", "listAcls", "getAcl"),
        "id_touch": "request_params['scope']",
        "active": ("getSecret",),
    },
    "git_folders": {
        "label": "Git folders",
        "service": "repos",
        "create": ("createRepo",),
        "id_create": f"coalesce({_j('$.id')}, {_j('$.project.id')})",
        "delete": ("deleteRepo",), "id_delete": "request_params['id']",
        "touch": ("updateRepo", "getRepo"), "id_touch": "request_params['id']",
        "active": ("updateRepo",),
    },
    "git_credentials": {
        "label": "Git credentials",
        "service": "gitCredentials",
        "create": ("createGitCredential",), "id_create": _j("$.credential_id"),
        "delete": ("deleteGitCredential",), "id_delete": "request_params['id']",
        "touch": ("updateGitCredential", "getGitCredential"),
        "id_touch": "request_params['id']",
        "active": None,
    },
    "cluster_policies": {
        "label": "custom cluster policies",
        "service": "clusterPolicies",
        "create": ("create",), "id_create": _j("$.policy_id"),
        "delete": ("delete",), "id_delete": "request_params['policy_id']",
        "touch": ("edit",), "id_touch": "request_params['policy_id']",
        "active": None,  # usage comes from system.compute.clusters, see policy_usage()
    },
    "apps": {
        "label": "Databricks Apps",
        "service": "apps",
        "create": ("createApp",), "id_create": _j("$.name"),
        "delete": ("deleteApp",), "id_delete": "request_params['name']",
        "touch": (), "id_touch": "NULL",
        "active": None,  # usage comes from system.billing.usage, see app_inventory()
    },
}


def _memo(ctx: Ctx, key: str, fn: Callable[[], Any]) -> Any:
    cache = ctx.__dict__.setdefault("_inventory_cache", {})
    if key not in cache:
        cache[key] = fn()
    return cache[key]


def window_days(ctx: Ctx) -> int:
    return int(ctx.cfg.get("inventory_window_days", DEFAULT_INVENTORY_WINDOW_DAYS))


def _in(actions: tuple[str, ...]) -> str:
    return ", ".join("'" + a + "'" for a in actions) or "''"


def lifecycle_sql(kind: str, window: int, lookback: int) -> str:
    """Per-workspace registered/active counts for one object kind (one row per workspace)."""
    s = LIFECYCLE[kind]
    active = s["active"]
    active_expr = (
        f"max(CASE WHEN action_name IN ({_in(active)}) AND event_date >= current_date() - "
        f"INTERVAL {lookback} DAYS THEN event_time END)" if active else "CAST(NULL AS TIMESTAMP)"
    )
    return f"""
        WITH ev AS (
          SELECT workspace_id, action_name, event_time, event_date,
                 CASE WHEN action_name IN ({_in(s['create'])}) THEN {s['id_create']}
                      WHEN action_name IN ({_in(s['delete'])}) THEN {s['id_delete']}
                      ELSE {s['id_touch']} END AS obj_id
          FROM {AUDIT}
          WHERE event_date >= current_date() - INTERVAL {window} DAYS
            AND service_name = '{s['service']}'
            AND action_name IN ({_in(s['create'] + s['delete'] + s['touch'])})
            AND response.status_code = 200
        ),
        life AS (
          SELECT workspace_id, obj_id,
                 max(CASE WHEN action_name IN ({_in(s['create'])}) THEN event_time END) AS created,
                 max(CASE WHEN action_name IN ({_in(s['delete'])}) THEN event_time END) AS deleted,
                 max(CASE WHEN action_name NOT IN ({_in(s['delete'])}) THEN event_time END) AS last_seen,
                 {active_expr} AS last_active
          FROM ev WHERE obj_id IS NOT NULL AND obj_id <> ''
          GROUP BY workspace_id, obj_id
        )
        SELECT workspace_id,
               count(*) AS registered,
               count(last_active) AS active,
               count(created) AS created_in_window
        FROM life
        WHERE deleted IS NULL OR last_seen > deleted
        GROUP BY workspace_id
    """


def lifecycle_ids_sql(kind: str, window: int, workspace_id: int) -> str:
    """Registered object ids for one workspace (used by :func:`coverage`)."""
    base = lifecycle_sql(kind, window, 0)
    # Reuse the same CTEs; return ids instead of counts.
    head, _, _ = base.rpartition("SELECT workspace_id,")
    return head + f"""
        SELECT obj_id FROM life
        WHERE (deleted IS NULL OR last_seen > deleted) AND workspace_id = {int(workspace_id)}
    """


def _rows_by_ws(rows: list[dict]) -> dict[int, dict[str, int]]:
    out: dict[int, dict[str, int]] = {}
    for r in rows:
        ws = r.get("workspace_id")
        if ws is None:
            continue
        out[int(ws)] = {k: int(v or 0) for k, v in r.items() if k != "workspace_id"}
    return out


def lifecycle(ctx: Ctx, kind: str) -> dict[int, dict[str, int]] | None:
    """``{workspace_id: {registered, active, created_in_window}}`` or None if unreadable."""
    def build():
        if not ctx.has_table(AUDIT):
            return None
        try:
            return _rows_by_ws(ctx.sql(lifecycle_sql(kind, window_days(ctx), ctx.lookback_days)))
        except Exception:
            return None
    return _memo(ctx, f"lifecycle:{kind}", build)


def totals(per_ws: dict[int, dict[str, int]] | None) -> dict[str, int]:
    """Sum a per-workspace inventory into account totals (+ workspace count)."""
    out = {"workspaces": 0, "registered": 0, "active": 0}
    for row in (per_ws or {}).values():
        out["workspaces"] += 1
        out["registered"] += row.get("registered", 0)
        out["active"] += row.get("active", 0)
    return out


# --------------------------------------------------------------------------- specific


def policy_usage(ctx: Ctx) -> dict[str, Any] | None:
    """Cluster-policy inventory across all workspaces.

    - ``custom``: per-workspace custom policies registered in the audit window
      (created/edited and not deleted) and how many of those an active cluster uses.
    - ``in_use_total``: distinct policies referenced by active clusters (includes the
      built-in default families, which never log a create event).
    """
    def build():
        if not ctx.has_table("system.compute.clusters"):
            return None
        window, lookback = window_days(ctx), ctx.lookback_days
        try:
            used = ctx.sql(f"""
                WITH latest AS (
                  SELECT workspace_id, cluster_id, policy_id, delete_time, change_time,
                         row_number() OVER (PARTITION BY workspace_id, cluster_id
                                            ORDER BY change_time DESC) AS rn
                  FROM system.compute.clusters
                  WHERE change_time >= current_timestamp() - INTERVAL {lookback} DAYS
                )
                SELECT workspace_id, count(DISTINCT policy_id) AS in_use
                FROM latest
                WHERE rn = 1 AND delete_time IS NULL AND coalesce(policy_id, '') <> ''
                GROUP BY workspace_id
            """)
            custom = []
            if ctx.has_table(AUDIT):
                base = lifecycle_sql("cluster_policies", window, lookback)
                head, _, _ = base.rpartition("SELECT workspace_id,")
                custom = ctx.sql(head + f"""
                    , used AS (
                      SELECT DISTINCT workspace_id, policy_id FROM (
                        SELECT workspace_id, policy_id, delete_time,
                               row_number() OVER (PARTITION BY workspace_id, cluster_id
                                                  ORDER BY change_time DESC) AS rn
                        FROM system.compute.clusters
                        WHERE change_time >= current_timestamp() - INTERVAL {lookback} DAYS)
                      WHERE rn = 1 AND delete_time IS NULL AND coalesce(policy_id, '') <> ''
                    )
                    SELECT l.workspace_id, count(*) AS registered,
                           count(u.policy_id) AS active
                    FROM life l LEFT JOIN used u
                      ON l.workspace_id = u.workspace_id AND l.obj_id = u.policy_id
                    WHERE l.deleted IS NULL OR l.last_seen > l.deleted
                    GROUP BY l.workspace_id
                """)
        except Exception:
            return None
        in_use = _rows_by_ws(used)
        return {
            "custom": _rows_by_ws(custom),
            "in_use": in_use,
            "in_use_total": sum(r.get("in_use", 0) for r in in_use.values()),
        }
    return _memo(ctx, "policy_usage", build)


def app_inventory(ctx: Ctx) -> dict[int, dict[str, int]] | None:
    """Apps per workspace: audit lifecycle (create - delete) unioned with apps that
    billed in the window; ``active`` = billed within ``lookback_days``."""
    def build():
        window, lookback = window_days(ctx), ctx.lookback_days
        parts = []
        if ctx.has_table(AUDIT):
            parts.append(f"""
                SELECT workspace_id, {_j('$.name')} AS app, event_time AS seen,
                       CAST(NULL AS TIMESTAMP) AS billed, 'create' AS kind
                FROM {AUDIT}
                WHERE event_date >= current_date() - INTERVAL {window} DAYS
                  AND service_name = 'apps' AND action_name = 'createApp'
                  AND response.status_code = 200
                UNION ALL
                SELECT workspace_id, request_params['name'], event_time, NULL, 'delete'
                FROM {AUDIT}
                WHERE event_date >= current_date() - INTERVAL {window} DAYS
                  AND service_name = 'apps' AND action_name = 'deleteApp'
                  AND response.status_code = 200
            """)
        if ctx.has_table("system.billing.usage"):
            parts.append(f"""
                SELECT workspace_id, usage_metadata.app_name, max(CAST(usage_date AS TIMESTAMP)),
                       max(CAST(usage_date AS TIMESTAMP)), 'billed'
                FROM system.billing.usage
                WHERE {billing_scope(ctx, "workspace_id")} AND usage_date >= current_date() - INTERVAL {window} DAYS
                  AND usage_metadata.app_name IS NOT NULL
                GROUP BY workspace_id, usage_metadata.app_name
            """)
        if not parts:
            return None
        try:
            rows = ctx.sql(f"""
                WITH ev AS ({' UNION ALL '.join(parts)}),
                life AS (
                  SELECT workspace_id, app,
                         max(CASE WHEN kind = 'delete' THEN seen END) AS deleted,
                         max(CASE WHEN kind <> 'delete' THEN seen END) AS last_seen,
                         max(billed) AS last_billed
                  FROM ev WHERE app IS NOT NULL GROUP BY workspace_id, app
                )
                SELECT workspace_id, count(*) AS registered,
                       count_if(last_billed >= current_date() - INTERVAL {lookback} DAYS) AS active
                FROM life WHERE deleted IS NULL OR last_seen > deleted
                GROUP BY workspace_id
            """)
        except Exception:
            return None
        return _rows_by_ws(rows)
    return _memo(ctx, "apps", build)


def simple_counts(ctx: Ctx) -> dict[str, dict[int, int]]:
    """Per-workspace counts straight from system tables (no audit reconstruction):
    saved jobs, saved pipelines, SQL warehouses."""
    specs = {
        "saved_jobs": ("system.lakeflow.jobs", """
            SELECT workspace_id, count(DISTINCT job_id) AS n FROM (
              SELECT workspace_id, job_id, delete_time,
                     row_number() OVER (PARTITION BY workspace_id, job_id
                                        ORDER BY change_time DESC) AS rn
              FROM system.lakeflow.jobs)
            WHERE rn = 1 AND delete_time IS NULL GROUP BY workspace_id"""),
        "saved_pipelines": ("system.lakeflow.pipelines", """
            SELECT workspace_id, count(DISTINCT pipeline_id) AS n FROM (
              SELECT workspace_id, pipeline_id, delete_time,
                     row_number() OVER (PARTITION BY workspace_id, pipeline_id
                                        ORDER BY change_time DESC) AS rn
              FROM system.lakeflow.pipelines)
            WHERE rn = 1 AND delete_time IS NULL GROUP BY workspace_id"""),
        "sql_warehouses": ("system.compute.warehouses", """
            SELECT workspace_id, count(DISTINCT warehouse_id) AS n FROM (
              SELECT workspace_id, warehouse_id, delete_time,
                     row_number() OVER (PARTITION BY workspace_id, warehouse_id
                                        ORDER BY change_time DESC) AS rn
              FROM system.compute.warehouses)
            WHERE rn = 1 AND delete_time IS NULL GROUP BY workspace_id"""),
    }

    def build():
        out: dict[str, dict[int, int]] = {}
        for key, (table, query) in specs.items():
            if not ctx.has_table(table):
                continue
            try:
                out[key] = {int(r["workspace_id"]): int(r["n"] or 0)
                            for r in ctx.sql(query) if r.get("workspace_id") is not None}
            except Exception:
                continue
        return out
    return _memo(ctx, "simple_counts", build)


def workspace_names(ctx: Ctx) -> dict[int, str]:
    def build():
        if not ctx.has_table("system.access.workspaces_latest"):
            return {}
        try:
            return {int(r["workspace_id"]): r.get("workspace_name") or str(r["workspace_id"])
                    for r in ctx.sql("SELECT workspace_id, workspace_name "
                                     "FROM system.access.workspaces_latest")}
        except Exception:
            return {}
    return _memo(ctx, "workspace_names", build)


def ws_label(ctx: Ctx, ws_id: int) -> str:
    """``name (id)`` when the workspace is in workspaces_latest, else the bare id."""
    name = workspace_names(ctx).get(int(ws_id))
    return f"{name} ({ws_id})" if name and name != str(ws_id) else str(ws_id)


def current_workspace_id(ctx: Ctx) -> int | None:
    def build():
        w = ctx.w
        if w is None:
            return None
        try:
            return int(w.get_workspace_id())
        except Exception:
            return None
    return _memo(ctx, "current_ws", build)


# --------------------------------------------------------------------------- coverage

#: Workspace SDK listers for the current-workspace coverage check: kind -> (fn, id).
_SDK_LISTERS: dict[str, tuple[Callable[[Any], Any], Callable[[Any], str]]] = {
    "secret_scopes": (lambda w: w.secrets.list_scopes(), lambda o: o.name),
    "git_folders": (lambda w: w.repos.list(), lambda o: str(o.id)),
    "git_credentials": (lambda w: w.git_credentials.list(), lambda o: str(o.credential_id)),
    "cluster_policies": (lambda w: w.cluster_policies.list(), lambda o: o.policy_id),
    "apps": (lambda w: w.apps.list(), lambda o: o.name),
}


def coverage(ctx: Ctx, kind: str) -> dict[str, Any] | None:
    """Compare the audit-derived inventory with the Workspace SDK in *this* workspace.

    Returns ``{"api": n, "audit": n, "both": n, "pct": share of API objects the audit
    log also sees}``; ``None`` when either side is unavailable. For cluster policies the
    built-in default families (``policy_family_id`` set, no creator) are excluded from
    the API side because they never log a create event.
    """
    def build():
        ws = current_workspace_id(ctx)
        w = ctx.w
        if ws is None or w is None or not ctx.has_table(AUDIT):
            return None
        fn, key = _SDK_LISTERS[kind]
        try:
            objs = list(fn(w) or [])
        except Exception:
            return None
        if kind == "cluster_policies":
            objs = [o for o in objs if not getattr(o, "policy_family_id", None)
                    or getattr(o, "creator_user_name", None)]
        api_ids = {key(o) for o in objs if key(o)}
        try:
            audit_ids = {str(r["obj_id"]) for r in
                         ctx.sql(lifecycle_ids_sql(kind, window_days(ctx), ws))
                         if r.get("obj_id")} if kind in LIFECYCLE else set()
        except Exception:
            return None
        both = api_ids & audit_ids
        return {
            "api": len(api_ids), "audit": len(audit_ids), "both": len(both),
            "pct": round(100 * len(both) / len(api_ids), 1) if api_ids else None,
        }
    return _memo(ctx, f"coverage:{kind}", build)


def coverage_note(ctx: Ctx, kind: str) -> str:
    """One-sentence coverage caveat for a rationale ("" when unavailable)."""
    c = coverage(ctx, kind)
    if not c:
        return ""
    label = LIFECYCLE[kind]["label"]
    if c["api"] == 0 and c["audit"] == 0:
        return ""
    if c["api"] == 0 and kind == "git_folders":
        # Observed: the Repos list API does not enumerate Git folders under /Users.
        return (f"Coverage check in this workspace: the Repos API lists 0 Git folders but "
                f"the audit log shows {c['audit']}; the API does not enumerate Git folders "
                "created under /Users, so the audit log is the more complete source.")
    if c["api"] == 0:
        return (f"Coverage check in this workspace: the Workspace API lists 0 {label} "
                f"(audit log: {c['audit']}).")
    return (f"Coverage check in this workspace: the audit log sees {c['both']} of the "
            f"{c['api']} {label} the Workspace API lists ({c['pct']}%); the rest predate "
            f"the {window_days(ctx)}-day audit window and have not been touched since.")


# --------------------------------------------------------------------------- identities


def identity_counts(ctx: Ctx) -> dict[str, int] | None:
    """Account-wide user / service principal / group totals.

    Read through the workspace's account-SCIM endpoint (``/api/2.0/account/scim/v2``),
    which returns the account directory from a workspace login - no account-level
    profile needed. ``count=1`` returns only ``totalResults``, so this is three cheap
    GETs. ``None`` when the SDK or endpoint is unavailable.
    """
    def build():
        w = ctx.w
        if w is None:
            return None
        # Unified (multi-workspace) hosts route raw API calls by this header.
        ws = current_workspace_id(ctx)
        headers = {"X-Databricks-Org-Id": str(ws)} if ws else None
        out: dict[str, int] = {}
        for res in ("Users", "ServicePrincipals", "Groups"):
            try:
                body = w.api_client.do(
                    "GET", f"/api/2.0/account/scim/v2/{res}",
                    query={"count": 1, "attributes": "id"}, headers=headers)
                out[res] = int(body.get("totalResults") or 0)
            except Exception:
                return None
        return out
    return _memo(ctx, "identities", build)


def active_identities(ctx: Ctx) -> int | None:
    """Distinct identities with any audit event in ``lookback_days`` (all workspaces)."""
    def build():
        if not ctx.has_table(AUDIT):
            return None
        try:
            return ctx.count(f"""
                SELECT count(DISTINCT user_identity.email) AS n FROM {AUDIT}
                WHERE event_date >= current_date() - INTERVAL {ctx.lookback_days} DAYS
                  AND user_identity.email IS NOT NULL AND user_identity.email <> ''
            """)
        except Exception:
            return None
    return _memo(ctx, "active_identities", build)


def admin_identities(ctx: Ctx) -> set[str] | None:
    """Workspace + account admins across the account, from audit grant/revoke events.

    An identity is an admin when its latest successful ``setAdmin``/``setAccountAdmin``
    is newer than any ``removeAdmin``/``removeAccountAdmin``. ``targetUserName`` is an
    email for users and an application id for service principals. Admins granted before
    the audit window and never changed since are not seen.
    """
    def build():
        if not ctx.has_table(AUDIT):
            return None
        try:
            rows = ctx.sql(f"""
                SELECT lower(request_params['targetUserName']) AS who,
                       max(CASE WHEN action_name IN ('setAdmin', 'setAccountAdmin')
                                THEN event_time END) AS granted,
                       max(CASE WHEN action_name IN ('removeAdmin', 'removeAccountAdmin')
                                THEN event_time END) AS revoked
                FROM {AUDIT}
                WHERE event_date >= current_date() - INTERVAL {window_days(ctx)} DAYS
                  AND service_name = 'accounts'
                  AND action_name IN ('setAdmin', 'removeAdmin', 'setAccountAdmin',
                                      'removeAccountAdmin')
                  AND response.status_code = 200
                GROUP BY 1
            """)
        except Exception:
            return None
        return {r["who"] for r in rows
                if r.get("who") and r.get("granted")
                and (not r.get("revoked") or r["granted"] > r["revoked"])}
    return _memo(ctx, "admins", build)


def policy_evidence(ctx: Ctx) -> dict[str, Any]:
    """Shared cluster-policy evidence for CO-02-03, IU-03-01, IU-03-03 and OE-02-02.

    Returns ``{"exist": bool | None, "text": str, "metrics": dict}``. ``exist`` is True
    when any custom policy is registered or any active cluster runs under a policy
    (built-in defaults included); None when neither source is readable.
    """
    pu = policy_usage(ctx)
    if pu is None:
        return {"exist": None, "text": "cluster policy inventory unavailable", "metrics": {}}
    custom = totals(pu["custom"])
    unused = custom["registered"] - custom["active"]
    in_use = pu["in_use_total"]
    eff = (round(100 * custom["active"] / custom["registered"], 1)
           if custom["registered"] else None)
    text = (
        f"{custom['registered']:,} custom cluster policy/policies registered across "
        f"{custom['workspaces']:,} workspace(s) in the {window_days(ctx)}-day audit window "
        f"({custom['active']:,} used by an active cluster, {unused:,} unused"
        + (f" - {eff}% in use" if eff is not None else "") + "); "
        f"{in_use:,} distinct policy/policies (built-in defaults included) govern active "
        "clusters"
    )
    note = coverage_note(ctx, "cluster_policies")
    if note:
        text += ". " + note.rstrip(".")
    return {
        "exist": custom["registered"] > 0 or in_use > 0,
        "text": text,
        "metrics": {"custom_policies": custom["registered"],
                    "custom_policies_used": custom["active"],
                    "custom_policies_unused": unused,
                    "custom_policy_workspaces": custom["workspaces"],
                    "policies_in_use": in_use,
                    "policy_efficiency_pct": eff},
    }


def lifecycle_evidence(ctx: Ctx, kind: str) -> dict[str, Any]:
    """``{"registered", "active", "workspaces", "text"}`` for an audit-lifecycle kind,
    with the coverage caveat appended; counts are None when the audit log is unreadable."""
    per_ws = lifecycle(ctx, kind)
    label = LIFECYCLE[kind]["label"]
    if per_ws is None:
        return {"registered": None, "active": None, "workspaces": None,
                "text": f"{label} inventory unavailable (audit log not readable)"}
    t = totals(per_ws)
    text = (f"{t['registered']:,} {label} registered across {t['workspaces']:,} "
            f"workspace(s) in the {window_days(ctx)}-day audit window")
    if LIFECYCLE[kind]["active"]:
        text += f" ({t['active']:,} active in the last {ctx.lookback_days} days)"
    note = coverage_note(ctx, kind)
    if note:
        text += ". " + note.rstrip(".")
    return {**t, "text": text}


# --------------------------------------------------------------------------- visibility

#: Cap on per-catalog binding lookups (one cheap GET each) per run.
MAX_BINDING_LOOKUPS = 200


def _invisible_catalogs(ctx: Ctx) -> dict[str, str] | None:
    """Catalogs in this metastore that ``system.information_schema`` does not show the
    runner, each classified as ``"binding"`` (bound only to other workspaces) or
    ``"privilege"`` (open to, or bound to, this workspace - or bindings unreadable - but
    the runner has no privileges on it).

    Candidates come from sources not filtered by the runner's privileges: catalogs created
    (and not since deleted) in this metastore during the audit window, plus catalogs with
    predictive-optimization activity in this metastore. Each is checked with the
    workspace-bindings API, which answers even from an unbound workspace. Catalogs that
    appear in neither source cannot be discovered. ``None`` = unknown.
    """
    def build():
        ws, w = current_workspace_id(ctx), ctx.w
        if ws is None or w is None or not ctx.has_table(AUDIT):
            return None
        po = "system.storage.predictive_optimization_operations_history"
        po_sql = (f"UNION SELECT DISTINCT catalog_name FROM {po} "
                  "WHERE metastore_id = element_at(split(current_metastore(), ':'), -1)"
                  if ctx.has_table(po) else "")
        try:
            rows = ctx.sql(f"""
                WITH ev AS (
                  SELECT coalesce(request_params['name'], request_params['name_arg']) AS name,
                         action_name, event_time
                  FROM {AUDIT}
                  WHERE event_date >= current_date() - INTERVAL {window_days(ctx)} DAYS
                    AND service_name = 'unityCatalog'
                    AND action_name IN ('createCatalog', 'deleteCatalog')
                    AND response.status_code = 200
                    AND request_params['metastore_id'] = element_at(split(current_metastore(), ':'), -1)
                ),
                life AS (
                  SELECT name,
                         max(CASE WHEN action_name = 'createCatalog' THEN event_time END) AS c,
                         max(CASE WHEN action_name = 'deleteCatalog' THEN event_time END) AS d
                  FROM ev WHERE name IS NOT NULL GROUP BY name
                ),
                known AS (
                  SELECT name FROM life WHERE c IS NOT NULL AND (d IS NULL OR c > d)
                  {po_sql}
                )
                SELECT k.name FROM known k
                LEFT ANTI JOIN system.information_schema.catalogs v ON v.catalog_name = k.name
                WHERE k.name IS NOT NULL
                  AND k.name NOT IN ('system', 'samples', 'hive_metastore')
                  AND k.name NOT LIKE '__databricks_internal%'
                ORDER BY k.name
            """)
        except Exception:
            return None
        out: dict[str, str] = {}
        for r in rows[:MAX_BINDING_LOOKUPS]:
            name = r.get("name")
            try:
                b = w.workspace_bindings.get_bindings("catalog", name)
                # Newer SDKs paginate (iterator of bindings); older return .bindings.
                items = b.bindings if hasattr(b, "bindings") else b
                bound = {int(x.workspace_id) for x in (items or [])}
            except Exception:
                bound = set()  # bindings unreadable: the runner lacks access to it
            out[name] = "binding" if bound and ws not in bound else "privilege"
        return out
    return _memo(ctx, "invisible_catalogs", build)


def hidden_catalogs(ctx: Ctx) -> list[str] | None:
    """Catalogs bound only to other workspaces (``None`` = unknown)."""
    inv = _invisible_catalogs(ctx)
    return None if inv is None else sorted(n for n, k in inv.items() if k == "binding")


def privilege_hidden_catalogs(ctx: Ctx) -> list[str] | None:
    """In-scope-pattern catalogs this workspace could see but the runner has no privileges
    on: they match ``PROD_CATALOG_PATTERNS`` (any non-system catalog when the scope fell
    back to all catalogs), so catalog-scoped results silently leave them out."""
    inv = _invisible_catalogs(ctx)
    if inv is None:
        return None
    scope = ctx.scope
    names = sorted(n for n, k in inv.items() if k == "privilege")
    if scope.mode == "prod":
        from waf_core import _like_match  # noqa: PLC0415
        names = [n for n in names if any(_like_match(n, p) for p in scope.include_patterns)]
    return names


def access_scope_note(ctx: Ctx) -> str:
    """``[ACCESS]`` caveat for catalog-scoped results when in-scope catalogs are hidden
    from the runner by privileges (``""`` if none or unknown)."""
    hidden = privilege_hidden_catalogs(ctx) or []
    if not hidden:
        return ""
    shown = ", ".join(hidden[:5]) + (", ..." if len(hidden) > 5 else "")
    return (f"[ACCESS: {len(hidden)} catalog(s) in the configured scope exist in this "
            f"metastore but {ctx.runner} has no privileges on them ({shown}), so this result "
            "leaves them out; grant BROWSE (or USE CATALOG, USE SCHEMA and SELECT) on them, "
            "or run as a metastore admin] ")


def visibility_note(ctx: Ctx) -> str:
    """Caveat for catalog-scoped results when some catalogs are invisible here."""
    hidden = hidden_catalogs(ctx) or []
    if not hidden:
        return ""
    shown = ", ".join(hidden[:5]) + (", ..." if len(hidden) > 5 else "")
    return (f"[VISIBILITY: {len(hidden)} catalog(s) are bound only to other workspaces and "
            f"are not visible from this one ({shown}); bind them read-only to this "
            "workspace or run there to include them] ")


# --------------------------------------------------------------------------- compute

#: Workload identity for a row of system.compute.clusters. Every job run gets a fresh
#: ephemeral cluster_id (``job-<job_id>-run-<run_id>-...``) and pipeline updates use
#: ``dlt-execution-<pipeline_id>``, so counting cluster_ids weights a workload by how often
#: it runs. Keying on the job / pipeline instead counts each workload once.
WORKLOAD_KEY_SQL = (
    "CASE WHEN upper(coalesce(cluster_source, '')) = 'JOB' "
    "AND cluster_name RLIKE '^job-[0-9]+-run-' "
    "THEN concat('job:', workspace_id, ':', regexp_extract(cluster_name, '^job-([0-9]+)-run-', 1)) "
    "WHEN upper(coalesce(cluster_source, '')) LIKE 'PIPELINE%' "
    "AND cluster_name RLIKE '^dlt-execution-' "
    "THEN concat('pipeline:', workspace_id, ':', regexp_extract(cluster_name, '^dlt-execution-([0-9a-f-]+)', 1)) "
    "ELSE concat('cluster:', cluster_id) END"
)

#: Plain-English unit for rationales built on :func:`workloads_cte`.
WORKLOAD_UNIT = "compute workloads (jobs, pipelines and all-purpose clusters)"


def workloads_cte(ctx: Ctx, cols: str) -> str:
    """``WITH latest AS (...)`` over system.compute.clusters, one row per *workload*
    (``rn = 1`` = its most recent configuration) that ran in ``lookback_days``.

    ``cols`` are the columns/expressions the caller needs; the CTE also exposes
    ``cluster_source`` and ``workload_key``. Workloads deleted since they ran still
    count - they consumed compute in the window.
    """
    extra = "" if "cluster_source" in cols else ", cluster_source"
    return f"""WITH latest AS (
          SELECT {cols}{extra}, workload_key,
                 row_number() OVER (PARTITION BY workload_key ORDER BY change_time DESC) AS rn
          FROM (SELECT *, {WORKLOAD_KEY_SQL} AS workload_key
                FROM system.compute.clusters
                WHERE change_time >= current_timestamp() - INTERVAL {ctx.lookback_days} DAYS)
        )"""


def scoped_model_count(ctx: Ctx) -> int | None:
    """UC registered models in the configured scope (the same catalogs/schemas as the
    catalog-scoped checks), or ``None`` when the registered-models API is unavailable."""
    def build():
        w = ctx.w
        if w is None:
            return None
        try:
            return sum(1 for m in w.registered_models.list()
                       if ctx.scope.contains(m.catalog_name, m.schema_name))
        except Exception:
            return None
    return _memo(ctx, "scoped_models", build)


def region_workspaces_sql(ctx: Ctx) -> str:
    """Subquery of this region's workspace ids.

    ``system.billing.usage`` covers every workspace in the account, in every region,
    while the compute, job, query and audit tables cover this region only. Billing is
    restricted to these workspaces so its numbers describe the same population as the
    rest of the assessment. ``system.access.workspaces_latest`` lists this region's
    workspaces; without it, fall back to workspaces seen in the audit log.
    """
    if ctx.has_table("system.access.workspaces_latest"):
        return "SELECT workspace_id FROM system.access.workspaces_latest"
    return (f"SELECT DISTINCT workspace_id FROM {AUDIT} "
            f"WHERE event_date > current_date() - INTERVAL {ctx.lookback_days} DAYS")


def billing_scope(ctx: Ctx, col: str = "workspace_id") -> str:
    """SQL predicate limiting billing rows (``col``) to this region's workspaces."""
    return f"{col} IN ({region_workspaces_sql(ctx)})"


def region_workspace_count(ctx: Ctx) -> int | None:
    """Number of workspaces in this region, counted once per run so every rationale
    quotes the same figure (workspaces can be created while the run is in progress)."""
    def build():
        try:
            return ctx.count(f"SELECT count(DISTINCT workspace_id) AS n FROM "
                             f"({region_workspaces_sql(ctx)})")
        except Exception:
            return None
    return _memo(ctx, "region_workspaces", build)


def region_label(ctx: Ctx) -> str:
    """``this region's N workspace(s)`` - the population billing-based numbers cover."""
    n = region_workspace_count(ctx)
    return f"this region's {n:,} workspace(s)" if n else "this region's workspaces"


#: Versioned Lakeflow tables (one row per change; a delete is a new row with
#: ``delete_time`` set) and the columns that identify one object. Ids are only unique
#: within a workspace, so ``workspace_id`` is always part of the identity.
_VERSIONED = {
    "jobs": ("system.lakeflow.jobs", ("workspace_id", "job_id")),
    "pipelines": ("system.lakeflow.pipelines", ("workspace_id", "pipeline_id")),
    "job_tasks": ("system.lakeflow.job_tasks", ("workspace_id", "job_id", "task_key")),
}


def _latest_rows(kind: str, cols: str) -> str:
    table, keys = _VERSIONED[kind]
    extra = [c.strip() for c in cols.split(",") if c.strip() and c.strip() not in keys]
    return f"""SELECT * FROM (
            SELECT {", ".join([*keys, *extra])}, delete_time AS _deleted_at,
                   row_number() OVER (PARTITION BY {", ".join(keys)}
                                      ORDER BY change_time DESC) AS rn
            FROM {table})
          WHERE rn = 1 AND _deleted_at IS NULL"""


def live_latest_cte(kind: str, cols: str = "", name: str = "latest") -> str:
    """``<name> AS (...)``: the current version of every job, pipeline or job task that
    still exists - one row each, ``rn = 1``, identity columns always included.

    The latest row is picked *before* deletes are dropped. Filtering ``delete_time IS
    NULL`` first keeps a deleted object's earlier versions, so it is still counted (on
    the oneenv account that inflated 116 live jobs to 5,024). Tasks also require their
    job to still exist.
    """
    body = _latest_rows(kind, cols)
    if kind == "job_tasks":
        body = f"""SELECT t.* FROM ({body}) t
          LEFT SEMI JOIN ({_latest_rows("jobs", "")}) j
            ON t.workspace_id = j.workspace_id AND t.job_id = j.job_id"""
    return f"""{name} AS (
          {body}
        )"""


def live_count_sql(kind: str, cols: str = "", where: str = "") -> str:
    """``SELECT count(*) AS n`` over :func:`live_latest_cte`, optionally filtered by a
    predicate on ``cols``."""
    return (f"WITH {live_latest_cte(kind, cols)} SELECT count(*) AS n FROM latest"
            + (f" WHERE {where}" if where else ""))


def window_text(ctx: Ctx, days: int | None = None) -> str:
    """``from YYYY-MM-DD to YYYY-MM-DD`` for the lookback (or a given) window, UTC."""
    from datetime import timedelta  # noqa: PLC0415

    end = ctx.run_ts.date()
    start = end - timedelta(days=int(days if days is not None else ctx.lookback_days))
    return f"from {start.isoformat()} to {end.isoformat()}"


def workload_unit(ctx: Ctx) -> str:
    """Rationale subject for :func:`workloads_cte` counts, with the exact date range."""
    return (f"classic compute workloads (jobs, pipelines and all-purpose clusters, each "
            f"counted once) that ran {window_text(ctx)}")


# --------------------------------------------------------------------------- run export


def catalog_inventory(ctx: Ctx) -> dict[str, dict[str, Any]]:
    """Per-catalog metadata visible from this workspace, for merging runs across
    workspaces with different catalog bindings: tables, schemas, the fullest schema's
    table / function / volume counts, metric views, and whether the catalog has a
    description. Read-only ``information_schema`` aggregates."""
    def build():
        sys_cats = list(ctx.cfg.get("system_catalogs", []))
        from waf_core import _like_clause  # noqa: PLC0415

        not_sys = _like_clause("catalog_name", sys_cats, negate=True) if sys_cats else "true"
        out: dict[str, dict[str, Any]] = {}

        def put(rows, key):
            for r in rows:
                c = r.get("c")
                if c is not None:
                    out.setdefault(c, {})[key] = r.get("v")

        queries = {
            "has_comment": f"""SELECT catalog_name AS c,
                    CAST(length(trim(coalesce(comment, ''))) > 0 AS INT) AS v
                FROM system.information_schema.catalogs WHERE {not_sys}""",
            "schemas": """SELECT catalog_name AS c, count(*) AS v
                FROM system.information_schema.schemata GROUP BY 1""",
            "tables": """SELECT table_catalog AS c, count(*) AS v
                FROM system.information_schema.tables GROUP BY 1""",
            "max_tables_per_schema": """SELECT c, max(n) AS v FROM (
                SELECT table_catalog AS c, table_schema, count(*) AS n
                FROM system.information_schema.tables GROUP BY 1, 2) GROUP BY c""",
            "max_functions_per_schema": """SELECT c, max(n) AS v FROM (
                SELECT routine_catalog AS c, routine_schema, count(*) AS n
                FROM system.information_schema.routines GROUP BY 1, 2) GROUP BY c""",
            "max_volumes_per_schema": """SELECT c, max(n) AS v FROM (
                SELECT volume_catalog AS c, volume_schema, count(*) AS n
                FROM system.information_schema.volumes GROUP BY 1, 2) GROUP BY c""",
            "metric_views": """SELECT table_catalog AS c, count(*) AS v
                FROM system.information_schema.tables WHERE table_type = 'METRIC_VIEW'
                GROUP BY 1""",
        }
        for key, q in queries.items():
            try:
                put(ctx.sql(q), key)
            except Exception:
                continue
        # Keep non-system catalogs only (the has_comment query defines that set).
        return {c: v for c, v in out.items() if "has_comment" in v}
    return _memo(ctx, "catalog_inventory", build)


def current_metastore(ctx: Ctx) -> str:
    """``cloud:region:metastore_id`` of this workspace (``""`` if unknown)."""
    def build():
        try:
            return str(ctx.one("SELECT current_metastore() AS m").get("m") or "")
        except Exception:
            return ""
    return _memo(ctx, "metastore", build)


def run_export(ctx: Ctx, results: list, workspace_url: str = "") -> dict[str, Any]:
    """Everything a multi-workspace combiner needs from one run (JSON-serializable).
    Rendered into the markdown report's appendices and written as ``_run.json``."""
    scope = ctx.scope
    return {
        "format": "waf-run-export/1",
        "run_id": ctx.run_id,
        "metastore": current_metastore(ctx),
        "complete_at": ctx.complete_at,
        "created": ctx.run_ts.isoformat(),
        "workspace_id": current_workspace_id(ctx),
        "workspace_url": workspace_url,
        "lookback_days": ctx.lookback_days,
        "window": window_text(ctx),
        "scope": {"mode": scope.mode, "label": scope.label, "catalogs": scope.catalogs},
        "hidden_catalogs": hidden_catalogs(ctx) or [],
        "runner": ctx.runner,
        "privilege_hidden_catalogs": privilege_hidden_catalogs(ctx) or [],
        "unreadable_tables": dict(sorted(ctx.unreadable.items())),
        "catalog_inventory": catalog_inventory(ctx),
        "results": [{"pillar": r.pillar, "question_id": r.qid, "principle": r.principle,
                     "question": r.title, "status": r.status, "reason": r.reason,
                     "metrics": r.metrics} for r in results],
    }


def access_report(ctx: Ctx, tables: list[tuple[str, str]]) -> list[str]:
    """Pre-flight lines for 00_config: who is running, which roles they hold, which system
    tables they can read, the GRANTs that are missing, and in-scope catalogs hidden from
    them. Being a workspace admin alone grants none of the system tables."""
    lines = [f"Running as        : {ctx.runner}"]
    try:
        ws_admin = bool(ctx.one("SELECT is_member('admins') AS a").get("a"))
        lines.append(f"Workspace admin   : {'yes' if ws_admin else 'no'}")
    except Exception:
        lines.append("Workspace admin   : unknown")
    ms_admin = None
    w = ctx.w
    if w is not None:
        try:
            owner = w.metastores.get(w.metastores.current().metastore_id).owner or ""
            ms_admin = owner == ctx.runner or bool(ctx.one(
                f"SELECT is_account_group_member('{owner.replace(chr(39), '')}') AS a").get("a"))
            lines.append(f"Metastore admin   : {'yes' if ms_admin else 'no'} (owner: {owner})")
        except Exception:
            lines.append("Metastore admin   : unknown")
    ids = identity_counts(ctx)
    lines.append("Account directory : " + ("readable (OE-04-01 identity limits counted)"
                 if ids else "not readable - OE-04-01 lists the user/group limits for "
                 "manual confirmation"))
    ok, denied, missing = [], [], []
    for fqn, why in tables:
        if ctx.has_table(fqn):
            ok.append((fqn, why))
        elif ctx.unreadable.get(fqn) == "denied":
            denied.append((fqn, why))
        else:
            missing.append((fqn, why))
    lines += ["", f"System tables readable: {len(ok)}/{len(tables)}"]
    lines += [f"  [x] {fqn:<50} {why}" for fqn, why in ok]
    lines += [f"  [!] {fqn:<50} {why} - missing grant" for fqn, why in denied]
    lines += [f"  [ ] {fqn:<50} {why} - not found" for fqn, why in missing]
    if denied:
        schemas = sorted({f.rsplit(".", 1)[0] for f, _ in denied})
        lines += ["", "Checks that need these tables will be manual-review with an [ACCESS] "
                  "note. A metastore or account admin can grant them:",
                  f"  GRANT USE CATALOG ON CATALOG system TO `{ctx.runner}`;"]
        lines += [f"  GRANT USE SCHEMA, SELECT ON SCHEMA {s} TO `{ctx.runner}`;" for s in schemas]
    if missing:
        lines += ["", "Not found = the system schema is not enabled in this metastore (an "
                  "account admin enables it) or the feature is not in use."]
    if ms_admin is False:
        lines += ["", "Not a metastore admin: information_schema shows only catalogs you "
                  "have privileges on, so production-scoped checks cover just those."]
    hidden = privilege_hidden_catalogs(ctx)
    if hidden:
        shown = ", ".join(hidden[:10]) + (", ..." if len(hidden) > 10 else "")
        lines += ["", f"In-scope catalogs you cannot see ({len(hidden)}): {shown}",
                  "Catalog-scoped checks will be manual-review with an [ACCESS] note. Grant "
                  "BROWSE (or USE CATALOG/USE SCHEMA/SELECT) on them, or run as a "
                  "metastore admin."]
    return lines
