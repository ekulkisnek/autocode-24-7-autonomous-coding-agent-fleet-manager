from __future__ import annotations

from sqlite3 import Row
from typing import Any

from .config import (
    DEFAULT_MAX_FAILURE_COUNT,
    DEFAULT_MAX_GOAL_RETRIES,
    DEFAULT_PRIORITY_MAX_FAILURE_COUNT,
    DEFAULT_RETRY_BACKOFF_BASE,
    DEFAULT_RETRY_BACKOFF_MAX,
    DEFAULT_STALL_SECONDS,
)
from . import goals
from .store import Store
from .util import json_dumps, json_loads, now_iso, now_ts, parse_ts


FAILURE_KINDS = frozenset(
    {
        "silent_failed",
        "provider_error",
        "killed",
        "timed_out_with_work",
        "running_silent",
        "goal_incomplete",
        "false_complete",
        "failed",
        "completed",
        "chat_paused",
    }
)


def chat_metadata(row: Row | None) -> dict[str, Any]:
    if not row:
        return {}
    raw = ""
    try:
        raw = str(row["metadata_json"] or "")
    except Exception:
        return {}
    data = json_loads(raw, {})
    return data if isinstance(data, dict) else {}


def max_failure_count(store: Store, chat_id: str) -> int:
    in_queue = store.row("select 1 from queue where chat_id=?", (chat_id,))
    if in_queue and goals.chat_has_active_goal(store, chat_id):
        return DEFAULT_MAX_GOAL_RETRIES
    if store.active_priority_for_chat(chat_id):
        return DEFAULT_PRIORITY_MAX_FAILURE_COUNT
    return DEFAULT_MAX_FAILURE_COUNT


def failure_kind(evidence_status: str, evidence_reason: str, job: Row | None = None) -> str:
    status = (evidence_status or "").strip()
    reason = (evidence_reason or "").strip().lower()
    if status in {"goal_incomplete", "false_complete"}:
        return status
    if status == "killed" or "chat_paused" in reason:
        return "killed"
    if status in {"silent_failed", "running_silent"}:
        return "silent_failed"
    if status == "provider_error":
        return "provider_error"
    if status == "timed_out_with_work":
        return "timed_out_with_work"
    if status == "worked" and ("goal incomplete" in reason or "require_fleet_done" in reason or "too minimal" in reason):
        return "goal_incomplete"
    if status == "completed" and "fleet_done" not in reason:
        return "goal_incomplete"
    if status == "failed":
        return "failed"
    return status or "unknown"


def backoff_seconds(kind: str, failure_count: int) -> int:
    count = max(1, int(failure_count or 1))
    base = DEFAULT_RETRY_BACKOFF_BASE
    if kind == "provider_error":
        base = max(base, 45)
    elif kind == "silent_failed":
        base = max(base, 60)
    elif kind == "killed":
        base = max(base, 15)
    elif kind in {"goal_incomplete", "false_complete", "completed"}:
        base = max(base, 20)
    delay = min(DEFAULT_RETRY_BACKOFF_MAX, int(base * (2 ** min(count - 1, 6))))
    return max(base, delay)


def next_retry_ts(meta: dict[str, Any]) -> float:
    value = meta.get("next_retry_at")
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 10_000_000_000 else v
    return parse_ts(value)


def retry_ready(row: Row) -> bool:
    if int(row["done"] or 0):
        return False
    if int(row["paused"] or 0):
        return False
    meta = chat_metadata(row)
    retry_at = next_retry_ts(meta)
    return retry_at <= 0 or now_ts() >= retry_at


def should_retry_chat(store: Store, row: Row, kind: str) -> bool:
    if int(row["done"] or 0):
        return False
    if int(row["paused"] or 0):
        return False
    in_queue = store.row("select 1 from queue where chat_id=?", (row["id"],))
    if not in_queue:
        return False
    failures = int(row["failure_count"] or 0)
    if failures >= max_failure_count(store, row["id"]):
        return False
    if kind not in FAILURE_KINDS and kind != "unknown":
        return False
    return True


def stall_seconds_for_chat(store: Store, chat_id: str, prompt: str, objective: str) -> int:
    import re

    haystack = f"{prompt} {objective}"
    base = DEFAULT_STALL_SECONDS
    if re.search(r"\be2e\b", haystack, re.IGNORECASE):
        base = max(base, 1800)
    row = store.row("select metadata_json from chats where id=?", (chat_id,))
    extra = int(chat_metadata(row).get("stall_extra_seconds") or 0)
    return base + max(0, extra)


def bump_stall_extra(meta: dict[str, Any], kind: str) -> dict[str, Any]:
    if kind != "silent_failed":
        return meta
    step = 300
    cap = 3600
    current = int(meta.get("stall_extra_seconds") or 0)
    meta["stall_extra_seconds"] = min(cap, current + step)
    return meta


def schedule_retry(
    store: Store,
    chat_id: str,
    *,
    kind: str,
    evidence_status: str,
    evidence_reason: str,
    job_id: str = "",
    immediate: bool = False,
) -> bool:
    row = store.row("select * from chats where id=?", (chat_id,))
    if not row or not should_retry_chat(store, row, kind):
        return False
    failures = int(row["failure_count"] or 0)
    delay = 0 if immediate else backoff_seconds(kind, failures)
    retry_at = now_ts() + delay
    meta = chat_metadata(row)
    meta["last_failure_kind"] = kind
    meta["last_failure_status"] = evidence_status
    meta["last_failure_reason"] = evidence_reason[:500]
    meta["next_retry_at"] = retry_at
    meta["recovery_attempts"] = int(meta.get("recovery_attempts") or 0) + 1
    meta = bump_stall_extra(meta, kind)
    with store.connect() as con:
        con.execute(
            "update chats set metadata_json=?,state='stalled' where id=? and done=0",
            (json_dumps(meta), chat_id),
        )
    store.event(
        "recovery_scheduled",
        chat_id,
        job_id,
        failure_kind=kind,
        delay_seconds=delay,
        retry_at=iso_from_retry(retry_at),
        failure_count=failures,
        stall_extra_seconds=int(meta.get("stall_extra_seconds") or 0),
    )
    store.queue_bump_front(chat_id)
    return True


def iso_from_retry(ts: float) -> str:
    from .util import iso_from_ts

    return iso_from_ts(ts)


def clear_retry_state(store: Store, chat_id: str) -> None:
    row = store.row("select metadata_json from chats where id=?", (chat_id,))
    meta = chat_metadata(row)
    changed = False
    for key in ("next_retry_at", "last_failure_kind", "last_failure_status", "last_failure_reason"):
        if key in meta:
            meta.pop(key, None)
            changed = True
    if changed:
        with store.connect() as con:
            con.execute("update chats set metadata_json=? where id=?", (json_dumps(meta), chat_id))


def handle_job_failure(
    store: Store,
    job: Row,
    *,
    evidence_status: str,
    evidence_reason: str,
) -> None:
    kind = failure_kind(evidence_status, evidence_reason, job)
    if kind == "provider_error":
        store.record_provider_failure(str(job["provider"] or ""), evidence_reason[:240])
    if schedule_retry(
        store,
        str(job["chat_id"]),
        kind=kind,
        evidence_status=evidence_status,
        evidence_reason=evidence_reason,
        job_id=str(job["id"]),
    ):
        return
    chat = store.row("select failure_count from chats where id=?", (job["chat_id"],))
    store.event(
        "recovery_skipped",
        str(job["chat_id"]),
        str(job["id"]),
        failure_kind=kind,
        evidence_status=evidence_status,
        failure_count=int(chat["failure_count"] or 0) if chat else 0,
    )


def schedule_goal_incomplete(
    store: Store,
    chat_id: str,
    *,
    reason: str,
    job_id: str = "",
    immediate: bool = True,
) -> bool:
    store.event("completion_rejected", chat_id, job_id, reason=reason[:500])
    return schedule_retry(
        store,
        chat_id,
        kind="goal_incomplete",
        evidence_status="goal_incomplete",
        evidence_reason=reason,
        job_id=job_id,
        immediate=immediate,
    )


def reconcile_killed_chats(store: Store) -> int:
    """Unstick chats whose jobs were killed but remain queued with goals open."""
    rows = store.rows(
        """
        select c.* from chats c
        join queue q on q.chat_id=c.id
        where c.done=0 and c.paused=0 and c.state in ('paused','stalled')
        """
    )
    fixed = 0
    for row in rows:
        if not retry_ready(row):
            continue
        last = store.row(
            """
            select evidence_status,evidence_reason from jobs
            where chat_id=? and status in ('killed','failed','completed')
            order by updated_at desc limit 1
            """,
            (row["id"],),
        )
        if not last:
            continue
        reason = str(last["evidence_reason"] or "").lower()
        status = str(last["evidence_status"] or "")
        if status != "killed" and "chat_paused" not in reason:
            continue
        if int(row["paused"] or 0):
            continue
        if int(row["failure_count"] or 0) >= max_failure_count(store, row["id"]):
            continue
        with store.connect() as con:
            con.execute(
                "update chats set state='stalled',paused=0 where id=? and done=0",
                (row["id"],),
            )
        store.queue_bump_front(row["id"])
        store.event("recovery_unstuck", row["id"], reason=reason)
        fixed += 1
    return fixed


def provider_in_backoff(store: Store, provider: str) -> bool:
    row = store.row("select backoff_until from provider_health where provider=?", (provider,))
    if not row:
        return False
    until = parse_ts(row["backoff_until"])
    return until > now_ts()


def should_use_fallback(row: Row, failure_count: int | None = None) -> bool:
    failures = failure_count if failure_count is not None else int(row["failure_count"] or 0)
    meta = chat_metadata(row)
    kind = str(meta.get("last_failure_kind") or "")
    source = str(row["source"] or "")
    provider = str(row["provider"] or "")
    if kind == "provider_error" and provider == "grok" and source == "grok.new":
        return failures >= 1
    if kind == "silent_failed":
        return failures >= 2
    return failures >= 2
