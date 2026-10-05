#!/bin/bash
# Resolve the Cube JWT signing key through the AWS Secrets Manager -> Kamal chain.

set -euo pipefail

cube_secrets="$(
  kamal secrets fetch \
    --adapter aws_secrets_manager \
    SCOUT_CUBEJS_API_SECRET
)"
kamal secrets extract SCOUT_CUBEJS_API_SECRET "$cube_secrets"
