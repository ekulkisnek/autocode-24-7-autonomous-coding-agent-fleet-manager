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
    set +e
    bash "$REDWALLET/scripts/run-l1-physical-bidirectional-e2e.sh"
    local rc=$?
    set -e
    return "$rc"
  fi

  echo "Physical iOS unavailable — simulator-only bidirectional (iOS sim → Android, then Android → iOS sim)"
  kill_physical_orchestrators
  export L1_E2E_FORCE_PATH="${L1_E2E_FORCE_PATH:-simulator}"
  export L1_E2E_SKIP_PHYSICAL_IOS="${L1_E2E_SKIP_PHYSICAL_IOS:-1}"
  export REDWALLET_SKIP_ANDROID_SEED="${REDWALLET_SKIP_ANDROID_SEED:-1}"
  export REDWALLET_SKIP_IOS_SEED="${REDWALLET_SKIP_IOS_SEED:-1}"
  export L1_E2E_BALANCE_WAIT_MS="${L1_E2E_BALANCE_WAIT_MS:-180000}"
  export L1_E2E_POST_FUND_RELAUNCH="${L1_E2E_POST_FUND_RELAUNCH:-1}"
  export L1_E2E_POST_FUND_MINE_BLOCKS="${L1_E2E_POST_FUND_MINE_BLOCKS:-6}"
  export L1_E2E_DETOX_REUSE=0
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
  export L1_E2E_BALANCE_WAIT_MS="${L1_E2E_BALANCE_WAIT_MS:-180000}"
  export L1_E2E_POST_FUND_RELAUNCH="${L1_E2E_POST_FUND_RELAUNCH:-1}"
  export L1_E2E_POST_FUND_MINE_BLOCKS="${L1_E2E_POST_FUND_MINE_BLOCKS:-6}"
  export L1_E2E_DETOX_REUSE=0

  local ios_rc=0 android_rc=0
  set +e
  L1_E2E_SKIP_LOCK=1 \
    L1_IOS_ANDROID_E2E_LOG_DIR="$run_dir/ios-to-android" \
    L1_E2E_BIDIRECTIONAL=0 \
    REDWALLET_SKIP_ANDROID_SEED="$REDWALLET_SKIP_ANDROID_SEED" \
    ANDROID_L1_RECEIVE_ADDRESS="$ANDROID_L1_RECEIVE_ADDRESS" \
    bash "$REDWALLET/scripts/run-l1-ios-simulator-to-android-phone-e2e.sh"
  ios_rc=$?

  # Propagate iOS receive address from ios→android leg for android→ios (skip seed only when known).
  IOS_L1_RECEIVE_ADDRESS="${IOS_L1_RECEIVE_ADDRESS:-}"
  if [[ -f "$run_dir/ios-to-android/ios-receive-address.txt" ]]; then
    parsed_ios="$(grep -Eo 'tb1[a-z0-9]{20,}' "$run_dir/ios-to-android/ios-receive-address.txt" | head -1 || true)"
    if [[ -n "$parsed_ios" ]]; then
      IOS_L1_RECEIVE_ADDRESS="$parsed_ios"
    fi
  fi
  if [[ -z "$IOS_L1_RECEIVE_ADDRESS" && -f "$run_dir/ios-to-android/detox.log" ]]; then
    parsed_ios="$(grep -Eo 'ios_receive_address=tb1[a-z0-9]+' "$run_dir/ios-to-android/detox.log" | head -1 | sed 's/.*=//' || true)"
    IOS_L1_RECEIVE_ADDRESS="${parsed_ios:-}"
  fi
  android_skip_ios_seed="${REDWALLET_SKIP_IOS_SEED:-1}"
  if [[ -z "$IOS_L1_RECEIVE_ADDRESS" ]]; then
    echo "ios_receive unset after ios leg — android leg will run iOS seed detox"
    android_skip_ios_seed=0
  else
    echo "ios_receive=$IOS_L1_RECEIVE_ADDRESS (reuse for android→ios)"
  fi
  export IOS_L1_RECEIVE_ADDRESS

  L1_E2E_SKIP_LOCK=1 \
    L1_ANDROID_IOS_E2E_LOG_DIR="$run_dir/android-to-ios" \
    REDWALLET_SKIP_IOS_SEED="$android_skip_ios_seed" \
    IOS_L1_RECEIVE_ADDRESS="${IOS_L1_RECEIVE_ADDRESS:-}" \
    bash "$REDWALLET/scripts/run-l1-android-phone-to-ios-simulator-e2e.sh"
  android_rc=$?
  set -e

  local final_rc=0
  if [[ "$ios_rc" -ne 0 || "$android_rc" -ne 0 ]]; then
    final_rc=1
  fi
  echo "simulator_bidirectional ios_rc=$ios_rc android_rc=$android_rc final_rc=$final_rc"
  append_l1_evidence "$run_dir" "$ios_rc" "$android_rc"
  return "$final_rc"
}

parse_summary_field() {
  local file="$1" key="$2"
  [[ -f "$file" ]] || return
  grep -E "^${key}=" "$file" 2>/dev/null | tail -1 | cut -d= -f2- || true
}

append_l1_evidence() {
  local run_dir="$1" ios_rc="$2" android_rc="$3"
  local ios_summary="$run_dir/ios-to-android/SUMMARY.txt"
  local android_summary="$run_dir/android-to-ios/SUMMARY.txt"
  local ios_txid android_txid ios_verify android_verify

  ios_txid="$(parse_summary_field "$ios_summary" ios_to_android_txid)"
  [[ -z "$ios_txid" || "$ios_txid" == "unset" ]] && ios_txid="$(grep -Eo 'L1_IOS_ANDROID_E2E\] txid=[0-9a-f]{64}' "$run_dir/ios-to-android/detox.log" 2>/dev/null | tail -1 | sed 's/.*txid=//' || true)"
  android_txid="$(parse_summary_field "$android_summary" txid)"
  [[ -z "$android_txid" || "$android_txid" == "unset" ]] && android_txid="$(grep -Eo 'L1_ANDROID_IOS_E2E\] txid=[0-9a-f]{64}' "$run_dir/android-to-ios/detox.log" 2>/dev/null | tail -1 | sed 's/.*txid=//' || true)"

  ios_verify="$(parse_summary_field "$ios_summary" ios_to_android_verify)"
  android_verify="$(grep -E '^verify=' "$android_summary" 2>/dev/null | tail -1 | cut -d= -f2 || true)"

  if [[ "$ios_rc" -ne 0 || "$android_rc" -ne 0 ]]; then
    echo "evidence_skip rc ios=$ios_rc android=$android_rc"
    return
  fi
  if [[ ! "$ios_txid" =~ ^[0-9a-f]{64}$ || ! "$android_txid" =~ ^[0-9a-f]{64}$ ]]; then
    echo "evidence_skip missing txids ios=${ios_txid:-unset} android=${android_txid:-unset}"
    return
  fi
  if [[ "$ios_verify" != "ok" || "$android_verify" != "ok" ]]; then
    echo "evidence_skip verify ios=$ios_verify android=$android_verify"
    return
  fi

  cat >>"$EVIDENCE" <<EOM

## L1 E2E Run $(date -u +%Y-%m-%dT%H:%M:%SZ) simulator bidirectional VERIFIED

| Direction | detox_exit | txid | verify |
|-----------|------------|------|--------|
| ios→android | 0 | $ios_txid | ok |
| android→ios | 0 | $android_txid | ok |

**run_dir:** $run_dir
detox_exit=0
ios_to_android_verify=ok
android_to_ios_verify=ok

EOM
  echo "evidence_appended run_dir=$run_dir"
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
