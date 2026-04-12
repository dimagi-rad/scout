#!/bin/bash
# Fetches the RDS password from AWS Secrets Manager and outputs the DATABASE_URL.
#
# Usage: scripts/resolve-database-url.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load infra config from .env.deploy (local) or expect env vars already set (CI)
if [ -f "$PROJECT_ROOT/.env.deploy" ]; then
  source "$PROJECT_ROOT/.env.deploy"
elif [ -z "${SCOUT_RDS_SECRET_ARN:-}" ]; then
  if [ -z "${CI:-}" ]; then
    echo "Generating .env.deploy..." >&2
    "$SCRIPT_DIR/fetch-deploy-env.sh -q" >&2
    source "$PROJECT_ROOT/.env.deploy"
  else
    echo "ERROR: SCOUT_RDS_SECRET_ARN not set in CI environment" >&2
    exit 1
  fi
fi

PROFILE_ARG=""
if [ -z "${CI:-}" ]; then
  PROFILE_ARG="--profile ${AWS_PROFILE:-scout}"
fi

RDS_SECRET=$(aws secretsmanager get-secret-value \
  --secret-id "$SCOUT_RDS_SECRET_ARN" \
  --query SecretString --output text \
  $PROFILE_ARG) || { echo "ERROR: Failed to fetch RDS secret" >&2; exit 1; }

DB_PASSWORD=$(echo "$RDS_SECRET" | python3 -c "import sys,json; print(json.load(sys.stdin)['password'])")
DB_PASSWORD_ENCODED=$(python3 -c "import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=''))" "$DB_PASSWORD")

echo "postgresql://platform:${DB_PASSWORD_ENCODED}@${SCOUT_RDS_ENDPOINT}:5432/agent_platform"
