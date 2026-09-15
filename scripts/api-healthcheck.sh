#!/bin/sh
set -eu

# Evaluate the host inside the container, not in Kamal's remote shell. The API
# listens only after migrations and OAuth setup have both completed.
health_host=${DJANGO_ALLOWED_HOSTS-}
health_host=${health_host%%,*}
health_host=${health_host#"${health_host%%[![:space:]]*}"}
health_host=${health_host%"${health_host##*[![:space:]]}"}
: "${health_host:?DJANGO_ALLOWED_HOSTS must contain the API host}"
case "$health_host" in
  *[[:space:]]*) echo "The first DJANGO_ALLOWED_HOSTS entry must not contain internal whitespace." >&2; exit 1 ;;
esac
# curl --fail alone accepts redirects without running the readiness view.
# Preserve transport/HTTP failures, and require the endpoint's exact success code.
health_status=$(curl --fail --silent --show-error --max-time 4 --noproxy '*' \
  --header "Host: $health_host" --output /dev/null --write-out '%{http_code}' \
  http://127.0.0.1:8000/health/) || exit "$?"
if [ "$health_status" != 200 ]; then
  echo "Unexpected /health/ status: $health_status" >&2
  exit 1
fi
