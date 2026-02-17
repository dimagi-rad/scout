# OAuth Token Pass-Through Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enable OAuth token pass-through from Django to MCP server for CommCare data materialization.

**Architecture:** Django owns OAuth tokens (Fernet-encrypted in allauth's SocialToken table). On each chat request, tokens are retrieved, decrypted, and injected into MCP tool calls via the `_meta` field at the transport layer. The LLM never sees tokens. The MCP server uses them for upstream API calls and discards them.

**Tech Stack:** django-allauth (OAuth + token storage), cryptography.fernet (encryption), LangGraph config dict (non-checkpointed token passing), MCP `_meta` field (transport-layer injection)

**Design doc:** `docs/plans/2026-02-17-oauth-token-passthrough-design.md`

---

### Task 1: Enable allauth token storage

**Files:**
- Modify: `config/settings/base.py:178` (after `SOCIALACCOUNT_EMAIL_VERIFICATION`)

**Step 1: Write the test**

```python
# tests/test_oauth_tokens.py
from django.conf import settings


def test_socialaccount_store_tokens_enabled():
    """Verify allauth is configured to persist OAuth tokens."""
    assert settings.SOCIALACCOUNT_STORE_TOKENS is True
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_oauth_tokens.py::test_socialaccount_store_tokens_enabled -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'SOCIALACCOUNT_STORE_TOKENS'`

**Step 3: Add the setting**

In `config/settings/base.py`, after line 178 (`SOCIALACCOUNT_EMAIL_VERIFICATION = "none"`), add:

```python
# Store OAuth tokens so we can use them for data materialization
SOCIALACCOUNT_STORE_TOKENS = True
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_oauth_tokens.py::test_socialaccount_store_tokens_enabled -v`
Expected: PASS

**Step 5: Commit**

```bash
git add config/settings/base.py tests/test_oauth_tokens.py
git commit -m "feat: enable allauth SOCIALACCOUNT_STORE_TOKENS for OAuth token persistence"
```

---

### Task 2: Fernet token encryption adapter

**Files:**
- Create: `apps/users/adapters.py`
- Create: `tests/test_oauth_tokens.py` (add tests)
- Modify: `config/settings/base.py` (set SOCIALACCOUNT_ADAPTER)

The adapter encrypts `SocialToken.token` and `SocialToken.token_secret` before they're written to the database, and decrypts them when read. Uses the existing `DB_CREDENTIAL_KEY` Fernet key from settings (same key used by `DatabaseConnection` in `apps/projects/models.py:62-67`).

**Step 1: Write failing tests**

```python
# tests/test_oauth_tokens.py (add to existing file)
import pytest
from unittest.mock import patch, MagicMock
from cryptography.fernet import Fernet
from django.conf import settings


TEST_FERNET_KEY = Fernet.generate_key().decode()


class TestTokenEncryptionAdapter:
    """Test that the social account adapter encrypts/decrypts tokens."""

    @pytest.fixture
    def adapter(self):
        from apps.users.adapters import EncryptingSocialAccountAdapter
        return EncryptingSocialAccountAdapter()

    @patch.object(settings, "DB_CREDENTIAL_KEY", TEST_FERNET_KEY)
    def test_encrypt_decrypt_roundtrip(self, adapter):
        """Token should survive encrypt -> decrypt roundtrip."""
        original = "ya29.a0AfH6SMB_secret_token_value"
        encrypted = adapter.encrypt_token(original)
        assert encrypted != original
        assert adapter.decrypt_token(encrypted) == original

    @patch.object(settings, "DB_CREDENTIAL_KEY", TEST_FERNET_KEY)
    def test_encrypt_empty_string(self, adapter):
        """Empty string should return empty string without encryption."""
        assert adapter.encrypt_token("") == ""
        assert adapter.decrypt_token("") == ""

    @patch.object(settings, "DB_CREDENTIAL_KEY", TEST_FERNET_KEY)
    def test_encrypted_value_is_not_plaintext(self, adapter):
        """Encrypted output must not contain the original token."""
        original = "secret_token_12345"
        encrypted = adapter.encrypt_token(original)
        assert original not in encrypted

    @patch.object(settings, "DB_CREDENTIAL_KEY", "")
    def test_missing_key_raises(self, adapter):
        """Should raise ValueError when DB_CREDENTIAL_KEY is not set."""
        with pytest.raises(ValueError, match="DB_CREDENTIAL_KEY"):
            adapter.encrypt_token("some_token")
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_oauth_tokens.py::TestTokenEncryptionAdapter -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'apps.users.adapters'`

**Step 3: Create the adapter**

```python
# apps/users/adapters.py
"""
Custom allauth social account adapter with Fernet token encryption.

Encrypts OAuth access tokens and refresh tokens before they are stored
in the database. Uses the same DB_CREDENTIAL_KEY Fernet key used for
project database credentials.
"""

from __future__ import annotations

import logging

from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from cryptography.fernet import Fernet
from django.conf import settings

logger = logging.getLogger(__name__)


class EncryptingSocialAccountAdapter(DefaultSocialAccountAdapter):
    """Adapter that Fernet-encrypts SocialToken fields at rest."""

    def _get_fernet(self) -> Fernet:
        key = settings.DB_CREDENTIAL_KEY
        if not key:
            raise ValueError("DB_CREDENTIAL_KEY is not set in settings")
        return Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt_token(self, plaintext: str) -> str:
        """Encrypt a token string. Returns empty string for empty input."""
        if not plaintext:
            return ""
        f = self._get_fernet()
        return f.encrypt(plaintext.encode()).decode()

    def decrypt_token(self, ciphertext: str) -> str:
        """Decrypt a token string. Returns empty string for empty input."""
        if not ciphertext:
            return ""
        f = self._get_fernet()
        return f.decrypt(ciphertext.encode()).decode()

    def serialize_instance(self, instance):
        """Encrypt token fields before serialization (storage)."""
        from allauth.socialaccount.models import SocialToken

        data = super().serialize_instance(instance)
        if isinstance(instance, SocialToken):
            if data.get("token"):
                data["token"] = self.encrypt_token(data["token"])
            if data.get("token_secret"):
                data["token_secret"] = self.encrypt_token(data["token_secret"])
        return data

    def deserialize_instance(self, model, data):
        """Decrypt token fields after deserialization (retrieval)."""
        from allauth.socialaccount.models import SocialToken

        if model is SocialToken:
            data = dict(data)  # don't mutate the original
            if data.get("token"):
                data["token"] = self.decrypt_token(data["token"])
            if data.get("token_secret"):
                data["token_secret"] = self.decrypt_token(data["token_secret"])
        return super().deserialize_instance(model, data)
```

**Step 4: Register the adapter in settings**

In `config/settings/base.py`, after the `SOCIALACCOUNT_STORE_TOKENS` line, add:

```python
SOCIALACCOUNT_ADAPTER = "apps.users.adapters.EncryptingSocialAccountAdapter"
```

**Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_oauth_tokens.py::TestTokenEncryptionAdapter -v`
Expected: PASS (4 tests)

**Step 6: Run ruff**

Run: `uv run ruff check apps/users/adapters.py tests/test_oauth_tokens.py`
Expected: All checks passed

**Step 7: Commit**

```bash
git add apps/users/adapters.py config/settings/base.py tests/test_oauth_tokens.py
git commit -m "feat: add Fernet-encrypting social account adapter for OAuth tokens"
```

---

### Task 3: CommCare Connect OAuth provider

**Files:**
- Create: `apps/users/providers/commcare_connect/__init__.py`
- Create: `apps/users/providers/commcare_connect/apps.py`
- Create: `apps/users/providers/commcare_connect/provider.py`
- Create: `apps/users/providers/commcare_connect/views.py`
- Create: `apps/users/providers/commcare_connect/urls.py`
- Modify: `config/settings/base.py` (INSTALLED_APPS, SOCIALACCOUNT_PROVIDERS)

Mirrors the existing CommCare HQ provider at `apps/users/providers/commcare/`. OAuth endpoints are placeholders (TBD).

**Step 1: Write the test**

```python
# tests/test_oauth_tokens.py (add to existing file)

class TestCommCareConnectProvider:
    """Test the CommCare Connect OAuth provider is properly configured."""

    def test_provider_registered(self):
        """CommCare Connect provider should be discoverable by allauth."""
        from allauth.socialaccount import providers
        registry = providers.registry
        provider_cls = registry.get_class("commcare_connect")
        assert provider_cls is not None
        assert provider_cls.id == "commcare_connect"

    def test_provider_in_installed_apps(self):
        assert "apps.users.providers.commcare_connect" in settings.INSTALLED_APPS
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_oauth_tokens.py::TestCommCareConnectProvider -v`
Expected: FAIL

**Step 3: Create the provider files**

```python
# apps/users/providers/commcare_connect/__init__.py
"""
CommCare Connect OAuth2 provider for django-allauth.

CommCare Connect is a separate service from CommCare HQ with its own
OAuth application and endpoints.

Usage:
    1. Add 'apps.users.providers.commcare_connect' to INSTALLED_APPS
    2. Configure OAuth app credentials via Django admin (SocialApp model)
    3. The provider will be available at /accounts/commcare_connect/login/
"""

default_app_config = "apps.users.providers.commcare_connect.apps.CommCareConnectProviderConfig"
```

```python
# apps/users/providers/commcare_connect/apps.py
"""Django app configuration for the CommCare Connect OAuth provider."""

from django.apps import AppConfig


class CommCareConnectProviderConfig(AppConfig):
    name = "apps.users.providers.commcare_connect"
    verbose_name = "CommCare Connect OAuth Provider"
```

```python
# apps/users/providers/commcare_connect/provider.py
"""CommCare Connect OAuth2 provider for django-allauth."""

from allauth.socialaccount.providers.base import ProviderAccount
from allauth.socialaccount.providers.oauth2.provider import OAuth2Provider


class CommCareConnectAccount(ProviderAccount):

    def get_avatar_url(self) -> str | None:
        return None

    def to_str(self) -> str:
        return self.account.extra_data.get("username", super().to_str())


class CommCareConnectProvider(OAuth2Provider):
    """
    OAuth2 provider for CommCare Connect.

    To add this provider:
    1. Add 'apps.users.providers.commcare_connect' to INSTALLED_APPS
    2. Create a SocialApp via Django admin with:
       - Provider: commcare_connect
       - Client ID: Your CommCare Connect OAuth client ID
       - Secret Key: Your CommCare Connect OAuth client secret
    """

    id = "commcare_connect"
    name = "CommCare Connect"
    account_class = CommCareConnectAccount

    def get_default_scope(self) -> list[str]:
        return ["read"]

    def extract_uid(self, data: dict) -> str:
        return str(data["id"])

    def extract_common_fields(self, data: dict) -> dict:
        return {
            "email": data.get("email"),
            "username": data.get("username"),
            "first_name": data.get("first_name", ""),
            "last_name": data.get("last_name", ""),
        }


provider_classes = [CommCareConnectProvider]
```

```python
# apps/users/providers/commcare_connect/views.py
"""CommCare Connect OAuth2 adapter and views for django-allauth."""

import requests
from allauth.socialaccount.providers.oauth2.views import (
    OAuth2Adapter,
    OAuth2CallbackView,
    OAuth2LoginView,
)

from .provider import CommCareConnectProvider


class CommCareConnectOAuth2Adapter(OAuth2Adapter):
    """
    OAuth2 adapter for CommCare Connect.

    Endpoint URLs are placeholders — update when Connect's OAuth URLs
    are confirmed.
    """

    provider_id = CommCareConnectProvider.id

    # Placeholder endpoints — replace with actual Connect OAuth URLs
    access_token_url = "https://connect.commcarehq.org/oauth/token/"
    authorize_url = "https://connect.commcarehq.org/oauth/authorize/"
    profile_url = "https://connect.commcarehq.org/api/v0.5/identity/"

    def complete_login(self, request, app, token, **kwargs):
        response = requests.get(
            self.profile_url,
            headers={"Authorization": f"Bearer {token.token}"},
            timeout=30,
        )
        response.raise_for_status()
        extra_data = response.json()
        return self.get_provider().sociallogin_from_response(request, extra_data)


oauth2_login = OAuth2LoginView.adapter_view(CommCareConnectOAuth2Adapter)
oauth2_callback = OAuth2CallbackView.adapter_view(CommCareConnectOAuth2Adapter)
```

```python
# apps/users/providers/commcare_connect/urls.py
"""URL configuration for CommCare Connect OAuth provider."""

from allauth.socialaccount.providers.oauth2.urls import default_urlpatterns

from .provider import CommCareConnectProvider

urlpatterns = default_urlpatterns(CommCareConnectProvider)
```

**Step 4: Register in settings**

In `config/settings/base.py`, add to `INSTALLED_APPS` (after the existing commcare line):

```python
"apps.users.providers.commcare_connect",
```

Add to `SOCIALACCOUNT_PROVIDERS`:

```python
"commcare_connect": {
    "SCOPE": ["read"],
},
```

**Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_oauth_tokens.py::TestCommCareConnectProvider -v`
Expected: PASS

**Step 6: Run ruff**

Run: `uv run ruff check apps/users/providers/commcare_connect/`
Expected: All checks passed

**Step 7: Commit**

```bash
git add apps/users/providers/commcare_connect/ config/settings/base.py tests/test_oauth_tokens.py
git commit -m "feat: add CommCare Connect OAuth provider (placeholder endpoints)"
```

---

### Task 4: Token retrieval helper

**Files:**
- Modify: `apps/agents/mcp_client.py` (add `get_user_oauth_tokens`)
- Modify: `tests/test_oauth_tokens.py` (add tests)

This helper queries allauth's SocialToken for a user's CommCare tokens. It uses the adapter's decrypt method to return plaintext tokens. Async-safe via `sync_to_async`.

**Step 1: Write failing tests**

```python
# tests/test_oauth_tokens.py (add to existing file)
from unittest.mock import AsyncMock, patch, MagicMock
from asgiref.sync import async_to_sync


class TestGetUserOAuthTokens:
    """Test the get_user_oauth_tokens helper in mcp_client."""

    def _make_social_token(self, provider, token, token_secret="refresh_tok", expires_at=None):
        """Build a mock SocialToken."""
        st = MagicMock()
        st.account.provider = provider
        st.token = token
        st.token_secret = token_secret
        st.expires_at = expires_at
        return st

    @patch("apps.agents.mcp_client.SocialToken")
    def test_returns_tokens_for_connected_providers(self, mock_social_token_cls):
        from apps.agents.mcp_client import get_user_oauth_tokens

        user = MagicMock()
        user.pk = 1

        mock_qs = MagicMock()
        mock_social_token_cls.objects.filter.return_value = mock_qs
        mock_qs.select_related.return_value = [
            self._make_social_token("commcare", "hq_token_123"),
            self._make_social_token("commcare_connect", "connect_token_456"),
        ]

        result = async_to_sync(get_user_oauth_tokens)(user)
        assert result == {
            "commcare": "hq_token_123",
            "commcare_connect": "connect_token_456",
        }

    @patch("apps.agents.mcp_client.SocialToken")
    def test_returns_empty_dict_for_no_tokens(self, mock_social_token_cls):
        from apps.agents.mcp_client import get_user_oauth_tokens

        user = MagicMock()
        user.pk = 1

        mock_qs = MagicMock()
        mock_social_token_cls.objects.filter.return_value = mock_qs
        mock_qs.select_related.return_value = []

        result = async_to_sync(get_user_oauth_tokens)(user)
        assert result == {}

    @patch("apps.agents.mcp_client.SocialToken")
    def test_skips_non_commcare_providers(self, mock_social_token_cls):
        from apps.agents.mcp_client import get_user_oauth_tokens

        user = MagicMock()
        user.pk = 1

        mock_qs = MagicMock()
        mock_social_token_cls.objects.filter.return_value = mock_qs
        mock_qs.select_related.return_value = [
            self._make_social_token("google", "google_token"),
            self._make_social_token("commcare", "hq_token"),
        ]

        result = async_to_sync(get_user_oauth_tokens)(user)
        assert result == {"commcare": "hq_token"}

    def test_returns_empty_dict_for_none_user(self):
        from apps.agents.mcp_client import get_user_oauth_tokens

        result = async_to_sync(get_user_oauth_tokens)(None)
        assert result == {}
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_oauth_tokens.py::TestGetUserOAuthTokens -v`
Expected: FAIL — `ImportError: cannot import name 'get_user_oauth_tokens'`

**Step 3: Implement the helper**

Add to the end of `apps/agents/mcp_client.py`:

```python
# --- OAuth token retrieval ---

COMMCARE_PROVIDERS = frozenset({"commcare", "commcare_connect"})


async def get_user_oauth_tokens(user) -> dict[str, str]:
    """Retrieve decrypted OAuth tokens for a user's CommCare providers.

    Returns a dict mapping provider ID to access token string.
    Only includes CommCare HQ and CommCare Connect tokens.
    Returns empty dict if user is None or has no connected accounts.
    """
    if user is None or not getattr(user, "pk", None):
        return {}

    tokens = await sync_to_async(_get_tokens_sync)(user)
    return tokens


def _get_tokens_sync(user) -> dict[str, str]:
    """Synchronous token retrieval — called via sync_to_async."""
    from allauth.socialaccount.models import SocialToken

    social_tokens = SocialToken.objects.filter(
        account__user=user,
        account__provider__in=COMMCARE_PROVIDERS,
    ).select_related("account")

    return {st.account.provider: st.token for st in social_tokens}
```

Also add the import at the top of `apps/agents/mcp_client.py`:

```python
from asgiref.sync import sync_to_async
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_oauth_tokens.py::TestGetUserOAuthTokens -v`
Expected: PASS (4 tests)

**Step 5: Run ruff**

Run: `uv run ruff check apps/agents/mcp_client.py tests/test_oauth_tokens.py`
Expected: All checks passed

**Step 6: Commit**

```bash
git add apps/agents/mcp_client.py tests/test_oauth_tokens.py
git commit -m "feat: add get_user_oauth_tokens helper for retrieving CommCare OAuth tokens"
```

---

### Task 5: MCP server auth extraction and envelope changes

**Files:**
- Create: `mcp_server/auth.py`
- Modify: `mcp_server/envelope.py` (add `AUTH_TOKEN_EXPIRED`, add log scrubbing)
- Modify: `tests/test_mcp_server.py` (add tests)

**Step 1: Write failing tests**

```python
# tests/test_mcp_server.py (add to existing test file)

class TestAuthTokenExtraction:
    """Test MCP auth token extraction from _meta field."""

    def test_extract_tokens_from_meta(self):
        from mcp_server.auth import extract_oauth_tokens
        meta = {"oauth_tokens": {"commcare": "tok_abc", "commcare_connect": "tok_xyz"}}
        assert extract_oauth_tokens(meta) == {"commcare": "tok_abc", "commcare_connect": "tok_xyz"}

    def test_extract_tokens_missing_meta(self):
        from mcp_server.auth import extract_oauth_tokens
        assert extract_oauth_tokens({}) == {}

    def test_extract_tokens_none_meta(self):
        from mcp_server.auth import extract_oauth_tokens
        assert extract_oauth_tokens(None) == {}


class TestAuditLogScrubbing:
    """Test that oauth_tokens are scrubbed from audit log extra_fields."""

    def test_scrub_removes_oauth_tokens(self):
        from mcp_server.envelope import scrub_extra_fields
        extra = {"sql": "SELECT 1", "oauth_tokens": {"commcare": "secret"}}
        scrubbed = scrub_extra_fields(extra)
        assert "oauth_tokens" not in scrubbed
        assert scrubbed["sql"] == "SELECT 1"

    def test_scrub_noop_when_no_tokens(self):
        from mcp_server.envelope import scrub_extra_fields
        extra = {"sql": "SELECT 1"}
        assert scrub_extra_fields(extra) == {"sql": "SELECT 1"}


class TestAuthTokenExpiredCode:
    """Test AUTH_TOKEN_EXPIRED error code exists."""

    def test_code_defined(self):
        from mcp_server.envelope import AUTH_TOKEN_EXPIRED
        assert AUTH_TOKEN_EXPIRED == "AUTH_TOKEN_EXPIRED"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_server.py::TestAuthTokenExtraction tests/test_mcp_server.py::TestAuditLogScrubbing tests/test_mcp_server.py::TestAuthTokenExpiredCode -v`
Expected: FAIL

**Step 3: Create `mcp_server/auth.py`**

```python
# mcp_server/auth.py
"""Authentication helpers for the MCP server.

Extracts OAuth tokens from MCP request metadata. Tokens are injected
by the Django chat view at the transport layer and are never visible
to the LLM.
"""

from __future__ import annotations

from typing import Any


def extract_oauth_tokens(meta: dict[str, Any] | None) -> dict[str, str]:
    """Extract OAuth tokens from MCP request _meta field.

    Args:
        meta: The _meta dict from an MCP tool call. May be None.

    Returns:
        Dict mapping provider ID to access token string.
        Empty dict if no tokens present.
    """
    if not meta:
        return {}
    return meta.get("oauth_tokens", {})
```

**Step 4: Add error code and scrub helper to `mcp_server/envelope.py`**

Add `AUTH_TOKEN_EXPIRED` to the error codes block (after `INTERNAL_ERROR`):

```python
AUTH_TOKEN_EXPIRED = "AUTH_TOKEN_EXPIRED"
```

Add the `scrub_extra_fields` function (before the `tool_context` context manager):

```python
# Fields that must never appear in audit logs
_SCRUB_KEYS = frozenset({"oauth_tokens"})


def scrub_extra_fields(extra: dict[str, Any]) -> dict[str, Any]:
    """Remove sensitive fields from audit log extra_fields."""
    return {k: v for k, v in extra.items() if k not in _SCRUB_KEYS}
```

Update the `tool_context` audit log line to scrub extra_fields. Change:

```python
" ".join(f"{k}={v!r}" for k, v in extra_fields.items()) if extra_fields else "",
```

to:

```python
" ".join(f"{k}={v!r}" for k, v in scrub_extra_fields(extra_fields).items()) if extra_fields else "",
```

**Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_server.py::TestAuthTokenExtraction tests/test_mcp_server.py::TestAuditLogScrubbing tests/test_mcp_server.py::TestAuthTokenExpiredCode -v`
Expected: PASS (6 tests)

**Step 6: Run all MCP server tests to check for regressions**

Run: `uv run pytest tests/test_mcp_server.py -v`
Expected: All existing tests still pass

**Step 7: Run ruff**

Run: `uv run ruff check mcp_server/auth.py mcp_server/envelope.py`
Expected: All checks passed

**Step 8: Commit**

```bash
git add mcp_server/auth.py mcp_server/envelope.py tests/test_mcp_server.py
git commit -m "feat: add MCP auth token extraction, AUTH_TOKEN_EXPIRED code, audit log scrubbing"
```

---

### Task 6: Wire tokens through chat view and graph builder

**Files:**
- Modify: `apps/chat/views.py:349-395` (retrieve tokens, pass to graph, inject into config)
- Modify: `apps/agents/graph/base.py:58-63` (accept `oauth_tokens` param)
- Modify: `tests/test_oauth_tokens.py` (add integration-style tests)

**Step 1: Write failing tests**

```python
# tests/test_oauth_tokens.py (add to existing file)
from apps.agents.graph.base import build_agent_graph


class TestGraphOAuthConfig:
    """Test that build_agent_graph accepts and ignores oauth_tokens gracefully."""

    @patch("apps.agents.graph.base.ChatAnthropic")
    @patch("apps.agents.graph.base.KnowledgeRetriever")
    @patch("apps.agents.graph.base.DataDictionaryGenerator")
    def test_build_graph_accepts_oauth_tokens(self, mock_dd, mock_kr, mock_llm):
        """build_agent_graph should accept oauth_tokens without error."""
        mock_kr_instance = MagicMock()
        mock_kr_instance.retrieve.return_value = ""
        mock_kr.return_value = mock_kr_instance

        mock_dd_instance = MagicMock()
        mock_dd_instance.render_for_prompt.return_value = "schema info"
        mock_dd.return_value = mock_dd_instance

        mock_llm_instance = MagicMock()
        mock_llm_instance.bind_tools.return_value = mock_llm_instance
        mock_llm.return_value = mock_llm_instance

        project = MagicMock()
        project.slug = "test"
        project.id = "test-id"
        project.llm_model = "claude-sonnet-4-5-20250929"
        project.system_prompt = ""
        project.max_rows_per_query = 500
        project.max_query_timeout_seconds = 30
        project.db_schema = "public"

        # Should not raise
        graph = build_agent_graph(
            project=project,
            oauth_tokens={"commcare": "test_token"},
        )
        assert graph is not None
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_oauth_tokens.py::TestGraphOAuthConfig -v`
Expected: FAIL — `TypeError: build_agent_graph() got an unexpected keyword argument 'oauth_tokens'`

**Step 3: Add `oauth_tokens` parameter to `build_agent_graph`**

In `apps/agents/graph/base.py`, change the function signature (lines 58-63) from:

```python
def build_agent_graph(
    project: "Project",
    user: "User | None" = None,
    checkpointer: "BaseCheckpointSaver | None" = None,
    mcp_tools: list | None = None,
):
```

to:

```python
def build_agent_graph(
    project: "Project",
    user: "User | None" = None,
    checkpointer: "BaseCheckpointSaver | None" = None,
    mcp_tools: list | None = None,
    oauth_tokens: dict | None = None,
):
```

The `oauth_tokens` parameter is accepted but not used directly in the graph builder — it's passed via the LangGraph `config` dict at invocation time in the chat view. The graph builder just stores it for use by the caller. For now, this is a no-op parameter that proves the interface is ready.

**Step 4: Update chat view to retrieve and pass tokens**

In `apps/chat/views.py`, add import at top:

```python
from apps.agents.mcp_client import get_mcp_tools, get_user_oauth_tokens
```

After the MCP tools loading block (~line 355) and before the agent build, add:

```python
    # Retrieve user's OAuth tokens for materialization
    oauth_tokens = await get_user_oauth_tokens(user)
```

Update both `build_agent_graph` calls (~lines 360, 371) to include:

```python
            oauth_tokens=oauth_tokens,
```

Update the config dict (~line 395) to include tokens:

```python
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": 50,
        "oauth_tokens": oauth_tokens,
    }
```

The `oauth_tokens` key in `config` is not a LangGraph built-in — it's custom data that will be available to tool wrappers at invocation time without being checkpointed.

**Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_oauth_tokens.py::TestGraphOAuthConfig -v`
Expected: PASS

**Step 6: Run ruff**

Run: `uv run ruff check apps/chat/views.py apps/agents/graph/base.py`
Expected: All checks passed

**Step 7: Commit**

```bash
git add apps/chat/views.py apps/agents/graph/base.py tests/test_oauth_tokens.py
git commit -m "feat: wire OAuth tokens through chat view into graph config"
```

---

### Task 7: Token refresh service

**Files:**
- Create: `apps/users/services/__init__.py`
- Create: `apps/users/services/token_refresh.py`
- Modify: `tests/test_oauth_tokens.py` (add tests)

**Step 1: Write failing tests**

```python
# tests/test_oauth_tokens.py (add to existing file)
from datetime import timedelta
from django.utils import timezone


class TestTokenRefresh:
    """Test the OAuth token refresh service."""

    @patch("apps.users.services.token_refresh.requests.post")
    def test_refresh_updates_token(self, mock_post):
        from apps.users.services.token_refresh import refresh_oauth_token

        mock_post.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={
                "access_token": "new_access_token",
                "refresh_token": "new_refresh_token",
                "expires_in": 3600,
            }),
        )
        mock_post.return_value.raise_for_status = MagicMock()

        social_token = MagicMock()
        social_token.token = "old_access_token"
        social_token.token_secret = "old_refresh_token"
        social_token.app.client_id = "client_123"
        social_token.app.secret = "secret_456"

        # CommCare HQ token URL
        token_url = "https://www.commcarehq.org/oauth/token/"

        result = refresh_oauth_token(social_token, token_url)

        assert result == "new_access_token"
        assert social_token.token == "new_access_token"
        assert social_token.token_secret == "new_refresh_token"
        social_token.save.assert_called_once()

    @patch("apps.users.services.token_refresh.requests.post")
    def test_refresh_failure_raises(self, mock_post):
        from apps.users.services.token_refresh import (
            TokenRefreshError,
            refresh_oauth_token,
        )

        mock_post.return_value = MagicMock(status_code=400)
        mock_post.return_value.raise_for_status.side_effect = Exception("Bad Request")

        social_token = MagicMock()
        social_token.token_secret = "old_refresh_token"
        social_token.app.client_id = "client_123"
        social_token.app.secret = "secret_456"

        with pytest.raises(TokenRefreshError):
            refresh_oauth_token(social_token, "https://example.com/oauth/token/")

    def test_token_needs_refresh_when_expiring_soon(self):
        from apps.users.services.token_refresh import token_needs_refresh

        soon = timezone.now() + timedelta(minutes=3)
        assert token_needs_refresh(soon) is True

    def test_token_does_not_need_refresh_when_fresh(self):
        from apps.users.services.token_refresh import token_needs_refresh

        later = timezone.now() + timedelta(hours=1)
        assert token_needs_refresh(later) is False

    def test_token_needs_refresh_when_expired(self):
        from apps.users.services.token_refresh import token_needs_refresh

        past = timezone.now() - timedelta(hours=1)
        assert token_needs_refresh(past) is True

    def test_token_needs_refresh_when_none(self):
        from apps.users.services.token_refresh import token_needs_refresh

        assert token_needs_refresh(None) is False
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_oauth_tokens.py::TestTokenRefresh -v`
Expected: FAIL

**Step 3: Create the service**

```python
# apps/users/services/__init__.py
```

```python
# apps/users/services/token_refresh.py
"""OAuth token refresh service.

Handles refreshing expired OAuth tokens for CommCare providers.
Called proactively (before token expires) and reactively (after 401).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import requests
from django.utils import timezone

logger = logging.getLogger(__name__)

# Refresh tokens that expire within this window
REFRESH_BUFFER = timedelta(minutes=5)


class TokenRefreshError(Exception):
    """Raised when token refresh fails."""


def token_needs_refresh(expires_at: datetime | None) -> bool:
    """Check if a token needs refreshing based on its expiry time.

    Returns True if the token expires within REFRESH_BUFFER.
    Returns False if expires_at is None (unknown expiry — assume valid).
    """
    if expires_at is None:
        return False
    return timezone.now() + REFRESH_BUFFER >= expires_at


def refresh_oauth_token(social_token, token_url: str) -> str:
    """Refresh an OAuth token using the refresh token grant.

    Args:
        social_token: allauth SocialToken instance with token_secret (refresh token)
            and app (SocialApp with client_id and secret).
        token_url: The provider's token endpoint URL.

    Returns:
        The new access token string.

    Raises:
        TokenRefreshError: If the refresh request fails.
    """
    try:
        response = requests.post(
            token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": social_token.token_secret,
                "client_id": social_token.app.client_id,
                "client_secret": social_token.app.secret,
            },
            timeout=30,
        )
        response.raise_for_status()
    except Exception as e:
        logger.error("Token refresh failed for app %s: %s", social_token.app.client_id, e)
        raise TokenRefreshError(f"Failed to refresh OAuth token: {e}") from e

    data = response.json()
    social_token.token = data["access_token"]
    if data.get("refresh_token"):
        social_token.token_secret = data["refresh_token"]
    if data.get("expires_in"):
        social_token.expires_at = timezone.now() + timedelta(seconds=data["expires_in"])
    social_token.save()

    logger.info("Successfully refreshed OAuth token for app %s", social_token.app.client_id)
    return social_token.token
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_oauth_tokens.py::TestTokenRefresh -v`
Expected: PASS (6 tests)

**Step 5: Run ruff**

Run: `uv run ruff check apps/users/services/`
Expected: All checks passed

**Step 6: Commit**

```bash
git add apps/users/services/ tests/test_oauth_tokens.py
git commit -m "feat: add OAuth token refresh service with proactive expiry check"
```

---

### Task 8: Full test suite verification

**Files:** None (verification only)

**Step 1: Run all OAuth tests**

Run: `uv run pytest tests/test_oauth_tokens.py -v`
Expected: All tests pass

**Step 2: Run all MCP tests**

Run: `uv run pytest tests/test_mcp_server.py tests/test_mcp_client.py tests/test_sql_validator.py -v`
Expected: All tests pass (no regressions from envelope.py changes)

**Step 3: Run ruff on all changed files**

Run: `uv run ruff check apps/users/adapters.py apps/users/services/ apps/users/providers/commcare_connect/ apps/agents/mcp_client.py apps/agents/graph/base.py apps/chat/views.py mcp_server/auth.py mcp_server/envelope.py tests/test_oauth_tokens.py`
Expected: All checks passed

**Step 4: Commit (if any ruff fixes needed)**

```bash
git add -A && git commit -m "fix: lint fixes for OAuth token pass-through"
```
