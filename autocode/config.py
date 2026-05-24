from __future__ import annotations

import os
from pathlib import Path

HOME = Path.home()
ROOT = Path(os.environ.get("AUTOCODE_HOME", "/Users/lukekensik/autocode"))
STATE = ROOT / "state"
JOBS = STATE / "jobs"
LOGS = ROOT / "logs"
DB = STATE / "autocode.sqlite"
LOG = LOGS / "autocode.log"
PID_FILE = STATE / "daemon.pid"
PLIST = HOME / "Library" / "LaunchAgents" / "com.lukekensik.autocode.plist"
LABEL = "com.lukekensik.autocode"

DEFAULT_DISCOVERY_INTERVAL = int(os.environ.get("AUTOCODE_DISCOVERY_INTERVAL", "300"))
DEFAULT_TICK_INTERVAL = int(os.environ.get("AUTOCODE_TICK_INTERVAL", "5"))
DEFAULT_JOB_TIMEOUT = int(os.environ.get("AUTOCODE_JOB_TIMEOUT", "1800"))
DEFAULT_CURSOR_JOB_TIMEOUT = int(os.environ.get("AUTOCODE_CURSOR_JOB_TIMEOUT", "14400"))
DEFAULT_STALL_SECONDS = int(os.environ.get("AUTOCODE_STALL_SECONDS", "600"))
DEFAULT_CURSOR_IDLE_SECONDS = int(os.environ.get("AUTOCODE_CURSOR_IDLE_SECONDS", "900"))
DEFAULT_MAX_ACTIVE = int(os.environ.get("AUTOCODE_MAX_ACTIVE", "5"))


def ensure_dirs() -> None:
    for path in (ROOT, STATE, JOBS, LOGS):
        path.mkdir(parents=True, exist_ok=True)
