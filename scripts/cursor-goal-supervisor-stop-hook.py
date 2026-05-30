#!/usr/bin/env python3
"""Cursor `stop` hook: inject goal-supervisor followup while goals incomplete."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

AUTOCODE = Path(os.environ.get("AUTOCODE_HOME", Path.home() / "autocode"))
sys.path.insert(0, str(AUTOCODE))

from autocode.goal_supervisor_adaptive import current_adaptive_context, format_adaptive_block, record_tick

VERIFY = AUTOCODE / "scripts" / "verify-goal-status.py"
INFRA = AUTOCODE / "scripts" / "autocode-infra-supervisor.py"

SUPERVISOR_PROMPT = (
    "Goal supervisor tick: Run python3 ~/autocode/scripts/autocode-infra-supervisor.py --json "
    "and python3 ~/autocode/scripts/verify-goal-status.py. If all_complete is false, read latest "
    "L1 logs under /Volumes/T705/redwallet-logs/, fix autocode/redwallet blockers, ensure "
    "run-l1-e2e-until-verified.sh and daemon running, dispatch "
    "python3 ~/autocode/scripts/dispatch-meta-supervisor.py if needed. Keep driving until "
    "verify-goal-status shows all_complete=true."
)

PROMPT_TEMPLATE = SUPERVISOR_PROMPT + """

{adaptive}

## verify-goal-status
{verify}

## autocode-infra-supervisor
{infra}
"""


def _run_json(script: Path) -> dict:
    try:
        r = subprocess.run(
            [sys.executable, str(script), "--json"],
            capture_output=True,
            text=True,
            timeout=90,
            cwd=str(AUTOCODE),
        )
        raw = (r.stdout or r.stderr or "").strip()
        if raw:
            return json.loads(raw)
    except Exception as exc:
        return {"error": str(exc)}
    return {"error": "empty output"}


def _adaptive_context_readonly() -> dict:
    """Read adaptive state without incrementing (stop hook runs every agent turn)."""
    return current_adaptive_context()


def main() -> None:
    if os.environ.get("CURSOR_GOAL_SUPERVISOR_HOOK", "1") == "0":
        print("{}")
        return

    pause_flag = AUTOCODE / "state" / "PAUSE_GOAL_SUPERVISOR"
    if pause_flag.is_file():
        print("{}")
        return

    # Consume optional stop-hook stdin (workspace, loop count, etc.)
    if not sys.stdin.isatty():
        try:
            sys.stdin.read()
        except Exception:
            pass

    verify = _run_json(VERIFY)
    if verify.get("all_complete"):
        record_tick(goals_complete=True)
        print("{}")
        return

    adaptive_ctx = _adaptive_context_readonly()
    infra = _run_json(INFRA)
    followup = PROMPT_TEMPLATE.format(
        adaptive=format_adaptive_block(adaptive_ctx),
        verify=json.dumps(verify, indent=2),
        infra=json.dumps(infra, indent=2),
    )
    print(json.dumps({"followup_message": followup}))


if __name__ == "__main__":
    main()
