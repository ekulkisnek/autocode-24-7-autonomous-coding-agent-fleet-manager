import json
from pathlib import Path

from autocode import goals
from autocode.models import Chat
from autocode.recovery import schedule_retry
from autocode.runner import JobRunner
from autocode.scheduler import Scheduler
from autocode.store import Store
from autocode.util import json_dumps, now_iso, now_ts


def test_false_complete_rejected_without_fleet_done(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    store.set_config("require_fleet_done", "on")
    objective = "Ship the feature with tests passing."
    output = "All done. Tests passed and verified in CI with full pytest coverage for the feature."
    ok, reason = goals.verify_goal_complete(store, objective, output)
    assert ok is False
    assert "FLEET_DONE" in reason


def test_fleet_done_accepts_verified_completion(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    store.set_config("require_fleet_done", "on")
    objective = "Ship the feature with tests passing."
    output = (
        'FLEET_DONE: {"status":"done","summary":"shipped","evidence":["pytest green"]}\n'
        "Implemented and verified. Tests passed.\n"
    )
    ok, _ = goals.verify_goal_complete(store, objective, output)
    assert ok is True


def test_minimal_worked_output_rejected(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="grok:grok.sqlite:minimal",
        provider="grok",
        source="grok.sqlite",
        provider_chat_id="minimal",
        title="Minimal",
        cwd=str(tmp_path),
        updated_at="2026-05-21T00:00:00-05:00",
        latest_text="work",
        transcript_hash="h1",
        alias="minimal",
        continuation="grok",
    )
    store.upsert_chat(chat, 5, "active", "finish task")
    store.set_goal(chat.id, "finish task")
    store.queue_add(chat.id, 1.0)
    job_dir = tmp_path / "job-minimal"
    job_dir.mkdir()
    stdout = job_dir / "stdout.txt"
    stderr = job_dir / "stderr.txt"
    stdout.write_text("ok\n", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    with store.connect() as con:
        con.execute(
            """
            insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at)
            values(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job-minimal",
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

    row = store.row("select done,state from chats where id=?", (chat.id,))
    job = store.row("select evidence_status from jobs where id='job-minimal'")
    assert row["done"] == 0
    assert job["evidence_status"] == "goal_incomplete"
    kinds = [r["kind"] for r in store.rows("select kind from events where chat_id=?", (chat.id,))]
    assert "completion_rejected" in kinds or "recovery_scheduled" in kinds


def test_silent_failed_still_schedules_retry(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="grok:grok.sqlite:silent",
        provider="grok",
        source="grok.sqlite",
        provider_chat_id="silent",
        title="Silent",
        cwd=str(tmp_path),
        updated_at="2026-05-21T00:00:00-05:00",
        latest_text="work",
        transcript_hash="h1",
        alias="silent",
        continuation="grok",
    )
    store.upsert_chat(chat, 5, "active", "finish")
    store.queue_add(chat.id, 1.0)
    assert schedule_retry(
        store,
        chat.id,
        kind="silent_failed",
        evidence_status="silent_failed",
        evidence_reason="no output",
    )
    meta = json.loads(store.row("select metadata_json from chats where id=?", (chat.id,))["metadata_json"])
    assert meta["last_failure_kind"] == "silent_failed"


def test_provider_error_records_backoff(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    store.record_provider_failure("grok", "api error")
    row = store.row("select backoff_until from provider_health where provider='grok'")
    assert row and row["backoff_until"]


def test_false_done_chat_reopened_on_tick(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="codex:codex.rollout:false-done",
        provider="codex",
        source="codex.rollout",
        provider_chat_id="false-done",
        title="False done",
        cwd=str(tmp_path),
        updated_at="2026-05-21T00:00:00-05:00",
        latest_text="work",
        transcript_hash="h1",
        alias="false-done",
        continuation="codex exec resume",
    )
    store.upsert_chat(chat, 5, "done", "keep going")
    store.set_goal(chat.id, "keep going until verified")
    store.queue_add(chat.id, 1.0)
    with store.connect() as con:
        con.execute("update chats set done=1,state='done' where id=?", (chat.id,))

    Scheduler(store).tick(dispatch=False)

    row = store.row("select done,state from chats where id=?", (chat.id,))
    assert row["done"] == 0
    assert row["state"] == "active"


def test_paused_chat_not_reopened(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="codex:codex.rollout:paused",
        provider="codex",
        source="codex.rollout",
        provider_chat_id="paused",
        title="Paused",
        cwd=str(tmp_path),
        updated_at="2026-05-21T00:00:00-05:00",
        latest_text="work",
        transcript_hash="h1",
        alias="paused",
        continuation="codex exec resume",
    )
    store.upsert_chat(chat, 5, "active", "goal")
    store.set_goal(chat.id, "goal")
    store.queue_add(chat.id, 1.0)
    with store.connect() as con:
        con.execute("update chats set done=1,state='done' where id=?", (chat.id,))
    store.pause_chat(chat.id)

    assert goals.reconcile_false_done_chats(store) == 0
    row = store.row("select done,paused from chats where id=?", (chat.id,))
    assert row["done"] == 1
    assert row["paused"] == 1


def test_candidates_include_false_done_with_active_goal(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="codex:codex.rollout:candidate",
        provider="codex",
        source="codex.rollout",
        provider_chat_id="candidate",
        title="Candidate",
        cwd=str(tmp_path),
        updated_at="2026-05-21T00:00:00-05:00",
        latest_text="work",
        transcript_hash="h1",
        alias="candidate",
        continuation="codex exec resume",
    )
    store.upsert_chat(chat, 5, "active", "drive me")
    store.set_goal(chat.id, "drive me")
    store.queue_add(chat.id, 1.0)
    with store.connect() as con:
        con.execute(
            """
            update chats set done=1,last_drive_at=?,metadata_json=?
            where id=?
            """,
            (now_iso(), json_dumps({"next_retry_at": now_ts() - 1}), chat.id),
        )

    rows = Scheduler(store).candidates(5)
    assert any(str(row["id"]) == chat.id for row in rows)
