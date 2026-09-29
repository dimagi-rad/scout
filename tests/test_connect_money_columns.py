"""Connect money columns must store what the API sent (#263, finding 02#7)."""

import os
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from mcp_server.services.materializer import (
    _write_connect_completed_works,
    _write_connect_invoices,
    _write_connect_payments,
    _write_connect_users,
)

MONEY_COLUMNS = [
    pytest.param(_write_connect_users, "raw_users", "payment_accrued", {"username": "u1"}),
    *(
        pytest.param(_write_connect_completed_works, "raw_completed_works", column, {})
        for column in (
            "saved_payment_accrued",
            "saved_payment_accrued_usd",
            "saved_org_payment_accrued",
            "saved_org_payment_accrued_usd",
        )
    ),
    pytest.param(_write_connect_payments, "raw_payments", "amount", {}),
    pytest.param(_write_connect_payments, "raw_payments", "amount_usd", {}),
    pytest.param(_write_connect_invoices, "raw_invoices", "amount", {}),
    pytest.param(_write_connect_invoices, "raw_invoices", "amount_usd", {}),
]


@pytest.fixture
def managed_conn():
    url = os.environ.get("MANAGED_DATABASE_URL")
    if not url:
        pytest.skip("MANAGED_DATABASE_URL not set")
    schema = f"test_connect_money_{uuid4().hex}"
    conn = psycopg.connect(url, autocommit=True)
    conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    conn.autocommit = False
    try:
        yield conn, schema
    finally:
        conn.rollback()
        conn.autocommit = True
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        conn.close()


@pytest.mark.parametrize(("writer", "table", "column", "base"), MONEY_COLUMNS)
@pytest.mark.parametrize(
    "amount",
    # Minor units or a weak currency exceed NUMERIC(14,2); sub-cent rates must not round.
    ["1234567890123.45", "0.125"],
    ids=["beyond-1e12", "sub-cent"],
)
def test_money_is_stored_unrounded_and_unbounded(managed_conn, writer, table, column, base, amount):
    conn, schema = managed_conn
    written = writer(iter([([{**base, column: amount}], 1)]), schema, conn)

    (stored,) = conn.execute(
        sql.SQL("SELECT {} FROM {}").format(sql.Identifier(column), sql.Identifier(schema, table))
    ).fetchone()
    assert written == 1
    assert stored == Decimal(amount)
