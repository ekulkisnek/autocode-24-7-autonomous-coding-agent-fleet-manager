"""Final-goal fleet loop: verify external criteria, re-dispatch until complete."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import ROOT
from .store import Store
from .util import json_dumps, json_loads, now_iso

DEFAULT_GOAL_TICK_INTERVAL = int(os.environ.get("AUTOCODE_GOAL_TICK_INTERVAL", "90"))
L1_E2E_SCRIPT = ROOT / "scripts" / "run-l1-e2e-until-verified.sh"
VERIFY_SCRIPT = ROOT / "scripts" / "verify-goal-status.py"
DISPATCH_SCRIPT = ROOT / "scripts" / "dispatch-goal-fleets.py"

# Map verify-goal-status ids → fleet chat alias substrings.
GOAL_FLEET_ALIASES: dict[str, str] = {
    "l1-e2e-verified": "l1-e2e-until-verified",
    "windows-remote-health": "windows-remote-health",
    "liquid-utreexo-windows": "liquid-utreexo-windows",
    "github-sync-ekulkisnek": "github-sync-ekulkisnek",
}


def external_goal_complete_for_chat(store: Store, chat_id: str) -> tuple[bool, str, str | None]:
    """Return (complete, goal_id, reason) for fleet chats tied to external verify criteria."""
    row = store.row("select alias from chats where id=?", (chat_id,))
    if not row:
        return True, "", ""
    alias = str(row["alias"] or "")
    goal_id = ""
    for gid, fleet_alias in GOAL_FLEET_ALIASES.items():
        if alias == fleet_alias or fleet_alias in alias:
            goal_id = gid
            break
    if not goal_id:
        return True, "", ""
    status = load_status()
    goal = next((g for g in status.get("goals", []) if g.get("id") == goal_id), None)
    if not goal:
        return True, goal_id, "goal status unavailable"
    if goal.get("complete"):
        return True, goal_id, "external goal complete"
    return False, goal_id, f"external {goal_id} at {goal.get('pct')}%"


def load_status() -> dict[str, Any]:
    """Run verify-goal-status.py and return parsed JSON."""
    if not VERIFY_SCRIPT.is_file():
        return {"all_complete": False, "goals": []}
    try:
        proc = subprocess.run(
            [sys.executable, str(VERIFY_SCRIPT), "--json"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(ROOT),
        )
        if proc.stdout.strip():
            return json.loads(proc.stdout)
    except Exception as exc:
        return {"all_complete": False, "goals": [], "error": str(exc)}
    return {"all_complete": False, "goals": []}


def _goal_tick_due(store: Store, *, force: bool = False) -> bool:
    if force:
        return True
    last = float(store.get_config("last_goal_tick_ts", "0") or 0)
    return time.time() - last >= DEFAULT_GOAL_TICK_INTERVAL


def _record_goal_tick(store: Store) -> None:
    store.set_config("last_goal_tick_ts", str(time.time()))


def clear_stale_l1_lock() -> bool:
    """Remove L1 lock file when holder pid is dead."""
    from . import coordination

    info = coordination.read_l1_lock()
    if not info:
        return False
    pid = int(info.get("pid") or 0)
    if pid <= 0:
        holder = str(info.get("holder") or "")
        if holder in {"coord-cli", "manual-pause"}:
            coordination.release_l1_lock()
            return True
        return False
    try:
        os.kill(pid, 0)
        return False
    except OSError:
        coordination.release_l1_lock()
        return True


def kill_simulator_l1_runs() -> list[int]:
    """Kill conflicting simulator Detox / run-l1-ios-simulator paths during physical L1."""
    killed: list[int] = []
    patterns = (
        r"l1_ios_simulator_to_android\.spec",
        r"run-l1-ios-simulator",
    )
    my_pid = os.getpid()
    for pattern in patterns:
        try:
            out = subprocess.run(
                ["pgrep", "-f", pattern],
                capture_output=True,
                text=True,
                timeout=5,
            )
            pids = [int(p.strip()) for p in (out.stdout or "").splitlines() if p.strip().isdigit()]
        except Exception:
            pids = []
        for pid in pids:
            if pid in {my_pid, os.getppid()}:
                continue
            try:
                os.kill(pid, 9)
                killed.append(pid)
            except OSError:
                pass
    return killed


def _l1_orchestrator_running() -> bool:
    try:
        out = subprocess.run(
            ["pgrep", "-f", r"run-l1-(physical|ios-phone|android-phone).*e2e\.sh"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return bool((out.stdout or "").strip())
    except Exception:
        return False


def _l1_loop_running() -> bool:
    try:
        out = subprocess.run(
            ["pgrep", "-f", r"run-l1-e2e-until-verified\.sh"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return bool((out.stdout or "").strip())
    except Exception:
        return False


def start_l1_loop_if_needed(status: dict[str, Any]) -> bool:
    """Spawn physical L1 retry loop when goal incomplete and nothing is running."""
    l1 = next((g for g in status.get("goals", []) if g.get("id") == "l1-e2e-verified"), None)
    if not l1 or l1.get("complete"):
        return False
    if _l1_orchestrator_running() or _l1_loop_running():
        return False
    if not L1_E2E_SCRIPT.is_file():
        return False
    log_dir = Path("/Volumes/T705/redwallet-logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "l1-e2e-until-verified-autocode.log"
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n=== autocode goal_fleets spawn {now_iso()} ===\n")
        subprocess.Popen(
            ["bash", str(L1_E2E_SCRIPT)],
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=str(ROOT),
            start_new_session=True,
        )
    return True


def goal_id_for_chat(alias: str, objective: str) -> str | None:
    alias_l = (alias or "").lower()
    for gid, hint in GOAL_FLEET_ALIASES.items():
        if alias_l == hint or hint in alias_l:
            return gid
    blob = f"{alias} {objective}".lower()
    if "l1-e2e-until-verified" in blob or "l1 e2e until verified" in blob:
        return "l1-e2e-verified"
    if "windows-remote-health" in blob:
        return "windows-remote-health"
    if "github-sync-ekulkisnek" in blob:
        return "github-sync-ekulkisnek"
    if "liquid-utreexo" in blob:
        return "liquid-utreexo-windows"
    return None


def is_fleet_goal_complete(alias: str, objective: str) -> tuple[bool, str]:
    gid = goal_id_for_chat(alias, objective)
    if not gid:
        return False, ""
    status = load_status()
    for g in status.get("goals", []):
        if g.get("id") == gid:
            if g.get("complete"):
                return True, f"verify-goal-status:{gid}:complete"
            return False, f"verify-goal-status:{gid}:pct={g.get('pct', 0)}"
    return False, f"verify-goal-status:{gid}:missing"


def pause_l1_competitors_no_lock(store: Store, scheduler: Any) -> tuple[int, int]:
    """Pause non-goal Mac fleets during L1 without acquiring the E2E lock."""
    from . import coordination

    paused = 0
    killed = 0
    rows = store.rows(
        """
        select id, alias, title, cwd from chats
        where paused=0 and done=0
        """
    )
    goal_aliases = set(GOAL_FLEET_ALIASES.values())
    for row in rows:
        alias = str(row["alias"] or "")
        title = str(row["title"] or "")
        cwd = str(row["cwd"] or "")
        if alias in goal_aliases or any(a in alias for a in goal_aliases):
            continue
        if not coordination.chat_matches_l1_competitor(alias, title, cwd):
            continue
        n = scheduler.runner.kill_chat_jobs(str(row["id"]), "l1_goal_competitor_pause")
        store.pause_chat(str(row["id"]))
        killed += n
        paused += 1
    return paused, killed


def pause_duplicate_l1_fleet_chats(store: Store, scheduler: Any) -> tuple[int, int]:
    """Keep only one active l1-e2e-until-verified fleet chat (prefer cursor over grok in backoff)."""
    from . import recovery

    alias = GOAL_FLEET_ALIASES["l1-e2e-verified"]
    rows = store.rows(
        """
        select id, provider, alias from chats
        where done=0 and (alias=? or alias like ?)
        order by case when provider='cursor' then 0 else 1 end, updated_at desc
        """,
        (alias, f"%{alias}%"),
    )
    if len(rows) <= 1:
        return 0, 0
    keep_id = str(rows[0]["id"])
    paused = 0
    killed = 0
    for row in rows[1:]:
        chat_id = str(row["id"])
        n = scheduler.runner.kill_chat_jobs(chat_id, "l1_fleet_dedupe")
        store.pause_chat(chat_id)
        killed += n
        paused += 1
    # If grok is in backoff, ensure cursor fleet is unpaused.
    if recovery.provider_in_backoff(store, "grok"):
        with store.connect() as con:
            con.execute("update chats set paused=0 where id=? and done=0", (keep_id,))
    return paused, killed


def _fleet_chat_for_goal(store: Store, goal_id: str) -> dict | None:
    alias_hint = GOAL_FLEET_ALIASES.get(goal_id, "")
    if not alias_hint:
        return None
    row = store.row(
        """
        select * from chats
        where alias=? or alias like ?
        order by updated_at desc limit 1
        """,
        (alias_hint, f"%{alias_hint}%"),
    )
    return dict(row) if row else None


def _inject_failure_context(store: Store, chat_id: str, goal: dict, reason: str) -> None:
    row = store.row("select metadata_json from chats where id=?", (chat_id,))
    meta = json_loads(str(row["metadata_json"] or "" if row else ""), {})
    if not isinstance(meta, dict):
        meta = {}
    meta["goal_fleet_retry"] = {
        "goal_id": goal.get("id"),
        "pct": goal.get("pct"),
        "reason": reason[:500],
        "at": now_iso(),
    }
    meta["remediation_prompt_prefix"] = (
        f"GOAL INCOMPLETE ({goal.get('id')} at {goal.get('pct')}%): {reason}\n"
        f"External verify-goal-status still failing. Continue until criteria met.\n\n"
    )
    with store.connect() as con:
        con.execute("update chats set metadata_json=? where id=?", (json_dumps(meta), chat_id))


def reconcile_false_complete_fleets(store: Store, status: dict[str, Any]) -> list[str]:
    """Re-open fleet chats marked done while external goal verification still fails."""
    from . import goals

    reopened: list[str] = []
    for goal in status.get("goals", []):
        if goal.get("complete"):
            continue
        chat = _fleet_chat_for_goal(store, str(goal["id"]))
        if not chat:
            continue
        chat_id = str(chat["id"])
        if not int(chat.get("done") or 0):
            last = store.row(
                """
                select evidence_status, stdout_path from jobs
                where chat_id=? and status in ('completed','failed')
                order by updated_at desc limit 1
                """,
                (chat_id,),
            )
            if not last:
                continue
            ev = str(last["evidence_status"] or "")
            if ev not in {"worked", "goal_incomplete"}:
                continue
        reason = f"external goal {goal['id']} incomplete at {goal.get('pct')}%"
        if goals.reopen_chat_for_goal(store, chat_id, reason="external_goal_incomplete"):
            _inject_failure_context(store, chat_id, goal, reason)
            reopened.append(chat_id)
            store.event("goal_fleet_reopened", chat_id, goal_id=goal["id"], pct=goal.get("pct"))
    return reopened


def _active_fleet_job(store: Store, goal_id: str) -> bool:
    alias_hint = GOAL_FLEET_ALIASES.get(goal_id, "")
    if not alias_hint:
        return False
    row = store.row(
        """
        select count(*) c from jobs j
        join chats c on c.id=j.chat_id
        where j.status='running' and (c.alias=? or c.alias like ?)
        """,
        (alias_hint, f"%{alias_hint}%"),
    )
    return bool(row and int(row["c"] or 0) > 0)


def dispatch_incomplete_goals(store: Store, status: dict[str, Any]) -> dict[str, Any]:
    """Dispatch goal fleets when idle and incomplete."""
    incomplete = [g for g in status.get("goals", []) if not g.get("complete")]
    if not incomplete:
        return {"dispatched": [], "skipped": "all_complete"}
    dispatched: list[str] = []
    skipped: list[str] = []
    for goal in incomplete:
        gid = str(goal["id"])
        if _active_fleet_job(store, gid):
            skipped.append(gid)
            continue
        chat = _fleet_chat_for_goal(store, gid)
        if chat:
            reason = f"external goal {gid} at {goal.get('pct')}% — re-dispatch after verify failure"
            _inject_failure_context(store, str(chat["id"]), goal, reason)
        dispatched.append(gid)
    if not dispatched:
        return {"dispatched": [], "skipped": skipped}
    if DISPATCH_SCRIPT.is_file():
        proc = subprocess.run(
            [sys.executable, str(DISPATCH_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(ROOT),
        )
        return {
            "dispatched": dispatched,
            "dispatch_rc": proc.returncode,
            "dispatch_out": (proc.stdout or proc.stderr or "")[:500],
            "skipped": skipped,
        }
    return {"dispatched": [], "skipped": skipped, "error": "dispatch script missing"}


def tick(store: Store, scheduler: Any, *, force: bool = False) -> dict[str, Any]:
    """Periodic final-goal pass: verify, remediate blockers, re-dispatch."""
    if not _goal_tick_due(store, force=force):
        return {"skipped": "interval"}

    result: dict[str, Any] = {"at": now_iso()}
    result["stale_lock_cleared"] = clear_stale_l1_lock()
    status = load_status()
    result["all_complete"] = status.get("all_complete", False)
    result["goals"] = {g["id"]: g.get("pct", 0) for g in status.get("goals", [])}

    if status.get("all_complete"):
        _record_goal_tick(store)
        store.event("goal_fleets_all_complete")
        return result

    l1_incomplete = any(g.get("id") == "l1-e2e-verified" and not g.get("complete") for g in status.get("goals", []))
    if l1_incomplete:
        result["l1_fleet_deduped"], result["l1_fleet_jobs_killed"] = pause_duplicate_l1_fleet_chats(store, scheduler)
        result["competitors_paused"], result["competitor_jobs_killed"] = pause_l1_competitors_no_lock(store, scheduler)
        from . import coordination

        lock = coordination.read_l1_lock()
        holder = str((lock or {}).get("holder") or "")
        if "simulator" in holder:
            if not coordination.l1_lock_active():
                result["physical_killed"] = coordination.kill_duplicate_l1_processes()
        elif coordination.l1_lock_active() or _l1_orchestrator_running():
            result["simulator_killed"] = kill_simulator_l1_runs()
        elif not _l1_orchestrator_running() and not _l1_loop_running():
            result["simulator_killed"] = kill_simulator_l1_runs()
        result["l1_loop_started"] = start_l1_loop_if_needed(status)

    result["reopened"] = reconcile_false_complete_fleets(store, status)

    # Re-load after reopening; dispatch if Mac has headroom or remote spill available.
    active_jobs = store.row("select count(*) c from jobs where status='running'")
    running = int(active_jobs["c"] if active_jobs else 0)
    cap = scheduler.capacity()
    if running < max(cap, 1) or cap == 0:
        result["dispatch"] = dispatch_incomplete_goals(store, status)
    else:
        result["dispatch"] = {"skipped": "mac_full", "running": running, "capacity": cap}

    _record_goal_tick(store)
    store.event("goal_fleets_tick", **{k: v for k, v in result.items() if k not in {"goals"}})
    return result
