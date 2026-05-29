from pathlib import Path

from autocode.models import Chat
from autocode.scheduler import Scheduler
from autocode.store import Store
from autocode.util import json_dumps, now_iso
from autocode import self_improve, watchdog_executor


def _si_chat(chat_id: str, alias: str) -> Chat:
    return Chat(
        id=chat_id,
        provider="grok",
        source="grok.self_improve",
        provider_chat_id=chat_id,
        title=f"self-improve: {alias}",
        cwd="/tmp/autocode",
        updated_at=now_iso(),
        latest_text="",
        transcript_hash="h",
        alias=alias,
        continuation="",
        metadata={"self_improve": True},
    )


def test_si_loop_breaker_archives_max_turns_stall(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = _si_chat("self-improve-provider_error-dead", "si-provider_error")
    store.upsert_chat(chat, 95, "stalled", "fix provider_error")
    store.queue_add(chat.id, 1.0)
    err = tmp_path / "err.txt"
    err.write_text('max_turns exceeded: limit is 40, but got 42 messages\n', encoding="utf-8")
    with store.connect() as con:
        con.execute("update chats set failure_count=8 where id=?", (chat.id,))
        con.execute(
            """
            insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at,evidence_status,evidence_reason)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job-1",
                chat.id,
                "grok",
                "failed",
                0,
                "/tmp",
                "[]",
                "",
                str(tmp_path / "out.txt"),
                str(err),
                now_iso(),
                now_iso(),
                "provider_error",
                "process exited",
            ),
        )

    archived = self_improve.reconcile_stalled_self_improvement(store)
    assert archived == [chat.id]
    assert store.queue_list() == []
    row = store.row("select paused from chats where id=?", (chat.id,))
    assert int(row["paused"]) == 1
    finished = store.queue_finished_list(5)
    assert finished and finished[0]["reason"] == "si_loop_breaker"


def test_si_loop_breaker_skips_below_threshold(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = _si_chat("self-improve-provider_error-young", "si-provider_error")
    store.upsert_chat(chat, 95, "stalled", "fix")
    store.queue_add(chat.id, 1.0)
    err = tmp_path / "err.txt"
    err.write_text("max_turns exceeded\n", encoding="utf-8")
    with store.connect() as con:
        con.execute("update chats set failure_count=3 where id=?", (chat.id,))
        con.execute(
            """
            insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at,evidence_status,evidence_reason)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job-2",
                chat.id,
                "grok",
                "failed",
                0,
                "/tmp",
                "[]",
                "",
                str(tmp_path / "out.txt"),
                str(err),
                now_iso(),
                now_iso(),
                "provider_error",
                "process exited",
            ),
        )

    assert self_improve.reconcile_stalled_self_improvement(store) == []
    assert len(store.queue_list()) == 1


def test_process_deterministic_unblock_clears_grok_backoff(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "autocode.sqlite")
    store.record_provider_failure("grok", "max_turns exceeded")
    chat = _si_chat("self-improve-goal_incomplete-dead", "si-goal_incomplete")
    store.upsert_chat(chat, 95, "stalled", "fix")
    store.queue_add(chat.id, 1.0)
    err = tmp_path / "err.txt"
    err.write_text("max_turns exceeded\n", encoding="utf-8")
    with store.connect() as con:
        con.execute("update chats set failure_count=8 where id=?", (chat.id,))
        con.execute(
            """
            insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at,evidence_status,evidence_reason)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job-3",
                chat.id,
                "grok",
                "failed",
                0,
                "/tmp",
                "[]",
                "",
                str(tmp_path / "out.txt"),
                str(err),
                now_iso(),
                now_iso(),
                "provider_error",
                "process exited",
            ),
        )

    from autocode import recovery

    assert recovery.provider_in_backoff(store, "grok")
    result = watchdog_executor.process_deterministic_unblock(store)
    assert result["si_archived"] == [chat.id]
    assert result["grok_backoff_cleared"] is True
    assert recovery.provider_in_backoff(store, "grok") is False


def test_watchdog_executor_noop_when_needs_luke(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "autocode.sqlite")
    actions_path = tmp_path / "watchdog-actions.json"
    monkeypatch.setattr(watchdog_executor, "ACTIONS_FILE", actions_path)
    monkeypatch.setenv("AUTOCODE_WATCHDOG_AUTO", "on")
    chat = _si_chat("self-improve-silent_failed-dead", "si-silent_failed")
    store.upsert_chat(chat, 95, "stalled", "fix")
    store.queue_add(chat.id, 1.0)
    with store.connect() as con:
        con.execute("update chats set failure_count=8 where id=?", (chat.id,))

    actions_path.write_text(
        json_dumps(
            [
                {
                    "id": "wa-test-1",
                    "status": "pending",
                    "type": "complete_chat",
                    "chat_id": chat.id,
                    "confidence": 0.95,
                    "needs_luke": True,
                    "params": {"reason": "test"},
                }
            ]
        ),
        encoding="utf-8",
    )

    applied = watchdog_executor.process_actions(store, None)
    assert applied == []
    data = __import__("json").loads(actions_path.read_text())
    assert data[0]["status"] == "pending"


def test_capacity_min_one_slot_15_to_25_mem(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "autocode.sqlite")
    store.set_config("yolo", "on")
    store.set_config("max_active", "5")
    chat = Chat(
        id="grok:grok.new:work",
        provider="grok",
        source="grok.new",
        provider_chat_id="work",
        title="Real work",
        cwd=str(tmp_path),
        updated_at=now_iso(),
        latest_text="fix",
        transcript_hash="h",
        alias="real-work",
        continuation="grok",
    )
    store.upsert_chat(chat, 10, "active", "ship feature")
    store.queue_add(chat.id, 1.0)
    sched = Scheduler(store)
    monkeypatch.setattr("autocode.scheduler.memory_free_percent", lambda: 18)
    monkeypatch.setattr("autocode.scheduler.load1", lambda: 2.0)
    monkeypatch.setattr("autocode.scheduler.disk_free_gb", lambda _p: 50.0)
    assert sched.capacity() >= 1
