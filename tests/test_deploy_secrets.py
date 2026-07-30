"""Guard: every secret a Kamal deploy config references must resolve in .kamal/secrets.

Kamal fails a deploy with ``Secret '<NAME>' not found in .kamal/secrets`` when a
config lists a name under ``env.secret:`` that ``.kamal/secrets`` never defines. This
shipped to prod twice: MCP_SHARED_SECRET (arch #253) and REDIS_URL (arch #254) were
both added to config secret blocks but never wired into .kamal/secrets, and every
production deploy from 2026-06-26 onward silently failed at boot. This test parses the
deploy configs and .kamal/secrets straight off disk (no Kamal/AWS needed) and fails
if any referenced secret is unresolved — catching the whole class before deploy.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _referenced_secret_names(config_text: str) -> list[str]:
    """Return the names listed under a Kamal config's ``env.secret:`` block.

    Parsed textually rather than with a YAML loader because the configs contain ERB
    (``<%= ENV.fetch(...) %>``) that a strict YAML parser rejects. The ``secret:`` list
    items themselves are plain ``- NAME`` entries, with comments interspersed.
    """
    names: list[str] = []
    in_secret = False
    secret_indent = 0
    for line in config_text.splitlines():
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if not in_secret:
            if stripped == "secret:":
                in_secret = True
                secret_indent = indent
            continue
        if not stripped or stripped.startswith("#"):
            continue
        if indent <= secret_indent:  # dedented back out of the block
            in_secret = False
            continue
        match = re.match(r"-\s*([A-Za-z_][A-Za-z0-9_]*)\s*$", stripped)
        if match:
            names.append(match.group(1))
    return names


def _defined_var_names(secrets_text: str) -> set[str]:
    """Return every top-level ``NAME=...`` assignment in .kamal/secrets."""
    return {
        match.group(1)
        for line in secrets_text.splitlines()
        if (match := re.match(r"([A-Za-z_][A-Za-z0-9_]*)=", line))
    }


def test_every_deploy_secret_is_resolved_in_kamal_secrets():
    defined = _defined_var_names((REPO_ROOT / ".kamal" / "secrets").read_text())

    configs = sorted(REPO_ROOT.glob("config/deploy*.yml"))
    assert configs, "no config/deploy*.yml files found — glob or layout changed"

    missing = {
        config.name: gaps
        for config in configs
        if (gaps := [n for n in _referenced_secret_names(config.read_text()) if n not in defined])
    }

    assert not missing, (
        "Deploy config(s) reference secrets that .kamal/secrets does not resolve. Kamal "
        "will fail at deploy with \"Secret '<NAME>' not found in .kamal/secrets\" and the "
        "container won't boot. Add each missing name to .kamal/secrets (fetch from AWS "
        f"Secrets Manager, or derive from an exported env var). Missing: {missing}"
    )
