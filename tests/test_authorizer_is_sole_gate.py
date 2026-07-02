"""Fitness test: workspace access must be decided ONLY by apps/workspaces/access.py.

The access rule (WorkspaceMembership AND a live tenant) lives in one authorizer so
it can't be partially forgotten. This test fails CI if any view/tool/service
resolves workspace access by querying ``WorkspaceMembership`` directly — i.e. a
query filtered by BOTH a workspace key and a user key, the authorizer's signature
— outside ``access.py``. Legitimate non-auth uses of that shape (e.g. checking
whether a *target* is already a member) opt out with an inline ``# authz-exempt``
comment on or just above the call.

This is intentionally narrow: filtering by user alone (listing a user's
workspaces) or by workspace alone (listing members) is not an access decision and
is not flagged.
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTHORIZER = REPO_ROOT / "apps" / "workspaces" / "access.py"
SCAN_DIRS = ["apps", "mcp_server"]

WORKSPACE_KEYS = {"workspace", "workspace_id"}
USER_KEYS = {"user", "user_id"}
# Only READS resolve access. Creating/updating a membership (a write) is not an
# access decision, so .create/.update/.get_or_create are not flagged.
READ_METHODS = {"get", "aget", "filter", "exclude", "exists", "aexists", "first", "afirst"}


def _authz_bypass_lines(source: str) -> list[int]:
    """Return 1-based line numbers of WorkspaceMembership READS that filter by
    both a workspace key and a user key (an access decision), minus exempted ones."""
    tree = ast.parse(source)
    lines = source.splitlines()
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in READ_METHODS:
            continue
        if "WorkspaceMembership.objects" not in ast.unparse(node.func):
            continue
        kwargs = {kw.arg for kw in node.keywords if kw.arg}
        if not (kwargs & WORKSPACE_KEYS and kwargs & USER_KEYS):
            continue
        window = "\n".join(lines[max(0, node.lineno - 4) : node.lineno])
        if "authz-exempt" in window:
            continue
        hits.append(node.lineno)
    return hits


def _production_files():
    for d in SCAN_DIRS:
        for path in (REPO_ROOT / d).rglob("*.py"):
            if path == AUTHORIZER:
                continue
            if "/migrations/" in str(path) or "/tests/" in str(path) or path.name.startswith("test_"):
                continue
            yield path


def test_workspace_access_resolved_only_in_authorizer():
    violations = []
    for path in _production_files():
        for lineno in _authz_bypass_lines(path.read_text()):
            violations.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert not violations, (
        "Workspace access resolved outside apps/workspaces/access.py. Route these "
        "through resolve_workspace_access / aresolve_workspace_access, or mark a "
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
    ],
)
def test_detector_catches_bypass_but_not_legitimate_use(snippet, expected):
    assert len(_authz_bypass_lines(snippet)) == expected
