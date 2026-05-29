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
FORCE_PATH="${L1_E2E_FORCE_PATH:-}"

if [[ "$FORCE_PATH" == "physical" || "$FORCE_PATH" == "simulator" ]]; then
  echo "$FORCE_PATH"
  exit 0
fi

physical_ok=1

if ! command -v adb >/dev/null 2>&1; then
  physical_ok=0
elif ! adb -s "$ANDROID_SERIAL" get-state >/dev/null 2>&1; then
  physical_ok=0
fi

if [[ "$physical_ok" -eq 1 ]]; then
  if command -v xcrun >/dev/null 2>&1; then
    if ! xcrun devicectl list devices 2>/dev/null | grep -q "$IOS_UDID"; then
      if ! xcrun xctrace list devices 2>/dev/null | grep -q "$IOS_UDID"; then
        physical_ok=0
      fi
    fi
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
