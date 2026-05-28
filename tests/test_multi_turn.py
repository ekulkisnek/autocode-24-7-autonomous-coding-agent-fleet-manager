import json
from pathlib import Path

from autocode.models import Chat
from autocode.policy import build_prompt
from autocode.runner import JobRunner
from autocode.scheduler import Scheduler
from autocode.store import Store
from autocode.util import now_iso, now_ts


def test_worked_milestone_schedules_immediate_next_turn(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="codex:codex.rollout:multi",
        provider="codex",
        source="codex.rollout",
        provider_chat_id="multi",
        title="Multi turn",
        cwd=str(tmp_path),
        updated_at="2026-05-21T00:00:00-05:00",
        latest_text="work",
        transcript_hash="h1",
        alias="multi",
        continuation="codex exec resume",
    )
    store.upsert_chat(chat, 5, "active", "Ship feature with tests.")
    store.queue_add(chat.id, 1.0)

    job_dir = tmp_path / "job-milestone"
    job_dir.mkdir()
    stdout = job_dir / "stdout.txt"
    stderr = job_dir / "stderr.txt"
    stdout.write_text(
        'FLEET_MILESTONE: {"status":"active","summary":"tests still running","next_action":"inspect log"}\n',
        encoding="utf-8",
    )
    stderr.write_text("", encoding="utf-8")
    with store.connect() as con:
        con.execute(
            """
            insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at)
            values(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job-milestone",
                chat.id,
                "codex",
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

    row = store.row("select done,state,metadata_json from chats where id=?", (chat.id,))
    meta = json.loads(row["metadata_json"])
    assert row["done"] == 0
    assert row["state"] == "stalled"
    assert meta.get("last_failure_kind") == "goal_incomplete"
    assert float(meta.get("next_retry_at") or 0) <= now_ts() + 1
    assert "FLEET_MILESTONE" in meta.get("last_job_summary", "")
    assert Scheduler(store).candidates(5)


def test_build_prompt_includes_prior_job_context(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="grok:grok.sqlite:ctx",
        provider="grok",
        source="grok.sqlite",
        provider_chat_id="ctx",
        title="Ctx",
        cwd=str(tmp_path),
        updated_at="2026-05-21T00:00:00-05:00",
        latest_text="older transcript",
        transcript_hash="h1",
        alias="ctx",
        continuation="grok",
    )
    store.upsert_chat(chat, 5, "active", "Finish the feature.")
    store.record_job_turn_context(
        chat.id,
        job_id="job-prev",
        evidence_status="worked",
        summary="Implemented parser and added unit tests.",
        reason="milestone",
    )
    row = store.find_chat("ctx")
    prompt = build_prompt(Scheduler(store)._row_with_plan(row))
    assert "Prior AutoCode turn" in prompt
    assert "Implemented parser" in prompt
    assert "older transcript" in prompt
