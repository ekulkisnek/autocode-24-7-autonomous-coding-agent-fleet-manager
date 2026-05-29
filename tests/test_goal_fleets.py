from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from autocode.goal_fleets import (
    GOAL_FLEET_ALIASES,
    _failure_context_from_run,
    clear_stale_l1_lock,
    find_latest_l1_run_dir,
    maybe_refresh_l1_provider_backoff,
    pause_l1_agent_work_during_shell,
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


def test_failure_context_includes_run_dir_tail(tmp_path: Path):
    run = tmp_path / "l1-run"
    leg = run / "ios-to-android"
    leg.mkdir(parents=True)
    (leg / "detox.log").write_text("\n".join(f"line{i}" for i in range(60)), encoding="utf-8")
    ctx = _failure_context_from_run(run)
    assert "latest_run_dir=" in ctx
    assert "line59" in ctx
    assert "line0" not in ctx


def test_find_latest_l1_run_dir_from_symlink(tmp_path: Path, monkeypatch):
    root = tmp_path / "logs"
    root.mkdir()
    run = root / "l1-simulator-bidirectional-e2e-test"
    run.mkdir()
    (root / "current-l1-simulator-bidirectional-e2e").symlink_to(run)
    monkeypatch.setattr("autocode.goal_fleets.LOG_ROOT", root)
    assert find_latest_l1_run_dir() == run


def test_tick_skips_when_interval_not_due(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "autocode.sqlite")
    store.set_config("last_goal_tick_ts", str(__import__("time").time()))
    sched = Scheduler(store)
    monkeypatch.setattr(sched, "capacity", lambda: 2)
    result = tick(store, sched)
    assert result.get("skipped") == "interval"


def test_maybe_refresh_clears_cursor_when_grok_oauth(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    from autocode.recovery import backoff_seconds
    from autocode.util import iso_from_ts, now_ts

    until = iso_from_ts(now_ts() + backoff_seconds("provider_error", 3))
    with store.connect() as con:
        con.execute(
            "insert into provider_health(provider,failure_count,backoff_until,last_error) values(?,?,?,?)",
            ("grok", 3, until, "Open this URL to sign in: https://auth.x.ai/oauth2/authorize"),
        )
        con.execute(
            "insert into provider_health(provider,failure_count,backoff_until,last_error) values(?,?,?,?)",
            ("cursor", 5, until, "api error"),
        )
    actions = maybe_refresh_l1_provider_backoff(store)
    assert actions.get("cursor") == "cleared_for_l1_grok_oauth"
    row = store.row("select failure_count from provider_health where provider=?", ("cursor",))
    assert int(row["failure_count"] or 0) == 0


def test_pause_l1_agent_work_during_shell(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "autocode.sqlite")
    sched = Scheduler(store)
    chat = Chat(
        id="grok:goal1-worker:l1-sim-detox-fix:abc",
        provider="grok",
        source="grok.new",
        provider_chat_id="w1",
        title="fix",
        cwd=str(tmp_path),
        updated_at=now_iso(),
        latest_text="go",
        transcript_hash="h",
        alias="l1-sim-detox-fix",
        continuation="grok",
    )
    store.upsert_chat(chat, 5, "active", "fix detox")
    monkeypatch.setattr("autocode.goal_fleets._l1_loop_running", lambda: True)
    monkeypatch.setattr("autocode.goal_fleets._l1_orchestrator_running", lambda: False)
    paused, killed = pause_l1_agent_work_during_shell(store, sched)
    assert paused >= 1
    row = store.row("select paused from chats where id=?", (chat.id,))
    assert int(row["paused"] or 0) == 1


def test_tick_records_when_all_complete(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "autocode.sqlite")
    sched = Scheduler(store)
    monkeypatch.setattr("autocode.goal_fleets.load_status", lambda: {"all_complete": True, "goals": []})
    monkeypatch.setattr(sched, "capacity", lambda: 2)
    result = tick(store, sched, force=True)
    assert result.get("all_complete") is True
    assert float(store.get_config("last_goal_tick_ts", "0") or 0) > 0
