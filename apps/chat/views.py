"""
Chat views: streaming chat endpoint and auth API.

The chat endpoint is a raw async Django view (not DRF) because DRF
does not support async streaming responses.  Auth endpoints are also
plain Django views to keep this app self-contained.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid

from allauth.socialaccount.models import SocialAccount, SocialApp
from asgiref.sync import sync_to_async
from django.contrib.auth import authenticate, login, logout
from django.contrib.sites.models import Site
from django.core.cache import cache
from django.http import JsonResponse, StreamingHttpResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from apps.agents.graph.base import build_agent_graph
from apps.agents.mcp_client import get_mcp_tools, get_user_oauth_tokens
from apps.agents.memory.checkpointer import get_database_url
from apps.chat.models import Thread
from apps.chat.stream import langgraph_to_ui_stream
from apps.projects.models import Project, ProjectMembership

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Checkpointer singleton (lazy initialization)
# ---------------------------------------------------------------------------
_checkpointer = None
_pool = None


async def _ensure_checkpointer(*, force_new: bool = False):
    global _checkpointer, _pool
    if _checkpointer is not None and not force_new:
        return _checkpointer

    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg_pool import AsyncConnectionPool

        database_url = get_database_url()

        # Close old pool if forcing a new one
        if _pool is not None:
            await _pool.close()

        # Use a connection pool instead of a single connection so the
        # checkpointer survives across requests.
        _pool = AsyncConnectionPool(
            conninfo=database_url,
            max_size=20,
            open=False,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
            },
        )
        await _pool.open(wait=True, timeout=10)

        _checkpointer = AsyncPostgresSaver(_pool)
        await _checkpointer.setup()
        logger.info("PostgreSQL checkpointer initialized")
    except Exception as e:
        from langgraph.checkpoint.memory import MemorySaver

        logger.warning("PostgreSQL checkpointer unavailable, using MemorySaver: %s", e)
        _checkpointer = MemorySaver()

    return _checkpointer


# ---------------------------------------------------------------------------
# Rate limiting helpers
# ---------------------------------------------------------------------------
AUTH_MAX_ATTEMPTS = 5
AUTH_LOCKOUT_SECONDS = 300


def _check_rate_limit(username: str) -> bool:
    """Return True if rate-limited (should block)."""
    return cache.get(f"auth_attempts:{username}", 0) >= AUTH_MAX_ATTEMPTS


def _record_attempt(username: str, success: bool) -> None:
    key = f"auth_attempts:{username}"
    if success:
        cache.delete(key)
    else:
        cache.set(key, cache.get(key, 0) + 1, AUTH_LOCKOUT_SECONDS)


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

@ensure_csrf_cookie
@require_GET
def csrf_view(request):
    """Return CSRF cookie so the SPA can read it."""
    return JsonResponse({"csrfToken": get_token(request)})


@require_GET
def me_view(request):
    """Return current user info or 401."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Not authenticated"}, status=401)
    user = request.user
    return JsonResponse({
        "id": str(user.id),
        "email": user.email,
        "name": user.get_full_name(),
        "is_staff": user.is_staff,
    })


@require_POST
def login_view(request):
    """Email/password login."""
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    email = body.get("email", "").strip()
    password = body.get("password", "")

    if not email or not password:
        return JsonResponse({"error": "Email and password are required"}, status=400)

    if _check_rate_limit(email):
        return JsonResponse({"error": "Too many attempts. Try again later."}, status=429)

    user = authenticate(request, username=email, password=password)
    if user is None or not user.is_active:
        _record_attempt(email, False)
        return JsonResponse({"error": "Invalid credentials"}, status=401)

    _record_attempt(email, True)
    login(request, user)

    return JsonResponse({
        "id": str(user.id),
        "email": user.email,
        "name": user.get_full_name(),
        "is_staff": user.is_staff,
    })


@require_POST
def logout_view(request):
    """Logout and clear session."""
    logout(request)
    return JsonResponse({"ok": True})


PROVIDER_DISPLAY = {
    "google": "Google",
    "github": "GitHub",
    "commcare": "CommCare",
    "commcare_connect": "CommCare Connect",
}


@require_POST
def disconnect_provider_view(request, provider_id):
    """Disconnect a social account. Prevents removing the last login method."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Not authenticated"}, status=401)

    from django.db import transaction

    with transaction.atomic():
        # SocialAccount.provider may store provider_id (e.g. "commcare_prod")
        # rather than the provider class id (e.g. "commcare"), so check both.
        account = SocialAccount.objects.select_for_update().filter(
            user=request.user, provider=provider_id
        ).first()
        if not account:
            # Try matching via SocialApp.provider_id
            app_provider_ids = list(
                SocialApp.objects.filter(provider=provider_id).values_list(
                    "provider_id", flat=True
                )
            )
            if app_provider_ids:
                account = SocialAccount.objects.select_for_update().filter(
                    user=request.user, provider__in=app_provider_ids
                ).first()
        if not account:
            return JsonResponse({"error": "Not connected"}, status=404)

        # Guard: must keep at least one login method
        has_password = request.user.has_usable_password()
        other_socials = (
            SocialAccount.objects.filter(user=request.user)
            .exclude(provider=provider_id)
            .exists()
        )
        if not has_password and not other_socials:
            return JsonResponse(
                {"error": "Cannot disconnect your only login method. Set a password first."},
                status=400,
            )

        account.delete()
    return JsonResponse({"status": "disconnected"})


@require_GET
def providers_view(request):
    """Return OAuth providers configured for this site, with connection status if authenticated."""
    current_site = Site.objects.get_current()
    apps = SocialApp.objects.filter(sites=current_site).order_by("provider")

    connected_providers = set()
    if request.user.is_authenticated:
        connected_providers = set(
            SocialAccount.objects.filter(user=request.user).values_list(
                "provider", flat=True
            )
        )

    providers = []
    for app in apps:
        entry = {
            "id": app.provider,
            "name": PROVIDER_DISPLAY.get(app.provider, app.name),
            "login_url": f"/accounts/{app.provider}/login/",
        }
        if request.user.is_authenticated:
            # SocialAccount.provider stores the provider_id (e.g. "commcare_prod"),
            # not the provider class id (e.g. "commcare"), so check both.
            entry["connected"] = (
                app.provider in connected_providers
                or app.provider_id in connected_providers
            )
        providers.append(entry)

    return JsonResponse({"providers": providers})


# ---------------------------------------------------------------------------
# Helpers to safely access request.user from an async context
# ---------------------------------------------------------------------------

@sync_to_async
def _get_user_if_authenticated(request):
    """Access request.user (triggers sync session load) from async context."""
    if request.user.is_authenticated:
        return request.user
    return None


@sync_to_async
def _get_membership(user, project_id):
    """Load project membership in a sync context."""
    try:
        return ProjectMembership.objects.select_related("project").get(
            user=user, project_id=project_id
        )
    except ProjectMembership.DoesNotExist:
        return None


@sync_to_async
def _upsert_thread(thread_id, project_id, user, title, *, tenant_membership=None):
    """Create or update a Thread record.

    auto_now on updated_at handles the timestamp on every save, so we only
    need to pass project/user in defaults and title in create_defaults.
    """
    defaults = {"user": user}
    create_defaults = {"user": user, "title": title[:200]}
    if project_id:
        defaults["project_id"] = project_id
        create_defaults["project_id"] = project_id
    if tenant_membership:
        defaults["tenant_membership"] = tenant_membership
        create_defaults["tenant_membership"] = tenant_membership
    Thread.objects.update_or_create(
        id=thread_id,
        defaults=defaults,
        create_defaults=create_defaults,
    )


@sync_to_async
def _get_thread(thread_id, user):
    """Load a thread ensuring ownership."""
    try:
        return Thread.objects.get(id=thread_id, user=user)
    except Thread.DoesNotExist:
        return None


@sync_to_async
def _get_public_thread(share_token):
    """Load a public thread by share token."""
    try:
        return Thread.objects.select_related("project", "user").get(
            share_token=share_token, is_public=True
        )
    except Thread.DoesNotExist:
        return None


@sync_to_async
def _update_thread_sharing(thread, is_shared=None, is_public=None):
    """Update sharing settings on a thread."""
    if is_shared is not None:
        thread.is_shared = is_shared
    if is_public is not None:
        thread.is_public = is_public
    thread.save()
    return {
        "id": str(thread.id),
        "is_shared": thread.is_shared,
        "is_public": thread.is_public,
        "share_token": thread.share_token,
    }


@sync_to_async
def _get_thread_artifacts(thread_id):
    """Load artifacts associated with a thread."""
    from apps.artifacts.models import Artifact

    qs = Artifact.objects.filter(conversation_id=str(thread_id)).order_by("created_at")
    return [
        {
            "id": str(a.id),
            "title": a.title,
            "artifact_type": a.artifact_type,
            "code": a.code,
            "data": a.data,
            "version": a.version,
        }
        for a in qs
    ]


@sync_to_async
def _list_threads(user, *, project_id=None, tenant_membership_id=None):
    """Return recent threads for a project or tenant/user."""
    qs = Thread.objects.filter(user=user)
    if project_id:
        qs = qs.filter(project_id=project_id)
    elif tenant_membership_id:
        qs = qs.filter(tenant_membership_id=tenant_membership_id)
    qs = qs.order_by("-updated_at")[:50]
    return [
        {
            "id": str(t.id),
            "title": t.title,
            "created_at": t.created_at.isoformat(),
            "updated_at": t.updated_at.isoformat(),
            "is_shared": t.is_shared,
            "is_public": t.is_public,
            "share_token": t.share_token,
        }
        for t in qs
    ]


# ---------------------------------------------------------------------------
# Streaming chat endpoint
# ---------------------------------------------------------------------------

MAX_MESSAGE_LENGTH = 10_000


async def chat_view(request):
    """
    POST /api/chat/

    Accepts Vercel AI SDK useChat request format, returns a
    StreamingHttpResponse in the Data Stream Protocol.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    # Access request.user via sync_to_async to avoid SynchronousOnlyOperation
    user = await _get_user_if_authenticated(request)
    if user is None:
        return JsonResponse({"error": "Authentication required"}, status=401)

    # Parse body
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    messages = body.get("messages", [])
    data = body.get("data", {})
    tenant_id = data.get("tenantId") or body.get("tenantId")
    thread_id = data.get("threadId") or body.get("threadId") or str(uuid.uuid4())

    if not messages:
        return JsonResponse({"error": "messages is required"}, status=400)
    if not tenant_id:
        return JsonResponse({"error": "tenantId is required"}, status=400)

    # Get the last user message.
    # AI SDK v6 sends {parts: [{type:"text", text:"..."}]} instead of {content: "..."}.
    last_msg = messages[-1]
    user_content = last_msg.get("content", "")
    if not user_content:
        parts = last_msg.get("parts", [])
        user_content = " ".join(
            p.get("text", "") for p in parts if p.get("type") == "text"
        )
    if not user_content or not user_content.strip():
        return JsonResponse({"error": "Empty message"}, status=400)
    if len(user_content) > MAX_MESSAGE_LENGTH:
        return JsonResponse({"error": f"Message exceeds {MAX_MESSAGE_LENGTH} characters"}, status=400)

    # Validate tenant membership
    from apps.users.models import TenantMembership

    try:
        tenant_membership = await TenantMembership.objects.aget(
            id=tenant_id, user=user
        )
    except TenantMembership.DoesNotExist:
        return JsonResponse({"error": "Tenant not found or access denied"}, status=403)

    # Record thread metadata (fire-and-forget on error)
    try:
        await _upsert_thread(
            thread_id, None, user, user_content,
            tenant_membership=tenant_membership,
        )
    except Exception:
        logger.warning("Failed to upsert thread %s", thread_id, exc_info=True)

    # Load MCP tools (data access via MCP server)
    try:
        mcp_tools = await get_mcp_tools()
    except Exception as e:
        error_ref = hashlib.sha256(f"{time.time()}{e}".encode()).hexdigest()[:8]
        logger.exception("Failed to load MCP tools [ref=%s]", error_ref)
        return JsonResponse({"error": f"Agent initialization failed. Ref: {error_ref}"}, status=500)

    # Retrieve user's OAuth tokens for materialization
    oauth_tokens = await get_user_oauth_tokens(user)

    # Build agent (retry once with fresh checkpointer on connection errors)
    try:
        checkpointer = await _ensure_checkpointer()
        agent = await sync_to_async(build_agent_graph)(
            tenant_membership=tenant_membership,
            user=user,
            checkpointer=checkpointer,
            mcp_tools=mcp_tools,
            oauth_tokens=oauth_tokens,
        )
    except Exception:
        # Connection may have gone stale -- force a new checkpointer and retry
        try:
            logger.info("Retrying agent build with fresh checkpointer")
            checkpointer = await _ensure_checkpointer(force_new=True)
            agent = await sync_to_async(build_agent_graph)(
                tenant_membership=tenant_membership,
                user=user,
                checkpointer=checkpointer,
                mcp_tools=mcp_tools,
                oauth_tokens=oauth_tokens,
            )
        except Exception as e:
            error_ref = hashlib.sha256(f"{time.time()}{e}".encode()).hexdigest()[:8]
            logger.exception("Failed to build agent [ref=%s]", error_ref)
            return JsonResponse({"error": f"Agent initialization failed. Ref: {error_ref}"}, status=500)

    # Build LangGraph input state
    from langchain_core.messages import HumanMessage

    input_state = {
        "messages": [HumanMessage(content=user_content)],
        "tenant_id": tenant_membership.tenant_id,
        "tenant_name": tenant_membership.tenant_name,
        "user_id": str(user.id),
        "user_role": "analyst",
        "needs_correction": False,
        "retry_count": 0,
        "correction_context": {},
    }
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": 50,
        "oauth_tokens": oauth_tokens,
    }

    # Return streaming response (SSE for AI SDK v6 DefaultChatTransport)
    response = StreamingHttpResponse(
        langgraph_to_ui_stream(agent, input_state, config),
        content_type="text/event-stream; charset=utf-8",
    )
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


# ---------------------------------------------------------------------------
# Thread list & messages endpoints
# ---------------------------------------------------------------------------

def _langchain_messages_to_ui(lc_messages) -> list[dict]:
    """Convert LangChain BaseMessages to AI SDK v6 UIMessage format."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    ui_messages: list[dict] = []
    # Collect tool results keyed by tool_call_id for pairing
    tool_results: dict[str, ToolMessage] = {}
    for msg in lc_messages:
        if isinstance(msg, ToolMessage):
            tool_results[msg.tool_call_id] = msg

    for msg in lc_messages:
        if isinstance(msg, HumanMessage):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            ui_messages.append({
                "id": msg.id or uuid.uuid4().hex,
                "role": "user",
                "parts": [{"type": "text", "text": content}],
            })
        elif isinstance(msg, AIMessage):
            parts: list[dict] = []

            # Text content
            text = ""
            if isinstance(msg.content, str):
                text = msg.content
            elif isinstance(msg.content, list):
                text = "".join(
                    b.get("text", "") if isinstance(b, dict) else str(b)
                    for b in msg.content
                    if not isinstance(b, dict) or b.get("type") == "text"
                )
            if text:
                parts.append({"type": "text", "text": text})

            # Tool calls
            for tc in getattr(msg, "tool_calls", []) or []:
                tool_part = {
                    "type": f"tool-{tc['name']}",
                    "toolCallId": tc["id"],
                    "toolName": tc["name"],
                    "input": tc.get("args", {}),
                    "state": "output-available",
                }
                # Pair with tool result if available
                tr = tool_results.get(tc["id"])
                if tr:
                    tool_part["output"] = tr.content if isinstance(tr.content, str) else str(tr.content)
                tool_part["state"] = "output-available" if tr else "input-available"
                parts.append(tool_part)

            if parts:
                ui_messages.append({
                    "id": msg.id or uuid.uuid4().hex,
                    "role": "assistant",
                    "parts": parts,
                })

    return ui_messages


async def thread_list_view(request):
    """
    GET /api/chat/threads/?project_id=X or ?tenant_id=X

    Returns recent threads for the authenticated user in a project or tenant.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = await _get_user_if_authenticated(request)
    if user is None:
        return JsonResponse({"error": "Authentication required"}, status=401)

    project_id = request.GET.get("project_id")
    tenant_id = request.GET.get("tenant_id")

    if project_id:
        membership = await _get_membership(user, project_id)
        if membership is None:
            return JsonResponse({"error": "Project not found or access denied"}, status=403)
        threads = await _list_threads(user, project_id=project_id)
    elif tenant_id:
        from apps.users.models import TenantMembership

        try:
            await TenantMembership.objects.aget(id=tenant_id, user=user)
        except TenantMembership.DoesNotExist:
            return JsonResponse({"error": "Tenant not found or access denied"}, status=403)
        threads = await _list_threads(user, tenant_membership_id=tenant_id)
    else:
        return JsonResponse({"error": "project_id or tenant_id is required"}, status=400)

    return JsonResponse(threads, safe=False)


async def thread_messages_view(request, thread_id):
    """
    GET /api/chat/threads/<thread_id>/messages/

    Loads conversation from the checkpointer and returns UIMessage format.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = await _get_user_if_authenticated(request)
    if user is None:
        return JsonResponse({"error": "Authentication required"}, status=401)

    thread = await _get_thread(thread_id, user)
    if thread is None:
        return JsonResponse([], safe=False)

    try:
        checkpointer = await _ensure_checkpointer()
        config = {"configurable": {"thread_id": str(thread_id)}}
        checkpoint_tuple = await checkpointer.aget_tuple(config)
    except Exception:
        logger.warning("Failed to load checkpoint for thread %s", thread_id, exc_info=True)
        return JsonResponse([], safe=False)

    if checkpoint_tuple is None:
        return JsonResponse([], safe=False)

    checkpoint = checkpoint_tuple.checkpoint
    lc_messages = (checkpoint.get("channel_values") or {}).get("messages", [])
    ui_messages = _langchain_messages_to_ui(lc_messages)
    return JsonResponse(ui_messages, safe=False)


# ---------------------------------------------------------------------------
# Thread sharing endpoints
# ---------------------------------------------------------------------------

async def thread_share_view(request, thread_id):
    """
    GET  /api/chat/threads/<thread_id>/share/  — get sharing settings
    PATCH /api/chat/threads/<thread_id>/share/ — update sharing settings
    """
    user = await _get_user_if_authenticated(request)
    if user is None:
        return JsonResponse({"error": "Authentication required"}, status=401)

    thread = await _get_thread(thread_id, user)
    if thread is None:
        return JsonResponse({"error": "Thread not found"}, status=404)

    if request.method == "GET":
        return JsonResponse({
            "id": str(thread.id),
            "is_shared": thread.is_shared,
            "is_public": thread.is_public,
            "share_token": thread.share_token,
        })

    if request.method == "PATCH":
        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        result = await _update_thread_sharing(
            thread,
            is_shared=body.get("is_shared"),
            is_public=body.get("is_public"),
        )
        return JsonResponse(result)

    return JsonResponse({"error": "Method not allowed"}, status=405)


async def public_thread_view(request, share_token):
    """
    GET /api/chat/threads/shared/<share_token>/

    Public read-only view of a shared thread's messages and artifacts.
    No authentication required.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    thread = await _get_public_thread(share_token)
    if thread is None:
        return JsonResponse({"error": "Thread not found"}, status=404)

    # Load messages from checkpointer
    try:
        checkpointer = await _ensure_checkpointer()
        config = {"configurable": {"thread_id": str(thread.id)}}
        checkpoint_tuple = await checkpointer.aget_tuple(config)
    except Exception:
        logger.warning("Failed to load checkpoint for shared thread %s", thread.id, exc_info=True)
        checkpoint_tuple = None

    messages = []
    if checkpoint_tuple is not None:
        checkpoint = checkpoint_tuple.checkpoint
        lc_messages = (checkpoint.get("channel_values") or {}).get("messages", [])
        messages = _langchain_messages_to_ui(lc_messages)

    # Load associated artifacts
    artifacts = await _get_thread_artifacts(thread.id)

    return JsonResponse({
        "thread": {
            "id": str(thread.id),
            "title": thread.title,
            "created_at": thread.created_at.isoformat(),
        },
        "messages": messages,
        "artifacts": artifacts,
    })
