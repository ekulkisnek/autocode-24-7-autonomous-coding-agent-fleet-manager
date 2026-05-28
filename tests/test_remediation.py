from pathlib import Path
from unittest.mock import patch

from autocode import remediation
from autocode.models import Chat
from autocode.store import Store
from autocode.util import now_iso


def test_needs_luke_only_when_paused_or_exhausted(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="cursor:cursor.ide:auth",
        provider="cursor",
        source="cursor.ide",
        provider_chat_id="auth",
        title="Cursor auth",
        cwd=str(tmp_path),
        updated_at=now_iso(),
        latest_text="work",
        transcript_hash="h1",
        alias="cursor-auth",
        continuation="cursor",
    )
    store.upsert_chat(chat, 5, "running", "Configure cursor agent authentication")
    store.queue_add(chat.id, 1.0)
    need, _ = remediation.needs_luke(store, chat.id)
    assert need is False

    store.pause_chat(chat.id)
    need, reason = remediation.needs_luke(store, chat.id)
    assert need is True
    assert "paused" in reason


def test_attempt_silent_remediation(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="cursor:cursor.ide:silent",
        provider="cursor",
        source="cursor.ide",
        provider_chat_id="silent",
        title="Silent cursor",
        cwd=str(tmp_path),
        updated_at=now_iso(),
        latest_text="work",
        transcript_hash="h2",
        alias="silent-cursor",
        continuation="cursor",
    )
    store.upsert_chat(chat, 5, "running", "Cursor agent authentication configuration")
    store.queue_add(chat.id, 1.0)
    job_dir = tmp_path / "job-silent"
    job_dir.mkdir()
    stdout = job_dir / "stdout.txt"
    stderr = job_dir / "stderr.txt"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    with store.connect() as con:
        con.execute(
            """
            insert into jobs(
              id,chat_id,provider,status,pid,cwd,cmd_json,prompt,
              stdout_path,stderr_path,created_at,updated_at,evidence_status
            ) values(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job-silent",
                chat.id,
                "cursor",
                "running",
                0,
                str(tmp_path),
                "[]",
                "p",
                str(stdout),
                str(stderr),
                "2020-01-01T00:00:00-06:00",
                now_iso(),
                "running_silent",
            ),
        )
    job = store.row("select * from jobs where id='job-silent'")
    from autocode.runner import JobRunner

    with patch.object(remediation, "kickstart_my_machines_worker", return_value=(True, "ok")):
        with patch.object(JobRunner, "kill_chat_jobs", return_value=1):
            assert remediation.attempt_silent_remediation(store, job)
    meta = store.row("select metadata_json from chats where id=?", (chat.id,))
    assert "remediation_attempts" in (meta["metadata_json"] or "")


def test_decompose_impossible_writes_artifact(tmp_path: Path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(remediation, "STATE", state_dir)
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="cursor:cursor.ide:impossible",
        provider="cursor",
        source="cursor.ide",
        provider_chat_id="impossible",
        title="Cursor sync",
        cwd=str(tmp_path),
        updated_at=now_iso(),
        latest_text="work",
        transcript_hash="h3",
        alias="cursor-sync",
        continuation="cursor",
    )
    store.upsert_chat(chat, 5, "active", "Bulk upload IDE chats to cursor.com")
    store.queue_add(chat.id, 1.0)
    meta = {"remediation_attempts": remediation.DEFAULT_MAX_REMEDIATION_ATTEMPTS}
    from autocode.util import json_dumps

    with store.connect() as con:
        con.execute(
            "update chats set metadata_json=? where id=?",
            (json_dumps(meta), chat.id),
        )
    with patch.object(remediation, "kickstart_my_machines_worker", return_value=(False, "fail")):
        with patch.object(remediation, "run_remediation_hook", return_value=(False, "")):
            ok = remediation.decompose_impossible_goal(store, chat.id, reason="no api")
    assert ok
    row = store.row("select done from chats where id=?", (chat.id,))
    assert row["done"] == 1
    docs = list((state_dir / "remediation").glob("*.md"))
    assert docs
