"""
API views for workspace schema refresh.
"""

import logging
from dataclasses import dataclass

from django.db import transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.error_codes import ErrorCode
from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import (
    SchemaState,
    TenantSchema,
    WorkspaceRole,
)
from apps.workspaces.services.refresh_requests import (
    find_legacy_refresh_jobs,
    settle_finished_refresh_candidates,
)
from apps.workspaces.services.schema_manager import SchemaManager
from apps.workspaces.task_dispatch import defer_refresh_tenant_schema
from apps.workspaces.workspace_resolver import resolve_workspace_drf as resolve_workspace

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _RefreshOutcome:
    """One source's refresh outcome: its public entry, plus the single-source
    response (body and status) it would have produced on its own."""

    public: dict
    body: dict
    http_status: int


def _refresh_source(tenant) -> dict:
    return {"tenant_id": str(tenant.id), "tenant_name": tenant.canonical_name}


def _unstarted_refresh(tenant, state, error, http_status, code=None) -> _RefreshOutcome:
    """The outcome of a source whose refresh was not queued, refused or failed."""
    body = {"error": error, **({"code": code} if code else {})}
    return _RefreshOutcome({**_refresh_source(tenant), "status": state, **body}, body, http_status)


class RefreshSchemaView(APIView):
    """
    POST /api/workspaces/<workspace_id>/refresh/

    Triggers a background refresh of every source in the workspace. Requires
    read-write or manage role. Returns 202 Accepted once at least one source's
    refresh is queued; each source reports its own outcome.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, workspace_id):
        workspace, _membership, err = resolve_workspace(
            request, workspace_id, minimum_role=WorkspaceRole.READ_WRITE
        )
        if err:
            return err

        tenants = sorted(workspace.tenants.all(), key=lambda tenant: str(tenant.id))
        if not tenants:
            return Response(
                {"error": "Workspace has no associated tenant."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        memberships = {
            m.tenant_id: m
            for m in TenantMembership.objects.filter(user=request.user, tenant__in=tenants)
        }
        # Queue scans must not run under the tenant row locks (see find_legacy_refresh_jobs).
        legacy_jobs = {
            tenant.id: find_legacy_refresh_jobs(tenant)
            for tenant in tenants
            if tenant.id in memberships
        }

        outcomes = []
        # One transaction per source, in ascending id order: a failure on one source
        # can't roll back the refreshes already queued for the others, and no two
        # Tenant row locks are ever held together.
        for tenant in tenants:
            membership = memberships.get(tenant.id)
            if membership is None:
                # Not locked: the caller can't refresh it, and holding a shared
                # tenant's row would stall its loads for unrelated workspaces.
                outcomes.append(
                    _unstarted_refresh(
                        tenant,
                        state="no_membership",
                        error="No tenant membership found for this workspace.",
                        http_status=status.HTTP_400_BAD_REQUEST,
                    )
                )
                continue
            try:
                with transaction.atomic():
                    locked = Tenant.objects.select_for_update().filter(id=tenant.id).first()
                    if locked is None:
                        # Deleted since the list was read; there is nothing to refresh,
                        # as when the old single transaction's lock query skipped it.
                        continue
                    outcome = self._queue_tenant_refresh(
                        request, workspace, locked, membership, legacy_jobs
                    )
            # Deliberately broad: this is the bulkhead that keeps one source's failure,
            # whatever it is, from undoing the others (B1). It is logged at error.
            except Exception:
                logger.exception(
                    "Refresh of tenant %s in workspace %s could not be queued",
                    tenant.id,
                    workspace.id,
                )
                outcome = _unstarted_refresh(
                    tenant,
                    state="error",
                    error="The refresh could not be started because of a server error.",
                    http_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
            outcomes.append(outcome)

        if not outcomes:
            return Response(
                {"error": "Workspace has no associated tenant."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(outcomes) == 1:
            # Single-source workspaces keep the original response shapes.
            only = outcomes[0]
            if only.public["status"] == "provisioning":
                return Response(
                    {"schema_id": only.public["schema_id"], "status": "provisioning"},
                    status=status.HTTP_202_ACCEPTED,
                )
            return Response(only.body, status=only.http_status)
        refused = [o for o in outcomes if o.public["status"] != "provisioning"]
        started = len(refused) < len(outcomes)
        # No top-level schema_id: each source reports its own in tenants[].
        # "partial" keeps a 202 but tells clients some sources were not refreshed.
        body = {
            "status": ("partial" if refused else "provisioning") if started else "not_started",
            "tenants": [o.public for o in outcomes],
        }
        if refused:
            reasons = {o.body["error"] for o in refused}
            if not started and len(reasons) == 1:
                body["error"] = reasons.pop()
            else:
                prefix = (
                    "Some sources could not be refreshed"
                    if started
                    else "No source could be refreshed"
                )
                body["error"] = (
                    prefix
                    + ": "
                    + "; ".join(f"{o.public['tenant_name']}: {o.body['error']}" for o in refused)
                )
        codes = {o.body.get("code") for o in outcomes} - {None}
        if ErrorCode.REFRESH_RECOVERY_REQUIRED in codes:
            body["code"] = ErrorCode.REFRESH_RECOVERY_REQUIRED
        if started:
            return Response(body, status=status.HTTP_202_ACCEPTED)
        # 400 only when every source was a bad request, as the single-source path.
        # When nothing started, a server error on any source outranks the refusals:
        # the refusal alone would misstate why nothing ran. (Once something started
        # the response is a 202 "partial", and tenants[] carries each "error".)
        http_statuses = {o.http_status for o in outcomes}
        if status.HTTP_500_INTERNAL_SERVER_ERROR in http_statuses:
            http_status = status.HTTP_500_INTERNAL_SERVER_ERROR
        elif http_statuses == {status.HTTP_400_BAD_REQUEST}:
            http_status = status.HTTP_400_BAD_REQUEST
        else:
            http_status = status.HTTP_409_CONFLICT
        return Response(body, status=http_status)

    @staticmethod
    def _queue_tenant_refresh(
        request, workspace, tenant, tenant_membership, legacy_jobs
    ) -> _RefreshOutcome:
        def refused(state, error, http_status, code=None):
            return _unstarted_refresh(tenant, state, error, http_status, code)

        legacy = settle_finished_refresh_candidates(tenant, legacy_jobs[tenant.id])
        if legacy.recovery_needed:
            return refused(
                "recovery_required",
                "A previous refresh could not be verified. Ask an operator to inspect "
                "and reconcile the queued refresh before retrying.",
                status.HTTP_409_CONFLICT,
                ErrorCode.REFRESH_RECOVERY_REQUIRED,
            )
        if (
            TenantSchema.objects.select_for_update()
            .filter(tenant=tenant, state=SchemaState.PROVISIONING)
            .exists()
        ):
            return refused(
                "in_progress", "A refresh is already in progress.", status.HTTP_409_CONFLICT
            )
        new_schema = SchemaManager().create_refresh_schema(tenant)
        job = defer_refresh_tenant_schema(
            schema_id=str(new_schema.id),
            membership_id=str(tenant_membership.id),
            actor_user_id=str(request.user.id),
            workspace_id=str(workspace.id),
        )
        new_schema.refresh_job_id = getattr(job, "id", job)
        new_schema.refresh_workspace_id = workspace.id
        new_schema.refresh_actor_user_id = request.user.id
        new_schema.refresh_membership_id = tenant_membership.id
        new_schema.save(
            update_fields=[
                "refresh_job_id",
                "refresh_workspace_id",
                "refresh_actor_user_id",
                "refresh_membership_id",
            ]
        )
        return _RefreshOutcome(
            {**_refresh_source(tenant), "status": "provisioning", "schema_id": str(new_schema.id)},
            {},
            status.HTTP_202_ACCEPTED,
        )
