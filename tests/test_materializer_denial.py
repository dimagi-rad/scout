"""Run failure accounting also removes the actor's denied upstream access."""

from unittest.mock import patch

import pytest
import requests
from allauth.socialaccount.models import SocialAccount, SocialToken

from apps.common.errors import OCSAccessDeniedError, OCSTokenExpiredError
from apps.users.models import Tenant, TenantConnection, TenantMembership
from apps.users.services.credential_resolver import _make_token_refresher
from apps.users.services.token_refresh import TokenRefreshRejected, TokenRefreshUnavailable
from apps.workspaces.access import resolve_workspace_access_ex
from apps.workspaces.models import MaterializationRun, TenantSchema, Workspace
from mcp_server.loaders.ocs_base import OCSBaseLoader
from mcp_server.pipeline_registry import PipelineConfig, SourceConfig
from mcp_server.services.materializer import run_pipeline


@pytest.fixture
def denial_context(user):
    account = SocialAccount.objects.create(user=user, provider="ocs", uid="user#team")
    token = SocialToken.objects.create(account=account, token="original")
    conn = TenantConnection.objects.create(
        user=user, provider="ocs", credential_type="oauth", scope_key="team", social_account=account
    )
    tenants = [
        Tenant.objects.create(provider="ocs", external_id=str(i), canonical_name=f"Bot {i}")
        for i in range(2)
    ]
    memberships = [
        TenantMembership.objects.create(user=user, tenant=t, connection=conn) for t in tenants
    ]
    schema = TenantSchema.objects.create(tenant=tenants[0], schema_name="denial_test")
    pipeline = PipelineConfig(
        name="ocs_test",
        description="",
        version="1",
        provider="ocs",
        sources=[SourceConfig(name="sessions")],
    )
    return conn, token, memberships, schema, pipeline


@pytest.mark.django_db
@pytest.mark.parametrize("stage", ["discover", "load"])
@pytest.mark.parametrize("error_type", [OCSAccessDeniedError, OCSTokenExpiredError])
def test_materializer_denial_scope(denial_context, stage, error_type):
    conn, token, memberships, schema, pipeline = denial_context
    error = error_type("upstream denied")
    with (
        patch(
            "mcp_server.services.materializer._run_discover_phase",
            side_effect=error if stage == "discover" else None,
            return_value={},
        ),
        patch("mcp_server.services.materializer._load_and_commit_source", side_effect=error),
    ):
        with pytest.raises(error_type):
            run_pipeline(
                memberships[0],
                {"type": "oauth", "value": token.token},
                pipeline,
                target_schema=schema,
            )
    assert not TenantMembership.objects.filter(pk=memberships[0].pk).exists()
    assert TenantMembership.objects.filter(pk=memberships[1].pk).exists() == (
        error_type is OCSAccessDeniedError
    )
    run = MaterializationRun.objects.get(tenant_schema=schema)
    assert run.state == MaterializationRun.RunState.FAILED
    assert run.completed_at is not None
    conn.refresh_from_db()
    assert bool(conn.upstream_denial_code) == (error_type is OCSTokenExpiredError)
    ws = Workspace.objects.filter(tenants=memberships[0].tenant).first()
    assert not resolve_workspace_access_ex(memberships[0].user, ws.pk).granted


@pytest.mark.django_db
@pytest.mark.parametrize("replacement", ["token", "identity", "membership"])
def test_stale_run_cannot_revoke_replacement(denial_context, replacement):
    conn, token, memberships, schema, pipeline = denial_context
    # Cache the actual identity handed to the run before a concurrent reconnect.
    _ = memberships[0].connection
    if replacement == "token":
        SocialToken.objects.filter(pk=token.pk).update(token="new-token")
    else:
        account = SocialAccount.objects.create(
            user=conn.user, provider="ocs", uid="replacement#team"
        )
        SocialToken.objects.create(account=account, token="new-token")
        if replacement == "identity":
            TenantConnection.objects.filter(pk=conn.pk).update(social_account=account)
        else:
            other = TenantConnection.objects.create(
                user=conn.user, provider="ocs", credential_type="api_key"
            )
            TenantMembership.objects.filter(pk=memberships[0].pk).update(connection=other)
    with patch(
        "mcp_server.services.materializer._run_discover_phase",
        side_effect=OCSAccessDeniedError("denied"),
    ):
        with pytest.raises(OCSAccessDeniedError):
            run_pipeline(
                memberships[0],
                {"type": "oauth", "value": "original"},
                pipeline,
                target_schema=schema,
            )
    assert TenantMembership.objects.filter(pk=memberships[0].pk).exists()


@pytest.mark.django_db
def test_unscoped_denial_does_not_revoke_tenant(denial_context):
    _conn, token, memberships, schema, pipeline = denial_context
    error = OCSAccessDeniedError("global discovery endpoint denied")
    error.denial_scope = "unknown"
    with patch("mcp_server.services.materializer._run_discover_phase", side_effect=error):
        with pytest.raises(OCSAccessDeniedError):
            run_pipeline(
                memberships[0],
                {"type": "oauth", "value": token.token},
                pipeline,
                target_schema=schema,
            )
    assert TenantMembership.objects.filter(pk=memberships[0].pk).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("final_status", [401, 403])
def test_midrun_denial_uses_rotated_credential(denial_context, final_status):

    _conn, token, memberships, schema, pipeline = denial_context
    credential = {"type": "oauth", "value": token.token}

    def refresh(*args):
        SocialToken.objects.filter(pk=token.pk).update(token="rotated")
        return "rotated"

    def load(*args, **kwargs):
        loader = OCSBaseLoader(experiment_id="0", credential=credential)
        responses = []
        for status in [401, final_status]:
            response = requests.Response()
            response.status_code = status
            responses.append(response)
        with patch.object(loader._session, "get", side_effect=responses):
            loader._get("https://ocs.example/api/experiments/0/")

    credential["refresh"] = _make_token_refresher(token, "https://ocs.example/o/token/", credential)
    with (
        patch(
            "apps.users.services.credential_resolver.refresh_oauth_token_sync", side_effect=refresh
        ),
        patch("mcp_server.services.materializer._run_discover_phase", side_effect=load),
    ):
        with pytest.raises((OCSTokenExpiredError, OCSAccessDeniedError)):
            run_pipeline(memberships[0], credential, pipeline, target_schema=schema)
    assert credential["value"] == "rotated"
    assert not TenantMembership.objects.filter(pk=memberships[0].pk).exists()
    assert TenantMembership.objects.filter(pk=memberships[1].pk).exists() == (final_status == 403)


@pytest.mark.django_db
@pytest.mark.parametrize("error", [TimeoutError("network"), ValueError("shape drift")])
def test_inconclusive_materialization_does_not_revoke(denial_context, error):
    _conn, token, memberships, schema, pipeline = denial_context
    with patch("mcp_server.services.materializer._run_discover_phase", side_effect=error):
        with pytest.raises(type(error)):
            run_pipeline(
                memberships[0],
                {"type": "oauth", "value": token.token},
                pipeline,
                target_schema=schema,
            )
    assert TenantMembership.objects.filter(pk=memberships[0].pk).exists()


@pytest.mark.django_db
def test_ocs_api_key_401_revokes_memberships_with_team_metadata(denial_context):
    conn, _token, memberships, schema, pipeline = denial_context
    conn.credential_type = "api_key"
    conn.scope_key = ""
    conn.social_account = None
    conn.save()
    for member in memberships:
        member.connection = conn
        member.provider_metadata = {"team_slug": "team"}
        member.save()
    with patch(
        "mcp_server.services.materializer._run_discover_phase",
        side_effect=OCSTokenExpiredError("denied"),
    ):
        with pytest.raises(OCSTokenExpiredError):
            run_pipeline(
                memberships[0], {"type": "api_key", "value": "key"}, pipeline, target_schema=schema
            )
    assert not TenantMembership.objects.filter(connection=conn).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("error_type", [TokenRefreshRejected, TokenRefreshUnavailable])
def test_refresh_failure_records_actionable_code_without_archival(denial_context, error_type):
    conn, token, memberships, schema, pipeline = denial_context
    with patch(
        "mcp_server.services.materializer._run_discover_phase",
        side_effect=error_type("refresh failed"),
    ):
        with pytest.raises(error_type):
            run_pipeline(
                memberships[0],
                {"type": "oauth", "value": token.token},
                pipeline,
                target_schema=schema,
            )
    assert TenantMembership.objects.filter(pk__in=[m.pk for m in memberships]).count() == 2
    conn.refresh_from_db()
    assert conn.upstream_denied_at is None
    run = MaterializationRun.objects.get(tenant_schema=schema)
    assert run.state == MaterializationRun.RunState.FAILED
    assert run.result["error_code"] == error_type.code
