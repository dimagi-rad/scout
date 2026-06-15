"""Guardrail: enforce the project's ``sync_to_async``-not-for-ORM convention.

CLAUDE.md (Async conventions) states::

    ``sync_to_async``: Only for wrapping external API calls (OAuth token
    refresh, CommCare API) and inherently sync operations (dbt). Do not use
    for ORM calls. Exception: a transactional write block
    (``with transaction.atomic(): ...``) may be wrapped in ``@sync_to_async``.

This rule was prose-only and kept getting violated, so this AST-based test
enforces it deterministically. It needs no database -- it parses source files
and flags disallowed usages.

Two forbidden forms are detected:

1. Call form: ``sync_to_async(<expr>)`` where ``<expr>`` is an ORM access
   (an attribute chain containing ``.objects.`` or ending in a known ORM
   method name), including ``lambda``-wrapped bodies.
2. Decorator form: ``@sync_to_async`` on a ``def`` whose body performs ORM
   access without wrapping every ORM statement inside ``with
   transaction.atomic():`` -- the one documented exception.

The import alias is resolved for both ``from asgiref.sync import
sync_to_async`` (a bare ``Name`` call) and ``asgiref.sync.sync_to_async(...)``
(an ``Attribute`` call).
"""

import ast
from pathlib import Path

import pytest

# ORM method names are split into two tiers to control false positives.
#
# UNAMBIGUOUS_ORM_METHODS are Django-ORM-specific: the async ``a``-prefixed
# methods and the compound manager methods (``*_or_create``, ``bulk_*``). These
# practically never appear on non-ORM objects, so a bare attribute chain ending
# in one (e.g. ``obj.acreate``) is enough to flag.
UNAMBIGUOUS_ORM_METHODS = frozenset(
    {
        "acreate",
        "asave",
        "adelete",
        "aget",
        "aget_or_create",
        "aupdate_or_create",
        "aupdate",
        "acount",
        "afirst",
        "alast",
        "aexists",
        "get_or_create",
        "update_or_create",
        "bulk_create",
        "bulk_update",
        "abulk_create",
        "abulk_update",
    }
)

# AMBIGUOUS_ORM_METHODS overlap with non-ORM APIs (``requests.get``,
# ``serializer.save``, ``dict.values``, ``client.delete``...). They only count
# as ORM when the chain also contains a manager segment (``.objects.``), EXCEPT
# in the narrowly-scoped call form ``sync_to_async(<expr>)`` where the
# expression is the direct wrapped target -- there a bare leaf is meaningful and
# matches the documented examples (``sync_to_async(obj.save)``).
AMBIGUOUS_ORM_METHODS = frozenset(
    {
        "create",
        "save",
        "delete",
        "get",
        "filter",
        "exclude",
        "all",
        "update",
        "count",
        "first",
        "last",
        "exists",
        "values",
        "values_list",
        "select_related",
        "prefetch_related",
        "earliest",
        "latest",
        "get_or_none",
    }
)

# Leaf names treated as ORM in the narrowly-scoped ``sync_to_async(<expr>)``
# call form, even without an ``.objects.`` segment. This honors the documented
# violation example ``sync_to_async(obj.save)`` and the mutation verbs, while
# still excluding pervasive read-style names (``get``/``filter``/``values``/...)
# that collide with non-ORM APIs such as ``requests.get`` and ``cache.get``.
CALLFORM_BARE_LEAF_METHODS = UNAMBIGUOUS_ORM_METHODS | {"save", "delete", "create"}

# Leaf names treated as ORM when walking a ``@sync_to_async``-decorated body.
# Only unambiguous ORM-specific names match as a bare leaf; ambiguous names
# require an ``.objects.`` chain (avoids serializer.save / dict.values noise).
DECORATOR_BARE_LEAF_METHODS = UNAMBIGUOUS_ORM_METHODS

# Directories never scanned for the repo-wide check.
SKIP_DIR_NAMES = frozenset(
    {
        "migrations",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".git",
        "build",
        "dist",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)

# Roots scanned by the repo-wide assertion.
SCAN_ROOTS = ("apps", "mcp_server", "config", "tests")

# Grandfathered legacy sites: ``"<relpath>:<lineno>"``. As of the audit on
# origin/main there were ZERO disallowed usages (both decorator sites in
# apps/users/views.py wrap their ORM writes in ``transaction.atomic()``), so
# this allowlist is empty and the check is strict. New legacy entries here MUST
# carry a ``# TODO: convert to native async ORM`` note.
_GRANDFATHERED: frozenset[str] = frozenset()


def _attr_chain_names(node: ast.AST) -> list[str] | None:
    """Return the dotted name parts of an attribute/name chain, else ``None``.

    ``Tenant.objects.create`` -> ``["Tenant", "objects", "create"]``.
    Returns ``None`` if the chain is rooted in something that is not a plain
    name (e.g. a subscript or call), which we cannot statically resolve.
    """
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        parts.reverse()
        return parts
    return None


def _is_orm_attribute(node: ast.AST, *, bare_leaf_methods: frozenset[str] | set[str]) -> bool:
    """True if ``node`` is an attribute chain that reads as an ORM access.

    A chain containing an ``objects`` manager segment always counts. Otherwise a
    bare leaf method counts only if it is in ``bare_leaf_methods`` -- callers
    pass a wider set for the narrowly-scoped call form and a conservative set for
    the broad decorator-body walk.
    """
    if not isinstance(node, ast.Attribute):
        return False
    parts = _attr_chain_names(node)
    if parts is not None and "objects" in parts:
        return True
    return node.attr in bare_leaf_methods


def _expr_is_orm(node: ast.AST) -> bool:
    """True if an expression passed directly to ``sync_to_async`` is ORM access.

    Handles a bare attribute reference (``Model.objects.create``, ``obj.save``)
    and a ``lambda`` whose body performs ORM access. Because this is the direct
    wrapped target, the wider call-form leaf set is honored.
    """
    if isinstance(node, ast.Attribute):
        return _is_orm_attribute(node, bare_leaf_methods=CALLFORM_BARE_LEAF_METHODS)
    if isinstance(node, ast.Lambda):
        return _body_has_orm(node.body, bare_leaf_methods=CALLFORM_BARE_LEAF_METHODS)
    return False


def _body_has_orm(node: ast.AST, *, bare_leaf_methods: frozenset[str] | set[str]) -> bool:
    """True if any expression within ``node`` is an ORM call/access."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and _is_orm_attribute(
            sub.func, bare_leaf_methods=bare_leaf_methods
        ):
            return True
        if isinstance(sub, ast.Attribute) and _is_orm_attribute(
            sub, bare_leaf_methods=bare_leaf_methods
        ):
            return True
    return False


class _Detector(ast.NodeVisitor):
    """Collect disallowed ``sync_to_async`` + ORM usages from one module."""

    def __init__(self, source_lines: list[str]) -> None:
        self._lines = source_lines
        # Names that refer to ``sync_to_async`` (handles aliased imports).
        self._aliases: set[str] = set()
        self.violations: list[tuple[int, str]] = []

    # --- import resolution -------------------------------------------------
    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "asgiref.sync":
            for alias in node.names:
                if alias.name == "sync_to_async":
                    self._aliases.add(alias.asname or alias.name)
        self.generic_visit(node)

    # --- helpers -----------------------------------------------------------
    def _is_sync_to_async(self, node: ast.AST) -> bool:
        """True if ``node`` references ``sync_to_async`` (Name or Attribute)."""
        if isinstance(node, ast.Name):
            return node.id in self._aliases or node.id == "sync_to_async"
        if isinstance(node, ast.Attribute):
            # asgiref.sync.sync_to_async(...)
            return node.attr == "sync_to_async"
        return False

    def _snippet(self, lineno: int) -> str:
        try:
            return self._lines[lineno - 1].strip()
        except IndexError:
            return "<source unavailable>"

    def _record(self, lineno: int) -> None:
        self.violations.append((lineno, self._snippet(lineno)))

    # --- call form: sync_to_async(<orm expr>) ------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        if (
            self._is_sync_to_async(node.func)
            and node.args
            and _expr_is_orm(node.args[0])
        ):
            self._record(node.lineno)
        self.generic_visit(node)

    # --- decorator form: @sync_to_async def f(): <orm without atomic> ------
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_decorated_def(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check_decorated_def(node)
        self.generic_visit(node)

    def _check_decorated_def(self, node: ast.AST) -> None:
        decorated = any(self._is_sync_to_async(d) for d in node.decorator_list)
        if not decorated:
            return
        if self._orm_outside_atomic(node.body):
            # Report on the first decorator line for a stable location.
            deco_line = min(
                (
                    d.lineno
                    for d in node.decorator_list
                    if self._is_sync_to_async(d)
                ),
                default=node.lineno,
            )
            self._record(deco_line)

    def _orm_outside_atomic(self, body: list[ast.stmt]) -> bool:
        """True if ORM access occurs in ``body`` outside a ``transaction.atomic()`` block.

        The documented exception permits ORM writes only when they live inside
        ``with transaction.atomic():``. We treat any ORM access not contained in
        such a ``with`` block as a violation.
        """
        for stmt in body:
            if self._stmt_is_atomic_with(stmt):
                # ORM inside an atomic block is explicitly allowed; skip it.
                continue
            # Conservative leaf matching inside decorated bodies: ambiguous
            # names like serializer.save / dict.values are NOT ORM here.
            if _body_has_orm(stmt, bare_leaf_methods=DECORATOR_BARE_LEAF_METHODS):
                return True
        return False

    @staticmethod
    def _stmt_is_atomic_with(stmt: ast.stmt) -> bool:
        if not isinstance(stmt, ast.With):
            return False
        for item in stmt.items:
            ctx = item.context_expr
            # transaction.atomic() or atomic() call, or bare attribute.
            target = ctx.func if isinstance(ctx, ast.Call) else ctx
            if isinstance(target, ast.Attribute) and target.attr == "atomic":
                return True
            if isinstance(target, ast.Name) and target.id == "atomic":
                return True
        return False


def find_violations(source: str, filename: str = "<unknown>") -> list[tuple[int, str]]:
    """Parse ``source`` and return ``(lineno, snippet)`` for each violation."""
    tree = ast.parse(source, filename=filename)
    detector = _Detector(source.splitlines())
    detector.visit(tree)
    return sorted(detector.violations)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _iter_python_files():
    root = _repo_root()
    for scan_root in SCAN_ROOTS:
        base = root / scan_root
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if any(part in SKIP_DIR_NAMES for part in path.parts):
                continue
            yield path


# ---------------------------------------------------------------------------
# Unit tests for the detector (inline code-string fixtures; no DB, no I/O).
# ---------------------------------------------------------------------------

DISALLOWED_SNIPPETS = [
    # Call form -- manager .objects. chain
    "from asgiref.sync import sync_to_async\n"
    "result = sync_to_async(Tenant.objects.create)(name='x')\n",
    # Call form -- bound instance method
    "from asgiref.sync import sync_to_async\n"
    "await sync_to_async(obj.save)()\n",
    # Call form -- lambda wrapping ORM
    "from asgiref.sync import sync_to_async\n"
    "await sync_to_async(lambda: Workspace.objects.create(name='x'))()\n",
    # Call form -- async ORM method name
    "from asgiref.sync import sync_to_async\n"
    "await sync_to_async(Model.objects.aget)(id=1)\n",
    # Fully-qualified attribute call form
    "import asgiref.sync\n"
    "await asgiref.sync.sync_to_async(Model.objects.filter)(active=True)\n",
    # Aliased import
    "from asgiref.sync import sync_to_async as s2a\n"
    "await s2a(Model.objects.delete)()\n",
    # Decorator form -- ORM with NO atomic block
    "from asgiref.sync import sync_to_async\n"
    "@sync_to_async\n"
    "def persist(user):\n"
    "    return Tenant.objects.create(name=user)\n",
    # Decorator form -- ORM outside atomic, some inside (still a violation)
    "from asgiref.sync import sync_to_async\n"
    "from django.db import transaction\n"
    "@sync_to_async\n"
    "def persist():\n"
    "    Model.objects.create(x=1)\n"
    "    with transaction.atomic():\n"
    "        Model.objects.update(y=2)\n",
]

ALLOWED_SNIPPETS = [
    # Non-ORM call -- external API
    "from asgiref.sync import sync_to_async\n"
    "await sync_to_async(requests.get)('http://x')\n",
    # Non-ORM call -- dbt / arbitrary callable
    "from asgiref.sync import sync_to_async\n"
    "await sync_to_async(runner.invoke)(['build'])\n",
    # Non-ORM call -- close_old_connections (config/procrastinate.py pattern)
    "from asgiref.sync import sync_to_async\n"
    "_acleanup = sync_to_async(close_old_connections, thread_sensitive=True)\n",
    # Non-ORM call -- client.login (tests pattern)
    "from asgiref.sync import sync_to_async\n"
    "await sync_to_async(client.login)(email='a', password='b')\n",
    # Non-ORM lambda
    "from asgiref.sync import sync_to_async\n"
    "await sync_to_async(lambda: connections['default'].close())()\n",
    # Documented exception -- decorator wrapping atomic ORM writes
    "from asgiref.sync import sync_to_async\n"
    "from django.db import transaction\n"
    "@sync_to_async\n"
    "def persist(user):\n"
    "    with transaction.atomic():\n"
    "        conn = TenantConnection.objects.create(user=user)\n"
    "        conn.memberships.update(connection=None)\n"
    "    return conn\n",
    # Decorator wrapping a NON-ORM body
    "from asgiref.sync import sync_to_async\n"
    "@sync_to_async\n"
    "def fetch():\n"
    "    return requests.get('http://x').json()\n",
    # sync_to_async not even imported from asgiref -- unrelated symbol
    "def sync_to_async(x):\n"
    "    return x\n"
    "sync_to_async(other.helper)()\n",
]


@pytest.mark.parametrize("snippet", DISALLOWED_SNIPPETS)
def test_detector_flags_disallowed(snippet):
    violations = find_violations(snippet)
    assert violations, f"expected a violation but found none in:\n{snippet}"


@pytest.mark.parametrize("snippet", ALLOWED_SNIPPETS)
def test_detector_allows_permitted(snippet):
    violations = find_violations(snippet)
    assert not violations, f"expected no violation but found {violations} in:\n{snippet}"


def test_detector_reports_lineno_and_snippet():
    source = (
        "from asgiref.sync import sync_to_async\n"  # line 1
        "x = 1\n"  # line 2
        "await sync_to_async(Tenant.objects.create)(name='x')\n"  # line 3
    )
    violations = find_violations(source)
    assert violations == [(3, "await sync_to_async(Tenant.objects.create)(name='x')")]


# ---------------------------------------------------------------------------
# Repo-wide assertion: scan source and fail on any non-grandfathered violation.
# ---------------------------------------------------------------------------


def test_no_sync_to_async_orm_in_repo():
    """No disallowed ``sync_to_async`` + ORM usage exists in scanned source.

    The documented ``transaction.atomic()`` exception is allowed. Legacy sites,
    if any, are tracked in ``_GRANDFATHERED`` (currently empty).
    """
    root = _repo_root()
    offenders: list[str] = []
    for path in _iter_python_files():
        rel = path.relative_to(root).as_posix()
        source = path.read_text(encoding="utf-8")
        for lineno, snippet in find_violations(source, filename=rel):
            key_line = f"{rel}:{lineno}"
            if key_line in _GRANDFATHERED or rel in _GRANDFATHERED:
                continue
            offenders.append(f"{key_line}  {snippet}")

    assert not offenders, (
        "sync_to_async must not wrap Django ORM calls (CLAUDE.md, Async "
        "conventions). Use native async ORM (acreate/aget/...) instead, or wrap "
        "transactional writes in `with transaction.atomic():`.\n"
        "Offenders:\n  " + "\n  ".join(offenders)
    )
