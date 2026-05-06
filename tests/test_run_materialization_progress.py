"""Tests for the MCP run_materialization tool's progress-formatting helpers."""

from mcp_server.server import _format_progress_message


def test_format_progress_with_total():
    msg = _format_progress_message(
        {
            "message": "Loading sessions from OCS API...",
            "rows_loaded": 500,
            "rows_total": 13028,
            "source": "sessions",
        },
        multi_tenant=False,
    )
    assert msg == "Loading sessions from OCS API... 3.8% (500 / 13,028 rows)"


def test_format_progress_without_total_falls_back_to_count():
    msg = _format_progress_message(
        {
            "message": "Loading sessions from OCS API...",
            "rows_loaded": 500,
            "rows_total": None,
            "source": "sessions",
        },
        multi_tenant=False,
    )
    assert msg == "Loading sessions from OCS API... 500 rows loaded"


def test_format_progress_non_load_phase():
    msg = _format_progress_message(
        {
            "message": "Provisioning schema for my-experiment...",
            "rows_loaded": 0,
            "rows_total": None,
            "source": None,
        },
        multi_tenant=False,
    )
    assert msg == "Provisioning schema for my-experiment..."


def test_format_progress_multi_tenant_prefixes_tenant_id():
    msg = _format_progress_message(
        {
            "message": "Loading sessions from OCS API...",
            "rows_loaded": 100,
            "rows_total": 200,
            "source": "sessions",
            "tenant_id": "exp-1",
        },
        multi_tenant=True,
    )
    assert msg.startswith("[exp-1] ")
    assert "100 / 200 rows" in msg


def test_format_progress_handles_missing_message():
    msg = _format_progress_message(
        {"rows_loaded": 0, "rows_total": None},
        multi_tenant=False,
    )
    assert msg == "Working..."
