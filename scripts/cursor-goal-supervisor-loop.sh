#!/usr/bin/env bash
# Optional Cursor chat wake loop — emits AGENT_LOOP_TICK for monitored shell.
# Usage: bash scripts/cursor-goal-supervisor-loop.sh
# Stop: kill $(cat ~/autocode/state/cursor-goal-supervisor-loop.pid)

set -euo pipefail

INTERVAL="${CURSOR_GOAL_SUPERVISOR_INTERVAL:-120}"
AUTOCODE="${AUTOCODE_HOME:-$HOME/autocode}"
PID_FILE="${AUTOCODE}/state/cursor-goal-supervisor-loop.pid"

PROMPT='Goal supervisor tick: Run python3 '"$AUTOCODE"'/scripts/autocode-infra-supervisor.py --json and python3 '"$AUTOCODE"'/scripts/verify-goal-status.py. If all_complete is false, read latest L1 logs under /Volumes/T705/redwallet-logs/, fix autocode/redwallet blockers, ensure run-l1-e2e-until-verified.sh and daemon running, dispatch python3 '"$AUTOCODE"'/scripts/dispatch-meta-supervisor.py if needed. Keep driving until verify-goal-status shows all_complete=true.'

if [[ -f "$PID_FILE" ]]; then
  old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
    echo "Cursor goal supervisor loop already running (PID $old_pid)"
    exit 0
  fi
fi

echo $$ >"$PID_FILE"
trap 'rm -f "$PID_FILE"' EXIT

echo "Cursor goal supervisor loop: every ${INTERVAL}s (PID $$)"
while true; do
  sleep "$INTERVAL"
  printf 'AGENT_LOOP_TICK_GOAL_SUPERVISOR {"prompt":%s}\n' "$(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$PROMPT")"
done
