#!/bin/bash
# Run on the deployment host. Never invoke this against an unselected destination.
set -euo pipefail
umask 077
shopt -s nullglob dotglob

destination=${1:?Specify production or staging}
drain_timeout=${2:-600}
case "$destination" in
  production) destination_label="" ;;
  staging) destination_label="staging" ;;
  *) echo "Refusing an unknown worker destination." >&2; exit 2 ;;
esac
if [[ ! "$drain_timeout" =~ ^[1-9][0-9]{0,2}$ ]] || (( drain_timeout > 600 )); then
  echo "The graceful drain budget must be between 1 and 600 seconds." >&2
  exit 2
fi

fail() {
  echo "$* No new publishers may start; do not restart old workers after new provenance is published." >&2
  exit 1
}

# Bound Docker control calls too. Timing out a client never sends a second
# container signal; an uncertain signal request is observation-only on retry.
run_docker() { timeout --foreground 15s docker "$@"; }

active_workers() {
  run_docker ps --all --no-trunc --quiet \
    --filter label=service=scout-worker --filter label=role=web \
    --filter "label=destination=$destination_label" \
    --filter status=running --filter status=restarting --filter status=paused
}

inspect_worker() {
  local expected_id=$1 details
  [[ "$expected_id" =~ ^[0-9a-f]{64}$ ]] || fail "Invalid worker container identity."
  details=$(run_docker inspect --format \
    '{{.Id}}|{{index .Config.Labels "service"}}|{{index .Config.Labels "role"}}|{{if index .Config.Labels "destination"}}{{index .Config.Labels "destination"}}{{end}}|{{.State.Status}}|{{.State.StartedAt}}|{{.State.ExitCode}}|{{.State.OOMKilled}}|{{range $key, $value := .Config.Labels}}{{if eq $key "destination"}}present{{end}}{{end}}' \
    "$expected_id") || fail "Could not verify the selected worker."
  [[ "$details" != *$'\n'* ]] || fail "Ambiguous worker inspection."
  IFS='|' read -r container_id container_service container_role container_destination \
    container_state container_start container_exit container_oom destination_presence <<< "$details"
  [[ "$container_id" == "$expected_id" && "$container_service" == "scout-worker" && \
     "$container_role" == "web" && "$container_destination" == "$destination_label" && \
     "$destination_presence" == "present" ]] \
    || fail "Worker labels do not match the selected destination."
  [[ "$container_start" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?Z$ ]] \
    || fail "Invalid worker process start time."
  [[ "$container_exit" =~ ^[0-9]+$ && "$container_oom" =~ ^(true|false)$ ]] \
    || fail "Invalid worker exit state."
}

# Receipts live on the deployment host, not inside containers: a failed/stopped
# container must remain a blocker on the next invocation. SSH starts in the
# deployment user's home directory; manual host invocations must use that same
# directory. Never follow links, chmod existing metadata, or broadly clean it up.
validate_directory() {
  local directory=$1 private=$2 mode
  [[ -d "$directory" && ! -L "$directory" && -O "$directory" ]] \
    || fail "Drain metadata must be an owned real directory."
  # GNU stat on the deployment host; BSD stat also supports owned local tests.
  # Permission bits include the ACL mask, so group/other writes cannot bypass it.
  mode=$(stat -c '%a' -- "$directory" 2>/dev/null) \
    || mode=$(stat -f '%Lp' -- "$directory") \
    || fail "Could not verify drain metadata permissions."
  [[ "$mode" =~ ^[0-7]{3,4}$ ]] || fail "Invalid drain metadata permissions."
  (( (8#$mode & 0022) == 0 )) || fail "Drain metadata has unsafe permissions."
  [[ "$private" == false || "$mode" == 700 ]] \
    || fail "Pending drain receipts must be private."
}

ensure_directory() {
  local directory=$1 private=$2
  if [[ ! -e "$directory" && ! -L "$directory" ]]; then
    mkdir -- "$directory" || fail "Could not create private drain metadata."
  fi
  validate_directory "$directory" "$private"
}

ensure_directory .kamal false
ensure_directory .kamal/scout-worker-drains-v1 true
receipt_directory=".kamal/scout-worker-drains-v1/$destination"
ensure_directory "$receipt_directory" true

validate_receipt() {
  local receipt=$1 name children
  name=${receipt##*/}
  [[ "$name" =~ ^[0-9a-f]{64}-[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?Z$ ]] \
    || fail "Malformed pending worker drain receipt; inspect it before deploying."
  validate_directory "$receipt" true
  children=("$receipt"/*)
  (( ${#children[@]} == 0 )) || fail "Pending worker drain receipt contains unexpected data."
}

targets=()
starts=()
receipts=()
add_target() {
  local target=$1 started=$2 index
  for index in "${!targets[@]}"; do
    if [[ "${targets[$index]}" == "$target" ]]; then
      [[ "${starts[$index]}" == "$started" ]] || fail "An old worker restarted after a pending drain."
      return
    fi
  done
  targets+=("$target")
  starts+=("$started")
  receipts+=("$receipt_directory/$target-$started")
}

validate_process() {
  local expected_start=$1
  [[ "$container_start" == "$expected_start" ]] || fail "An old worker restarted during drain."
  case "$container_state" in
    running) ;;
    exited)
      [[ "$container_exit" == 0 && "$container_oom" == false ]] \
        || fail "An old worker exited unsuccessfully; verify its jobs before deploying."
      ;;
    *) fail "An old worker is paused, restarting, or did not exit cleanly." ;;
  esac
}

# Validate all pending receipts before signaling any newly discovered worker.
# A missing container, changed process, or failed exit is retained and blocks
# every retry. Only the selected destination's private metadata is inspected.
for receipt in "$receipt_directory"/*; do
  validate_receipt "$receipt"
  name=${receipt##*/}
  inspect_worker "${name:0:64}"
  validate_process "${name:65}"
  add_target "$container_id" "$container_start"
done

# Resolve every active old version as well as the persisted pending versions.
# Inspect only state/identity, never container environment or application logs.
snapshot=$(active_workers) || fail "Could not inventory old workers."
while IFS= read -r candidate; do
  [[ -n "$candidate" ]] || continue
  inspect_worker "$candidate"
  [[ "$container_state" == "running" ]] \
    || fail "An old worker is paused or restarting; inspect it before deploying."
  add_target "$container_id" "$container_start"
done <<< "$snapshot"

deadline=$((SECONDS + drain_timeout))
for index in "${!targets[@]}"; do
  inspect_worker "${targets[$index]}"
  validate_process "${starts[$index]}"

  # The first Procrastinate SIGTERM stops claiming work and waits for current
  # jobs. A second signal can interrupt them. mkdir is an atomic pending receipt
  # before the signal, scoped to this exact process across workflow retries. An
  # uncertain/crashed attempt may leave an unsignaled receipt: never guess or
  # signal again; observation-only retries will time out for operator inspection.
  receipt=${receipts[$index]}
  if [[ -e "$receipt" || -L "$receipt" ]]; then
    validate_receipt "$receipt"
    echo "Already draining worker ${container_id:0:12}; waiting without another signal."
  elif mkdir -- "$receipt" 2>/dev/null; then
    validate_receipt "$receipt"
    inspect_worker "${targets[$index]}"
    validate_process "${starts[$index]}"
    # A worker may have exited cleanly before the receipt was created.
    [[ "$container_state" == running ]] || continue
    # Docker records a manual stop for SIGTERM, preventing unless-stopped from
    # restarting the worker after its current jobs finish. Never use SIGKILL.
    run_docker kill --signal=TERM "$container_id" >/dev/null \
      || fail "The graceful signal was not confirmed; inspect before retrying."
  else
    # Another serialized/retried invocation may have won the atomic mkdir.
    validate_receipt "$receipt"
    echo "Already draining worker ${container_id:0:12}; waiting without another signal."
  fi
done

while :; do
  remaining=0
  for index in "${!targets[@]}"; do
    inspect_worker "${targets[$index]}"
    validate_process "${starts[$index]}"
    case "$container_state" in
      running) remaining=$((remaining + 1)) ;;
    esac
  done
  current=$(active_workers) || fail "Could not verify the final worker inventory."
  while IFS= read -r candidate; do
    [[ -n "$candidate" ]] || continue
    known=false
    for index in "${!targets[@]}"; do
      [[ "$candidate" != "${targets[$index]}" ]] || known=true
    done
    [[ "$known" == true ]] || fail "A different old worker appeared during drain."
  done <<< "$current"
  if (( remaining == 0 )) && [[ -z "$current" ]]; then
    # Clear only exact validated clean-process receipts, never failed, missing,
    # restarted, malformed, or other-destination metadata. A retry after a
    # partial cleanup still validates every remaining receipt before success.
    for index in "${!targets[@]}"; do
      inspect_worker "${targets[$index]}"
      validate_process "${starts[$index]}"
      [[ "$container_state" == exited ]] || fail "An old worker became active before drain completion."
      validate_receipt "${receipts[$index]}"
    done
    for index in "${!receipts[@]}"; do
      rmdir -- "${receipts[$index]}" || fail "Could not clear the exact completed drain receipt."
    done
    echo "All selected $destination workers exited cleanly. Queued jobs remain for the new worker."
    exit 0
  fi
  if (( SECONDS >= deadline )); then
    fail "Graceful worker drain timed out. In-flight jobs may still finish; queued jobs wait."
  fi
  sleep 1
done
