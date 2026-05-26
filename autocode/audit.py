from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import AUDIT_LOG, ensure_dirs
from .util import now_iso


def append_audit(kind: str, *, chat_id: str | None = None, job_id: str | None = None, **details: Any) -> None:
    """Append an immutable replay-friendly event line.

    SQLite remains the fast mutable cache. This JSONL log is intentionally
    simple: one complete event per line, ordered by local write time.
    """
    ensure_dirs()
    event = {
        "ts": now_iso(),
        "kind": kind,
        "chat_id": chat_id,
        "job_id": job_id,
        "details": details,
    }
    path = Path(AUDIT_LOG)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
