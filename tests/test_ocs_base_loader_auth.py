def test_oauth_credential_uses_bearer_header():
    from mcp_server.loaders.ocs_base import OCSBaseLoader

    loader = OCSBaseLoader(
        experiment_id="exp-1",
        credential={"type": "oauth", "value": "tok123"},
    )
    assert loader._session.headers["Authorization"] == "Bearer tok123"


def test_api_key_credential_uses_x_api_key_header():
    from mcp_server.loaders.ocs_base import OCSBaseLoader

    loader = OCSBaseLoader(
        experiment_id="exp-1",
        credential={"type": "api_key", "value": "ocs_xxx"},
    )
    assert loader._session.headers["X-api-key"] == "ocs_xxx"
    assert "Authorization" not in loader._session.headers


def test_default_credential_type_treated_as_oauth():
    """Backward compat: missing 'type' key defaults to OAuth/Bearer."""
    from mcp_server.loaders.ocs_base import OCSBaseLoader

    loader = OCSBaseLoader(
        experiment_id="exp-1",
        credential={"value": "tok"},
    )
    assert loader._session.headers["Authorization"] == "Bearer tok"
