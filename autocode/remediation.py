from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from sqlite3 import Row
from typing import Any

from .config import (
    DEFAULT_EXTERNAL_IDLE_REMEDIATION_SECONDS,
    DEFAULT_MAX_REMEDIATION_ATTEMPTS,
    DEFAULT_SILENT_REMEDIATION_SECONDS,
    HOME,
    STATE,
)
from . import goals
from . import recovery
from .store import Store
from .util import json_dumps, json_loads, now_iso, now_ts, parse_ts

CURSOR_BRIDGE_CLOSED = re.compile(
    r"bridge.*closed\s+instead\s+of\s+connected|no\s+bulk\s+cursor\s+api|cannot\s+upload.*cursor\.com",
    re.I,
)
CURSOR_AUTH_OBJECTIVE = re.compile(
    r"\b(cursor\s+agent|cursor\s+ide|authentication|my-machines|bridge)\b",
    re.I,
)
MY_MACHINES_WORKER = "com.cursor.my-machines-worker"


def chat_metadata(row: Row | None) -> dict[str, Any]:
    if not row:
        return {}
    data = json_loads(str(row["metadata_json"] or ""), {})
    return data if isinstance(data, dict) else {}


def remediation_attempts(meta: dict[str, Any]) -> int:
    return int(meta.get("remediation_attempts") or 0)


def record_remediation(store: Store, chat_id: str, action: str, detail: str = "") -> int:
    row = store.row("select metadata_json from chats where id=?", (chat_id,))
    meta = chat_metadata(row)
    count = remediation_attempts(meta) + 1
    meta["remediation_attempts"] = count
    meta["last_remediation_action"] = action
    meta["last_remediation_at"] = now_iso()
    if detail:
        meta["last_remediation_detail"] = detail[:500]
    with store.connect() as con:
        con.execute("update chats set metadata_json=? where id=?", (json_dumps(meta), chat_id))
    store.event("remediation", chat_id, action=action, attempt=count, detail=detail[:300])
    return count


def run_remediation_hook(pattern: str, chat_id: str) -> tuple[bool, str]:
    hook = os.environ.get("AUTOCODE_REMEDIATION_HOOK", "").strip()
    if not hook:
        return False, "no hook configured"
    try:
        proc = subprocess.run(
            [hook, pattern, chat_id],
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = (proc.stdout or proc.stderr or "").strip()
        return proc.returncode == 0, out[:500]
    except Exception as exc:
        return False, str(exc)


def kickstart_my_machines_worker() -> tuple[bool, str]:
    uid = os.getuid()
    target = f"gui/{uid}/{MY_MACHINES_WORKER}"
    try:
        proc = subprocess.run(
            ["launchctl", "kickstart", "-k", target],
            capture_output=True,
            text=True,
            timeout=30,
        )
        msg = (proc.stdout or proc.stderr or "").strip()
        return proc.returncode == 0, msg[:300]
    except Exception as exc:
        return False, str(exc)


def _job_silent_age_seconds(job: Row) -> float:
    return max(0.0, now_ts() - parse_ts(job["created_at"]))


def _remediation_threshold(job: Row) -> int:
    status = str(job["evidence_status"] or "")
    if status == "running_external_idle":
        return DEFAULT_EXTERNAL_IDLE_REMEDIATION_SECONDS
    return DEFAULT_SILENT_REMEDIATION_SECONDS


def _cursor_auth_chat(store: Store, chat_id: str) -> bool:
    row = store.row("select objective,alias,title from chats where id=?", (chat_id,))
    if not row:
        return False
    hay = " ".join(str(row[k] or "") for k in ("objective", "alias", "title"))
    return bool(CURSOR_AUTH_OBJECTIVE.search(hay))


def _write_handoff_artifact(chat_id: str, objective: str) -> Path:
    doc_dir = STATE / "remediation"
    doc_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", chat_id.split(":")[-1][:40]).strip("-").lower()
    path = doc_dir / f"cursor-handoff-{slug or 'chat'}.md"
    body = (
        f"# Cursor IDE→cursor.com sync limitation\n\n"
        f"Chat: `{chat_id}`\n\n"
        f"Goal: {objective}\n\n"
        f"AutoCode cannot bulk-upload IDE transcripts to cursor.com (bridge closed / no public API).\n"
        f"Achievable milestone: document per-chat manual handoff steps and verify bridge after "
        f"`launchctl kickstart -k gui/$UID/com.cursor.my-machines-worker`.\n\n"
        f"Generated: {now_iso()}\n"
    )
    path.write_text(body, encoding="utf-8")
    return path


def decompose_impossible_goal(store: Store, chat_id: str, *, reason: str) -> bool:
    """Complete with documentation artifact when bulk automation is impossible."""
    row = store.row("select objective from chats where id=?", (chat_id,))
    if not row:
        return False
    objective = str(row["objective"] or "")
    path = _write_handoff_artifact(chat_id, objective)
    record_remediation(store, chat_id, "decompose_impossible", str(path))
    goals.mark_goal_complete(
        store,
        chat_id,
        f"impossible bulk API — documented handoff at {path.name}",
        kill_running=True,
    )
    store.event("goal_decomposed", chat_id, artifact=str(path), reason=reason[:300])
    return True


def remediation_note(action: str, detail: str) -> str:
    lines = [
        "REMEDIATION: prior run stalled without progress. Take corrective action immediately.",
        f"Remediation step: {action}",
    ]
    if detail:
        lines.append(f"Detail: {detail}")
    if action == "restart_my_machines_worker":
        lines.extend(
            [
                "Run or verify: launchctl kickstart -k gui/$UID/com.cursor.my-machines-worker",
                "Then document per-chat IDE→cursor.com handoff if bulk upload remains impossible.",
                "End with FLEET_DONE when the documented milestone is complete.",
            ]
        )
    return "\n".join(lines) + "\n\n"


def attempt_silent_remediation(store: Store, job: Row) -> bool:
    """Kill stale silent/idle job and schedule retry with remediation prompt."""
    from .runner import JobRunner

    chat_id = str(job["chat_id"])
    chat = store.row("select * from chats where id=?", (chat_id,))
    if not chat or int(chat["done"] or 0) or int(chat["paused"] or 0):
        return False
    meta = chat_metadata(chat)
    if remediation_attempts(meta) >= DEFAULT_MAX_REMEDIATION_ATTEMPTS:
        return False

    age = _job_silent_age_seconds(job)
    if age < _remediation_threshold(job):
        return False

    ev = str(job["evidence_status"] or "")
    if ev not in {"running_silent", "running_external_idle"}:
        return False

    action = "kill_stale_silent"
    detail = f"{ev} for {int(age)}s"
    if _cursor_auth_chat(store, chat_id):
        ok, msg = kickstart_my_machines_worker()
        hook_ok, hook_msg = run_remediation_hook("cursor_auth", chat_id)
        detail = "; ".join(x for x in (msg, hook_msg) if x)
        action = "restart_my_machines_worker"
        if not ok and not hook_ok and remediation_attempts(meta) + 1 >= DEFAULT_MAX_REMEDIATION_ATTEMPTS:
            return decompose_impossible_goal(
                store,
                chat_id,
                reason=f"remediation exhausted: {detail}",
            )

    count = record_remediation(store, chat_id, action, detail)
    JobRunner(store).kill_chat_jobs(chat_id, f"remediation_{action}")
    row = store.row("select metadata_json from chats where id=?", (chat_id,))
    meta = chat_metadata(row)
    meta["remediation_prompt_prefix"] = remediation_note(action, detail)
    with store.connect() as con:
        con.execute("update chats set metadata_json=? where id=?", (json_dumps(meta), chat_id))
    recovery.schedule_retry(
        store,
        chat_id,
        kind="silent_failed",
        evidence_status=ev,
        evidence_reason=f"auto-remediation {action} (attempt {count}): {detail}",
        job_id=str(job["id"]),
        immediate=True,
    )
    store.event("remediation_scheduled", chat_id, action=action, attempt=count)
    return True


def remediation_pass(store: Store) -> dict[str, Any]:
    """Daemon/doctor pass: overdelivery, done-queue cleanup, silent remediation."""
    result: dict[str, Any] = {
        "overdelivery_completed": [],
        "remediated": [],
        "decomposed": [],
    }
    result["overdelivery_completed"] = goals.auto_complete_overdelivery(store)

    jobs = store.rows(
        """
        select j.* from jobs j
        join chats c on c.id=j.chat_id
        join queue q on q.chat_id=j.chat_id
        where j.status='running'
          and j.evidence_status in ('running_silent', 'running_external_idle')
          and c.done=0 and c.paused=0
        """
    )
    for job in jobs:
        if attempt_silent_remediation(store, job):
            result["remediated"].append(str(job["chat_id"]))
    return result


def needs_luke(store: Store, chat_id: str) -> tuple[bool, str]:
    """Whether human action is required (for fleet reporting)."""
    row = store.row("select * from chats where id=?", (chat_id,))
    if not row:
        return False, ""
    if int(row["paused"] or 0):
        return True, "user paused"
    meta = chat_metadata(row)
    if remediation_attempts(meta) >= DEFAULT_MAX_REMEDIATION_ATTEMPTS:
        last = str(meta.get("last_remediation_action") or "")
        if last != "decompose_impossible":
            return True, f"remediation failed {DEFAULT_MAX_REMEDIATION_ATTEMPTS}x ({last})"
    cap = recovery.max_failure_count(store, chat_id)
    if int(row["failure_count"] or 0) >= cap:
        return True, "max retries exhausted"
    if str(row["state"] or "") == "blocked":
        return True, "blocked chat"
    return False, ""
