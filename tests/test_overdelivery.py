from pathlib import Path

from autocode import goals
from autocode.models import Chat
from autocode.scheduler import Scheduler
from autocode.store import Store
from autocode.util import now_iso


def _simplicity_output(txid: str) -> str:
    return (
        f"txid={txid}\n"
        "mined height=103 confs=1 in_block=true\n"
        "getdeploymentinfo simplicity active=true status=active\n"
        'FLEET_DONE: {"status":"done","summary":"Simplicity 0xbe verified","evidence":["pytest"]}\n'
    )


def test_detect_overdelivery_repeated_fleet_done(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="grok:grok.sqlite:over",
        provider="grok",
        source="grok.sqlite",
        provider_chat_id="over",
        title="Simplicity 0xbe",
        cwd=str(tmp_path),
        updated_at=now_iso(),
        latest_text="work",
        transcript_hash="h1",
        alias="simplicity-0xbe",
        continuation="grok",
    )
    objective = "First Simplicity 0xbe transaction broadcast on signet"
    store.upsert_chat(chat, 5, "active", objective)
    store.set_goal(chat.id, objective)
    store.queue_add(chat.id, 1.0)
    job_dir = tmp_path / "jobs"
    job_dir.mkdir()
    for i, txid in enumerate(("a" * 64, "b" * 64)):
        out = job_dir / f"out{i}.txt"
        out.write_text(_simplicity_output(txid), encoding="utf-8")
        with store.connect() as con:
            con.execute(
                """
                insert into jobs(
                  id,chat_id,provider,status,pid,cwd,cmd_json,prompt,
                  stdout_path,stderr_path,created_at,updated_at,
                  evidence_status,marker_kind
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"job-{i}",
                    chat.id,
                    "grok",
                    "completed",
                    0,
                    str(tmp_path),
                    "[]",
                    "p",
                    str(out),
                    str(out),
                    now_iso(),
                    now_iso(),
                    "worked",
                    "FLEET_DONE",
                ),
            )
    hit = goals.detect_overdelivery(store, chat.id)
    assert hit is not None
    assert "fleet_done" in hit.evidence_keys


def test_auto_complete_overdelivery_archives_queue(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="grok:grok.sqlite:auto-complete",
        provider="grok",
        source="grok.sqlite",
        provider_chat_id="auto-complete",
        title="Activation test",
        cwd=str(tmp_path),
        updated_at=now_iso(),
        latest_text="done",
        transcript_hash="h2",
        alias="activation",
        continuation="grok",
    )
    objective = "Run Simplicity activation functional test"
    store.upsert_chat(chat, 5, "active", objective)
    store.queue_add(chat.id, 1.0)
    out = tmp_path / "stdout.txt"
    out.write_text(
        'FLEET_DONE: {"status":"done","summary":"activation ok","evidence":["tests pass"]}\n'
        "Tests passed and verified.\n",
        encoding="utf-8",
    )
    for i in range(3):
        with store.connect() as con:
            con.execute(
                """
                insert into jobs(
                  id,chat_id,provider,status,pid,cwd,cmd_json,prompt,
                  stdout_path,stderr_path,created_at,updated_at,evidence_status
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"job-{i}",
                    chat.id,
                    "grok",
                    "completed",
                    0,
                    str(tmp_path),
                    "[]",
                    "p",
                    str(out),
                    str(out),
                    now_iso(),
                    now_iso(),
                    "worked",
                ),
            )
    completed = goals.auto_complete_overdelivery(store)
    assert chat.id in completed
    row = store.row("select done from chats where id=?", (chat.id,))
    assert row["done"] == 1
    assert store.queue_list() == []


def test_reconcile_done_still_in_queue_on_tick(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="grok:grok.sqlite:stale-queue",
        provider="grok",
        source="grok.sqlite",
        provider_chat_id="stale-queue",
        title="Done but queued",
        cwd=str(tmp_path),
        updated_at=now_iso(),
        latest_text="done",
        transcript_hash="h3",
        alias="stale-queue",
        continuation="grok",
    )
    objective = "Ship feature with tests"
    store.upsert_chat(chat, 5, "active", objective)
    store.queue_add(chat.id, 1.0)
    with store.connect() as con:
        con.execute("update chats set done=1,state='done' where id=?", (chat.id,))
    out = tmp_path / "done.txt"
    out.write_text(
        'FLEET_DONE: {"status":"done","summary":"shipped","evidence":["pytest green"]}\n'
        "Tests passed and verified.\n",
        encoding="utf-8",
    )
    with store.connect() as con:
        con.execute(
            """
            insert into jobs(
              id,chat_id,provider,status,pid,cwd,cmd_json,prompt,
              stdout_path,stderr_path,created_at,updated_at,evidence_status
            ) values(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job-done",
                chat.id,
                "grok",
                "completed",
                0,
                str(tmp_path),
                "[]",
                "p",
                str(out),
                str(out),
                now_iso(),
                now_iso(),
                "worked",
            ),
        )
    Scheduler(store).tick(dispatch=False)
    assert store.queue_list() == []
    assert store.queue_finished_list()
