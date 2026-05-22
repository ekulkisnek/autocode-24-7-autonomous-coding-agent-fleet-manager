from __future__ import annotations

import signal
import sys
import time

from .config import DEFAULT_TICK_INTERVAL, LOG, PID_FILE, ensure_dirs
from .scheduler import Scheduler
from .store import Store
from .util import now_iso


class Daemon:
    def __init__(self, interval: int = DEFAULT_TICK_INTERVAL):
        ensure_dirs()
        self.interval = interval
        self.stop = False
        self.store = Store()
        self.scheduler = Scheduler(self.store)

    def log(self, message: str) -> None:
        line = f"{now_iso()} {message}"
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line, flush=True)

    def run(self) -> None:
        PID_FILE.write_text(str(os_getpid()), encoding="utf-8")
        signal.signal(signal.SIGTERM, self._signal)
        signal.signal(signal.SIGINT, self._signal)
        self.log("daemon started")
        while not self.stop:
            try:
                result = self.scheduler.tick(dispatch=True)
                self.log(f"tick sent={result['sent']} active={result['active_jobs']} candidates={result['candidates']} capacity={result['capacity']}")
            except Exception as exc:
                self.log(f"tick_error {exc!r}")
                self.store.event("daemon_error", error=str(exc))
            time.sleep(self.interval)
        self.log("daemon stopped")

    def _signal(self, signum, frame) -> None:
        self.stop = True


def os_getpid() -> int:
    import os
    return os.getpid()

