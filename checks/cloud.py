"""Cloud-specific instance rules, selected by ``ctx.cloud`` (``CLOUD_PROVIDER`` in 00_config).

Instance names, GPU families and spot settings differ per cloud, so a single pattern
silently misreads the other clouds (an AWS-only GPU regex finds no GPUs on Azure; a
``'%spot%'`` match on Azure attributes hits the ``spot_bid_max_price`` key on every
cluster). Each rule here is keyed by cloud; regexes are plain enough to use both in
Spark ``RLIKE`` and Python ``re``.
"""

from __future__ import annotations

import re

from waf_core import Ctx

#: GPU instance families, matched against the lowercased node type.
GPU_NODE_REGEX = {
    "aws": r"^(g[0-9]|gr[0-9]|p[0-9])",      # g4dn.xlarge, g5, gr6, p4d.24xlarge
    "azure": r"^standard_n[cdgv]",           # Standard_NC8as_T4_v3, ND, NG, NV series
    "gcp": r"^(a2|a3|g2)-",                  # a2-highgpu-1g, g2-standard-4
}

#: Cluster attribute that says whether workers run on spot / preemptible capacity.
SPOT_SQL = {
    "aws": "aws_attributes.availability IN ('SPOT', 'SPOT_WITH_FALLBACK')",
    "azure": "azure_attributes.availability IN ('SPOT_AZURE', 'SPOT_WITH_FALLBACK_AZURE')",
    "gcp": ("(gcp_attributes.availability IN ('PREEMPTIBLE_GCP', 'PREEMPTIBLE_WITH_FALLBACK_GCP')"
            " OR coalesce(gcp_attributes.use_preemptible_executors, false))"),
}

#: AWS / GCP current-generation family prefixes (anchored to the start of the node
#: type, so ``m5`` matches ``m5d.xlarge`` but ``c8`` no longer matches ``nc8as``).
#: Override with ``MODERN_INSTANCE_FAMILIES`` in 00_config.
DEFAULT_MODERN_INSTANCE_FAMILIES = {
    "aws": ["c5", "c6", "c7", "c8", "d3", "i3", "i4", "im4", "is4",
            "m5", "m6", "m7", "m8", "r5", "r6", "r7", "r8"],
    "gcp": ["c2", "c3", "c4", "e2", "n2", "n4", "t2"],
}

#: Azure encodes the generation as a ``_vN`` suffix (no suffix = v1). v5 and later are
#: current; override with ``AZURE_MIN_VM_VERSION`` in 00_config.
DEFAULT_AZURE_MIN_VM_VERSION = 5


def gpu_regex(ctx: Ctx) -> str:
    return GPU_NODE_REGEX[ctx.cloud]


def is_gpu(ctx: Ctx, node_type: str) -> bool:
    return bool(re.search(gpu_regex(ctx), (node_type or "").lower()))


def spot_sql(ctx: Ctx) -> str:
    return SPOT_SQL[ctx.cloud]


def is_current_generation(ctx: Ctx, node_type: str) -> bool:
    """Whether a (non-GPU) node type is a current-generation instance for ``ctx.cloud``."""
    nt = (node_type or "").lower()
    if ctx.cloud == "azure":
        m = re.search(r"_v([0-9]+)$", nt)
        min_v = int(ctx.cfg.get("azure_min_vm_version") or DEFAULT_AZURE_MIN_VM_VERSION)
        return (int(m.group(1)) if m else 1) >= min_v
    fams = (ctx.cfg.get("modern_instance_families")
            or DEFAULT_MODERN_INSTANCE_FAMILIES[ctx.cloud])
    return any(nt.startswith(f.lower()) for f in fams)


def generation_rule(ctx: Ctx) -> str:
    """One-line description of the current-generation rule, for rationales."""
    if ctx.cloud == "azure":
        min_v = int(ctx.cfg.get("azure_min_vm_version") or DEFAULT_AZURE_MIN_VM_VERSION)
        return f"Azure VM version v{min_v} or later (the _vN suffix)"
    fams = (ctx.cfg.get("modern_instance_families")
            or DEFAULT_MODERN_INSTANCE_FAMILIES[ctx.cloud])
    return f"{ctx.cloud.upper()} family prefix in {', '.join(fams)}"
