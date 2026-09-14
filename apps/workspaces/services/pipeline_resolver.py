"""The one place a tenant's materialization pipeline is resolved (#155, arch 01#9).

Every read surface used to end its resolution chain with
``registry.get("commcare_sync")``. For an OCS or Connect tenant that returned
the *wrong provider's* pipeline, which maps source names to physical tables and
supplies their descriptions — so the user got commcare table descriptions, or an
empty catalog when no ``raw_*`` name matched, with nothing logged anywhere. The
lie was indistinguishable from "this workspace has no data".

Resolution either succeeds or raises ``PipelineResolutionError``. Callers that
genuinely have no tenant to attribute (a multi-tenant ``ws_*`` view schema) pass
``pipeline_config=None`` down instead of calling in here — an explicit "no
pipeline" that the metadata layer renders as "no pipeline-derived descriptions".

``PipelineResolutionError`` is deliberately NOT an ``ExpectedStateError``: a
missing pipeline for a provider Scout supports is a deploy or code defect, and
adding the pipeline YAML prevents it — so it fails the "routine in a correct
system" test in ``apps/common/errors`` and must keep reaching Sentry.
"""

from __future__ import annotations

from apps.common.error_codes import ErrorCode
from apps.users.models import Tenant
from mcp_server.pipeline_registry import PipelineConfig, PipelineRegistry, get_registry


class PipelineResolutionError(Exception):
    """No materialization pipeline could be resolved for a tenant."""

    code = ErrorCode.PIPELINE_UNRESOLVED


def no_pipeline_message(
    registry: PipelineRegistry,
    provider: str | None,
    *,
    last_run_pipeline: str | None = None,
) -> str:
    """Build the 'no pipeline' error, distinguishing cause (07#7).

    An unconfigured provider and a pipeline YAML that failed to parse used to
    share one message that wrongly pointed at workspace config; when the registry
    recorded load errors, say so explicitly so blame lands on the deploy.
    """
    subject = f"provider '{provider}'"
    if last_run_pipeline:
        subject += f" (last run used pipeline '{last_run_pipeline}')"
    load_errors = registry.load_errors
    if load_errors:
        return (
            f"No pipeline available for {subject}: "
            f"{len(load_errors)} pipeline definition(s) failed to load "
            f"({', '.join(sorted(load_errors))}). This is a deploy/config error, "
            "not a workspace setting — check the pipeline YAML files."
        )
    return f"No pipeline configured for {subject}"


def config_for_run(last_run_pipeline: str | None) -> PipelineConfig | None:
    """The pipeline a run recorded, if that pipeline still exists in the registry."""
    if not last_run_pipeline:
        return None
    return get_registry().get(last_run_pipeline)


def select_pipeline_config(
    *,
    last_run_pipeline: str | None = None,
    provider: str | None = None,
) -> PipelineConfig:
    """Resolve a pipeline from a run's recorded name, then the tenant's provider.

    The last run's pipeline wins because it names the loader that actually wrote
    the tables being described; the provider is the fallback for a tenant with no
    terminal run yet. Raises ``PipelineResolutionError`` when neither resolves.
    """
    config = config_for_run(last_run_pipeline)
    if config is not None:
        return config
    registry = get_registry()
    if provider:
        config = registry.get_by_provider(provider)
        if config is not None:
            return config
    raise PipelineResolutionError(
        no_pipeline_message(registry, provider, last_run_pipeline=last_run_pipeline)
    )


async def aresolve_pipeline_config(tenant_schema, last_run) -> PipelineConfig:
    """Async resolver for a single tenant's schema. ``tenant_schema`` must not be None."""
    last_run_pipeline = last_run.pipeline if last_run else None
    # Short-circuit before loading the tenant: the run names the loader that
    # wrote these tables, and the provider is only needed for the fallback.
    config = config_for_run(last_run_pipeline)
    if config is not None:
        return config
    tenant = await Tenant.objects.aget(id=tenant_schema.tenant_id)
    return select_pipeline_config(last_run_pipeline=last_run_pipeline, provider=tenant.provider)


def resolve_pipeline_config(tenant_schema, last_run) -> PipelineConfig:
    """Sync sibling of ``aresolve_pipeline_config`` for DRF views."""
    return select_pipeline_config(
        last_run_pipeline=last_run.pipeline if last_run else None,
        provider=tenant_schema.tenant.provider,
    )
