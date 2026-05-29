from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from autocode.goal_fleets import (
    GOAL_FLEET_ALIASES,
    clear_stale_l1_lock,
    reconcile_false_complete_fleets,
    tick,
)
from autocode.models import Chat
from autocode.scheduler import Scheduler
from autocode.store import Store
from autocode.util import now_iso


def test_clear_stale_manual_pause_lock(tmp_path: Path):
    lock = tmp_path / ".l1-e2e-lock"
    lock.write_text(
        json.dumps({"pid": 0, "holder": "coord-cli", "run_dir": "manual-pause"}),
        encoding="utf-8",
    )
    with patch("autocode.coordination.l1_lock_path", return_value=lock):
        assert clear_stale_l1_lock() is True
        assert not lock.exists()


def test_reconcile_reopens_done_fleet_when_external_goal_fails(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="grok:grok.sqlite:fleet",
        provider="grok",
        source="grok.new",
        provider_chat_id="fleet",
        title="L1 fleet",
        cwd=str(tmp_path),
        updated_at=now_iso(),
        latest_text="work",
        transcript_hash="h",
        alias=GOAL_FLEET_ALIASES["l1-e2e-verified"],
        continuation="grok",
    )
    store.upsert_chat(chat, 5, "done", "L1 goal")
    store.set_goal(chat.id, "L1 E2E verified", "user")
    with store.connect() as con:
        con.execute("update chats set done=1,state='done' where id=?", (chat.id,))

    status = {
        "all_complete": False,
        "goals": [{"id": "l1-e2e-verified", "complete": False, "pct": 0}],
    }
    reopened = reconcile_false_complete_fleets(store, status)
    assert chat.id in reopened
    row = store.row("select done,state from chats where id=?", (chat.id,))
    assert int(row["done"] or 0) == 0


def test_tick_skips_when_interval_not_due(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "autocode.sqlite")
    store.set_config("last_goal_tick_ts", str(__import__("time").time()))
    sched = Scheduler(store)
    monkeypatch.setattr(sched, "capacity", lambda: 2)
    result = tick(store, sched)
    assert result.get("skipped") == "interval"


def test_tick_records_when_all_complete(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "autocode.sqlite")
    sched = Scheduler(store)
    monkeypatch.setattr("autocode.goal_fleets.load_status", lambda: {"all_complete": True, "goals": []})
    monkeypatch.setattr(sched, "capacity", lambda: 2)
    result = tick(store, sched, force=True)
    assert result.get("all_complete") is True
    assert float(store.get_config("last_goal_tick_ts", "0") or 0) > 0
