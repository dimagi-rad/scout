"""Truthful pipeline resolution (#155, arch 01#9).

Every read surface used to end its resolution chain with
``registry.get("commcare_sync")``, serving an OCS or Connect tenant the wrong
provider's table names and descriptions with nothing logged. These tests pin the
replacement behaviour per surface: resolve, or say so — never guess.
"""

import pytest

from apps.common.error_codes import ErrorCode
from apps.common.errors import ExpectedStateError
from apps.workspaces.services.pipeline_resolver import (
    PipelineResolutionError,
    select_pipeline_config,
)

UNKNOWN_PROVIDER = "mystery_provider"


class TestSelectPipelineConfig:
    def test_prefers_the_last_run_pipeline(self):
        config = select_pipeline_config(last_run_pipeline="ocs_sync", provider="commcare")
        assert config.name == "ocs_sync"

    def test_falls_back_to_the_provider_pipeline(self):
        config = select_pipeline_config(last_run_pipeline="retired_pipeline", provider="ocs")
        assert config.name == "ocs_sync"

    def test_unknown_provider_does_not_resolve_to_commcare_sync(self):
        with pytest.raises(PipelineResolutionError) as exc:
            select_pipeline_config(provider=UNKNOWN_PROVIDER)
        assert UNKNOWN_PROVIDER in str(exc.value)

    def test_raises_when_there_is_nothing_to_resolve_from(self):
        with pytest.raises(PipelineResolutionError):
            select_pipeline_config()

    def test_error_carries_a_stable_code(self):
        assert PipelineResolutionError.code == ErrorCode.PIPELINE_UNRESOLVED

    def test_is_not_an_expected_state(self):
        """A missing pipeline for a supported provider is a deploy defect: it must
        keep reaching Sentry rather than being filtered as routine."""
        assert not issubclass(PipelineResolutionError, ExpectedStateError)
