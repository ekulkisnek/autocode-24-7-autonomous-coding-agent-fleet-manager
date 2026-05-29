from __future__ import annotations

from .store import Store
from .util import json_dumps, json_loads, now_iso, now_ts


def _get_meta(store: Store, chat_id: str) -> dict:
    row = store.row("select metadata_json from chats where id=?", (chat_id,))
    if not row:
        return {}
    data = json_loads(str(row["metadata_json"] or ""), {})
    return data if isinstance(data, dict) else {}


def _save_meta(store: Store, chat_id: str, meta: dict) -> None:
    with store.connect() as con:
        con.execute(
            "update chats set metadata_json=? where id=?",
            (json_dumps(meta), chat_id),
        )


def apply_complete_chat(store: Store, chat_id: str, reason: str) -> bool:
    """Mark chat done and archive from queue."""
    from . import goals
    return goals.mark_goal_complete(store, chat_id, reason, kill_running=True, archive=True)


def apply_retry_with_prompt(store: Store, chat_id: str, prompt_prefix: str) -> bool:
    """Prepend remediation prompt and schedule immediate retry."""
    row = store.row("select metadata_json from chats where id=?", (chat_id,))
    if not row:
        return False
    meta = _get_meta(store, chat_id)
    existing = str(meta.get("remediation_prompt_prefix") or "")
    meta["remediation_prompt_prefix"] = prompt_prefix + ("\n\n" + existing if existing else "")
    meta["next_retry_at"] = now_ts()
    with store.connect() as con:
        con.execute(
            "update chats set metadata_json=?, state='stalled' where id=? and done=0",
            (json_dumps(meta), chat_id),
        )
    store.queue_bump_front(chat_id)
    store.event("watchdog_retry_with_prompt", chat_id, prefix_len=len(prompt_prefix))
    return True


def apply_dispatch_provider(store: Store, chat_id: str, provider: str) -> bool:
    """Set a provider hint so the next dispatch uses this provider."""
    meta = _get_meta(store, chat_id)
    meta["gw_provider_hint"] = provider
    meta["gw_updated_at"] = now_iso()
    _save_meta(store, chat_id, meta)
    store.event("watchdog_provider_hint", chat_id, provider=provider)
    return True


def apply_kill_job(store: Store, chat_id: str, reason: str) -> bool:
    """Kill the running job for this chat."""
    from .runner import JobRunner
    JobRunner(store).kill_chat_jobs(chat_id, reason)
    store.event("watchdog_kill_job", chat_id, reason=reason[:200])
    return True


def apply_change_goal(store: Store, chat_id: str, new_objective: str) -> bool:
    """Update the chat's objective."""
    if not new_objective.strip():
        return False
    with store.connect() as con:
        con.execute("update chats set objective=? where id=?", (new_objective, chat_id))
    store.event("watchdog_change_goal", chat_id, new_objective=new_objective[:200])
    return True


def apply_block_completion(store: Store, chat_id: str, reason: str) -> bool:
    """Set gw_completion_override=reject so verify_goal_complete returns False."""
    meta = _get_meta(store, chat_id)
    meta["gw_completion_override"] = "reject"
    meta["gw_completion_reason"] = reason
    meta["gw_updated_at"] = now_iso()
    _save_meta(store, chat_id, meta)
    store.event("watchdog_block_completion", chat_id, reason=reason[:200])
    return True


def apply_reposition_queue(store: Store, chat_id: str, position: float) -> bool:
    """Move a chat to a new queue position."""
    with store.connect() as con:
        con.execute("update queue set position=? where chat_id=?", (position, chat_id))
    store.event("watchdog_reposition", chat_id, new_position=position)
    return True
