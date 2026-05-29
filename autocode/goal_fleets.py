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
L1_RUNNER_STUCK_SECONDS = int(os.environ.get("L1_E2E_RUNNER_STUCK_SECONDS", "2700"))
LOG_ROOT = Path("/Volumes/T705/redwallet-logs")
L1_RUN_SYMLINKS = (
    "current-l1-simulator-bidirectional-e2e",
    "current-l1-ios-android-e2e",
    "current-l1-android-ios-e2e",
)
L1_E2E_SCRIPT = ROOT / "scripts" / "run-l1-e2e-until-verified.sh"
PICK_L1_PATH_SCRIPT = ROOT / "scripts" / "pick-l1-e2e-path.sh"
VERIFY_SCRIPT = ROOT / "scripts" / "verify-goal-status.py"
DISPATCH_SCRIPT = ROOT / "scripts" / "dispatch-goal-fleets.py"
L1_WORKERS_SCRIPT = ROOT / "scripts" / "dispatch-l1-goal-workers.py"
L1_SIMULATOR_CONTEXT = (
    "LiPhone unplugged — simulator paths only. "
    "Do NOT run run-l1-physical-bidirectional-e2e.sh or run-l1-ios-phone-* orchestrators. "
    "Bidirectional proof via run-l1-ios-simulator-to-android-phone-e2e.sh then "
    "run-l1-android-phone-to-ios-simulator-e2e.sh. Android 0A201JECB03306 still connected."
)

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


def pick_l1_e2e_path() -> str:
    """Return physical|simulator from pick-l1-e2e-path.sh."""
    if not PICK_L1_PATH_SCRIPT.is_file():
        return "simulator"
    try:
        proc = subprocess.run(
            ["bash", str(PICK_L1_PATH_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(ROOT),
        )
        path = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
        return path if path in {"physical", "simulator"} else "simulator"
    except Exception:
        return "simulator"


def kill_physical_l1_runs() -> list[int]:
    """Kill physical iPhone L1 orchestrators when simulator path is active."""
    killed: list[int] = []
    patterns = (
        r"run-l1-physical-bidirectional",
        r"run-l1-ios-phone-to-android",
        r"run-l1-ios-phone-to-android-phone",
        r"run-l1-android-phone-to-ios-phone",
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
            [
                "pgrep",
                "-f",
                r"run-l1-(physical|ios-phone|android-phone|ios-simulator-to-android|android-phone-to-ios-simulator).*e2e\.sh",
            ],
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
    """Spawn L1 retry loop when goal incomplete and nothing is running."""
    l1 = next((g for g in status.get("goals", []) if g.get("id") == "l1-e2e-verified"), None)
    if not l1 or l1.get("complete"):
        return False
    if _l1_orchestrator_running() or _l1_loop_running():
        if not _l1_runner_stuck():
            return False
    if not L1_E2E_SCRIPT.is_file():
        return False
    path = pick_l1_e2e_path()
    if path == "simulator":
        kill_physical_l1_runs()
    log_dir = Path("/Volumes/T705/redwallet-logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "l1-e2e-until-verified-autocode.log"
    env = os.environ.copy()
    if path == "simulator":
        env["L1_E2E_SKIP_PHYSICAL_IOS"] = "1"
        env.setdefault("L1_E2E_FORCE_PATH", "simulator")
        env.setdefault("REDWALLET_SKIP_ANDROID_SEED", "1")
        env.setdefault("REDWALLET_SKIP_IOS_SEED", "1")
        env.setdefault("L1_E2E_MAX_ATTEMPTS", "9999")
        env.setdefault("L1_E2E_BALANCE_WAIT_MS", "120000")
        env.setdefault("L1_E2E_POST_FUND_RELAUNCH", "1")
        env.setdefault("L1_E2E_RETRY_SLEEP", "90")
        env.setdefault("ANDROID_L1_RECEIVE_ADDRESS", "tb1qewdkqej3xc6hh2v5q88rnaekd2zkccf0zq6kdf")
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n=== autocode goal_fleets spawn {now_iso()} path={path} ===\n")
        subprocess.Popen(
            ["bash", str(L1_E2E_SCRIPT)],
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=str(ROOT),
            start_new_session=True,
            env=env,
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


def pause_l1_agent_work_during_shell(store: Store, scheduler: Any) -> tuple[int, int]:
    """Pause/kill agent L1 fleets while the shell retry loop or Detox orchestrator runs."""
    if not (_l1_orchestrator_running() or _l1_loop_running()):
        return 0, 0
    paused = 0
    killed = 0
    fleet_alias = GOAL_FLEET_ALIASES["l1-e2e-verified"]
    rows = store.rows(
        """
        select id, alias from chats
        where paused=0 and done=0
          and (
            id like '%goal1-worker%'
            or alias like '%goal1-worker%'
            or alias=?
            or alias like ?
          )
        """,
        (fleet_alias, f"%{fleet_alias}%"),
    )
    for row in rows:
        chat_id = str(row["id"])
        n = scheduler.runner.kill_chat_jobs(chat_id, "l1_shell_orchestrator_active")
        store.pause_chat(chat_id)
        killed += n
        paused += 1
    return paused, killed


def maybe_refresh_l1_provider_backoff(store: Store) -> dict[str, str]:
    """Clear expired backoffs; keep cursor available when grok is OAuth-blocked."""
    from . import recovery
    from .util import parse_ts, now_ts

    actions: dict[str, str] = {}
    for provider in ("grok", "cursor"):
        row = store.row("select backoff_until,last_error,failure_count from provider_health where provider=?", (provider,))
        if not row:
            continue
        until = parse_ts(str(row["backoff_until"] or ""))
        if until and until <= now_ts():
            store.clear_provider_health(provider)
            actions[provider] = "cleared_expired"
    grok_row = store.row("select last_error from provider_health where provider=?", ("grok",))
    grok_oauth = grok_row and any(
        m in str(grok_row["last_error"] or "").lower()
        for m in ("sign in", "oauth", "authorize", "open this url")
    )
    if grok_oauth and not recovery.provider_in_backoff(store, "cursor"):
        actions["grok"] = actions.get("grok", "oauth_human_gate")
    elif grok_oauth and recovery.provider_in_backoff(store, "cursor"):
        # Grok needs login; do not let grok OAuth failures block cursor L1 fix workers.
        store.clear_provider_health("cursor")
        actions["cursor"] = "cleared_for_l1_grok_oauth"
    return actions


def pause_duplicate_l1_fleet_chats(store: Store, scheduler: Any) -> tuple[int, int]:
    """Keep only one active l1-e2e-until-verified fleet chat (prefer cursor over grok in backoff)."""
    from . import recovery

    if _l1_orchestrator_running() or _l1_loop_running():
        return pause_l1_agent_work_during_shell(store, scheduler)

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


def find_latest_l1_run_dir() -> Path | None:
    """Resolve the active L1 simulator run directory from symlinks or newest stamp dir."""
    for name in L1_RUN_SYMLINKS:
        link = LOG_ROOT / name
        if link.is_symlink():
            target = link.resolve()
            if target.is_dir():
                return target
    candidates = sorted(
        LOG_ROOT.glob("l1-simulator-bidirectional-e2e-*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _tail_file(path: Path, *, lines: int = 50) -> str:
    if not path.is_file():
        return ""
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(content[-lines:])
    except OSError:
        return ""


def _failure_context_from_run(run_dir: Path | None) -> str:
    if not run_dir:
        return ""
    parts: list[str] = [f"latest_run_dir={run_dir}"]
    for rel in ("ios-to-android/detox.log", "android-to-ios/detox.log", "detox.log", "SUMMARY.txt", "run.log"):
        tail = _tail_file(run_dir / rel, lines=50)
        if tail:
            parts.append(f"--- tail {rel} ---\n{tail}")
    return "\n\n".join(parts)


def _l1_lock_age_seconds() -> float | None:
    from . import coordination

    lock = coordination.read_l1_lock()
    if not lock:
        return None
    started = str(lock.get("started_at") or "")
    if not started:
        return None
    try:
        from datetime import datetime, timezone

        dt = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return max(0.0, time.time() - dt.timestamp())
    except ValueError:
        return None


def _l1_runner_stuck() -> bool:
    """True when an active L1 runner has exceeded the stuck threshold (default 45min)."""
    if not (_l1_orchestrator_running() or _l1_loop_running()):
        return False
    age = _l1_lock_age_seconds()
    return age is not None and age >= L1_RUNNER_STUCK_SECONDS


def _inject_failure_context(store: Store, chat_id: str, goal: dict, reason: str) -> None:
    row = store.row("select metadata_json from chats where id=?", (chat_id,))
    meta = json_loads(str(row["metadata_json"] or "" if row else ""), {})
    if not isinstance(meta, dict):
        meta = {}
    path = pick_l1_e2e_path()
    if path == "simulator":
        reason = f"{L1_SIMULATOR_CONTEXT}\n{reason}"
    run_dir = find_latest_l1_run_dir()
    run_ctx = _failure_context_from_run(run_dir)
    if run_ctx:
        reason = f"{reason}\n\n{run_ctx}"
    meta["goal_fleet_retry"] = {
        "goal_id": goal.get("id"),
        "pct": goal.get("pct"),
        "reason": reason[:4000],
        "l1_path": path,
        "latest_run_dir": str(run_dir) if run_dir else "",
        "at": now_iso(),
    }
    meta["remediation_prompt_prefix"] = (
        f"GOAL INCOMPLETE ({goal.get('id')} at {goal.get('pct')}%): {reason[:6000]}\n"
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
    l1_incomplete = any(g.get("id") == "l1-e2e-verified" for g in incomplete)
    for goal in incomplete:
        gid = str(goal["id"])
        # Goal 1 blocks goals 2-4 until L1_VERIFIED_EVIDENCE has two verify=ok txids.
        if l1_incomplete and gid != "l1-e2e-verified":
            skipped.append(gid)
            continue
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
    l1_busy = _l1_orchestrator_running() or _l1_loop_running()
    l1_path = pick_l1_e2e_path() if l1_incomplete else ""
    result["l1_path"] = l1_path
    if l1_incomplete:
        result["provider_backoff"] = maybe_refresh_l1_provider_backoff(store)
        result["l1_fleet_deduped"], result["l1_fleet_jobs_killed"] = pause_duplicate_l1_fleet_chats(store, scheduler)
        if not l1_busy:
            result["competitors_paused"], result["competitor_jobs_killed"] = pause_l1_competitors_no_lock(
                store, scheduler
            )
        from . import coordination

        lock = coordination.read_l1_lock()
        holder = str((lock or {}).get("holder") or "")
        if l1_path == "simulator":
            result["physical_killed"] = kill_physical_l1_runs()
            if holder and "physical" in holder:
                coordination.release_l1_lock()
                result["stale_physical_lock_released"] = True
        elif not l1_busy:
            if "simulator" in holder:
                if not coordination.l1_lock_active():
                    result["physical_killed"] = coordination.kill_duplicate_l1_processes()
            else:
                result["simulator_killed"] = kill_simulator_l1_runs()
        result["l1_loop_started"] = start_l1_loop_if_needed(status)
        result["l1_runner_stuck"] = _l1_runner_stuck()
        result["latest_l1_run_dir"] = str(find_latest_l1_run_dir() or "")

    result["reopened"] = reconcile_false_complete_fleets(store, status)

    # Goal fleets always re-dispatch on verify failure (yolo); never gate on Mac capacity.
    result["dispatch"] = dispatch_incomplete_goals(store, status)

    if l1_incomplete and L1_WORKERS_SCRIPT.is_file():
        if l1_busy:
            result["l1_workers"] = {"skipped": "detox_or_shell_active"}
        else:
            try:
                proc = subprocess.run(
                    [sys.executable, str(L1_WORKERS_SCRIPT)],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    cwd=str(ROOT),
                )
                result["l1_workers"] = {
                    "rc": proc.returncode,
                    "out": (proc.stdout or proc.stderr or "")[:400],
                }
            except Exception as exc:
                result["l1_workers"] = {"error": str(exc)}

    _record_goal_tick(store)
    store.event("goal_fleets_tick", **{k: v for k, v in result.items() if k not in {"goals"}})
    return result
