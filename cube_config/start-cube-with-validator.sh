#!/bin/bash
set -euo pipefail

validator_pid=""
cube_pid=""

shutdown() {
  local status=${1:-0}

  if [ -n "${validator_pid}" ] && kill -0 "${validator_pid}" 2>/dev/null; then
    kill "${validator_pid}" 2>/dev/null || true
  fi

  if [ -n "${cube_pid}" ] && kill -0 "${cube_pid}" 2>/dev/null; then
    kill "${cube_pid}" 2>/dev/null || true
  fi

  wait "${validator_pid}" 2>/dev/null || true
  wait "${cube_pid}" 2>/dev/null || true

  exit "${status}"
}

trap 'shutdown 143' TERM INT

export CUBEJS_EXTERNAL_DEFAULT=${CUBEJS_EXTERNAL_DEFAULT:-false}
export CUBEJS_PRE_AGGREGATIONS_SCHEMA=${CUBEJS_PRE_AGGREGATIONS_SCHEMA:-false}

echo "[INFO] Starting Cube validator on port ${CUBE_VALIDATOR_PORT:-4010}"
node /cube/conf/validator-server.js &
validator_pid=$!

echo "[INFO] Starting Cube server on port ${CUBEJS_PORT:-4000}"
node /cube/node_modules/.bin/cubejs-server &
cube_pid=$!

while true; do
  if ! kill -0 "${validator_pid}" 2>/dev/null; then
    status=0
    wait "${validator_pid}" || status=$?
    echo "[ERROR] Cube validator exited with status ${status}"
    shutdown "${status}"
  fi

  if ! kill -0 "${cube_pid}" 2>/dev/null; then
    status=0
    wait "${cube_pid}" || status=$?
    echo "[ERROR] Cube server exited with status ${status}"
    shutdown "${status}"
  fi

  sleep 1
done
