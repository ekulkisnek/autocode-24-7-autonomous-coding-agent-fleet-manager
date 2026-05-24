from pathlib import Path

from autocode.dashboard import model_info, render_dashboard
from autocode.models import Chat
from autocode.store import Store
from autocode.util import json_dumps, now_iso


def test_dashboard_renders_running_job_model_and_usage(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="cursor:cursor.cli:agent-123",
        provider="cursor",
        source="cursor.cli",
        provider_chat_id="agent-123",
        alias="redwallet-cursor-helper",
        title="RedWallet security audit",
        cwd="/tmp/redwallet",
        updated_at=now_iso(),
        latest_text="Audit wallet persistence.",
        continuation="cursor-agent --resume",
        metadata={"model": "composer-2.5", "active": True},
    )
    store.upsert_chat(chat, coding_score=3, state="active", objective="Make RedWallet safer and cleaner.")
    with store.connect() as con:
        con.execute(
            """
            insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at,evidence_status)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job-test",
                chat.id,
                "cursor",
                "running",
                0,
                "/tmp/redwallet",
                json_dumps(["cursor-agent", "--resume", "agent-123", "--model", "composer-2.5", "continue"]),
                "Continue the RedWallet audit.",
                str(tmp_path / "stdout.txt"),
                str(tmp_path / "stderr.txt"),
                now_iso(),
                now_iso(),
                "running_working",
            ),
        )
    (tmp_path / "stdout.txt").write_text("Inspecting auth and persistence flows.", encoding="utf-8")
    (tmp_path / "stderr.txt").write_text("", encoding="utf-8")

    text = render_dashboard(store, width=120, limit=5, refresh_jobs=False)

    assert "AutoCode Dashboard" in text
    assert "Driving Now" in text
    assert "composer-2.5" in text
    assert "redwallet-cursor-helper" in text
    assert "Provider Usage / Health" in text
    assert "cursor" in text
    assert "remaining" in text
    assert "unknown" in text


def test_model_info_reads_effort_and_fast_model(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    with store.connect() as con:
        con.execute(
            """
            insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at)
            values(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job-model",
                "grok:grok.sqlite:1",
                "grok",
                "running",
                0,
                "/tmp",
                json_dumps(["grok", "--model", "grok-build-fast", "--effort", "high"]),
                "go",
                str(tmp_path / "out.txt"),
                str(tmp_path / "err.txt"),
                now_iso(),
                now_iso(),
            ),
        )
    row = store.row("select * from jobs where id='job-model'")

    info = model_info(row)

    assert info.model == "grok-build-fast"
    assert info.effort == "high"
    assert info.speed == "fast"
