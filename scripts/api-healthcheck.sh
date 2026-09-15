#!/bin/sh
set -eu

# Evaluate the host inside the container, not in Kamal's remote shell. The API
# listens only after migrations and OAuth setup have both completed.
health_host=${DJANGO_ALLOWED_HOSTS%%,*}
: "${health_host:?DJANGO_ALLOWED_HOSTS must contain the API host}"
exec curl --fail --silent --show-error --max-time 4 --noproxy '*' \
  --header "Host: $health_host" --output /dev/null http://127.0.0.1:8000/health/
