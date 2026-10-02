"""0012/0013 add Thread.title_source and mark renamed titles as the user's."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.chat.models import Thread

BEFORE = ("chat", "0011_thread_turn_lease")
AFTER = ("chat", "0013_backfill_thread_title_source")


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    return executor.loader.project_state(targets).apps


@pytest.fixture
def old_apps(transactional_db):
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        yield _migrate([BEFORE])
    finally:
        _migrate(leaves)


def test_migration_backfills_title_source_without_touching_titles(old_apps, workspace, user):
    old_thread = old_apps.get_model("chat", "Thread")
    renamed = old_thread.objects.create(
        workspace_id=workspace.id, user_id=user.id, title="Q3 review", title_is_custom=True
    )
    provisional = old_thread.objects.create(
        workspace_id=workspace.id, user_id=user.id, title="Visits by worker?"
    )
    blank = old_thread.objects.create(workspace_id=workspace.id, user_id=user.id, title="")

    _migrate([AFTER])

    rows = {t.id: t for t in Thread.objects.filter(id__in=[renamed.id, provisional.id, blank.id])}
    assert rows[renamed.id].title_source == Thread.TitleSource.USER
    assert rows[renamed.id].title == "Q3 review"
    assert rows[provisional.id].title_source == Thread.TitleSource.FIRST_MESSAGE
    assert rows[provisional.id].title == "Visits by worker?"
    assert rows[blank.id].title_source == Thread.TitleSource.FIRST_MESSAGE
    assert rows[blank.id].title == ""
