import os

# dbt sends anonymous usage stats to dbt Labs on every invocation. Read per
# invocation, so setting it at conftest import is early enough.
os.environ["DO_NOT_TRACK"] = "1"
