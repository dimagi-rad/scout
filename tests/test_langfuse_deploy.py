"""Fitness tests for how production ships Langfuse traces."""

from tests.kamal_config import base_config_names, load_config


def test_every_langfuse_role_gzips_trace_exports():
    tracing_roles = []
    for name in base_config_names():
        clear = (load_config(name).get("env") or {}).get("clear") or {}
        if "LANGFUSE_BASE_URL" in clear:
            tracing_roles.append(name)
            assert clear.get("OTEL_EXPORTER_OTLP_TRACES_COMPRESSION") == "gzip", name

    assert tracing_roles == ["deploy-worker.yml", "deploy.yml"]
