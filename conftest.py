import os

# dbt sends anonymous usage stats to dbt Labs on every invocation. Read per
# invocation, so setting it at conftest import is early enough.
os.environ["DO_NOT_TRACK"] = "1"

# Registered from the rootdir conftest so the guard also covers apps/*/tests and
# single-file runs that never load tests/conftest.py.
pytest_plugins = ["tests.network_guard"]
