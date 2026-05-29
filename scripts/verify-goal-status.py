#!/usr/bin/env python3
"""Verify final-goal completion for autocode goal fleets."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

AUTOCODE_ROOT = Path(__file__).resolve().parents[1]
REDWALLET = Path("/Volumes/T705/code/work-on-something-to-do-with/redwallet")
EVIDENCE = Path("/Volumes/T705/redwallet-logs/L1_VERIFIED_EVIDENCE.md")
TXID_OK = re.compile(
    r"\|\s*[^|]+\|\s*0\s*\|\s*[0-9a-f]{64}\s*\|\s*ok\s*\|",
    re.I,
)
VERIFY_OK = re.compile(r"verify\s*=\s*ok|ios_to_android_verify=ok|android_to_ios_verify=ok", re.I)
DETOX_OK = re.compile(r"detox_exit\s*=\s*0|ios_send_exit=0|android_send_exit=0", re.I)


def _run(cmd: list[str], *, timeout: int = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or r.stderr or "").strip()
    except Exception as exc:
        return 1, str(exc)


def goal_l1() -> dict:
    """Goal 1: L1 E2E verified with two mainchain txids verify=ok."""
    evidence_text = EVIDENCE.read_text(encoding="utf-8") if EVIDENCE.is_file() else ""
    ok_rows = len(TXID_OK.findall(evidence_text))
    verify_hits = len(VERIFY_OK.findall(evidence_text))
    detox_ok = bool(DETOX_OK.search(evidence_text))
    lock_active = Path("/Volumes/T705/redwallet-logs/.l1-e2e-lock").is_file()
    complete = ok_rows >= 2 and verify_hits >= 2
    pct = min(100, int(100 * (ok_rows + verify_hits) / 4))
    return {
        "id": "l1-e2e-verified",
        "complete": complete,
        "pct": pct if not complete else 100,
        "ok_txid_rows": ok_rows,
        "verify_ok_hits": verify_hits,
        "detox_ok_in_evidence": detox_ok,
        "l1_lock_active": lock_active,
        "evidence_path": str(EVIDENCE),
    }


def goal_windows() -> dict:
    """Goal 2: Windows remote worker healthy + at least one remote job worked."""
    rc, ping_out = _run([sys.executable, "-m", "autocode", "worker", "ping", "windows-main"])
    ping_ok = rc == 0 and "ok" in ping_out.lower()
    rc2, bench_out = _run([sys.executable, "-m", "autocode", "worker", "bench", "windows-main"], timeout=60)
    bench_ok = rc2 == 0 and "total_s" in bench_out
    sys.path.insert(0, str(AUTOCODE_ROOT))
    from autocode.store import Store

    store = Store()
    worked = store.row(
        """
        select id, evidence_status, updated_at from jobs
        where worker_id='windows-main' and evidence_status='worked'
        order by updated_at desc limit 1
        """
    )
    recent_fail = store.row(
        """
        select id, evidence_status from jobs
        where worker_id='windows-main' and status in ('failed','completed')
        order by updated_at desc limit 1
        """
    )
    remote_worked = bool(worked)
    complete = ping_ok and bench_ok and remote_worked
    pct = 0
    if ping_ok:
        pct += 30
    if bench_ok:
        pct += 30
    if remote_worked:
        pct += 40
    return {
        "id": "windows-remote-health",
        "complete": complete,
        "pct": min(100, pct),
        "ping_ok": ping_ok,
        "bench_ok": bench_ok,
        "remote_worked": remote_worked,
        "last_worked_job": str(worked["id"]) if worked else None,
        "last_job_status": str(recent_fail["evidence_status"]) if recent_fail else None,
    }


def goal_liquid() -> dict:
    """Goal 3: Liquid/Floresta Windows progress + Mac signet reachability."""
    mac_ts = "100.76.117.106"
    probes: dict[str, bool] = {}
    for port in (6004, 38333):
        rc, _ = _run(["nc", "-z", "-w", "3", mac_ts, str(port)])
        probes[f"{mac_ts}:{port}"] = rc == 0
    sys.path.insert(0, str(AUTOCODE_ROOT))
    from autocode.store import Store

    store = Store()
    liquid_chats = store.rows(
        """
        select alias, done, paused from chats
        where alias like 'liquid-%' or title like 'liquid-%'
        order by updated_at desc limit 10
        """
    )
    done_liquid = sum(1 for r in liquid_chats if int(r["done"] or 0))
    connectivity = all(probes.values())
    complete = connectivity and done_liquid >= 2
    pct = 20 * sum(1 for v in probes.values() if v) + min(60, done_liquid * 15)
    return {
        "id": "liquid-utreexo-windows",
        "complete": complete,
        "pct": min(100, pct),
        "mac_probes": probes,
        "liquid_chats_done": done_liquid,
        "liquid_chats_total": len(liquid_chats),
    }


def goal_github() -> dict:
    """Goal 4: ekulkisnek forks synced."""
    remotes_ok = False
    branch = ""
    ahead = 0
    if REDWALLET.is_dir():
        rc, out = _run(["git", "-C", str(REDWALLET), "remote", "-v"])
        remotes_ok = "ekulkisnek/BlueWallet" in out
        _, branch = _run(["git", "-C", str(REDWALLET), "branch", "--show-current"])
        branch = branch.strip()
        _, status = _run(["git", "-C", str(REDWALLET), "status", "-sb"])
        if "ahead" in status:
            m = re.search(r"ahead (\d+)", status)
            ahead = int(m.group(1)) if m else 0
    branch_ok = branch == "codex/redwallet-utreexo-quic-sync"
    complete = remotes_ok and branch_ok and ahead == 0
    pct = 0
    if remotes_ok:
        pct += 40
    if branch_ok:
        pct += 30
    if ahead == 0 and remotes_ok:
        pct += 30
    return {
        "id": "github-sync-ekulkisnek",
        "complete": complete,
        "pct": min(100, pct),
        "fork_remote": remotes_ok,
        "branch": branch,
        "ahead_of_fork": ahead,
    }


def main() -> None:
    goals = [goal_l1(), goal_windows(), goal_liquid(), goal_github()]
    all_complete = all(g["complete"] for g in goals)
    report = {"all_complete": all_complete, "goals": goals}
    if "--json" in sys.argv:
        print(json.dumps(report, indent=2))
    else:
        for g in goals:
            mark = "DONE" if g["complete"] else f"{g['pct']}%"
            print(f"{g['id']}: {mark}")
        print(f"all_complete={all_complete}")
    sys.exit(0 if all_complete else 1)


if __name__ == "__main__":
    main()
