#!/usr/bin/env bash
# Pick ONE L1 E2E path: physical (preferred) or simulator (fallback).
# Prints: physical | simulator
set -euo pipefail

REDWALLET="${REDWALLET_ROOT:-/Volumes/T705/code/work-on-something-to-do-with/redwallet}"
BITASSETS_HOST="${BITASSETS_RPC_HOST:-127.0.0.1}"
BITASSETS_PORT="${BITASSETS_RPC_PORT:-6004}"
ELECTRUM_PORT="${REDWALLET_ELECTRUM_PORT:-60101}"
ANDROID_SERIAL="${ANDROID_SERIAL:-${REDWALLET_ANDROID_SERIAL:-0A201JECB03306}}"
IOS_UDID="${IOS_UDID:-00008020-0011204911F3002E}"
IOS_DEVICE_NAME="${L1_IOS_DEVICE_NAME:-LiPhone}"
FORCE_PATH="${L1_E2E_FORCE_PATH:-}"

if [[ "$FORCE_PATH" == "physical" || "$FORCE_PATH" == "simulator" ]]; then
  echo "$FORCE_PATH"
  exit 0
fi

# LiPhone unplugged / physical iOS intentionally disabled.
if [[ "${L1_E2E_SKIP_PHYSICAL_IOS:-0}" == "1" ]]; then
  echo simulator
  exit 0
fi

ios_physical_connected() {
  if ! command -v xcrun >/dev/null 2>&1; then
    return 1
  fi
  local json
  json="$(mktemp)"
  if ! xcrun devicectl list devices --json-output "$json" >/dev/null 2>&1; then
    rm -f "$json"
    return 1
  fi
  local rc=1
  if python3 - "$IOS_UDID" "$IOS_DEVICE_NAME" "$json" <<'PY'
import json, sys

target_udid = sys.argv[1]
device_name = sys.argv[2]
path = sys.argv[3]

with open(path, encoding="utf-8") as f:
    data = json.load(f)

for dev in data.get("result", {}).get("devices", []):
    hw = dev.get("hardwareProperties") or {}
    props = dev.get("deviceProperties") or {}
    udid = str(hw.get("udid", ""))
    name = str(props.get("name", ""))
    if udid != target_udid and name != device_name:
        continue
    conn = dev.get("connectionProperties") or {}
    transport = str(conn.get("transportType", "")).lower()
    tunnel = str(conn.get("tunnelState", "")).lower()
    # Unplugged devices show "available (paired)" — not actively connected.
    if transport == "wired" or tunnel == "connected":
        raise SystemExit(0)
raise SystemExit(1)
PY
  then
    rc=0
  fi
  rm -f "$json"
  # Text fallback: LiPhone line must show live "connected", not merely paired/unavailable.
  if [[ "$rc" -ne 0 ]]; then
    if xcrun devicectl list devices >/dev/null 2>&1 && \
       xcrun devicectl list devices 2>/dev/null | grep -i "$IOS_DEVICE_NAME" | grep -qE '[[:space:]]connected[[:space:]]'; then
      rc=0
    fi
  fi
  return "$rc"
}

physical_ok=1

if ! command -v adb >/dev/null 2>&1; then
  physical_ok=0
elif ! adb -s "$ANDROID_SERIAL" get-state >/dev/null 2>&1; then
  physical_ok=0
fi

if [[ "$physical_ok" -eq 1 ]]; then
  if ! ios_physical_connected; then
    physical_ok=0
  fi
fi

if [[ "$physical_ok" -eq 1 ]]; then
  if ! nc -z -w 3 "$BITASSETS_HOST" "$BITASSETS_PORT" 2>/dev/null; then
    physical_ok=0
  fi
fi

if [[ "$physical_ok" -eq 1 ]]; then
  if ! nc -z -w 3 127.0.0.1 "$ELECTRUM_PORT" 2>/dev/null; then
    physical_ok=0
  fi
fi

if [[ "$physical_ok" -eq 1 ]]; then
  echo physical
else
  echo simulator
fi
