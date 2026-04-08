#!/bin/bash
# Fetches all deployment secrets from AWS Secrets Manager and outputs a single
# requested value. Caches results in a temp file so repeated calls (one per
# secret in .kamal/secrets) don't re-fetch from AWS.
#
# Usage: scripts/resolve-secrets.sh <KEY>
# Keys: DATABASE_URL, MANAGED_DATABASE_URL, DJANGO_SECRET_KEY, DB_CREDENTIAL_KEY, ANTHROPIC_API_KEY, SENTRY_DSN
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CACHE_FILE="/tmp/scout-deploy-secrets.env"
REQUESTED_KEY="${1:?Usage: resolve-secrets.sh <KEY>}"

# Build the cache if it doesn't exist or is stale (>5 min)
if [ ! -f "$CACHE_FILE" ] || [ "$(find "$CACHE_FILE" -mmin +5 2>/dev/null)" ]; then
  # Load infra config
  if [ -f "$PROJECT_ROOT/.env.deploy" ]; then
    source "$PROJECT_ROOT/.env.deploy"
  else
    echo "ERROR: $PROJECT_ROOT/.env.deploy not found" >&2
    exit 1
  fi

  PROFILE_ARG=""
  if [ -z "${CI:-}" ]; then
    PROFILE_ARG="--profile ${AWS_PROFILE:-scout}"
  fi

  # RDS password
  RDS_SECRET=$(aws secretsmanager get-secret-value \
    --secret-id "$SCOUT_RDS_SECRET_ARN" \
    --query SecretString --output text \
    $PROFILE_ARG) || { echo "ERROR: Failed to fetch RDS secret" >&2; exit 1; }

  DB_PASSWORD=$(echo "$RDS_SECRET" | python3 -c "import sys,json; print(json.load(sys.stdin)['password'])")
  DB_PASSWORD_ENCODED=$(python3 -c "import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=''))" "$DB_PASSWORD")
  DATABASE_URL="postgresql://platform:${DB_PASSWORD_ENCODED}@${SCOUT_RDS_ENDPOINT}:5432/agent_platform"

  # Django secrets
  DJANGO_SECRETS=$(aws secretsmanager get-secret-value \
    --secret-id "scout/django" \
    --query SecretString --output text \
    $PROFILE_ARG) || { echo "ERROR: Failed to fetch Django secrets" >&2; exit 1; }

  DJANGO_SECRET_KEY=$(echo "$DJANGO_SECRETS" | python3 -c "import sys,json; print(json.load(sys.stdin)['SCOUT_DJANGO_SECRET_KEY'])")
  DB_CREDENTIAL_KEY=$(echo "$DJANGO_SECRETS" | python3 -c "import sys,json; print(json.load(sys.stdin)['SCOUT_DB_CREDENTIAL_KEY'])")

  # API keys
  API_SECRETS=$(aws secretsmanager get-secret-value \
    --secret-id "scout/api-keys" \
    --query SecretString --output text \
    $PROFILE_ARG) || { echo "ERROR: Failed to fetch API keys" >&2; exit 1; }

  ANTHROPIC_API_KEY=$(echo "$API_SECRETS" | python3 -c "import sys,json; print(json.load(sys.stdin)['SCOUT_ANTHROPIC_API_KEY'])")
  SENTRY_DSN=$(echo "$API_SECRETS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('SCOUT_SENTRY_DSN', ''))")

  # Write cache
  cat > "$CACHE_FILE" <<CACHE
DATABASE_URL=$DATABASE_URL
MANAGED_DATABASE_URL=$DATABASE_URL
DJANGO_SECRET_KEY=$DJANGO_SECRET_KEY
DB_CREDENTIAL_KEY=$DB_CREDENTIAL_KEY
ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
SENTRY_DSN=$SENTRY_DSN
CACHE
fi

# Output the requested key's value
VALUE=$(grep "^${REQUESTED_KEY}=" "$CACHE_FILE" | cut -d= -f2-)
if [ -z "$VALUE" ] && [ "$REQUESTED_KEY" != "SENTRY_DSN" ]; then
  echo "ERROR: $REQUESTED_KEY not found in secrets cache" >&2
  exit 1
fi
echo "$VALUE"
