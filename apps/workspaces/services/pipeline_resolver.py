"""One canonical pipeline resolver (arch #251, Phase 4, Decision 7 / #256).

Pipeline resolution used to fall back to ``registry.get("commcare_sync")`` when
resolution failed — even for OCS/Connect tenants — silently serving WRONG-provider
metadata. This module is the SINGLE factored resolver every surface (agent
prompt, MCP tools, DRF dictionary) routes through. When a pipeline is
unresolvable it raises ``PipelineResolutionError`` (a truthful error the surface
turns into an error response) instead of lying with commcare metadata. A tenant
whose provider legitimately IS commcare still resolves normally.

Decision 1 direction: apps-owned; ``mcp_server`` imports from here.
"""

from __future__ import annotations

from apps.users.models import Tenant
from mcp_server.pipeline_registry import PipelineConfig, PipelineRegistry, get_registry


class PipelineResolutionError(Exception):
    """No materialization pipeline could be resolved for a tenant/run.

    Raised instead of falling back to commcare_sync metadata for a non-commcare
    tenant; the calling surface converts it into a truthful error response.
    """


def _unresolvable_message(
    last_run_pipeline: str | None, provider: str | None, registry: PipelineRegistry
) -> str:
    base = (
        "No materialization pipeline could be resolved "
        f"(last run pipeline={last_run_pipeline!r}, provider={provider!r})."
    )
    # A YAML parse failure silently drops a provider from the registry; surface it
    # so "no pipeline" points at the real cause (a broken deploy) not workspace
    # config (arch #256, 07#7).
    load_errors = registry.load_errors
    if load_errors:
        return f"{base} Pipeline definitions failed to load: {', '.join(load_errors)}."
    return base


def select_pipeline_config(last_run_pipeline: str | None, provider: str | None) -> PipelineConfig:
    """Resolve a PipelineConfig from a run's pipeline name then a tenant provider.

    Prefers the last run's pipeline, then the provider's pipeline. Raises
    ``PipelineResolutionError`` when neither resolves.
    """
    registry = get_registry()
    if last_run_pipeline:
        cfg = registry.get(last_run_pipeline)
        if cfg:
            return cfg
    if provider:
        cfg = registry.get_by_provider(provider)
        if cfg:
            return cfg
    raise PipelineResolutionError(_unresolvable_message(last_run_pipeline, provider, registry))


async def aresolve_pipeline_config(tenant_schema, last_run) -> PipelineConfig:
    """Async resolver for a single-tenant schema (``tenant_schema`` not None).

    Multi-tenant ``ws_*`` view schemas have no single tenant pipeline to infer;
    callers pass ``pipeline_config=None`` for them and must NOT call this.
    """
    provider = None
    if tenant_schema is not None:
        tenant = await Tenant.objects.aget(id=tenant_schema.tenant_id)
        provider = tenant.provider
    return select_pipeline_config(last_run.pipeline if last_run else None, provider)


def resolve_pipeline_config_sync(tenant_schema, last_run) -> PipelineConfig:
    """Sync sibling of ``aresolve_pipeline_config`` for the DRF data dictionary."""
    provider = tenant_schema.tenant.provider if tenant_schema is not None else None
    return select_pipeline_config(last_run.pipeline if last_run else None, provider)
