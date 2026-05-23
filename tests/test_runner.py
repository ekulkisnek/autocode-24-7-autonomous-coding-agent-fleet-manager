from pathlib import Path

from autocode.models import Chat
from autocode.runner import JobRunner
from autocode.store import Store
from autocode.util import now_iso


def test_runner_assesses_stdout_completion_without_prompt_stderr_noise(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="codex:codex.rollout:redwallet",
        provider="codex",
        source="codex.rollout",
        provider_chat_id="redwallet",
        title="Implement wallet persistence",
        cwd="/tmp/redwallet",
        updated_at="2026-05-21T00:00:00-05:00",
        latest_text="work",
        transcript_hash="h1",
        alias="redwallet",
        continuation="codex exec resume",
    )
    goal = (
        "Make RedWallet production ready. HARD REQUIREMENT: do not call this done until tests prove "
        "full Utreexo/proof-backed storage and validation for BitAssets asset creation, sending, and receiving."
    )
    store.upsert_chat(chat, 5, "active", goal)
    store.add_priority("redwallet", goal, 1001, "/tmp/redwallet", chat.id, 1)

    job_dir = tmp_path / "job-complete"
    job_dir.mkdir()
    stdout = job_dir / "stdout.txt"
    stderr = job_dir / "stderr.txt"
    stdout.write_text(
        "FLEET_DONE\n\n"
        "Complete and verified. Tests passed for Utreexo proof-backed storage and validation, "
        "BitAssets asset creation, sending/transfer, and receiving/change.\n",
        encoding="utf-8",
    )
    stderr.write_text(
        "user prompt: Current known next step: continue. "
        "Rules: do not call this done until tests prove the hard requirement.\n",
        encoding="utf-8",
    )
    with store.connect() as con:
        con.execute(
            """
            insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at)
            values(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job-complete",
                chat.id,
                "codex",
                "running",
                0,
                "/tmp/redwallet",
                "[]",
                "prompt",
                str(stdout),
                str(stderr),
                now_iso(),
                now_iso(),
            ),
        )

    JobRunner(store).refresh()

    row = store.row("select * from chats where id=?", (chat.id,))
    priority = store.row("select * from project_priorities where target_chat_id=?", (chat.id,))
    assert row["done"] == 1
    assert row["state"] == "done"
    assert priority["status"] == "complete"
