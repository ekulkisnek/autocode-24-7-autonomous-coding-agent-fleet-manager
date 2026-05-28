from __future__ import annotations

from . import remediation
from .store import Store
from .util import compact


def needs_luke_lines(store: Store | None = None, *, limit: int = 8) -> list[str]:
    """Chats that genuinely need human action (for Grok watchdog / doctor)."""
    store = store or Store()
    lines: list[str] = []
    rows = store.rows(
        """
        select c.id, c.alias, c.title, c.state, c.paused, c.failure_count
        from chats c
        join queue q on q.chat_id=c.id
        where c.done=0
        order by q.position asc
        limit ?
        """,
        (limit * 3,),
    )
    for row in rows:
        need, reason = remediation.needs_luke(store, str(row["id"]))
        if need:
            name = compact(row["alias"] or row["title"] or row["id"], 36)
            lines.append(f"{name}: {reason}")
    paused = store.rows("select id,alias,title from chats where paused=1 and done=0 limit ?", (limit,))
    for row in paused:
        name = compact(row["alias"] or row["title"] or row["id"], 36)
        if not any(name in line for line in lines):
            lines.append(f"{name}: user paused")
    return lines[:limit]


def needs_luke_summary(store: Store | None = None) -> str:
    lines = needs_luke_lines(store)
    if not lines:
        return "none — fleet healthy (auto-remediation handling silent/idle/overdelivery)"
    return "; ".join(lines)


def collect_stuck_patterns_enriched(store: Store | None = None) -> str:
    """Extended stuck patterns for watchdog prompt (includes auto-fix hints)."""
    store = store or Store()
    flags: list[str] = []
    rows = store.rows(
        """
        select j.chat_id, j.evidence_status, count(*) as cnt,
               min(j.created_at) as first_at, max(j.created_at) as last_at,
               c.alias, c.title, c.failure_count, c.done
        from jobs j
        join queue q on q.chat_id=j.chat_id
        join chats c on c.id=j.chat_id
        where j.created_at > datetime('now', '-3 hours')
        group by j.chat_id, j.evidence_status
        order by cnt desc
        limit 20
        """
    )
    if not rows:
        return "(no job activity in last 3h for queued chats)"
    for r in rows:
        cnt = int(r["cnt"])
        ev = str(r["evidence_status"])
        name = compact(r["alias"] or r["title"] or r["chat_id"], 30)
        need, reason = remediation.needs_luke(store, str(r["chat_id"]))
        auto = ""
        if ev in ("running_silent", "running_external_idle") and not need:
            auto = " [auto-remediation queued]"
        elif int(r["done"] or 0) and ev == "worked":
            auto = " [auto-archive on tick]"
        severity = ""
        if need:
            severity = f"NEEDS_LUKE ({reason})"
        elif ev in ("silent_failed", "timed_out_with_work") and cnt >= 3:
            severity = "REPEATED_FAIL"
        elif ev == "running_silent":
            severity = f"SILENT{auto}"
        elif cnt >= 8 and ev == "worked":
            severity = f"OVERDELIVERY{auto}"
        elif cnt >= 3:
            severity = f"{cnt}x"
        if severity:
            flags.append(f"{severity} {name}: {ev} x{cnt}")
    return "\n".join(flags) if flags else "(no notable patterns)"
