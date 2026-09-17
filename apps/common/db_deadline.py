"""Transaction-deadline helpers shared by services that bound their own DB waits."""

import contextlib

from django.db import connections

DEFAULT_ALIAS = "default"


@contextlib.contextmanager
def preserve_transaction_timeouts(*, using=DEFAULT_ALIAS):
    """Restore the caller's lock/statement timeouts when this block exits normally.

    PostgreSQL scopes ``set_config(..., is_local=true)`` to the *transaction*, not to
    the savepoint that a nested ``transaction.atomic()`` opens. A callee that tightens
    its deadline therefore leaks the shorter timeout to its caller once the savepoint
    is released. Capture the prior values on entry and put them back before the block
    exits, so a bounded wait cannot outlive the work it was meant to bound.

    Use inside ``transaction.atomic()`` so the restore lands before the savepoint is
    released::

        with transaction.atomic(), preserve_transaction_timeouts():
            ...

    On a propagated exception nothing is restored here: the enclosing rollback already
    reverts the settings, and the connection may be in a failed transaction that would
    reject the restore.
    """
    conn = connections[using]
    if conn.vendor != "postgresql":
        yield
        return

    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT current_setting('lock_timeout'), current_setting('statement_timeout')"
        )
        lock_timeout, statement_timeout = cursor.fetchone()

    yield

    with conn.cursor() as cursor:
        cursor.execute("SELECT set_config('lock_timeout', %s, true)", [lock_timeout])
        cursor.execute("SELECT set_config('statement_timeout', %s, true)", [statement_timeout])
