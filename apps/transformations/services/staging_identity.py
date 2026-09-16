"""Identity errors that must stop materialization before staging assets change."""

from apps.common.error_codes import ErrorCode


class StagingModelMigrationRequired(ValueError):
    """A staging refresh needs an explicit source-identity migration."""

    code = ErrorCode.SCHEMA_BUILD_FAILED


class RepeatModelMigrationRequired(StagingModelMigrationRequired):
    """An existing repeat model cannot be mapped safely to a current source."""
