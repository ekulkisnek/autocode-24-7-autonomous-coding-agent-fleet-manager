"""Event-driven triggers for ~/bin/autocode-grok-watchdog fleet writeups."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import STATE

DEFAULT_BIN = Path.home() / "bin" / "autocode-grok-watchdog"
GROK_BIN = Path(os.environ.get("AUTOCODE_GROK_WATCHDOG_BIN", str(DEFAULT_BIN)))
COALESCE_SEC = float(os.environ.get("AUTOCODE_GROK_WATCHDOG_COALESCE", "30"))
FALLBACK_INTERVAL = int(os.environ.get("AUTOCODE_GROK_WATCHDOG_INTERVAL", "900"))
PENDING_PATH = STATE / "grok-watchdog-pending.json"
PYTHON = os.environ.get("AUTOCODE_PYTHON", sys.executable)

_lock = None  # lazy import to avoid threading in tests unless needed


def _thread_lock():
    global _lock
    if _lock is None:
        import threading

        _lock = threading.Lock()
    return _lock


def enabled() -> bool:
    return os.environ.get("AUTOCODE_GROK_WATCHDOG", "on").lower() not in {"0", "false", "no", "off"}


def _load_pending() -> dict[str, Any]:
    if not PENDING_PATH.exists():
        return {"reasons": [], "scheduled_at": 0.0, "flush_armed": False}
    try:
        data = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"reasons": [], "scheduled_at": 0.0, "flush_armed": False}
    if not isinstance(data.get("reasons"), list):
        data["reasons"] = []
    return data


def _save_pending(data: dict[str, Any]) -> None:
    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    PENDING_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _load_last_run() -> float:
    data = _load_pending()
    try:
        return float(data.get("last_run") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def request(reason: str) -> None:
    """Queue a debounced grok watchdog writeup for ``reason``."""
    if not enabled():
        return
    if not reason.strip():
        return
    if not GROK_BIN.exists():
        return
    with _thread_lock():
        data = _load_pending()
        reasons: list[str] = list(data.get("reasons") or [])
        if reason not in reasons:
            reasons.append(reason)
        data["reasons"] = reasons[-12:]
        if not data.get("scheduled_at"):
            data["scheduled_at"] = time.time()
        _save_pending(data)
        _ensure_flush_scheduled_locked(data)


def on_daemon_tick() -> None:
    """Hook from daemon loop: tick event, flush overdue pending, optional fallback."""
    request("daemon_tick")
    flush_pending_if_due()
    _maybe_fallback()


def flush_pending_if_due() -> None:
    """Run a pending writeup once the coalesce window has elapsed."""
    with _thread_lock():
        data = _load_pending()
        reasons = [str(r) for r in (data.get("reasons") or []) if str(r).strip()]
        if not reasons:
            return
        scheduled = float(data.get("scheduled_at") or 0.0)
        if scheduled and (time.time() - scheduled) < COALESCE_SEC:
            return
        _fire_pending_locked(data)


def _maybe_fallback() -> None:
    if FALLBACK_INTERVAL <= 0:
        return
    last = _load_last_run()
    if last <= 0 or (time.time() - last) >= FALLBACK_INTERVAL:
        request("fallback")


def _ensure_flush_scheduled_locked(data: dict[str, Any]) -> None:
    if data.get("flush_armed"):
        return
    scheduled = float(data.get("scheduled_at") or time.time())
    delay = max(0.05, COALESCE_SEC - (time.time() - scheduled))
    data["flush_armed"] = True
    _save_pending(data)
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(Path(__file__).resolve().parent.parent))
    subprocess.Popen(
        [
            PYTHON,
            "-c",
            (
                "import time; "
                f"time.sleep({delay:.3f}); "
                "from autocode.grok_watchdog import flush_pending_if_due; "
                "flush_pending_if_due()"
            ),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )


def _fire_pending_locked(data: dict[str, Any] | None = None) -> None:
    data = data or _load_pending()
    reasons = [str(r) for r in (data.get("reasons") or []) if str(r).strip()]
    if not reasons:
        data["flush_armed"] = False
        _save_pending(data)
        return
    trigger = ",".join(reasons)
    data["reasons"] = []
    data["scheduled_at"] = 0.0
    data["flush_armed"] = False
    data["last_run"] = time.time()
    _save_pending(data)

    cmd = [str(GROK_BIN), "--trigger", trigger]
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return


def reset_state_for_tests() -> None:
    """Clear pending state (tests only)."""
    if PENDING_PATH.exists():
        PENDING_PATH.unlink()


if __name__ == "__main__":
    request(sys.argv[1] if len(sys.argv) > 1 else "cli")
