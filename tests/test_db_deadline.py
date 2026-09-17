from __future__ import annotations

import pytest
from django.db import connection as django_connection
from django.db import transaction

from apps.common.db_deadline import preserve_transaction_timeouts


def _timeouts():
    with django_connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_setting('lock_timeout'), current_setting('statement_timeout')"
        )
        return cursor.fetchone()


def _tighten(value="250ms"):
    with django_connection.cursor() as cursor:
        cursor.execute("SELECT set_config('lock_timeout', %s, true)", [value])
        cursor.execute("SELECT set_config('statement_timeout', %s, true)", [value])


@pytest.mark.django_db(transaction=True)
def test_restores_timeouts_when_nested_block_exits_normally():
    with transaction.atomic():
        _tighten("13s")
        with transaction.atomic(), preserve_transaction_timeouts():
            _tighten("250ms")
            assert _timeouts() == ("250ms", "250ms")
        assert _timeouts() == ("13s", "13s")


@pytest.mark.django_db(transaction=True)
def test_leaks_without_the_helper():
    """Pin the PostgreSQL behaviour the helper exists to correct."""
    with transaction.atomic():
        _tighten("13s")
        with transaction.atomic():
            _tighten("250ms")
        assert _timeouts() == ("250ms", "250ms")


@pytest.mark.django_db(transaction=True)
def test_rollback_restores_timeouts_on_propagated_error():
    with transaction.atomic():
        _tighten("13s")
        with pytest.raises(ValueError, match="boom"):
            with transaction.atomic(), preserve_transaction_timeouts():
                _tighten("250ms")
                raise ValueError("boom")
        assert _timeouts() == ("13s", "13s")


@pytest.mark.django_db(transaction=True)
def test_restores_through_several_nesting_levels():
    with transaction.atomic():
        _tighten("30s")
        with transaction.atomic(), preserve_transaction_timeouts():
            _tighten("10s")
            with transaction.atomic(), preserve_transaction_timeouts():
                _tighten("500ms")
                assert _timeouts() == ("500ms", "500ms")
            assert _timeouts() == ("10s", "10s")
        assert _timeouts() == ("30s", "30s")


@pytest.mark.django_db(transaction=True)
def test_restores_unset_defaults():
    before = _timeouts()
    with transaction.atomic(), preserve_transaction_timeouts():
        _tighten("250ms")
    assert _timeouts() == before


@pytest.mark.django_db(transaction=True)
def test_block_that_changes_nothing_is_transparent():
    with transaction.atomic():
        _tighten("7s")
        with transaction.atomic(), preserve_transaction_timeouts():
            pass
        assert _timeouts() == ("7s", "7s")
