import json
from pathlib import Path

from autocode.models import Chat
from autocode.recovery import (
    backoff_seconds,
    failure_kind,
    reconcile_killed_chats,
    retry_ready,
    schedule_retry,
    should_use_fallback,
)
from autocode.runner import JobRunner
from autocode.scheduler import Scheduler
from autocode.store import Store
from autocode.util import json_dumps, now_iso, now_ts


def test_silent_failed_schedules_retry_with_backoff(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="grok:grok.sqlite:retry",
        provider="grok",
        source="grok.sqlite",
        provider_chat_id="retry",
        title="Retry me",
        cwd=str(tmp_path),
        updated_at="2026-05-21T00:00:00-05:00",
        latest_text="work",
        transcript_hash="h1",
        alias="retry",
        continuation="grok",
    )
    store.upsert_chat(chat, 5, "stalled", "finish task")
    store.queue_add(chat.id, 1.0)
    with store.connect() as con:
        con.execute("update chats set failure_count=1 where id=?", (chat.id,))

    assert schedule_retry(
        store,
        chat.id,
        kind="silent_failed",
        evidence_status="silent_failed",
        evidence_reason="timed out after 610s",
    )

    row = store.row("select * from chats where id=?", (chat.id,))
    meta = json.loads(row["metadata_json"])
    assert meta["last_failure_kind"] == "silent_failed"
    assert meta["stall_extra_seconds"] == 300
    assert float(meta["next_retry_at"]) > now_ts()
    assert not retry_ready(row)


def test_runner_failure_schedules_recovery(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="grok:grok.sqlite:job-fail",
        provider="grok",
        source="grok.sqlite",
        provider_chat_id="job-fail",
        title="Fail job",
        cwd=str(tmp_path),
        updated_at="2026-05-21T00:00:00-05:00",
        latest_text="work",
        transcript_hash="h1",
        alias="job-fail",
        continuation="grok",
    )
    store.upsert_chat(chat, 5, "active", "finish")
    store.queue_add(chat.id, 1.0)
    job_dir = tmp_path / "job-fail"
    job_dir.mkdir()
    stdout = job_dir / "stdout.txt"
    stderr = job_dir / "stderr.txt"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    with store.connect() as con:
        con.execute(
            """
            insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at)
            values(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job-fail",
                chat.id,
                "grok",
                "running",
                0,
                str(tmp_path),
                "[]",
                "prompt",
                str(stdout),
                str(stderr),
                now_iso(),
                now_iso(),
            ),
        )

    JobRunner(store).refresh()

    events = store.rows("select kind from events where chat_id=? order by id desc", (chat.id,))
    kinds = [row["kind"] for row in events]
    assert "recovery_scheduled" in kinds
    row = store.row("select failure_count,state from chats where id=?", (chat.id,))
    assert row["failure_count"] == 1
    assert row["state"] == "stalled"


def test_killed_chat_paused_without_pause_flag_is_unstuck(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="cursor:cursor.cli:unstick",
        provider="cursor",
        source="cursor.cli",
        provider_chat_id="unstick",
        title="Unstick",
        cwd=str(tmp_path),
        updated_at="2026-05-21T00:00:00-05:00",
        latest_text="work",
        transcript_hash="h1",
        alias="unstick",
        continuation="cursor-agent",
    )
    store.upsert_chat(chat, 5, "paused", "keep going")
    store.queue_add(chat.id, 1.0)
    with store.connect() as con:
        con.execute("update chats set paused=0,state='paused' where id=?", (chat.id,))
        con.execute(
            """
            insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at,completed_at,evidence_status,evidence_reason)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job-killed",
                chat.id,
                "cursor",
                "killed",
                0,
                str(tmp_path),
                "[]",
                "prompt",
                str(tmp_path / "o.txt"),
                str(tmp_path / "e.txt"),
                now_iso(),
                now_iso(),
                now_iso(),
                "killed",
                "chat_paused",
            ),
        )

    assert reconcile_killed_chats(store) == 1
    row = store.row("select state,paused from chats where id=?", (chat.id,))
    assert row["state"] == "stalled"
    assert row["paused"] == 0


def test_grok_new_provider_error_uses_fallback_after_one_failure(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="grok:grok.new:abc123",
        provider="grok",
        source="grok.new",
        provider_chat_id="abc123",
        title="New grok",
        cwd=str(tmp_path),
        updated_at="2026-05-21T00:00:00-05:00",
        latest_text="work",
        transcript_hash="h1",
        alias="new-grok",
        continuation="grok",
    )
    store.upsert_chat(chat, 5, "stalled", "goal")
    with store.connect() as con:
        con.execute(
            "update chats set failure_count=1,metadata_json=? where id=?",
            (
                json_dumps({"last_failure_kind": "provider_error"}),
                chat.id,
            ),
        )
    row = store.find_chat("new-grok")
    assert should_use_fallback(row) is True


def test_candidates_skip_until_retry_backoff_elapsed(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="codex:codex.rollout:backoff",
        provider="codex",
        source="codex.rollout",
        provider_chat_id="backoff",
        title="Backoff",
        cwd=str(tmp_path),
        updated_at="2026-05-21T00:00:00-05:00",
        latest_text="work",
        transcript_hash="h1",
        alias="backoff",
        continuation="codex exec resume",
    )
    store.upsert_chat(chat, 5, "stalled", "goal")
    store.queue_add(chat.id, 1.0)
    future = now_ts() + 600
    with store.connect() as con:
        con.execute(
            "update chats set metadata_json=? where id=?",
            (json_dumps({"next_retry_at": future}), chat.id),
        )

    assert Scheduler(store).candidates(5) == []

    with store.connect() as con:
        con.execute(
            "update chats set metadata_json=? where id=?",
            (json_dumps({"next_retry_at": now_ts() - 1}), chat.id),
        )
    assert len(Scheduler(store).candidates(5)) == 1


def test_backoff_grows_with_failure_count():
    assert backoff_seconds("silent_failed", 1) <= backoff_seconds("silent_failed", 4)
    assert backoff_seconds("provider_error", 3) >= 45


def test_failure_kind_maps_stall_timeout():
    assert failure_kind("silent_failed", "timed out after 610s") == "silent_failed"
    assert failure_kind("killed", "chat_paused") == "killed"
    assert failure_kind("provider_error", "process exited") == "provider_error"
