"""Every Python Postgres connection to a remote host verifies the RDS certificate."""

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from psycopg.conninfo import conninfo_to_dict

from apps.chat import checkpointer
from apps.common import db_urls
from apps.common.db_urls import (
    DB_SSL_ROOT_CERT_ENV,
    DEFAULT_DB_SSL_ROOT_CERT,
    DatabaseTLSConfigError,
    enforce_db_tls,
    enforce_db_tls_conninfo,
    enforce_django_db_tls,
    is_local_db_host,
)
from apps.workspaces.services import schema_manager
from mcp_server.context import _parse_db_url
from mcp_server.services.pool import _base_conninfo

REPO = Path(__file__).resolve().parents[1]
REMOTE = "postgresql://u:p@scout.abc123.us-east-1.rds.amazonaws.com:5432/scout"
CA = str(DEFAULT_DB_SSL_ROOT_CERT)
# Same pin as cube_config/cube.test.js: one committed bundle serves Cube and Python.
BUNDLE_SHA256 = "fe45bbebf92ad3e27a583bbb2ddd1553c521ed4d49af5514dc0a40372ea5395c"


@pytest.fixture(autouse=True)
def _default_root_cert(monkeypatch):
    monkeypatch.delenv(DB_SSL_ROOT_CERT_ENV, raising=False)


@pytest.mark.parametrize(
    ("host", "local"),
    [
        ("localhost", True),
        ("127.0.0.1", True),
        ("::1", True),
        ("platform-db", True),
        ("", True),
        (None, True),
        ("/var/run/postgresql", True),
        ("LOCALHOST", True),
        ("scout.abc123.us-east-1.rds.amazonaws.com", False),
        ("db.example", False),
        ("10.0.0.5", False),
        ("localhost,db.example", False),
    ],
)
def test_is_local_db_host(host, local):
    assert is_local_db_host(host) is local


def test_bundle_is_the_pinned_rds_bundle_shipped_in_the_python_image():
    assert hashlib.sha256(DEFAULT_DB_SSL_ROOT_CERT.read_bytes()).hexdigest() == BUNDLE_SHA256
    # The image gets the bundle via `COPY . .`; an ignore rule would drop it silently.
    assert "\nCOPY . .\n" in (REPO / "Dockerfile").read_text()
    ignored = (REPO / ".dockerignore").read_text().splitlines()
    assert not [
        rule for rule in ignored if rule.strip() and ("cube_config" in rule or ".pem" in rule)
    ]


def test_remote_params_forced_to_verify_full():
    params = {"host": "db.example", "sslmode": "disable", "sslrootcert": "/elsewhere/other.pem"}
    assert enforce_db_tls(params) == {
        "host": "db.example",
        "sslmode": "verify-full",
        "sslrootcert": CA,
    }


@pytest.mark.parametrize(("key", "value"), [("hostaddr", "10.0.0.5"), ("service", "prod")])
def test_local_name_with_remote_target_is_forced(key, value):
    assert enforce_db_tls({"host": "localhost", key: value})["sslmode"] == "verify-full"
    db = {"HOST": "localhost", "OPTIONS": {key: value, "sslmode": "disable"}}
    assert enforce_django_db_tls(db)["OPTIONS"]["sslmode"] == "verify-full"
    url = f"postgresql://u@localhost/scout?sslmode=disable&{key}={value}"
    assert conninfo_to_dict(enforce_db_tls_conninfo(url))["sslmode"] == "verify-full"


@pytest.mark.parametrize("var", ["PGHOST", "PGHOSTADDR", "PGSERVICE"])
def test_empty_host_resolved_remote_by_env_is_forced(monkeypatch, var):
    monkeypatch.setenv(var, "db.example" if var != "PGHOSTADDR" else "10.0.0.5")
    assert enforce_db_tls({"host": ""})["sslmode"] == "verify-full"


def test_loopback_hostaddr_stays_local():
    params = {"host": "localhost", "hostaddr": "127.0.0.1"}
    assert enforce_db_tls(params) is params


def test_local_params_unchanged():
    params = {"host": "localhost", "sslmode": "disable"}
    assert enforce_db_tls(params) is params


@pytest.mark.parametrize("query", ["", "?sslmode=disable", "?sslmode=prefer", "?sslmode=require"])
def test_conninfo_remote_forced(query):
    parsed = conninfo_to_dict(enforce_db_tls_conninfo(REMOTE + query))
    assert parsed["sslmode"] == "verify-full"
    assert parsed["sslrootcert"] == CA
    assert parsed["password"] == "p"


def test_conninfo_local_returned_verbatim():
    url = "postgresql://u:p@platform-db:5432/scout?sslmode=disable"
    assert enforce_db_tls_conninfo(url) == url


def test_django_entry_remote_forced_and_other_options_kept():
    db = {"HOST": "db.example", "OPTIONS": {"sslmode": "disable", "connect_timeout": 5}}
    assert enforce_django_db_tls(db)["OPTIONS"] == {
        "sslmode": "verify-full",
        "sslrootcert": CA,
        "connect_timeout": 5,
    }


def test_django_entry_local_unchanged():
    db = {"HOST": "localhost", "OPTIONS": {"sslmode": "disable"}}
    assert enforce_django_db_tls(db) is db


def test_override_env_selects_bundle(monkeypatch, tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text(DEFAULT_DB_SSL_ROOT_CERT.read_text())
    monkeypatch.setenv(DB_SSL_ROOT_CERT_ENV, str(ca))
    assert enforce_db_tls({"host": "db.example"})["sslrootcert"] == str(ca)


def test_missing_bundle_fails_for_remote(monkeypatch, tmp_path):
    monkeypatch.setenv(DB_SSL_ROOT_CERT_ENV, str(tmp_path / "missing.pem"))
    with pytest.raises(DatabaseTLSConfigError, match="missing.pem"):
        enforce_db_tls({"host": "db.example"})
    with pytest.raises(DatabaseTLSConfigError):
        enforce_db_tls_conninfo(REMOTE)
    with pytest.raises(DatabaseTLSConfigError):
        enforce_django_db_tls({"HOST": "db.example"})
    # A local host never needs the bundle.
    assert enforce_db_tls({"host": "localhost"}) == {"host": "localhost"}


def test_bundle_without_certificates_fails(monkeypatch, tmp_path):
    empty = tmp_path / "empty.pem"
    empty.write_text("")
    monkeypatch.setenv(DB_SSL_ROOT_CERT_ENV, str(empty))
    with pytest.raises(DatabaseTLSConfigError, match="no certificates"):
        db_urls.db_ssl_root_cert()


def test_checkpointer_database_url_forced(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", REMOTE + "?sslmode=disable")
    parsed = conninfo_to_dict(checkpointer.get_database_url())
    assert (parsed["sslmode"], parsed["sslrootcert"]) == ("verify-full", CA)


def test_checkpointer_django_fallback_forced(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    databases = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "scout",
            "HOST": "db.example",
            "PORT": 5432,
            "USER": "u",
            "PASSWORD": "p",
        }
    }
    monkeypatch.setattr(checkpointer.settings, "DATABASES", databases)
    parsed = conninfo_to_dict(checkpointer.get_database_url())
    assert (parsed["sslmode"], parsed["sslrootcert"]) == ("verify-full", CA)


def test_checkpointer_local_url_unchanged(monkeypatch):
    url = "postgresql://u:p@localhost:5432/scout"
    monkeypatch.setenv("DATABASE_URL", url)
    assert checkpointer.get_database_url() == url


def test_managed_connection_forced(settings):
    settings.MANAGED_DATABASE_URL = REMOTE + "?sslmode=prefer"
    with mock.patch.object(schema_manager.psycopg, "connect") as connect:
        schema_manager.get_managed_db_connection()
    parsed = conninfo_to_dict(connect.call_args.args[0])
    assert (parsed["sslmode"], parsed["sslrootcert"]) == ("verify-full", CA)


def test_mcp_pool_conninfo_carries_root_cert():
    parsed = conninfo_to_dict(_base_conninfo(_parse_db_url(REMOTE + "?sslmode=disable", "t_x")))
    assert (parsed["sslmode"], parsed["sslrootcert"]) == ("verify-full", CA)


def _production_settings(**env):
    script = (
        "import django; django.setup()\n"
        "from django.conf import settings\n"
        "from procrastinate.contrib.django.utils import connector_params\n"
        "options = settings.DATABASES['default']['OPTIONS']\n"
        "params = connector_params()\n"
        "print(options['sslmode'], options['sslrootcert'], params['sslmode'], params['sslrootcert'])\n"
    )
    base_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "DJANGO_SETTINGS_MODULE": "config.settings.production",
        "DJANGO_SECRET_KEY": "test",
        "MCP_SHARED_SECRET": "test",
        "DATABASE_URL": REMOTE + "?sslmode=disable",
        "PYTHONPATH": str(REPO),
    }
    # S603: fixed interpreter and script, no shell.
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        env={**base_env, **env},
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )


def test_production_settings_and_procrastinate_verify_remote():
    result = _production_settings()
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["verify-full", CA, "verify-full", CA]


def test_production_settings_refuse_to_load_without_bundle(tmp_path):
    result = _production_settings(**{DB_SSL_ROOT_CERT_ENV: str(tmp_path / "missing.pem")})
    assert result.returncode != 0
    assert "DatabaseTLSConfigError" in result.stderr
