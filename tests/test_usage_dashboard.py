"""The usage dashboard: who may see it, what it adds up, and how access is granted."""

import uuid
from datetime import timedelta
from io import StringIO

import pytest
from django.contrib.auth.models import Group, Permission
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.test import Client
from django.utils import timezone

from apps.recipes.models import Recipe, RecipeRun, RecipeRunStatus
from apps.telemetry.dashboard import build_dashboard
from apps.telemetry.models import (
    USAGE_DASHBOARD_PERMISSION,
    DailySnapshot,
    EventKind,
    Outcome,
    SnapshotMetric,
    TelemetryEvent,
)
from apps.users.models import User
from apps.workspaces.models import MaterializationRun, TenantSchema, WorkspaceLoadTiming

URL = "/api/telemetry/dashboard/"


def _grant(user):
    call_command("grant_usage_dashboard", user.email, stdout=StringIO())
    return User.objects.get(pk=user.pk)


def _client_for(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.django_db
class TestPermissionGate:
    @pytest.fixture(autouse=True)
    def _fresh_cache(self):
        cache.clear()

    def test_anonymous_is_refused(self):
        assert Client().get(URL).status_code in (401, 403)

    def test_a_member_without_the_permission_is_refused(self, user):
        assert _client_for(user).get(URL).status_code == 403

    def test_staff_alone_is_not_enough(self, user):
        user.is_staff = True
        user.save()

        assert _client_for(user).get(URL).status_code == 403

    def test_a_granted_user_sees_the_dashboard(self, user):
        response = _client_for(_grant(user)).get(URL)

        assert response.status_code == 200
        assert response.json()["window"]["days"] == 30

    def test_a_superuser_passes_without_a_grant(self, admin_user):
        assert _client_for(admin_user).get(URL).status_code == 200

    def test_the_window_is_clamped(self, admin_user):
        client = _client_for(admin_user)

        assert client.get(URL, {"days": "9999"}).json()["window"]["days"] == 365
        assert client.get(URL, {"days": "0"}).json()["window"]["days"] == 1
        assert client.get(URL, {"days": "x"}).json()["window"]["days"] == 30


@pytest.mark.django_db
class TestMeFlag:
    @pytest.fixture(autouse=True)
    def _fresh_cache(self):
        cache.clear()

    def test_me_reports_the_permission(self, user):
        assert _client_for(user).get("/api/auth/me/").json()["can_view_usage_dashboard"] is False
        granted = _grant(user)

        me = _client_for(granted).get("/api/auth/me/").json()

        assert me["can_view_usage_dashboard"] is True


@pytest.mark.django_db
def test_the_dashboard_is_cached_per_window(admin_user):
    cache.clear()
    client = _client_for(admin_user)
    first = client.get(URL, {"days": "7"}).json()
    TelemetryEvent.objects.create(kind=EventKind.LOGIN, user_id=admin_user.id)

    assert client.get(URL, {"days": "7"}).json() == first
    assert client.get(URL, {"days": "8"}).json()["active_users"]["dau"] == 1


@pytest.mark.django_db
def test_a_non_numeric_attr_never_breaks_the_dashboard():
    TelemetryEvent.objects.create(
        kind=EventKind.CHAT_TURN, name="live", outcome=Outcome.COMPLETED, attrs={"ttft_ms": True}
    )

    assert build_dashboard(days=1)["turns"]["ttft_ms"]["p50"] is None


@pytest.mark.django_db
def test_dashboard_adds_up_events_and_existing_tables(user, workspace, tenant):
    # Midday, so events an hour back stay on today's UTC bucket whenever this runs.
    now = timezone.now().replace(hour=12, minute=0, second=0, microsecond=0)
    ws = workspace.id

    other = User.objects.create_user(email="background@example.com", password="x")

    def event(kind, **fields):
        fields.setdefault("name", "live" if kind == EventKind.CHAT_TURN else "")
        return TelemetryEvent(kind=kind, occurred_at=now - timedelta(hours=1), **fields)

    TelemetryEvent.objects.bulk_create(
        [
            event(
                EventKind.CHAT_TURN,
                user_id=user.id,
                workspace_id=ws,
                outcome=Outcome.COMPLETED,
                duration_ms=1000,
                attrs={"ttft_ms": 200, "tool_calls": 2, "input_tokens": 50, "output_tokens": 5},
            ),
            event(
                EventKind.CHAT_TURN,
                user_id=user.id,
                workspace_id=ws,
                outcome=Outcome.FAILED,
                duration_ms=3000,
                attrs={"ttft_ms": 400, "tool_calls": 0, "input_tokens": 10, "output_tokens": 1},
            ),
            event(EventKind.TOOL_CALL, name="query", outcome=Outcome.OK, duration_ms=100),
            event(EventKind.TOOL_CALL, name="query", outcome=Outcome.ERROR, duration_ms=300),
            event(EventKind.ARTIFACT_VIEW, user_id=user.id, workspace_id=ws),
            # A resume the worker ran: not activity, not a user turn.
            event(
                EventKind.CHAT_TURN,
                user_id=other.id,
                workspace_id=ws,
                name="flush",
                outcome=Outcome.COMPLETED,
                duration_ms=90_000,
                attrs={"input_tokens": 7, "output_tokens": 1},
            ),
            # Outside the window: never counted.
            TelemetryEvent(
                kind=EventKind.CHAT_TURN,
                user_id=user.id,
                occurred_at=now - timedelta(days=40),
                outcome=Outcome.COMPLETED,
            ),
        ]
    )
    for job_id, seconds, succeeded in ((1, 1, True), (2, 3, True), (3, 600, False)):
        WorkspaceLoadTiming.objects.create(
            workspace=workspace,
            job_id=job_id,
            started_at=now - timedelta(minutes=20),
            completed_at=now - timedelta(minutes=20) + timedelta(seconds=seconds),
            succeeded=succeeded,
            phase_seconds={"loading": 50.0, "building_model": 10.0},
        )
    schema = TenantSchema.objects.create(tenant=tenant, schema_name="dash_probe")
    for state, seconds in ((MaterializationRun.RunState.COMPLETED, 4), ("failed", 1)):
        run = MaterializationRun.objects.create(tenant_schema=schema, pipeline="p", state=state)
        MaterializationRun.objects.filter(pk=run.pk).update(
            started_at=now - timedelta(minutes=5),
            completed_at=now - timedelta(minutes=5) + timedelta(seconds=seconds),
        )
    recipe = Recipe.objects.create(workspace=workspace, name="r", prompt="p", created_by=user)
    RecipeRun.objects.create(
        recipe=recipe,
        run_by=user,
        status=RecipeRunStatus.FAILED,
        started_at=now - timedelta(minutes=5),
        completed_at=now - timedelta(minutes=4),
    )
    tenant_id = str(workspace.tenants.first().id)
    DailySnapshot.objects.bulk_create(
        [
            DailySnapshot(
                day=now.date(), metric=SnapshotMetric.SCHEMA_BYTES, dimension=tenant_id, value=900
            ),
            DailySnapshot(day=now.date(), metric=SnapshotMetric.SCHEMA_BYTES, value=900),
            DailySnapshot(day=now.date(), metric=SnapshotMetric.SCHEMA_BYTES_RETAINED, value=100),
        ]
    )

    data = build_dashboard(days=30, now=now)

    assert data["active_users"]["dau"] == 1  # the background turn's user is not active
    assert data["active_users"]["daily"][-1] == 1
    assert data["features"]["turns"][-1] == 2
    assert data["features"]["artifact_views"][-1] == 1
    turns = data["turns"]
    assert turns["total"] == 2
    assert turns["outcomes"] == {"completed": 1, "stopped": 0, "failed": 1}
    assert turns["duration_ms"]["p50"] == 2000
    # Only the completed turn's first token counts.
    assert turns["ttft_ms"]["p50"] == 200
    assert turns["tool_calls_per_turn"] == 1
    # Spend counts the worker's turn too, so it reconciles with the per-workspace table.
    assert turns["tokens"]["input"] == 67
    [tool] = data["tools"]
    assert (tool["name"], tool["calls"], tool["errors"], tool["error_rate"]) == ("query", 2, 1, 0.5)
    [usage] = data["tokens_by_workspace"]
    assert (usage["workspace_id"], usage["name"], usage["input_tokens"]) == (
        str(ws),
        workspace.name,
        67,
    )
    assert data["turns"]["background"] == 1
    loads = data["loads"]
    assert (loads["total"], loads["failed"]) == (3, 1)
    # Interpolated like every other percentile here, over successful loads only.
    assert loads["duration_ms"]["p50"] == 2000
    assert {p["phase"]: p["p50_ms"] for p in loads["phases"]} == {
        "building_model": 10_000,
        "loading": 50_000,
    }
    mats = data["materializations"]
    assert mats["total"] == 2
    assert mats["states"] == {"completed": 1, "failed": 1}
    assert mats["duration_ms"]["p50"] == 4000
    assert data["updated"]["threads"] == [None] * 30
    assert data["recipe_runs"] == {
        "total": 1,
        "failed": 1,
        "duration_ms": {"p50": None, "p95": None},
    }
    assert data["created"]["workspaces"][-1] == 1
    assert data["schema_sizes"]["total_daily"][-1] == 900
    assert data["schema_sizes"]["top_tenants"][0]["bytes"] == 900
    assert data["schema_sizes"]["retained_bytes"] == 100
    assert len(data["days"]) == 30


@pytest.mark.django_db
def test_an_empty_dashboard_renders():
    data = build_dashboard(days=7)

    assert data["turns"]["total"] == 0
    assert data["turns"]["duration_ms"] == {"p50": None, "p95": None}
    assert data["tools"] == []
    assert data["schema_sizes"]["as_of"] is None


@pytest.mark.django_db
class TestGrantCommand:
    def _run(self, *args):
        out = StringIO()
        call_command("grant_usage_dashboard", *args, stdout=out)
        return out.getvalue()

    def test_grant_is_idempotent_and_says_what_changed(self, user):
        assert "Granted" in self._run(user.email.upper())
        assert "already has" in self._run(user.email)
        assert User.objects.get(pk=user.pk).has_perm(USAGE_DASHBOARD_PERMISSION)

    def test_revoke_is_idempotent_and_says_what_changed(self, user):
        self._run(user.email)

        assert "Revoked" in self._run(user.email, "--revoke")
        assert "no direct grant" in self._run(user.email, "--revoke")
        assert not User.objects.get(pk=user.pk).has_perm(USAGE_DASHBOARD_PERMISSION)

    def test_revoke_says_when_a_group_still_grants_access(self, user):
        group = Group.objects.create(name="usage viewers")
        group.permissions.add(
            Permission.objects.get(
                codename="view_usage_dashboard", content_type__app_label="telemetry"
            )
        )
        user.groups.add(group)

        assert "still has access" in self._run(user.email, "--revoke")

    def test_an_unknown_email_fails(self):
        with pytest.raises(CommandError):
            self._run(f"{uuid.uuid4().hex}@example.com")


@pytest.mark.django_db
def test_sizes_come_from_the_last_complete_night(tenant):
    today = timezone.now().date()
    yesterday = today - timedelta(days=1)
    DailySnapshot.objects.bulk_create(
        [
            DailySnapshot(day=yesterday, metric=SnapshotMetric.SCHEMA_BYTES, value=500),
            DailySnapshot(
                day=yesterday,
                metric=SnapshotMetric.SCHEMA_BYTES,
                dimension=str(tenant.id),
                value=500,
            ),
            # Tonight a lock skipped this tenant: no total, no tenant row.
            DailySnapshot(day=today, metric=SnapshotMetric.SCHEMAS_SKIPPED, value=1),
        ]
    )

    sizes = build_dashboard(days=7)["schema_sizes"]

    assert sizes["as_of"] == yesterday.isoformat()
    assert [row["bytes"] for row in sizes["top_tenants"]] == [500]
    assert sizes["latest_skipped"] == {"day": today.isoformat(), "schemas": 1}


@pytest.mark.django_db
def test_login_reports_the_permission(user):
    _grant(user)

    response = Client().post(
        "/api/auth/login/",
        data={"email": user.email, "password": "testpass123"},
        content_type="application/json",
    )

    assert response.json()["can_view_usage_dashboard"] is True
