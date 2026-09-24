import pytest
from django.db import OperationalError

from tests.row_locks import row_locked, user_row


@pytest.mark.django_db(transaction=True)
def test_contended_acquire_reports_the_database_error(user):
    """A row already locked elsewhere must fail fast with the real cause attached."""
    with row_locked(user_row(user.id)):
        with pytest.raises(AssertionError, match="never acquired") as excinfo:
            with row_locked(user_row(user.id), acquire_timeout=0.2):
                pytest.fail("the contended holder must not acquire the row")

    assert isinstance(excinfo.value.__cause__, OperationalError)


@pytest.mark.django_db(transaction=True)
def test_lock_is_released_when_the_body_fails(user):
    with pytest.raises(RuntimeError), row_locked(user_row(user.id)):
        raise RuntimeError("body failed")

    with row_locked(user_row(user.id), acquire_timeout=0.5):
        pass
