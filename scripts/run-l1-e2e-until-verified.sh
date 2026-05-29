#!/usr/bin/env bash
# Run L1 physical bidirectional E2E in a loop until L1_VERIFIED_EVIDENCE.md shows success.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REDWALLET="${REDWALLET_ROOT:-/Volumes/T705/code/work-on-something-to-do-with/redwallet}"
EVIDENCE="${REDWALLET_LOG_ROOT:-/Volumes/T705/redwallet-logs}/L1_VERIFIED_EVIDENCE.md"
MAX_ATTEMPTS="${L1_E2E_MAX_ATTEMPTS:-5}"
SLEEP_BETWEEN="${L1_E2E_RETRY_SLEEP:-120}"

attempt=0
while [[ "$attempt" -lt "$MAX_ATTEMPTS" ]]; do
  attempt=$((attempt + 1))
  echo "=== L1 E2E attempt $attempt/$MAX_ATTEMPTS $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

  if python3 "$ROOT/scripts/verify-goal-status.py" --json 2>/dev/null | python3 -c "import json,sys; g=json.load(sys.stdin); sys.exit(0 if any(x['id']=='l1-e2e-verified' and x['complete'] for x in g['goals']) else 1)"; then
    echo "Goal 1 already complete."
    exit 0
  fi

  python3 -m autocode coord pause-l1-competitors 2>/dev/null || true

  set +e
  bash "$REDWALLET/scripts/run-l1-physical-bidirectional-e2e.sh"
  rc=$?
  set -e
  echo "orchestrator_exit=$rc"

  if python3 "$ROOT/scripts/verify-goal-status.py" --json 2>/dev/null | python3 -c "import json,sys; g=json.load(sys.stdin); sys.exit(0 if any(x['id']=='l1-e2e-verified' and x['complete'] for x in g['goals']) else 1)"; then
    echo "L1 E2E verified."
    exit 0
  fi

  echo "Not verified yet; evidence at $EVIDENCE"
  sleep "$SLEEP_BETWEEN"
done

echo "Max attempts reached without verification."
exit 1
