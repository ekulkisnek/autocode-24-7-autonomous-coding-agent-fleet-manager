from __future__ import annotations

from sqlite3 import Row

from .config import DEFAULT_MIN_OUTPUT_CHARS, DEFAULT_REQUIRE_FLEET_DONE
from .markers import parse_fleet_marker
from .policy import FLEET_DONE_MARKER, OutputAssessment, assess_output_state
from pathlib import Path

from .store import Store
from .util import now_iso, read_text


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


def verify_goal_complete(store: Store, objective: str, output: str) -> tuple[bool, str]:
    """Return whether job output may mark the chat goal complete."""
    if output_too_minimal(store, output):
        return False, "output too minimal to count as completion"
    assessment = assess_output_state(objective, output)
    if not assessment.complete:
        return False, assessment.reason
    if require_fleet_done(store):
        marker = parse_fleet_marker(output)
        if not ((marker and marker.kind == "FLEET_DONE") or FLEET_DONE_MARKER.search(output or "")):
            return False, "require_fleet_done: missing FLEET_DONE marker"
    return True, assessment.reason


def assess_for_completion(store: Store, objective: str, output: str) -> OutputAssessment:
    if output_too_minimal(store, output):
        return OutputAssessment("stalled", False, "output too minimal to count as completion")
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
        return True
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
            or exists (select 1 from queue q where q.chat_id=c.id)
            or exists (select 1 from queue_finished qf where qf.chat_id=c.id)
          )
        """
    )
    fixed = 0
    for row in rows:
        if reopen_chat_for_goal(store, str(row["id"]), reason="false_done_self_heal"):
            fixed += 1
    return fixed
