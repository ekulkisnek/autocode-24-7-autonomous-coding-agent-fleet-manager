"""Cross-fleet mutexes: L1 E2E exclusive lock, Mac job pauses during Detox windows."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

DEFAULT_L1_LOCK = Path("/Volumes/T705/redwallet-logs/.l1-e2e-lock")

# Chat alias/title substrings paused while L1 lock is held (Mac local dispatch).
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
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
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


def kill_duplicate_l1_processes(*, keep_pid: int | None = None) -> list[int]:
    """Kill stray run-l1-* / detox orchestrators (not the lock holder)."""
    lock = read_l1_lock()
    holder_pid = int((lock or {}).get("pid") or 0)
    keep = keep_pid or holder_pid or 0
    patterns = ("run-l1-", "detox test", "detox/build")
    killed: list[int] = []
    try:
        out = subprocess.run(["pgrep", "-fl", "run-l1-"], capture_output=True, text=True, timeout=5)
        lines = (out.stdout or "").splitlines()
    except Exception:
        lines = []
    my_pid = os.getpid()
    for line in lines:
        parts = line.strip().split(None, 1)
        if not parts:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid in {my_pid, os.getppid()}:
            continue
        if keep and pid == keep:
            continue
        # Never kill the lock holder or its parent shell chain.
        if holder_pid and pid == holder_pid:
            continue
        if any(p in line for p in patterns):
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
