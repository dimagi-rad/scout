"""Tenant-owned storage survives revocation; reads require a live tenant membership."""

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import SchemaState, TenantMetadata, TenantSchema
from apps.workspaces.services.tenant_metadata import aget_tenant_metadata, get_tenant_metadata

SCHEMA_NAME = "t_md_domain"


@pytest.fixture
def md_tenant(db):
    tenant = Tenant.objects.create(
        provider="commcare", external_id="md-domain", canonical_name="MD Domain"
    )
    TenantSchema.objects.create(tenant=tenant, schema_name=SCHEMA_NAME, state=SchemaState.ACTIVE)
    return tenant


def _member(tenant, name, *, archived=False):
    user = get_user_model().objects.create_user(email=f"{name}@example.com")
    return TenantMembership.all_objects.create(
        user=user,
        tenant=tenant,
        archived_at=timezone.now() if archived else None,
    )


def _every_surface(tenant):
    """Read the tenant's metadata through each surface; return their ``owner`` values.

    The sync service backs the materializer; the async one backs MCP
    ``describe_table``/``get_metadata`` and the async catalog.
    """
    reads = [
        get_tenant_metadata(tenant.id),
        async_to_sync(aget_tenant_metadata)(tenant.id),
    ]
    return [None if md is None else md.metadata["owner"] for md in reads]


@pytest.mark.django_db
def test_every_surface_reads_the_tenants_one_row(md_tenant):
    _member(md_tenant, "one")
    _member(md_tenant, "two")
    TenantMetadata.objects.create(
        tenant=md_tenant, metadata={"owner": "tenant"}, discovered_at=timezone.now()
    )

    assert _every_surface(md_tenant) == ["tenant"] * 2


@pytest.mark.django_db
def test_metadata_outlives_the_member_who_discovered_it(md_tenant):
    """#305's wrong ``CASCADE``: one member leaving used to wipe the metadata the
    rest of the tenant still needed.
    """
    discoverer = _member(md_tenant, "discoverer")
    _member(md_tenant, "colleague")
    TenantMetadata.objects.create(tenant=md_tenant, metadata={"owner": "tenant"})

    discoverer.delete()

    assert _every_surface(md_tenant) == ["tenant"] * 2


@pytest.mark.django_db
def test_revoking_last_membership_hides_but_retains_metadata(md_tenant):
    member = _member(md_tenant, "revoked")
    TenantMetadata.objects.create(tenant=md_tenant, metadata={"owner": "tenant"})

    member.archived_at = timezone.now()
    member.save(update_fields=["archived_at"])

    assert _every_surface(md_tenant) == [None] * 2
    assert TenantMetadata.objects.get(tenant=md_tenant).metadata == {"owner": "tenant"}

    member.archived_at = None
    member.save(update_fields=["archived_at"])
    assert _every_surface(md_tenant) == ["tenant"] * 2


@pytest.mark.django_db
def test_deleting_last_membership_retains_hidden_storage(md_tenant):
    member = _member(md_tenant, "last")
    TenantMetadata.objects.create(tenant=md_tenant, metadata={"owner": "tenant"})
    member.delete()
    assert _every_surface(md_tenant) == [None] * 2
    assert TenantMetadata.objects.filter(tenant=md_tenant).exists()


@pytest.mark.django_db
def test_undiscovered_tenant_reads_as_absent(md_tenant):
    _member(md_tenant, "member")

    assert _every_surface(md_tenant) == [None] * 2
