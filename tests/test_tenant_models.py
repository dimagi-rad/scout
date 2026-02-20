import pytest

from apps.users.models import TenantMembership


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
