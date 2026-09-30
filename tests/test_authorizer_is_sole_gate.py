"""Fitness test: workspace access must be decided ONLY by apps/workspaces/access.py.

The access rule (WorkspaceMembership AND coverage of its tenants) lives in one authorizer so
it can't be partially forgotten. This test fails CI if any view/tool/service
resolves workspace access by reading ``WorkspaceMembership`` directly — i.e. a
read constrained by BOTH a workspace and a user, the authorizer's signature —
outside ``access.py``. That covers ``WorkspaceMembership.objects`` and the related
managers, where one side is implied by the receiver: ``workspace.memberships``
filtered by a user, or ``user.workspace_memberships`` filtered by a workspace.
Keywords are collected across a chained query, so splitting them over two
``.filter()`` calls does not hide the lookup. Legitimate non-auth uses of that
shape (e.g. checking whether a *target* is already a member) opt out with an
inline ``# authz-exempt`` comment on or just above the call.

This is a narrow lint, not proof of coverage: ``Q`` objects, ``**kwargs``, ``__in``
lookups and aliased managers are not recognised. Filtering by user alone (listing a
user's workspaces) or by workspace alone (listing members) is not an access
decision and is not flagged. ``.memberships`` is also the related name of
``Tenant`` and ``TenantConnection`` memberships; a user-filtered read of those
is flagged too and needs an exemption saying so.
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTHORIZER = REPO_ROOT / "apps" / "workspaces" / "access.py"
SCAN_DIRS = ["apps", "mcp_server"]

WORKSPACE_KEYS = {"workspace", "workspace_id"}
USER_KEYS = {"user", "user_id"}
# Suffixes that still pin one workspace or user; ``__in`` and the like are listings.
SINGLE_ROW_SUFFIXES = {"", "id", "pk", "exact"}
# Only READS resolve access. Creating/updating a membership (a write) is not an
# access decision, so .create/.update/.get_or_create are not flagged.
READ_METHODS = {"get", "aget", "filter", "exclude", "exists", "aexists", "first", "afirst"}
# Related managers onto WorkspaceMembership, and the side their receiver implies.
RELATED_MANAGERS = {"memberships": WORKSPACE_KEYS, "workspace_memberships": USER_KEYS}


def _lookup_field(keyword: str) -> str | None:
    field, _, suffix = keyword.partition("__")
    return field if suffix in SINGLE_ROW_SUFFIXES else None


def _query_chain(call: ast.Call) -> tuple[set[str], set[str]]:
    """Keyword fields and attribute names across ``call``'s receiver chain."""
    fields, attrs = set(), set()
    node = call
    while isinstance(node, ast.Call | ast.Attribute):
        if isinstance(node, ast.Call):
            fields |= {_lookup_field(kw.arg) for kw in node.keywords if kw.arg}
            node = node.func
        else:
            attrs.add(node.attr)
            node = node.value
    return fields - {None}, attrs


def _implied_keys(call: ast.Call, attrs: set[str]) -> set[str] | None:
    """The side a membership manager implies, or ``None`` if this is not one."""
    if "WorkspaceMembership.objects" in ast.unparse(call.func):
        return set()
    for manager, implied in RELATED_MANAGERS.items():
        if manager in attrs:
            return implied
    return None


def _authz_bypass_lines(source: str) -> list[int]:
    """Return 1-based line numbers of WorkspaceMembership READS that pin both a
    workspace and a user (an access decision), minus exempted ones."""
    tree = ast.parse(source)
    lines = source.splitlines()
    hits = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in READ_METHODS:
            continue
        fields, attrs = _query_chain(node)
        implied = _implied_keys(node, attrs)
        if implied is None:
            continue
        keys = fields | implied
        if not (keys & WORKSPACE_KEYS and keys & USER_KEYS):
            continue
        window = "\n".join(lines[max(0, node.lineno - 4) : node.lineno])
        if "authz-exempt" in window:
            continue
        hits.add(node.lineno)
    return sorted(hits)


def _production_files():
    for d in SCAN_DIRS:
        for path in (REPO_ROOT / d).rglob("*.py"):
            if path == AUTHORIZER:
                continue
            if (
                "/migrations/" in str(path)
                or "/tests/" in str(path)
                or path.name.startswith("test_")
            ):
                continue
            yield path


# Known bypasses left in place deliberately, keyed by file and the flagged line's
# text so an unrelated edit moving them does not matter. Each needs a follow-up.
KNOWN_BYPASSES = {
    # Membership-and-role write check with no coverage; the whole Transformations
    # HTTP API is being removed rather than fixed (#760), so it is not rerouted here.
    ("apps/transformations/views.py", "has_write = user.workspace_memberships.filter("),
}


def test_workspace_access_resolved_only_in_authorizer():
    violations = []
    for path in _production_files():
        source = path.read_text()
        lines = source.splitlines()
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno in _authz_bypass_lines(source):
            if (rel, lines[lineno - 1].strip()) not in KNOWN_BYPASSES:
                violations.append(f"{rel}:{lineno}")
    assert not violations, (
        "Workspace access resolved outside apps/workspaces/access.py. Route these "
        "through resolve_workspace_access_ex / aresolve_workspace_access_ex, or mark a "
        "genuine non-auth use with `# authz-exempt`:\n  " + "\n  ".join(violations)
    )


@pytest.mark.parametrize(
    "snippet, expected",
    [
        # a bypass: resolves a specific workspace's membership for a user
        ("WorkspaceMembership.objects.get(workspace_id=wid, user=u)", 1),
        ("WorkspaceMembership.objects.filter(workspace=ws, user=u).exists()", 1),
        # not a bypass: listing a user's workspaces (user only)
        ("WorkspaceMembership.objects.filter(user=u)", 0),
        # not a bypass: listing members of a workspace (workspace only)
        ("WorkspaceMembership.objects.filter(workspace=ws)", 0),
        # exempted
        ("# authz-exempt\nWorkspaceMembership.objects.filter(workspace=ws, user=u)", 0),
        # a bypass split across a chained query, or spelled through a relation
        ("WorkspaceMembership.objects.filter(user=u).filter(workspace=ws).exists()", 1),
        ("WorkspaceMembership.objects.filter(user__pk=uid, workspace__id=wid).first()", 1),
        # a bypass through a related manager: the receiver implies one side
        ("request.user.workspace_memberships.filter(workspace=ws).exists()", 1),
        ("user.workspace_memberships.filter(role=r).filter(workspace_id=wid).first()", 1),
        ("workspace.memberships.filter(user=u).exists()", 1),
        ("await ws.memberships.aget(user_id=uid)", 1),
        ("workspace.memberships.exclude(user=u).exists()", 1),
        # not a bypass: a related manager constrained on its implied side only
        ("user.workspace_memberships.values_list('workspace_id', flat=True)", 0),
        ("user.workspace_memberships.filter(role=r)", 0),
        ("workspace.memberships.filter(role=r).count()", 0),
        ("conn.memberships.filter(archived_at__isnull=True)", 0),
        # not a bypass: a listing over several workspaces
        ("WorkspaceMembership.objects.filter(user=u, workspace_id__in=ids)", 0),
        # exempted related-manager read
        ("# authz-exempt: target check\nworkspace.memberships.filter(user=u).exists()", 0),
    ],
)
def test_detector_catches_bypass_but_not_legitimate_use(snippet, expected):
    assert len(_authz_bypass_lines(snippet)) == expected
