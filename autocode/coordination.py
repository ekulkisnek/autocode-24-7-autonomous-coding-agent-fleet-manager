"""Cross-fleet mutexes: L1 E2E exclusive lock, Mac job pauses during Detox windows."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

DEFAULT_L1_LOCK = Path("/Volumes/T705/redwallet-logs/.l1-e2e-lock")

# Chat alias/title substrings paused while L1 is incomplete (Mac local dispatch).
L1_COMPETING_QUERIES = (
    "patreon-transcribe",
    "liquid-utreexo",
    "liquid-floresta",
    "liquid-redwallet",
    "liquid-tailscale",
    "l1-detox-spec",
    "l1-detox-review",
    "l1-ios",
    "l1-android",
    "run-l1-",
    "lukekensik-",
    "redwallet-l1-e2e",
    "fund-l1-e2e",
    "physical-l1-e2e",
    "retry-l1-e2e",
    # Parallel L1 fix workers (goal1-worker:* chats) must not run during Detox.
    "goal1-worker",
    "l1-sim-detox-fix",
    "l1-electrum-sync-fix",
    "l1-orchestrator-hardening",
    "l1-signet-shared-tests",
    "l1-log-analysis",
    "l1-blueelectrum-signet",
    "l1-docs-e2e",
)

# Fleets that must not run on Mac during L1 (liquid cursor/grok on redwallet).
L1_MAC_BLOCK_TAGS = L1_COMPETING_QUERIES


def l1_lock_path() -> Path:
    raw = os.environ.get("REDWALLET_L1_E2E_LOCK", "")
    return Path(raw) if raw else DEFAULT_L1_LOCK


def read_l1_lock() -> dict[str, Any] | None:
    path = l1_lock_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return {"pid": 0, "run_dir": str(path), "started_at": "", "holder": "unknown"}


def l1_lock_active() -> bool:
    info = read_l1_lock()
    if not info:
        return False
    pid = int(info.get("pid") or 0)
    if pid <= 0:
        holder = str(info.get("holder") or "")
        # Stale manual pause locks from coord-cli should not block physical E2E.
        if holder in {"coord-cli", "manual-pause"}:
            release_l1_lock()
            return False
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        release_l1_lock()
        return False


def acquire_l1_lock(*, run_dir: str, holder: str = "l1-e2e") -> bool:
    path = l1_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if l1_lock_active():
        return False
    payload = {
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "run_dir": run_dir,
        "holder": holder,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return True


def release_l1_lock() -> None:
    path = l1_lock_path()
    if path.is_file():
        try:
            path.unlink()
        except OSError:
            pass


def _process_tree_pids(root_pid: int, *, depth: int = 8) -> set[int]:
    """Return root_pid plus ancestors (up to depth) and descendants (pgrep -P BFS)."""
    protected: set[int] = set()
    if root_pid <= 0:
        return protected
    protected.add(root_pid)
    # Walk up to find run-l1-e2e-until-verified parent loop.
    cur = root_pid
    for _ in range(depth):
        try:
            out = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(cur)],
                capture_output=True,
                text=True,
                timeout=3,
            )
            ppid = int((out.stdout or "").strip() or 0)
        except (ValueError, OSError, subprocess.SubprocessError):
            break
        if ppid <= 1 or ppid in protected:
            break
        protected.add(ppid)
        cur = ppid
    # Walk down: all child processes.
    frontier = [root_pid]
    for _ in range(depth * 4):
        if not frontier:
            break
        next_frontier: list[int] = []
        for parent in frontier:
            try:
                out = subprocess.run(
                    ["pgrep", "-P", str(parent)],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                for line in (out.stdout or "").splitlines():
                    if line.strip().isdigit():
                        child = int(line.strip())
                        if child not in protected:
                            protected.add(child)
                            next_frontier.append(child)
            except Exception:
                pass
        frontier = next_frontier
    return protected


def l1_protected_pids(*, extra_keep: int | None = None) -> set[int]:
    """PIDs that must not be killed during L1 dedup (lock holder + loop + children)."""
    protected: set[int] = {os.getpid(), os.getppid()}
    if extra_keep and extra_keep > 0:
        protected |= _process_tree_pids(extra_keep)
    lock = read_l1_lock()
    holder_pid = int((lock or {}).get("pid") or 0)
    if holder_pid > 0:
        protected |= _process_tree_pids(holder_pid)
    # Protect active run-l1-e2e-until-verified monitor loops.
    try:
        out = subprocess.run(
            ["pgrep", "-f", r"run-l1-e2e-until-verified\.sh"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in (out.stdout or "").splitlines():
            if line.strip().isdigit():
                protected |= _process_tree_pids(int(line.strip()))
    except Exception:
        pass
    return protected


def kill_duplicate_l1_processes(*, keep_pid: int | None = None) -> list[int]:
    """Kill stray run-l1-* orchestrators and simulator Detox L1 runs (not the lock holder tree)."""
    protected = l1_protected_pids(extra_keep=keep_pid)
    killed: list[int] = []
    patterns = (
        r"l1_ios_simulator_to_android\.spec\.js",
        r"run-l1-ios-simulator",
    )
    pids: set[int] = set()
    for pattern in patterns:
        try:
            out = subprocess.run(
                ["pgrep", "-f", pattern],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in (out.stdout or "").splitlines():
                if line.strip().isdigit():
                    pids.add(int(line.strip()))
        except Exception:
            pass
    for pid in pids:
        if pid in protected:
            continue
        try:
            os.kill(pid, 9)
            killed.append(pid)
        except OSError:
            pass
    return killed


def chat_matches_l1_competitor(alias: str, title: str, cwd: str) -> bool:
    blob = f"{alias} {title}".lower()
    return any(tag in blob for tag in L1_MAC_BLOCK_TAGS)


def should_block_mac_dispatch(alias: str, title: str, cwd: str) -> bool:
    """True when L1 lock is held and this chat would compete."""
    if not l1_lock_active():
        return False
    return chat_matches_l1_competitor(alias, title, cwd)


def should_block_remote_dispatch(worker_id: str) -> bool:
    """Windows remote: only one job at a time; skip if worker already busy."""
    return False  # enforced via weight_capacity=1 in store; kept for extension


def pause_competing_chats(store: Any, scheduler: Any) -> tuple[int, int]:
    """Pause Mac chats that compete with an active L1 E2E window."""
    if not l1_lock_active():
        return 0, 0
    paused = 0
    killed = 0
    rows = store.rows(
        """
        select id, alias, title, cwd from chats
        where paused=0 and done=0
        """
    )
    for row in rows:
        alias = str(row["alias"] or "")
        title = str(row["title"] or "")
        cwd = str(row["cwd"] or "")
        if not chat_matches_l1_competitor(alias, title, cwd):
            continue
        n = scheduler.runner.kill_chat_jobs(str(row["id"]), "l1_e2e_lock")
        store.pause_chat(str(row["id"]))
        killed += n
        paused += 1
    return paused, killed
