#!/usr/bin/env bash
# Run L1 bidirectional E2E in a loop until L1_VERIFIED_EVIDENCE.md shows success.
# ONE path at a time: physical (preferred) OR simulator (fallback when BitAssets/devices blocked).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REDWALLET="${REDWALLET_ROOT:-/Volumes/T705/code/work-on-something-to-do-with/redwallet}"
EVIDENCE="${REDWALLET_LOG_ROOT:-/Volumes/T705/redwallet-logs}/L1_VERIFIED_EVIDENCE.md"
MAX_ATTEMPTS="${L1_E2E_MAX_ATTEMPTS:-5}"
SLEEP_BETWEEN="${L1_E2E_RETRY_SLEEP:-120}"

goal_complete() {
  python3 "$ROOT/scripts/verify-goal-status.py" --json 2>/dev/null \
    | python3 -c "import json,sys; g=json.load(sys.stdin); sys.exit(0 if any(x['id']=='l1-e2e-verified' and x['complete'] for x in g['goals']) else 1)"
}

run_orchestrator() {
  local path
  path="$(bash "$ROOT/scripts/pick-l1-e2e-path.sh")"
  echo "l1_path=$path"

  if [[ "$path" == "physical" ]]; then
    PYTHONPATH="${ROOT}:${PYTHONPATH:-}" python3 -m autocode coord pause-l1-competitors 2>/dev/null || true
    bash "$REDWALLET/scripts/run-l1-physical-bidirectional-e2e.sh"
    return $?
  fi

  echo "Physical blocked — using iOS simulator + Android physical path"
  PYTHONPATH="${ROOT}:${PYTHONPATH:-}" python3 -m autocode coord pause-l1-competitors 2>/dev/null || true
  local log_root="${REDWALLET_LOG_ROOT:-/Volumes/T705/redwallet-logs}"
  local stamp run_dir
  stamp="$(date +%Y%m%d-%H%M%S)"
  run_dir="$log_root/l1-simulator-bidirectional-e2e-$stamp"
  mkdir -p "$run_dir"
  ln -sfn "$run_dir" "${log_root%/}/current-l1-simulator-bidirectional-e2e"

  # shellcheck source=/dev/null
  source "$REDWALLET/scripts/l1-e2e-lock.sh"
  l1_lock_maybe_acquire "$run_dir" "simulator-bidirectional" || {
    echo "FAIL another L1 E2E run holds the lock"
    return 2
  }

  L1_E2E_SKIP_LOCK=1 \
    L1_IOS_ANDROID_E2E_LOG_DIR="$run_dir/ios-to-android" \
    L1_E2E_BIDIRECTIONAL=0 \
    bash "$REDWALLET/scripts/run-l1-ios-simulator-to-android-phone-e2e.sh"
  local ios_rc=$?

  L1_E2E_SKIP_LOCK=1 \
    L1_ANDROID_IOS_E2E_LOG_DIR="$run_dir/android-to-ios" \
    bash "$REDWALLET/scripts/run-l1-android-phone-to-ios-simulator-e2e.sh"
  local android_rc=$?

  local final_rc=0
  if [[ "$ios_rc" -ne 0 || "$android_rc" -ne 0 ]]; then
    final_rc=1
  fi
  echo "simulator_bidirectional ios_rc=$ios_rc android_rc=$android_rc final_rc=$final_rc"
  return "$final_rc"
}

attempt=0
while [[ "$attempt" -lt "$MAX_ATTEMPTS" ]]; do
  attempt=$((attempt + 1))
  echo "=== L1 E2E attempt $attempt/$MAX_ATTEMPTS $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

  if goal_complete; then
    echo "Goal 1 already complete."
    exit 0
  fi

  set +e
  run_orchestrator
  rc=$?
  set -e
  echo "orchestrator_exit=$rc"

  if goal_complete; then
    echo "L1 E2E verified."
    exit 0
  fi

  echo "Not verified yet; evidence at $EVIDENCE"
  sleep "$SLEEP_BETWEEN"
done

echo "Max attempts reached without verification."
exit 1
