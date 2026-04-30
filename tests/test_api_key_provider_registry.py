def test_registry_contains_expected_providers():
    from apps.users.services.api_key_providers.commcare import CommCareStrategy
    from apps.users.services.api_key_providers.ocs import OCSStrategy
    from apps.users.services.api_key_providers.registry import STRATEGIES

    assert STRATEGIES["commcare"] is CommCareStrategy
    assert STRATEGIES["ocs"] is OCSStrategy


def test_get_strategy_returns_class_or_none():
    from apps.users.services.api_key_providers.registry import get_strategy

    cls = get_strategy("ocs")
    assert cls is not None
    assert cls.provider_id == "ocs"
    assert get_strategy("does-not-exist") is None
