#!/usr/bin/env bash
# Run L1 bidirectional E2E in a loop until L1_VERIFIED_EVIDENCE.md shows success.
# ONE path at a time: physical (preferred) OR simulator (fallback when iPhone absent).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REDWALLET="${REDWALLET_ROOT:-/Volumes/T705/code/work-on-something-to-do-with/redwallet}"
EVIDENCE="${REDWALLET_LOG_ROOT:-/Volumes/T705/redwallet-logs}/L1_VERIFIED_EVIDENCE.md"
LOG_ROOT="${REDWALLET_LOG_ROOT:-/Volumes/T705/redwallet-logs}"
MAX_ATTEMPTS="${L1_E2E_MAX_ATTEMPTS:-9999}"
SLEEP_BETWEEN="${L1_E2E_RETRY_SLEEP:-90}"
ANDROID_SERIAL="${ANDROID_SERIAL:-${REDWALLET_ANDROID_SERIAL:-0A201JECB03306}}"

# Known Android receive address from recent successful seed (skip re-seed when set).
DEFAULT_ANDROID_RECEIVE="${ANDROID_L1_RECEIVE_ADDRESS:-tb1qewdkqej3xc6hh2v5q88rnaekd2zkccf0zq6kdf}"

goal_complete() {
  python3 "$ROOT/scripts/verify-goal-status.py" --json 2>/dev/null \
    | python3 -c "import json,sys; g=json.load(sys.stdin); sys.exit(0 if any(x['id']=='l1-e2e-verified' and x['complete'] for x in g['goals']) else 1)"
}

kill_physical_orchestrators() {
  echo "Stopping physical iPhone L1 orchestrators (LiPhone unplugged / simulator-only)"
  PYTHONPATH="${ROOT}:${PYTHONPATH:-}" python3 -m autocode coord kill-physical-l1 2>/dev/null || {
    pkill -f 'run-l1-physical-bidirectional' 2>/dev/null || true
    pkill -f 'run-l1-ios-phone' 2>/dev/null || true
    pkill -f 'run-l1-android-phone-to-ios-phone' 2>/dev/null || true
  }
  sleep 2
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

  echo "Physical iOS unavailable — simulator-only bidirectional (iOS sim → Android, then Android → iOS sim)"
  kill_physical_orchestrators
  export L1_E2E_FORCE_PATH="${L1_E2E_FORCE_PATH:-simulator}"
  export L1_E2E_SKIP_PHYSICAL_IOS="${L1_E2E_SKIP_PHYSICAL_IOS:-1}"
  export REDWALLET_SKIP_ANDROID_SEED="${REDWALLET_SKIP_ANDROID_SEED:-1}"
  export REDWALLET_SKIP_IOS_SEED="${REDWALLET_SKIP_IOS_SEED:-1}"
  export L1_E2E_BALANCE_WAIT_MS="${L1_E2E_BALANCE_WAIT_MS:-120000}"
  export L1_E2E_POST_FUND_RELAUNCH="${L1_E2E_POST_FUND_RELAUNCH:-1}"
  bash "$ROOT/scripts/l1-e2e-autocode-preflight.sh" || {
    echo "FAIL autocode preflight — retry after infra ready"
    return 2
  }
  PYTHONPATH="${ROOT}:${PYTHONPATH:-}" python3 -m autocode coord pause-l1-competitors 2>/dev/null || true
  local stamp run_dir
  stamp="$(date +%Y%m%d-%H%M%S)"
  run_dir="$LOG_ROOT/l1-simulator-bidirectional-e2e-$stamp"
  mkdir -p "$run_dir"
  ln -sfn "$run_dir" "${LOG_ROOT%/}/current-l1-simulator-bidirectional-e2e"

  # shellcheck source=/dev/null
  source "$REDWALLET/scripts/l1-e2e-lock.sh"
  l1_lock_maybe_acquire "$run_dir" "simulator-bidirectional" || {
    echo "FAIL another L1 E2E run holds the lock"
    return 2
  }

  export REDWALLET_SKIP_ANDROID_SEED="${REDWALLET_SKIP_ANDROID_SEED:-1}"
  export REDWALLET_SKIP_IOS_SEED="${REDWALLET_SKIP_IOS_SEED:-1}"
  export ANDROID_L1_RECEIVE_ADDRESS="${ANDROID_L1_RECEIVE_ADDRESS:-$DEFAULT_ANDROID_RECEIVE}"
  export L1_RECEIVE_ADDRESS="${L1_RECEIVE_ADDRESS:-$ANDROID_L1_RECEIVE_ADDRESS}"
  export L1_E2E_BALANCE_WAIT_MS="${L1_E2E_BALANCE_WAIT_MS:-120000}"
  export L1_E2E_POST_FUND_RELAUNCH="${L1_E2E_POST_FUND_RELAUNCH:-1}"

  L1_E2E_SKIP_LOCK=1 \
    L1_IOS_ANDROID_E2E_LOG_DIR="$run_dir/ios-to-android" \
    L1_E2E_BIDIRECTIONAL=0 \
    REDWALLET_SKIP_ANDROID_SEED="$REDWALLET_SKIP_ANDROID_SEED" \
    ANDROID_L1_RECEIVE_ADDRESS="$ANDROID_L1_RECEIVE_ADDRESS" \
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
