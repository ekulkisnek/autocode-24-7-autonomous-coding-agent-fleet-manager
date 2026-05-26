from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import AUDIT_LOG, ensure_dirs
from .util import now_iso


def append_audit(
    kind: str,
    *,
    chat_id: str | None = None,
    job_id: str | None = None,
    path: Path = AUDIT_LOG,
    **details: Any,
) -> None:
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
    path = Path(path)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")


def iter_audit(path: Path = AUDIT_LOG) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def replay_summary(path: Path = AUDIT_LOG) -> dict[str, Any]:
    counts: dict[str, int] = {}
    chats: set[str] = set()
    jobs: set[str] = set()
    for event in iter_audit(path):
        kind = str(event.get("kind") or "")
        counts[kind] = counts.get(kind, 0) + 1
        if event.get("chat_id"):
            chats.add(str(event["chat_id"]))
        if event.get("job_id"):
            jobs.add(str(event["job_id"]))
    return {
        "events": sum(counts.values()),
        "kinds": counts,
        "chats": len(chats),
        "jobs": len(jobs),
    }
