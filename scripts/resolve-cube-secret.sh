#!/bin/bash
# Resolve the environment's Cube JWT signing key without making staging depend
# on the production secret. Staging injects SCOUT_CUBEJS_API_SECRET directly;
# production follows the normal AWS Secrets Manager -> Kamal chain.

set -euo pipefail

if [[ -n "${SCOUT_CUBEJS_API_SECRET:-}" ]]; then
  printf '%s' "$SCOUT_CUBEJS_API_SECRET"
  exit 0
fi

cube_secrets="$(
  kamal secrets fetch \
    --adapter aws_secrets_manager \
    SCOUT_CUBEJS_API_SECRET
)"
kamal secrets extract SCOUT_CUBEJS_API_SECRET "$cube_secrets"
