import pytest

from apps.users.models import TenantMembership


@pytest.mark.django_db
class TestTenant:
    def test_create_tenant(self, db):
        from apps.users.models import Tenant

        t = Tenant.objects.create(
            provider="commcare",
            external_id="dimagi",
            canonical_name="Dimagi",
        )
        assert t.provider == "commcare"
        assert t.external_id == "dimagi"
        assert str(t) == "commcare:dimagi (Dimagi)"

    def test_unique_constraint(self, db):
        from apps.users.models import Tenant

        Tenant.objects.create(provider="commcare", external_id="dimagi", canonical_name="Dimagi")
        with pytest.raises(Exception):  # noqa: B017
            Tenant.objects.create(
                provider="commcare", external_id="dimagi", canonical_name="Dimagi2"
            )


@pytest.mark.django_db
class TestTenantMembership:
    def test_create_membership_with_tenant(self, db, user):
        from apps.users.models import Tenant

        tenant = Tenant.objects.create(
            provider="commcare", external_id="dimagi", canonical_name="Dimagi"
        )
        tm = TenantMembership.objects.create(user=user, tenant=tenant)
        assert tm.tenant.external_id == "dimagi"
        assert tm.tenant.provider == "commcare"
        assert tm.tenant.canonical_name == "Dimagi"
        assert str(tm) == f"TenantMembership({tm.user_id} - {tm.tenant_id})"

    def test_unique_constraint(self, db, user):
        from apps.users.models import Tenant

        tenant = Tenant.objects.create(
            provider="commcare", external_id="dimagi", canonical_name="Dimagi"
        )
        TenantMembership.objects.create(user=user, tenant=tenant)
        with pytest.raises(Exception):  # noqa: B017
            TenantMembership.objects.create(user=user, tenant=tenant)

    def test_last_selected_at_nullable(self, db, user):
        from apps.users.models import Tenant

        tenant = Tenant.objects.create(
            provider="commcare", external_id="dimagi", canonical_name="Dimagi"
        )
        tm = TenantMembership.objects.create(user=user, tenant=tenant)
        assert tm.last_selected_at is None
