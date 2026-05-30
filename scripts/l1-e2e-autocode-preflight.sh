#!/usr/bin/env bash
# Autocode preflight before each L1 E2E attempt (simulator path).
set -euo pipefail

REDWALLET="${REDWALLET_ROOT:-/Volumes/T705/code/work-on-something-to-do-with/redwallet}"
LOG_ROOT="${REDWALLET_LOG_ROOT:-/Volumes/T705/redwallet-logs}"
LOCAL_DEV="${LOCAL_DEV:-/Volumes/T705/code/drivechain-wallet-dev/local-dev}"
COMPOSE_FILE="${COMPOSE_FILE:-$LOCAL_DEV/docker-compose.local-minimal.yml}"
ANDROID_SERIAL="${ANDROID_SERIAL:-${REDWALLET_ANDROID_SERIAL:-0A201JECB03306}}"
METRO_PORT="${METRO_PORT:-8081}"
ELECTRUM_PORT="${REDWALLET_ELECTRUM_PORT:-60101}"
SIM_UDID="${DETOX_IOS_SIM_UDID:-FC7DDD6B-DFCB-432A-98CE-48C453E6EF48}"
PREFLIGHT_LOG="${L1_E2E_PREFLIGHT_LOG:-$LOG_ROOT/l1-e2e-autocode-preflight.log}"

log() {
  local line
  line="$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"
  printf '%s\n' "$line" | tee -a "$PREFLIGHT_LOG"
}

probe_tcp() {
  python3 - <<'PY' "$1" "$2"
import socket, sys
with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=3):
    pass
PY
}

electrum_height() {
  python3 - <<'PY' "$ELECTRUM_PORT"
import json, socket, sys
port = int(sys.argv[1])
payload = json.dumps({"id": 1, "method": "blockchain.headers.subscribe", "params": []}) + "\n"
with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
    s.sendall(payload.encode())
    line = s.makefile().readline()
res = json.loads(line)
result = res.get("result")
if isinstance(result, list) and result:
    print(int(result[0]))
elif isinstance(result, dict):
    print(int(result.get("height", 0) or 0))
else:
    print(0)
PY
}

mainchain_height() {
  docker compose -f "$COMPOSE_FILE" exec -T mainchain \
    drivechain-cli -signet -rpccookiefile=/data/signet/.cookie getblockcount 2>/dev/null || echo 0
}

log "preflight start simulator path"

if ! docker info >/dev/null 2>&1; then
  log "BLOCKER docker unavailable"
  exit 2
fi

export L1_E2E_COMPOSE_FILE="$COMPOSE_FILE"
export L1_E2E_LOG_FILE="$PREFLIGHT_LOG"
export L1_E2E_REQUIRE_ADB=1
export ANDROID_SERIAL
export REDWALLET_ELECTRUM_HOST="127.0.0.1"
export REDWALLET_ELECTRUM_PORT="$ELECTRUM_PORT"
# Fix ANDROID_SDK_ROOT propagation for detox/android paths (windows remote + mac local)
if [[ -z "${ANDROID_SDK_ROOT:-}" ]]; then
  for sdk in "$HOME/Library/Android/sdk" "$HOME/Android/sdk" "/opt/homebrew/share/android-commandlinetools" "/usr/local/share/android-sdk"; do
    if [[ -d "$sdk" ]]; then export ANDROID_SDK_ROOT="$sdk"; break; fi
  done
fi
[[ -n "${ANDROID_SDK_ROOT:-}" ]] && log "ANDROID_SDK_ROOT=$ANDROID_SDK_ROOT" || log "ANDROID_SDK_ROOT unset (ok for simulator-only)"
bash "$REDWALLET/scripts/l1-e2e-preflight.sh" || exit 2

if ! probe_tcp 127.0.0.1 "$METRO_PORT" 2>/dev/null; then
  if ! curl --silent --max-time 2 "http://127.0.0.1:$METRO_PORT/status" >/dev/null 2>&1; then
    log "WARN metro :$METRO_PORT not ready (orchestrator will start it)"
  fi
else
  log "metro ok port=$METRO_PORT"
fi

if ! adb -s "$ANDROID_SERIAL" get-state >/dev/null 2>&1; then
  log "BLOCKER adb device $ANDROID_SERIAL not ready"
  exit 2
fi
log "adb ok serial=$ANDROID_SERIAL"

xcrun simctl boot "$SIM_UDID" >/dev/null 2>&1 || true
log "detox_simulator boot udid=$SIM_UDID"

if ! probe_tcp 127.0.0.1 "$ELECTRUM_PORT" 2>/dev/null; then
  log "BLOCKER florestad electrum :$ELECTRUM_PORT unreachable"
  exit 2
fi

e_height="$(electrum_height 2>/dev/null || echo 0)"
m_height="$(mainchain_height 2>/dev/null || echo 0)"
log "height electrum=$e_height mainchain=$m_height"
if [[ "$e_height" -gt 0 && "$m_height" -gt 0 ]]; then
  drift=$((m_height - e_height))
  if [[ "$drift" -gt 3 ]]; then
    log "WARN electrum height lag drift=$drift blocks (continuing)"
  fi
fi

log "preflight ok"
exit 0
