#!/bin/bash
# Run on the deployment host as the scout account, before any image is pulled.
#
#   host-disk-guard.sh prune-workers   Remove old stopped worker containers when no
#                                      drain receipt could reference them.
#   host-disk-guard.sh check <min-gb>  Fail unless Docker's filesystem has <min-gb> free.
#
# A full disk once failed every production deploy for six days: the first pull
# died with "no space left on device", so Kamal's end-of-deploy prune never ran.
set -euo pipefail
shopt -s nullglob dotglob

# Stopped workers to keep per destination, matching retain_containers elsewhere.
WORKER_RETAIN=3

run_docker() { timeout --foreground 60s docker "$@"; }

prune_workers() {
  local account home destination label stopped not_removed
  # Worker deploys skip Kamal's service-wide prune so a stopped worker named by
  # either destination's pending drain receipt survives (DEPLOYMENT.md). Only
  # prune when no receipt exists anywhere; otherwise leave cleanup to an operator.
  account=$(timeout --foreground 15s getent passwd scout) || {
    echo "::warning title=Worker prune skipped::Could not resolve the scout account."
    return 0
  }
  # Same record and legacy-path checks as drain-workers.sh: a malformed record
  # or unreadable .kamal would hide receipts and make this guard fail open.
  if [[ "$account" == *$'\n'* || "$account" == *$'\r'* || \
        ! "$account" =~ ^scout:[^:]*:[0-9]+:[0-9]+:[^:]*:[^:]+:[^:]*$ ]]; then
    echo "::warning title=Worker prune skipped::Invalid scout account record."
    return 0
  fi
  home=$(cut -d: -f6 <<< "$account")
  local kamal="$home/.kamal"
  if [[ "$home" != /* || "$home" == / || -L "$home" || ! -d "$home" || ! -r "$home" || ! -x "$home" || \
        -L "$kamal" || \
        ( -e "$kamal" && ( ! -d "$kamal" || ! -r "$kamal" || ! -x "$kamal" ) ) || \
        -e "$kamal/scout-worker-drains-v1" || -L "$kamal/scout-worker-drains-v1" ]]; then
    echo "::warning title=Worker prune skipped::Unreadable home, or legacy or unreadable drain metadata; inspect it before pruning workers."
    return 0
  fi
  local root="$home/.scout-worker-drains-v1"
  if [[ -e "$root" || -L "$root" ]]; then
    if [[ -L "$root" || ! -d "$root" || ! -r "$root" || ! -x "$root" ]]; then
      echo "::warning title=Worker prune skipped::Drain receipt root is not a readable directory."
      return 0
    fi
    local entry
    for entry in "$root"/*; do
      if [[ -L "$entry" || ! -d "$entry" || ! -r "$entry" || ! -x "$entry" ]]; then
        echo "::warning title=Worker prune skipped::Unexpected drain metadata at $entry."
        return 0
      fi
      local receipts=("$entry"/*)
      if (( ${#receipts[@]} > 0 )); then
        echo "::warning title=Worker prune skipped::Pending worker drain receipts exist; stopped workers are kept for inspection."
        return 0
      fi
    done
  fi

  for destination in production staging; do
    [[ "$destination" == production ]] && label="" || label="staging"
    # docker ps lists newest first, so everything after the first N is older.
    stopped=$(run_docker ps --all --no-trunc --quiet \
      --filter label=service=scout-worker --filter "label=destination=$label" \
      --filter status=created --filter status=exited --filter status=dead) || {
      echo "::warning title=Worker prune incomplete::Could not list stopped $destination workers."
      continue
    }
    # A container that is already gone is fine; report every other failure,
    # including an unreachable daemon or a timeout.
    not_removed=$(tail -n "+$((WORKER_RETAIN + 1))" <<< "$stopped" | while read -r container; do
      [[ -n "$container" ]] || continue
      if ! error=$(run_docker rm "$container" 2>&1 >/dev/null) && \
         [[ "$error" != *"No such container"* ]]; then
        echo "$container"
      fi
    done)
    if [[ -n "$not_removed" ]]; then
      echo "::warning title=Worker prune incomplete::$(wc -l <<< "$not_removed" | tr -d ' ') stopped $destination worker(s) could not be removed."
    fi
  done
  # Only images no container references; stopped rollback containers keep theirs.
  run_docker image prune --force >/dev/null
}

check() {
  local min_gb=${1:-}
  [[ "$min_gb" =~ ^[1-9][0-9]{0,3}$ ]] || { echo "Invalid minimum free space: $min_gb" >&2; exit 2; }
  local root available_kb available_gb
  root=$(run_docker info --format '{{.DockerRootDir}}' 2>/dev/null) || root=/
  [[ -e "$root" ]] || root=/
  available_kb=$(df -Pk -- "$root" | awk 'NR == 2 { print $4 }') || available_kb=""
  [[ "$available_kb" =~ ^[0-9]+$ ]] || {
    echo "::error title=Host disk check failed::Could not read free space for $root. Follow DEPLOYMENT.md: Host disk full."
    exit 1
  }
  available_gb=$((available_kb / 1024 / 1024))
  df -Ph -- "$root"
  run_docker system df || true
  if (( available_gb < min_gb )); then
    echo "::error title=Host disk nearly full::Only ${available_gb} GB free on the deploy host (need ${min_gb} GB) after pruning. Deploying now would fail mid-pull. Follow DEPLOYMENT.md: Host disk full."
    exit 1
  fi
  if (( available_gb < 2 * min_gb )); then
    echo "::warning title=Host disk getting full::${available_gb} GB free on the deploy host after pruning."
  fi
  echo "${available_gb} GB free on the deploy host."
}

case "${1:-}" in
  prune-workers) prune_workers ;;
  check) check "${2:-}" ;;
  *) echo "Usage: $0 prune-workers | check <min-gb>" >&2; exit 2 ;;
esac
