#!/usr/bin/env python3
"""Deterministic autocode goal infra repairs (no LLM). Run each goal_fleets tick."""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

AUTOCODE_ROOT = Path(__file__).resolve().parents[1]
REDWALLET = Path("/Volumes/T705/code/work-on-something-to-do-with/redwallet")
LOG_ROOT = Path("/Volumes/T705/redwallet-logs")
L1_LOOP_SCRIPT = AUTOCODE_ROOT / "scripts" / "run-l1-e2e-until-verified.sh"
ENSURE_ELECTRUM = REDWALLET / "scripts" / "ensure-l1-electrum.sh"
VERIFY_SCRIPT = AUTOCODE_ROOT / "scripts" / "verify-goal-status.py"


def _run(cmd: list[str], *, timeout: int = 60, cwd: str | None = None) -> tuple[int, str]:
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd or str(AUTOCODE_ROOT),
        )
        return r.returncode, (r.stdout or r.stderr or "").strip()
    except Exception as exc:
        return 1, str(exc)


def _pgrep(pattern: str) -> list[int]:
    rc, out = _run(["pgrep", "-f", pattern], timeout=10)
    if rc != 0 or not out:
        return []
    return [int(x) for x in out.split() if x.isdigit()]


def _l1_loop_pids() -> list[int]:
    """PIDs running the until-verified shell loop (exclude cursor-agent prompts mentioning the script)."""
    pids: list[int] = []
    for pid in _pgrep(r"run-l1-e2e-until-verified\.sh"):
        rc, cmd = _run(["ps", "-p", str(pid), "-o", "command="], timeout=5)
        if rc != 0:
            continue
        cmd = (cmd or "").strip()
        if "cursor-agent" in cmd:
            continue
        if "bash" in cmd and "run-l1-e2e-until-verified.sh" in cmd:
            pids.append(pid)
    return pids


def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _load_status() -> dict:
    if not VERIFY_SCRIPT.is_file():
        return {"all_complete": False, "goals": []}
    rc, out = _run([sys.executable, str(VERIFY_SCRIPT), "--json"], timeout=120)
    if out.strip():
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            pass
    return {"all_complete": False, "goals": []}


def ensure_daemon(actions: list[str]) -> bool:
    pids = _pgrep("autocode.cli daemon run")
    if pids:
        return False
    _run([sys.executable, "-m", "autocode.cli", "daemon", "run", "--interval", "2"], timeout=5)
    actions.append("started_autocode_daemon")
    return True


def ensure_yolo(actions: list[str]) -> bool:
    rc, _ = _run([sys.executable, "-m", "autocode.cli", "yolo", "on"], timeout=15)
    if rc == 0:
        actions.append("enabled_yolo")
        return True
    return False


def ensure_l1_loop(actions: list[str], status: dict) -> bool:
    l1 = next((g for g in status.get("goals", []) if g.get("id") == "l1-e2e-verified"), None)
    if not l1 or l1.get("complete"):
        return False
    if _l1_loop_pids():
        return False
    if not L1_LOOP_SCRIPT.is_file():
        actions.append("missing_l1_loop_script")
        return False
    log_path = LOG_ROOT / "l1-e2e-until-verified-autocode.log"
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.setdefault("L1_E2E_FORCE_PATH", "simulator")
    env.setdefault("L1_E2E_SKIP_PHYSICAL_IOS", "1")
    env.setdefault("L1_E2E_MAX_ATTEMPTS", "9999")
    env.setdefault("L1_E2E_RETRY_SLEEP", "45")
    env.setdefault("L1_E2E_POST_FUND_RELAUNCH", "0")
    with log_path.open("a", encoding="utf-8") as log:
        log.write("\n=== infra-supervisor spawn loop ===\n")
        subprocess.Popen(
            ["bash", str(L1_LOOP_SCRIPT)],
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=str(AUTOCODE_ROOT),
            start_new_session=True,
            env=env,
        )
    actions.append("spawned_l1_until_verified_loop")
    return True


def ensure_electrum(actions: list[str]) -> bool:
    if _port_open("127.0.0.1", 60101):
        return False
    if ENSURE_ELECTRUM.is_file():
        rc, out = _run(["bash", str(ENSURE_ELECTRUM)], timeout=120, cwd=str(REDWALLET))
        if rc == 0 and _port_open("127.0.0.1", 60101):
            actions.append("started_l1_electrum")
            return True
        actions.append(f"electrum_start_failed:{out[:200]}")
        return False
    actions.append("electrum_down_no_ensure_script")
    return False


def clear_provider_backoff(actions: list[str]) -> bool:
    sys.path.insert(0, str(AUTOCODE_ROOT))
    from autocode import recovery
    from autocode.store import Store

    store = Store()
    changed = False
    for provider in ("grok", "cursor"):
        if recovery.provider_in_backoff(store, provider):
            row = store.row("select last_error from provider_health where provider=?", (provider,))
            err = str(row["last_error"] or "").lower() if row else ""
            if provider == "grok" and any(x in err for x in ("sign in", "oauth", "authorize")):
                recovery.clear_provider_backoff(store, provider)
                actions.append(f"cleared_{provider}_backoff_oauth")
                changed = True
    return changed


def run_supervisor(*, json_out: bool = False) -> dict:
    actions: list[str] = []
    status = _load_status()

    sys.path.insert(0, str(AUTOCODE_ROOT))
    from autocode.goal_fleets import clear_stale_l1_lock

    if clear_stale_l1_lock():
        actions.append("cleared_stale_l1_lock")

    ensure_daemon(actions)
    ensure_yolo(actions)
    ensure_electrum(actions)
    clear_provider_backoff(actions)

    l1 = next((g for g in status.get("goals", []) if g.get("id") == "l1-e2e-verified"), None)
    if l1 and not l1.get("complete"):
        ensure_l1_loop(actions, status)

    report = {
        "all_complete": status.get("all_complete", False),
        "goals": {g["id"]: g.get("pct", 0) for g in status.get("goals", [])},
        "actions": actions,
        "daemon_pids": _pgrep("autocode.cli daemon run"),
        "l1_loop_pids": _l1_loop_pids(),
        "detox_pids": _pgrep("detox test"),
        "electrum_up": _port_open("127.0.0.1", 60101),
        "l1_lock": (LOG_ROOT / ".l1-e2e-lock").is_file(),
    }
    if json_out:
        print(json.dumps(report, indent=2))
    else:
        print(json.dumps(report, indent=2))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Autocode deterministic goal infra supervisor")
    parser.add_argument("--json", action="store_true", help="JSON output (default)")
    args = parser.parse_args()
    run_supervisor(json_out=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
