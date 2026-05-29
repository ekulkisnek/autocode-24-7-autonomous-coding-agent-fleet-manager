from __future__ import annotations

import os
from pathlib import Path

HOME = Path.home()
ROOT = Path(os.environ.get("AUTOCODE_HOME", "/Users/lukekensik/autocode"))
STATE = ROOT / "state"
JOBS = STATE / "jobs"
WORKTREES = STATE / "worktrees"
LOGS = ROOT / "logs"
DB = STATE / "autocode.sqlite"
AUDIT_LOG = STATE / "audit.jsonl"
LOG = LOGS / "autocode.log"
PID_FILE = STATE / "daemon.pid"
PLIST = HOME / "Library" / "LaunchAgents" / "com.lukekensik.autocode.plist"
LABEL = "com.lukekensik.autocode"

DEFAULT_DISCOVERY_INTERVAL = int(os.environ.get("AUTOCODE_DISCOVERY_INTERVAL", "300"))
DEFAULT_ACTIVE_DISCOVERY_INTERVAL = int(os.environ.get("AUTOCODE_ACTIVE_DISCOVERY_INTERVAL", "30"))
DEFAULT_IDLE_DISCOVERY_INTERVAL = int(os.environ.get("AUTOCODE_IDLE_DISCOVERY_INTERVAL", "300"))
DEFAULT_TICK_INTERVAL = int(os.environ.get("AUTOCODE_TICK_INTERVAL", "2"))
DEFAULT_JOB_TIMEOUT = int(os.environ.get("AUTOCODE_JOB_TIMEOUT", "1800"))
DEFAULT_CURSOR_JOB_TIMEOUT = int(os.environ.get("AUTOCODE_CURSOR_JOB_TIMEOUT", "14400"))
DEFAULT_STALL_SECONDS = int(os.environ.get("AUTOCODE_STALL_SECONDS", "900"))
DEFAULT_MAX_FAILURE_COUNT = int(os.environ.get("AUTOCODE_MAX_FAILURE_RETRIES", "8"))
DEFAULT_MAX_GOAL_RETRIES = int(os.environ.get("AUTOCODE_MAX_GOAL_RETRIES", "20"))
DEFAULT_PRIORITY_MAX_FAILURE_COUNT = int(os.environ.get("AUTOCODE_PRIORITY_MAX_FAILURE_RETRIES", "12"))
DEFAULT_MIN_OUTPUT_CHARS = int(os.environ.get("AUTOCODE_MIN_OUTPUT_CHARS", "48"))
DEFAULT_REQUIRE_FLEET_DONE = os.environ.get("AUTOCODE_REQUIRE_FLEET_DONE", "1").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
DEFAULT_RETRY_BACKOFF_BASE = int(os.environ.get("AUTOCODE_RETRY_BACKOFF_BASE", "30"))
DEFAULT_RETRY_BACKOFF_MAX = int(os.environ.get("AUTOCODE_RETRY_BACKOFF_MAX", "900"))
DEFAULT_CURSOR_IDLE_SECONDS = int(os.environ.get("AUTOCODE_CURSOR_IDLE_SECONDS", "900"))
DEFAULT_MAX_ACTIVE = int(os.environ.get("AUTOCODE_MAX_ACTIVE", "5"))
DEFAULT_PRESERVE_JOBS_ON_SHUTDOWN = os.environ.get("AUTOCODE_PRESERVE_JOBS_ON_SHUTDOWN", "on").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
DEFAULT_GOAL_OVERDELIVERY_COUNT = int(os.environ.get("AUTOCODE_GOAL_OVERDELIVERY_COUNT", "2"))
DEFAULT_OVERDELIVERY_WINDOW_SECONDS = int(os.environ.get("AUTOCODE_OVERDELIVERY_WINDOW", "3600"))
DEFAULT_SILENT_REMEDIATION_SECONDS = int(os.environ.get("AUTOCODE_SILENT_REMEDIATION_SECONDS", "1200"))
DEFAULT_EXTERNAL_IDLE_REMEDIATION_SECONDS = int(os.environ.get("AUTOCODE_EXTERNAL_IDLE_REMEDIATION_SECONDS", "1800"))
DEFAULT_MAX_REMEDIATION_ATTEMPTS = int(os.environ.get("AUTOCODE_MAX_REMEDIATION_ATTEMPTS", "2"))


def ensure_dirs() -> None:
    for path in (ROOT, STATE, JOBS, WORKTREES, LOGS):
        path.mkdir(parents=True, exist_ok=True)
