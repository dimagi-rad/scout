"""SocialToken.token / token_secret encryption boundary and mixed-state reads."""

from io import StringIO
from types import SimpleNamespace

import pytest
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from asgiref.sync import sync_to_async
from cryptography.fernet import Fernet
from django.core.exceptions import FieldError, ImproperlyConfigured
from django.core.management import call_command
from django.db import connection as db_connection
from django.db import models
from django.test import override_settings

from apps.common.error_codes import ErrorCode
from apps.users.models import Tenant, TenantConnection, TenantMembership
from apps.users.services import token_refresh
from apps.users.services.access_verification_service import _load_claim_token
from apps.users.services.credential_resolver import (
    CredentialResolutionError,
    _aresolve_oauth_credential,
)
from apps.users.services.tenant_resolution import _aoauth_connection
from apps.users.services.token_refresh import (
    TokenRefreshStatus,
    refresh_oauth_token_result_sync,
)
from apps.users.services.upstream_denial import (
    adiscovery_connection,
    arecord_upstream_denial,
    credential_is_current,
)
from apps.users.token_encryption import (
    CIPHERTEXT_PREFIX,
    EncryptedTokenField,
    decrypt_token_value,
    encrypt_token_value,
    install_socialtoken_encryption,
    is_encrypted,
)

TOKEN_TABLE = SocialToken._meta.db_table


def _store_raw(token_pk, **columns):
    """Write column values straight to the table, bypassing the ORM field."""
    assignments = ", ".join(f"{name} = %s" for name in columns)
    with db_connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {TOKEN_TABLE} SET {assignments} WHERE id = %s",
            [*columns.values(), token_pk],
        )


def _raw(token_pk):
    with db_connection.cursor() as cursor:
        cursor.execute(f"SELECT token, token_secret FROM {TOKEN_TABLE} WHERE id = %s", [token_pk])
        return cursor.fetchone()


def _assert_stored_encrypted(token_pk, access, refresh):
    stored_access, stored_refresh = _raw(token_pk)
    for stored, plain in ((stored_access, access), (stored_refresh, refresh)):
        assert stored.startswith(CIPHERTEXT_PREFIX)
        assert plain not in stored
        assert decrypt_token_value(stored) == plain


def _encrypt_raw(token):
    _store_raw(
        token.pk,
        token=encrypt_token_value(token.token),
        token_secret=encrypt_token_value(token.token_secret),
    )


_aencrypt_raw = sync_to_async(_encrypt_raw)


@pytest.fixture
def commcare_token(user):
    app = SocialApp.objects.create(provider="commcare", name="CommCare", client_id="c", secret="s")
    account = SocialAccount.objects.create(user=user, provider="commcare", uid="enc-account")
    token = SocialToken.objects.create(
        account=account, app=app, token="plain-access", token_secret="plain-refresh"
    )
    conn = TenantConnection.objects.create(
        user=user,
        provider="commcare",
        credential_type=TenantConnection.OAUTH,
        social_account=account,
    )
    return token, conn


class TestTokenValueCrypto:
    def test_round_trip(self):
        encrypted = encrypt_token_value("ya29.secret")
        assert encrypted.startswith(CIPHERTEXT_PREFIX)
        assert "ya29.secret" not in encrypted
        assert decrypt_token_value(encrypted) == "ya29.secret"

    def test_encryption_is_idempotent(self):
        encrypted = encrypt_token_value("secret")
        assert encrypt_token_value(encrypted) == encrypted

    @pytest.mark.parametrize("value", ["", None])
    def test_empty_values_pass_through(self, value):
        assert encrypt_token_value(value) == value
        assert decrypt_token_value(value) == value

    def test_legacy_plaintext_reads_unchanged(self):
        assert not is_encrypted("plain-token")
        assert decrypt_token_value("plain-token") == "plain-token"

    def test_undecryptable_ciphertext_reads_as_empty(self):
        encrypted = encrypt_token_value("secret")
        with override_settings(DB_CREDENTIAL_KEY=Fernet.generate_key().decode()):
            assert decrypt_token_value(encrypted) == ""

    @pytest.mark.parametrize("key", ["", "not-a-fernet-key"])
    def test_misconfigured_key_reads_as_empty(self, key):
        encrypted = encrypt_token_value("secret")
        with override_settings(DB_CREDENTIAL_KEY=key):
            assert decrypt_token_value(encrypted) == ""

    @override_settings(DB_CREDENTIAL_KEY="")
    def test_missing_key_raises(self):
        with pytest.raises(ValueError, match="DB_CREDENTIAL_KEY"):
            encrypt_token_value("secret")


class TestFieldInstallation:
    @pytest.mark.parametrize("name", ["token", "token_secret"])
    def test_socialtoken_fields_are_encrypted_fields(self, name):
        assert isinstance(SocialToken._meta.get_field(name), EncryptedTokenField)

    def test_install_refuses_an_unexpected_field_type(self):
        model = SimpleNamespace(
            _meta=SimpleNamespace(get_field=lambda name: models.CharField(max_length=10))
        )
        with pytest.raises(ImproperlyConfigured, match="not TextField"):
            install_socialtoken_encryption(model)

    @pytest.mark.django_db
    def test_allauth_migration_state_is_unchanged(self):
        out = StringIO()
        call_command("makemigrations", "socialaccount", check=True, dry_run=True, stdout=out)
        assert "No changes detected" in out.getvalue()

    @pytest.mark.django_db
    @pytest.mark.parametrize("lookup", ["token", "token_secret", "token__startswith"])
    def test_value_lookups_are_refused(self, lookup):
        with pytest.raises(FieldError, match="encrypted at rest"):
            SocialToken.objects.filter(**{lookup: "x"}).exists()


@pytest.mark.django_db
class TestMixedStateReads:
    def test_encrypted_row_reads_as_plaintext(self, commcare_token):
        token, _conn = commcare_token
        _encrypt_raw(token)

        loaded = SocialToken.objects.get(pk=token.pk)
        assert (loaded.token, loaded.token_secret) == ("plain-access", "plain-refresh")
        assert list(
            SocialToken.objects.filter(pk=token.pk).values_list("token", "token_secret")
        ) == [("plain-access", "plain-refresh")]

    def test_plaintext_and_encrypted_rows_read_together(self, user, commcare_token):
        token, _conn = commcare_token
        _encrypt_raw(token)
        other_account = SocialAccount.objects.create(user=user, provider="commcare", uid="legacy")
        legacy = SocialToken.objects.create(account=other_account)
        _store_raw(legacy.pk, token="legacy-access", token_secret="legacy-refresh")

        rows = dict(
            SocialToken.objects.filter(pk__in=[token.pk, legacy.pk]).values_list("pk", "token")
        )
        assert rows == {token.pk: "plain-access", legacy.pk: "legacy-access"}

    def test_refresh_from_db_decrypts(self, commcare_token):
        token, _conn = commcare_token
        _encrypt_raw(token)
        token.refresh_from_db()
        assert token.token == "plain-access"


@pytest.mark.django_db
class TestValueComparisonsAgainstEncryptedRows:
    """Call sites that used to filter by token value in SQL."""

    def test_credential_is_current_matches_encrypted_row(self, commcare_token):
        token, conn = commcare_token
        _encrypt_raw(token)
        snapshot = (token.pk, "plain-refresh", token.app_id)

        assert credential_is_current(conn, "plain-access", snapshot)
        assert not credential_is_current(conn, "other-access", snapshot)
        assert not credential_is_current(conn, "plain-access", (token.pk, "old", token.app_id))

    def test_undecryptable_row_never_matches_an_empty_credential(self, user, commcare_token):
        token, conn = commcare_token
        _encrypt_raw(token)
        with override_settings(DB_CREDENTIAL_KEY=Fernet.generate_key().decode()):
            assert SocialToken.objects.get(pk=token.pk).token == ""
            assert not credential_is_current(conn, "", (token.pk, "", token.app_id))
            assert not credential_is_current(conn, "")
            assert (
                _aoauth_connection.func(
                    user,
                    "commcare",
                    scope_key="",
                    scope_label="",
                    account=token.account,
                    access_token="",
                )
                is None
            )

    @pytest.mark.asyncio
    @pytest.mark.django_db(transaction=True)
    async def test_undecryptable_row_is_not_resolved_by_discovery_or_claims(self, user):
        account = await SocialAccount.objects.acreate(
            user=user, provider="ocs", uid="broken", extra_data={"team": "team-b"}
        )
        token = await SocialToken.objects.acreate(account=account, token="broken-access")
        await TenantConnection.objects.acreate(
            user=user,
            provider="ocs",
            credential_type="oauth",
            scope_key="team-b",
            social_account=account,
        )
        await _aencrypt_raw(token)
        claim = SimpleNamespace(
            request=SimpleNamespace(token_snapshot=(token.pk, "", None), credential=""),
            observation=SimpleNamespace(account_identity=str(account.pk)),
        )

        with override_settings(DB_CREDENTIAL_KEY=Fernet.generate_key().decode()):
            assert await adiscovery_connection(user, "ocs", "") is None
            assert await _load_claim_token(claim) is None

    @pytest.mark.asyncio
    @pytest.mark.django_db(transaction=True)
    async def test_undecryptable_row_resolves_to_reconnect(self, user):
        account = await SocialAccount.objects.acreate(user=user, provider="commcare", uid="r")
        token = await SocialToken.objects.acreate(account=account, token="resolve-access")
        await _aencrypt_raw(token)

        with override_settings(DB_CREDENTIAL_KEY=Fernet.generate_key().decode()):
            loaded = await SocialToken.objects.aget(pk=token.pk)
            with pytest.raises(CredentialResolutionError) as excinfo:
                await _aresolve_oauth_credential(loaded, "commcare")
        assert excinfo.value.code == ErrorCode.AUTH_TOKEN_EXPIRED

    def test_bind_checks_current_token_against_encrypted_row(self, user, commcare_token):
        token, conn = commcare_token
        _encrypt_raw(token)
        bind = _aoauth_connection.func

        kwargs = dict(scope_key="", scope_label="", account=token.account)
        assert bind(user, "commcare", access_token="plain-access", **kwargs) == conn
        assert bind(user, "commcare", access_token="rotated-away", **kwargs) is None

    @pytest.mark.asyncio
    @pytest.mark.django_db(transaction=True)
    async def test_discovery_connection_resolves_encrypted_row(self, user):
        account = await SocialAccount.objects.acreate(
            user=user, provider="ocs", uid="d", extra_data={"team": "team-a"}
        )
        token = await SocialToken.objects.acreate(account=account, token="disc-access")
        conn = await TenantConnection.objects.acreate(
            user=user,
            provider="ocs",
            credential_type="oauth",
            scope_key="team-a",
            social_account=account,
        )
        await _aencrypt_raw(token)

        assert await adiscovery_connection(user, "ocs", "disc-access") == conn

    @pytest.mark.asyncio
    @pytest.mark.django_db(transaction=True)
    async def test_denial_with_encrypted_row_archives_and_respects_rotation(self, user):
        account = await SocialAccount.objects.acreate(user=user, provider="commcare", uid="u")
        token = await SocialToken.objects.acreate(
            account=account, token="deny-access", token_secret="deny-refresh"
        )
        conn = await TenantConnection.objects.acreate(
            user=user, provider="commcare", credential_type="oauth", social_account=account
        )
        tenant = await Tenant.objects.acreate(
            provider="commcare", external_id="d", canonical_name="D"
        )
        tm = await TenantMembership.all_objects.acreate(user=user, tenant=tenant, connection=conn)
        await _aencrypt_raw(token)
        stale_snapshot = (token.pk, "rotated-refresh", token.app_id)

        await arecord_upstream_denial(
            conn,
            credential="deny-access",
            code=ErrorCode.AUTH_TOKEN_EXPIRED,
            token_snapshot=stale_snapshot,
        )
        assert await TenantMembership.objects.filter(pk=tm.pk).aexists()

        await arecord_upstream_denial(
            conn,
            credential="deny-access",
            code=ErrorCode.AUTH_TOKEN_EXPIRED,
            token_snapshot=(token.pk, "deny-refresh", token.app_id),
        )
        assert not await TenantMembership.objects.filter(pk=tm.pk).aexists()

    @pytest.mark.asyncio
    @pytest.mark.django_db(transaction=True)
    async def test_claim_token_load_compares_decrypted_values(self, user):
        account = await SocialAccount.objects.acreate(user=user, provider="commcare", uid="c")
        token = await SocialToken.objects.acreate(
            account=account, token="claim-access", token_secret="claim-refresh"
        )
        await _aencrypt_raw(token)

        def claim(credential, refresh):
            return SimpleNamespace(
                request=SimpleNamespace(
                    token_snapshot=(token.pk, refresh, None), credential=credential
                ),
                observation=SimpleNamespace(account_identity=str(account.pk)),
            )

        loaded = await _load_claim_token(claim("claim-access", "claim-refresh"))
        assert loaded is not None
        assert loaded.pk == token.pk
        assert await _load_claim_token(claim("claim-access", "rotated")) is None
        assert await _load_claim_token(claim("rotated", "claim-refresh")) is None


@pytest.mark.django_db
class TestWritesEncrypt:
    def test_create_stores_ciphertext(self, commcare_token):
        token, _conn = commcare_token
        _assert_stored_encrypted(token.pk, "plain-access", "plain-refresh")

    def test_save_stores_ciphertext(self, commcare_token):
        token, _conn = commcare_token
        token.token = "saved-access"
        token.token_secret = "saved-refresh"
        token.save()
        _assert_stored_encrypted(token.pk, "saved-access", "saved-refresh")

    def test_queryset_update_stores_ciphertext(self, commcare_token):
        token, _conn = commcare_token
        SocialToken.objects.filter(pk=token.pk).update(
            token="updated-access", token_secret="updated-refresh"
        )
        _assert_stored_encrypted(token.pk, "updated-access", "updated-refresh")

    def test_saving_a_legacy_row_encrypts_it(self, commcare_token):
        token, _conn = commcare_token
        _store_raw(token.pk, token="legacy-access", token_secret="legacy-refresh")
        loaded = SocialToken.objects.get(pk=token.pk)
        loaded.save()
        _assert_stored_encrypted(token.pk, "legacy-access", "legacy-refresh")

    def test_empty_refresh_secret_stays_empty(self, user):
        account = SocialAccount.objects.create(user=user, provider="commcare", uid="no-refresh")
        token = SocialToken.objects.create(account=account, token="only-access")
        stored_access, stored_refresh = _raw(token.pk)
        assert stored_access.startswith(CIPHERTEXT_PREFIX)
        assert stored_refresh == ""


@pytest.mark.django_db
class TestRefreshRotationStoresCiphertext:
    URL = "https://provider.example/o/token/"

    def test_applied_refresh_is_encrypted_and_readable(self, commcare_token, requests_mock):
        token, _conn = commcare_token
        requests_mock.post(
            self.URL,
            json={"access_token": "rot-access", "refresh_token": "rot-refresh", "expires_in": 900},
        )

        result = refresh_oauth_token_result_sync(token, self.URL)

        assert result.status == TokenRefreshStatus.APPLIED
        assert "plain-refresh" in requests_mock.last_request.text
        _assert_stored_encrypted(token.pk, "rot-access", "rot-refresh")
        stored = SocialToken.objects.get(pk=token.pk)
        assert (stored.token, stored.token_secret) == ("rot-access", "rot-refresh")

    def test_rotation_by_another_writer_supersedes_stale_refresh(
        self, commcare_token, requests_mock, monkeypatch
    ):
        token, _conn = commcare_token
        original = token_refresh._persist_refresh_response

        def rotate_then_persist(*args, **kwargs):
            SocialToken.objects.filter(pk=token.pk).update(
                token="winner-access", token_secret="winner-refresh"
            )
            return original(*args, **kwargs)

        monkeypatch.setattr(token_refresh, "_persist_refresh_response", rotate_then_persist)
        requests_mock.post(
            self.URL,
            json={"access_token": "loser-access", "refresh_token": "loser-refresh"},
        )

        result = refresh_oauth_token_result_sync(token, self.URL)

        assert result.status == TokenRefreshStatus.SUPERSEDED
        assert result.snapshot.access_token == "winner-access"
        _assert_stored_encrypted(token.pk, "winner-access", "winner-refresh")

    def test_refresh_of_legacy_plaintext_row_encrypts_it(self, commcare_token, requests_mock):
        token, _conn = commcare_token
        _store_raw(token.pk, token="plain-access", token_secret="plain-refresh")
        requests_mock.post(
            self.URL, json={"access_token": "new-access", "refresh_token": "new-refresh"}
        )

        result = refresh_oauth_token_result_sync(SocialToken.objects.get(pk=token.pk), self.URL)

        assert result.status == TokenRefreshStatus.APPLIED
        _assert_stored_encrypted(token.pk, "new-access", "new-refresh")
