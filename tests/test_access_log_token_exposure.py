"""Guard that bearer capabilities don't transit access logs (arch #257, 08#6).

Share-token URLs (``/api/chat/threads/shared/<token>/``,
``/api/recipes/runs/shared/<token>/``) and OAuth callbacks (``?code=``) carry a
bearer capability in the request line. Uvicorn's access log is on by default and
ships to CloudWatch (30-day retention), so anyone with CloudWatch read access
could harvest live share tokens from the access log.

The minimal, grounded fix is to disable uvicorn's access log on the API
container (Django's own request logging never logged these paths, and request
metrics come from elsewhere). These tests pin ``--no-access-log`` on the API
Kamal command so the capability never lands in the access log in the first place.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _load(name: str) -> dict:
    with (_CONFIG_DIR / name).open() as f:
        return yaml.safe_load(f)


def test_api_uvicorn_disables_access_log():
    """The API container's uvicorn command must disable the access log so share
    tokens / OAuth codes in the request line never reach CloudWatch."""
    cfg = _load("deploy.yml")
    cmd = cfg["servers"]["web"]["cmd"]
    assert "uvicorn" in cmd
    assert "--no-access-log" in cmd, (
        "API uvicorn must run with --no-access-log; otherwise share tokens and "
        "OAuth ?code= values in the request line are written to the access log "
        "and shipped to CloudWatch (finding 08#6)."
    )


@pytest.mark.parametrize("name", ["deploy.yml", "deploy-mcp.yml", "deploy-worker.yml"])
def test_no_uvicorn_access_log_anywhere(name):
    """No container should run uvicorn with the access log enabled."""
    cfg = _load(name)
    for server in cfg.get("servers", {}).values():
        cmd = server.get("cmd", "")
        if "uvicorn" in cmd:
            assert "--no-access-log" in cmd, f"{name}: uvicorn access log must be disabled"
