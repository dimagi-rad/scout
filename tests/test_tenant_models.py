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
    def test_create_membership(self, user):
        tm = TenantMembership.objects.create(
            user=user,
            provider="commcare",
            tenant_id="dimagi",
            tenant_name="Dimagi",
        )
        assert tm.tenant_id == "dimagi"
        assert tm.provider == "commcare"
        assert str(tm) == f"{user.email} - commcare:dimagi"

    def test_unique_constraint(self, user):
        TenantMembership.objects.create(
            user=user, provider="commcare", tenant_id="dimagi", tenant_name="Dimagi"
        )
        with pytest.raises(Exception):  # noqa: B017 - IntegrityError varies by DB backend
            TenantMembership.objects.create(
                user=user, provider="commcare", tenant_id="dimagi", tenant_name="Dimagi"
            )

    def test_last_selected_at_nullable(self, user):
        tm = TenantMembership.objects.create(
            user=user, provider="commcare", tenant_id="dimagi", tenant_name="Dimagi"
        )
        assert tm.last_selected_at is None
