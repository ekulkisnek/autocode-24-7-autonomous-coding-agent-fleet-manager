from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import STATE
from .store import Store
from .util import now_iso, now_ts

ACTIONS_FILE = STATE / "watchdog-actions.json"

ALLOWED_TYPES = frozenset({
    "complete_chat",
    "retry_with_prompt",
    "dispatch_provider",
    "kill_job",
    "change_goal",
    "block_completion",
    "reposition_queue",
})


def _load_actions() -> list[dict]:
    if not ACTIONS_FILE.exists():
        return []
    try:
        data = json.loads(ACTIONS_FILE.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_actions(actions: list[dict]) -> None:
    ACTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACTIONS_FILE.write_text(json.dumps(actions, indent=2))


def _hour_ago() -> float:
    return now_ts() - 3600


def _applied_this_hour(actions: list[dict]) -> int:
    cutoff = _hour_ago()
    return sum(
        1 for a in actions
        if a.get("status") == "applied"
        and float(a.get("applied_at_ts") or 0) >= cutoff
    )


def process_deterministic_unblock(store: Store, scheduler: Any | None = None) -> dict[str, Any]:
    """
    Closed-loop fixes that do not require Grok or AUTOCODE_WATCHDOG_AUTO.
    Runs every scheduler.tick() after remediation_pass (see scheduler.py).
    Grok watchdog (grok_watchdog.py) is Signal-only; this module mutates queue/health.
    """
    from . import recovery, self_improve

    result: dict[str, Any] = {
        "si_archived": [],
        "grok_backoff_cleared": False,
    }
    archived = self_improve.reconcile_stalled_self_improvement(store)
    result["si_archived"] = archived
    if archived:
        store.clear_provider_health("grok")
        result["grok_backoff_cleared"] = True
        store.event("watchdog_deterministic_unblock", si_archived=len(archived))
    elif recovery.provider_in_backoff(store, "grok"):
        # Clear stale grok backoff when queue has only SI dead letters left.
        rows = store.rows(
            """
            select c.alias, c.failure_count
            from queue q join chats c on c.id=q.chat_id
            where c.done=0 and c.paused=0
            """
        )
        if rows and all(
            str(r["alias"] or "").startswith("si-")
            and int(r["failure_count"] or 0) >= 8
            for r in rows
        ):
            store.clear_provider_health("grok")
            result["grok_backoff_cleared"] = True
    return result


def process_actions(store: Store, scheduler: Any) -> list[str]:
    """Process pending watchdog actions. Returns applied action IDs. No-op when AUTO is off."""
    auto = os.environ.get("AUTOCODE_WATCHDOG_AUTO", "off").lower() in ("1", "true", "yes", "on")
    if not auto:
        return []

    actions = _load_actions()
    pending = [a for a in actions if a.get("status") == "pending"]
    if not pending:
        return []

    max_tick = int(os.environ.get("AUTOCODE_WATCHDOG_MAX_PER_TICK", "3"))
    max_hour = int(os.environ.get("AUTOCODE_WATCHDOG_MAX_PER_HOUR", "20"))
    threshold = float(os.environ.get("AUTOCODE_WATCHDOG_AUTO_THRESHOLD", "0.9"))
    applied_hour = _applied_this_hour(actions)

    applied: list[str] = []
    dirty = False

    for action in pending:
        if len(applied) >= max_tick:
            break
        if applied_hour + len(applied) >= max_hour:
            break

        action_id = str(action.get("id") or "")
        action_type = str(action.get("type") or "")

        if action_type not in ALLOWED_TYPES:
            action["status"] = "rejected"
            action["reject_reason"] = f"unknown type: {action_type}"
            dirty = True
            store.event("watchdog_action_rejected", reason=f"unknown type: {action_type}", action_id=action_id)
            continue

        # Idempotency: reject if same id was applied in last 1h
        if action_id and any(
            a.get("id") == action_id
            and a.get("status") == "applied"
            and float(a.get("applied_at_ts") or 0) >= _hour_ago()
            for a in actions
            if a is not action
        ):
            action["status"] = "rejected"
            action["reject_reason"] = "idempotency: applied within 1h"
            dirty = True
            continue

        # needs_luke gate: human must apply manually
        if action.get("needs_luke"):
            continue

        # Confidence threshold
        confidence = float(action.get("confidence") or 1.0)
        if confidence < threshold:
            action["status"] = "rejected"
            action["reject_reason"] = f"confidence {confidence:.2f} < threshold {threshold}"
            dirty = True
            continue

        try:
            result = _apply_action(store, action)
            if result:
                action["status"] = "applied"
                action["applied_at"] = now_iso()
                action["applied_at_ts"] = now_ts()
                applied.append(action_id)
                dirty = True
                store.event(
                    "watchdog_action_applied",
                    action_id=action_id,
                    action_type=action_type,
                    chat_id=str(action.get("chat_id") or ""),
                )
            else:
                action["status"] = "rejected"
                action["reject_reason"] = "apply returned False"
                dirty = True
        except Exception as exc:
            action["status"] = "rejected"
            action["reject_reason"] = str(exc)[:200]
            dirty = True
            store.event("watchdog_action_error", action_id=action_id, error=str(exc)[:200])

    if dirty:
        _save_actions(actions)

    return applied


def _apply_action(store: Store, action: dict) -> bool:
    from . import fleet_actions
    atype = str(action.get("type") or "")
    chat_id = str(action.get("chat_id") or "")
    params = action.get("params") or {}

    if atype == "complete_chat":
        return fleet_actions.apply_complete_chat(store, chat_id, str(params.get("reason", "watchdog")))
    if atype == "retry_with_prompt":
        return fleet_actions.apply_retry_with_prompt(store, chat_id, str(params.get("prompt_prefix", "")))
    if atype == "dispatch_provider":
        return fleet_actions.apply_dispatch_provider(store, chat_id, str(params.get("provider", "")))
    if atype == "kill_job":
        return fleet_actions.apply_kill_job(store, chat_id, str(params.get("reason", "watchdog")))
    if atype == "change_goal":
        return fleet_actions.apply_change_goal(store, chat_id, str(params.get("new_objective", "")))
    if atype == "block_completion":
        return fleet_actions.apply_block_completion(store, chat_id, str(params.get("reason", "")))
    if atype == "reposition_queue":
        return fleet_actions.apply_reposition_queue(store, chat_id, float(params.get("position", 50)))
    return False


def append_actions(new_actions: list[dict]) -> int:
    """Append new watchdog actions to the actions file. Returns count appended."""
    if not new_actions:
        return 0
    actions = _load_actions()
    existing_ids = {str(a.get("id") or "") for a in actions}
    added = 0
    for action in new_actions:
        action_id = str(action.get("id") or "")
        if action_id and action_id in existing_ids:
            continue
        if "status" not in action:
            action["status"] = "pending"
        actions.append(action)
        added += 1
    if added:
        _save_actions(actions)
    return added
