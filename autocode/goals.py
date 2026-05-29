from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Row

from .config import (
    DEFAULT_GOAL_OVERDELIVERY_COUNT,
    DEFAULT_MIN_OUTPUT_CHARS,
    DEFAULT_OVERDELIVERY_WINDOW_SECONDS,
    DEFAULT_REQUIRE_FLEET_DONE,
)
from .markers import parse_fleet_marker
from .policy import FLEET_DONE_MARKER, OutputAssessment, assess_output_state
from .store import Store
from .util import now_iso, now_ts, read_text

TXID_EVIDENCE = re.compile(
    r"\b(?:txid|tx=)\s*[:=]?\s*([0-9a-f]{64})\b",
    re.I,
)
DEPLOYMENT_ACTIVE = re.compile(
    r"\b(?:simplicity|deployment)\b.{0,80}\b(?:active\s*=\s*true|status\s*=\s*active)\b",
    re.I,
)
MINED_IN_BLOCK = re.compile(r"\b(?:in_block\s*=\s*true|mined\s+(?:height|in\s+block)|confirmations?\s*=\s*[1-9])\b", re.I)


def require_fleet_done(store: Store) -> bool:
    raw = store.get_config("require_fleet_done", "on" if DEFAULT_REQUIRE_FLEET_DONE else "off")
    return raw.lower() in {"1", "true", "yes", "on"}


def min_output_chars(store: Store) -> int:
    raw = store.get_config("min_output_chars", str(DEFAULT_MIN_OUTPUT_CHARS))
    try:
        return max(8, int(raw))
    except ValueError:
        return DEFAULT_MIN_OUTPUT_CHARS


def output_too_minimal(store: Store, text: str) -> bool:
    stripped = (text or "").strip()
    if len(stripped) >= min_output_chars(store):
        return False
    if parse_fleet_marker(stripped):
        return False
    if FLEET_DONE_MARKER.search(stripped):
        return False
    return True


def _gw_completion_override(store: Store, chat_id: str) -> tuple[str, str]:
    """Return (override, reason) from gw_completion_override metadata field."""
    if not chat_id:
        return "", ""
    row = store.row("select metadata_json from chats where id=?", (chat_id,))
    if not row:
        return "", ""
    from .util import json_loads
    meta = json_loads(str(row["metadata_json"] or ""), {})
    if not isinstance(meta, dict):
        return "", ""
    return str(meta.get("gw_completion_override") or ""), str(meta.get("gw_completion_reason") or "")


def verify_goal_complete(store: Store, objective: str, output: str, chat_id: str = "") -> tuple[bool, str]:
    """Return whether job output may mark the chat goal complete."""
    override, reason = _gw_completion_override(store, chat_id)
    if override == "confirm":
        return True, reason or "watchdog: completion confirmed"
    if output_too_minimal(store, output):
        return False, "output too minimal to count as completion"
    if override == "reject":
        return False, reason or "watchdog: completion blocked"
    assessment = assess_output_state(objective, output)
    if not assessment.complete:
        return False, assessment.reason
    if require_fleet_done(store):
        marker = parse_fleet_marker(output)
        if not ((marker and marker.kind == "FLEET_DONE") or FLEET_DONE_MARKER.search(output or "")):
            return False, "require_fleet_done: missing FLEET_DONE marker"
    return True, assessment.reason


def assess_for_completion(store: Store, objective: str, output: str, chat_id: str = "") -> OutputAssessment:
    override, reason = _gw_completion_override(store, chat_id)
    if override == "confirm":
        return OutputAssessment("done", True, reason or "watchdog: completion confirmed")
    if output_too_minimal(store, output):
        return OutputAssessment("stalled", False, "output too minimal to count as completion")
    if override == "reject":
        return OutputAssessment("active", False, reason or "watchdog: completion blocked")
    assessment = assess_output_state(objective, output)
    if assessment.complete and require_fleet_done(store):
        marker = parse_fleet_marker(output)
        if not ((marker and marker.kind == "FLEET_DONE") or FLEET_DONE_MARKER.search(output or "")):
            return OutputAssessment("active", False, "require_fleet_done: missing FLEET_DONE marker")
    return assessment


def chat_has_active_goal(store: Store, chat_id: str) -> bool:
    if store.row("select 1 from goals where chat_id=? and status='active' limit 1", (chat_id,)):
        return True
    return bool(store.active_priority_for_chat(chat_id))


def last_job_output(store: Store, chat_id: str) -> str:
    job = store.row(
        """
        select stdout_path,stderr_path from jobs
        where chat_id=? and status in ('completed','failed','killed')
        order by updated_at desc limit 1
        """,
        (chat_id,),
    )
    if not job:
        return ""
    out = Path(str(job["stdout_path"] or ""))
    err = Path(str(job["stderr_path"] or ""))
    text = read_text(out, limit=12000) if out.exists() else ""
    if not text.strip() and err.exists():
        text = read_text(err, limit=4000)
    return text


def should_reopen_done_chat(store: Store, chat_id: str) -> bool:
    row = store.row("select * from chats where id=?", (chat_id,))
    if not row or int(row["paused"] or 0) or not int(row["done"] or 0):
        return False
    if chat_has_active_goal(store, chat_id):
        return True
    objective = str(row["objective"] or "").strip()
    if not objective:
        return False
    if store.row("select 1 from queue where chat_id=?", (chat_id,)):
        verified, _ = verify_goal_complete(store, objective, last_job_output(store, chat_id))
        return not verified
    if store.row("select 1 from queue_finished where chat_id=?", (chat_id,)):
        verified, _ = verify_goal_complete(store, objective, last_job_output(store, chat_id))
        return not verified
    return False


def reopen_chat_for_goal(store: Store, chat_id: str, *, reason: str) -> bool:
    row = store.row("select * from chats where id=?", (chat_id,))
    if not row or not should_reopen_done_chat(store, chat_id):
        return False
    store.queue_reopen(chat_id)
    with store.connect() as con:
        con.execute(
            "update chats set done=0,state='active',paused=0 where id=? and paused=0",
            (chat_id,),
        )
        con.execute(
            "update goals set status='active',updated_at=? where chat_id=? and status='complete'",
            (now_iso(), chat_id),
        )
        con.execute(
            """
            update project_priorities set status='active',updated_at=?
            where target_chat_id=? and status='complete'
            """,
            (now_iso(), chat_id),
        )
    store.event("goal_reopened", chat_id, reason=reason)
    store.queue_bump_front(chat_id)
    return True


@dataclass(frozen=True)
class OverdeliveryResult:
    chat_id: str
    reason: str
    evidence_keys: tuple[str, ...]


def goal_evidence_keys(objective: str, output: str) -> frozenset[str]:
    """Stable evidence fingerprints for repeated-success / churn detection."""
    text = output or ""
    keys: set[str] = set()
    marker = parse_fleet_marker(text)
    if marker and marker.kind == "FLEET_DONE":
        keys.add("fleet_done")
    elif FLEET_DONE_MARKER.search(text):
        keys.add("fleet_done")
    for match in TXID_EVIDENCE.finditer(text):
        keys.add(f"txid:{match.group(1).lower()}")
    if DEPLOYMENT_ACTIVE.search(text):
        keys.add("deployment_active")
    if MINED_IN_BLOCK.search(text):
        keys.add("mined_confirmed")
    goal = (objective or "").lower()
    if "0xbe" in goal or "simplicity" in goal:
        if re.search(r"\b0xbe\b|tapsimplicity", text, re.I):
            keys.add("simplicity_0xbe")
    if "authentication" in goal or "cursor" in goal and "sync" in goal:
        if re.search(r"\bhandoff\b|\bper-chat\b|\bmanual\b", text, re.I):
            keys.add("cursor_handoff_doc")
    return frozenset(keys)


def detect_overdelivery(store: Store, chat_id: str) -> OverdeliveryResult | None:
    """Detect goals already satisfied but still looping (repeated proof, worked churn)."""
    chat = store.row("select * from chats where id=?", (chat_id,))
    if not chat or int(chat["done"] or 0) or int(chat["paused"] or 0):
        return None
    objective = str(chat["objective"] or "").strip()
    if not objective:
        return None

    window_sec = DEFAULT_OVERDELIVERY_WINDOW_SECONDS
    since = now_ts() - window_sec
    jobs = store.rows(
        """
        select id,evidence_status,marker_kind,stdout_path,stderr_path,created_at,updated_at
        from jobs
        where chat_id=? and status in ('completed','failed','killed')
          and evidence_status='worked'
        order by updated_at desc
        limit 50
        """,
        (chat_id,),
    )
    if len(jobs) < DEFAULT_GOAL_OVERDELIVERY_COUNT:
        return None

    recent_in_window = [j for j in jobs if _job_ts(j) >= since]
    if len(recent_in_window) < DEFAULT_GOAL_OVERDELIVERY_COUNT:
        return None

    proofs: list[frozenset[str]] = []
    verified_outputs: list[str] = []
    for job in recent_in_window:
        text = _job_output_text(job)
        if not text.strip():
            continue
        keys = goal_evidence_keys(objective, text)
        if keys:
            proofs.append(keys)
        ok, _ = verify_goal_complete(store, objective, text)
        if ok:
            verified_outputs.append(text)

    if len(verified_outputs) >= DEFAULT_GOAL_OVERDELIVERY_COUNT:
        keys = goal_evidence_keys(objective, verified_outputs[0])
        return OverdeliveryResult(
            chat_id,
            f"{len(verified_outputs)} verified completions in {window_sec // 60}m",
            tuple(sorted(keys)),
        )

    if len(recent_in_window) >= 5 and proofs:
        stable = proofs[0]
        if all(p & stable for p in proofs[:DEFAULT_GOAL_OVERDELIVERY_COUNT]):
            if stable & {"fleet_done", "txid"} or stable & {"fleet_done", "deployment_active"}:
                return OverdeliveryResult(
                    chat_id,
                    f"worked churn ({len(recent_in_window)} jobs) with stable evidence {sorted(stable)[:3]}",
                    tuple(sorted(stable)),
                )
    return None


def _job_ts(job: Row) -> float:
    from .util import parse_ts

    return max(parse_ts(job["updated_at"]), parse_ts(job["created_at"]))


def _job_output_text(job: Row) -> str:
    out = Path(str(job["stdout_path"] or ""))
    err = Path(str(job["stderr_path"] or ""))
    text = read_text(out, limit=12000) if out.exists() else ""
    if not text.strip() and err.exists():
        text = read_text(err, limit=4000)
    return text


def mark_goal_complete(
    store: Store,
    chat_id: str,
    reason: str,
    *,
    kill_running: bool = True,
    archive: bool = True,
) -> bool:
    """Mark chat goal complete, optionally kill redundant loop job and archive queue."""
    row = store.row("select * from chats where id=?", (chat_id,))
    if not row:
        return False
    if int(row["done"] or 0) and not store.row("select 1 from queue where chat_id=?", (chat_id,)):
        return False
    if kill_running:
        from .runner import JobRunner

        JobRunner(store).kill_chat_jobs(chat_id, "goal_overdelivery")
    with store.connect() as con:
        con.execute(
            "update chats set done=1,state='done',failure_count=0,last_evidence_at=? where id=?",
            (now_iso(), chat_id),
        )
        con.execute(
            "update goals set status='complete',updated_at=? where chat_id=? and status!='complete'",
            (now_iso(), chat_id),
        )
        con.execute(
            "update project_priorities set status='complete',updated_at=? where target_chat_id=? and status='active'",
            (now_iso(), chat_id),
        )
    if archive:
        store.queue_archive(chat_id, reason="overdelivery")
    store.event("goal_auto_complete", chat_id, reason=reason[:500])
    return True


def auto_complete_overdelivery(store: Store) -> list[str]:
    """Scan active queue chats and complete those that over-delivered."""
    completed: list[str] = []
    rows = store.rows(
        """
        select c.id from chats c
        join queue q on q.chat_id=c.id
        where c.done=0 and c.paused=0
        """
    )
    for row in rows:
        chat_id = str(row["id"])
        hit = detect_overdelivery(store, chat_id)
        if not hit:
            continue
        if mark_goal_complete(store, chat_id, hit.reason):
            completed.append(chat_id)
    return completed


def reconcile_done_still_in_queue(store: Store) -> list[str]:
    """Archive done chats still on the active queue when last job verifies the goal."""
    rows = store.rows(
        """
        select c.id, c.objective from chats c
        join queue q on q.chat_id=c.id
        where c.done=1 and c.paused=0
        """
    )
    archived: list[str] = []
    for row in rows:
        chat_id = str(row["id"])
        if store.row("select 1 from goals where chat_id=? and status='active'", (chat_id,)):
            continue
        if store.row(
            "select 1 from project_priorities where target_chat_id=? and status='active'",
            (chat_id,),
        ):
            continue
        objective = str(row["objective"] or "").strip()
        output = last_job_output(store, chat_id)
        if objective and output:
            ok, _ = verify_goal_complete(store, objective, output)
            if not ok:
                continue
        if store.queue_archive(chat_id, reason="done_verified"):
            archived.append(chat_id)
            store.event("queue_auto_archive", chat_id, reason="done_verified")
    return archived


def reconcile_false_done_chats(store: Store) -> int:
    """Self-heal chats marked done while an active goal remains."""
    rows = store.rows(
        """
        select c.id from chats c
        where c.done=1 and c.paused=0
          and trim(c.objective) != ''
          and (
            exists (select 1 from goals g where g.chat_id=c.id and g.status='active')
            or exists (
              select 1 from project_priorities p
              where p.target_chat_id=c.id and p.status='active'
            )
          )
        """
    )
    fixed = 0
    for row in rows:
        if reopen_chat_for_goal(store, str(row["id"]), reason="false_done_self_heal"):
            fixed += 1
    return fixed
