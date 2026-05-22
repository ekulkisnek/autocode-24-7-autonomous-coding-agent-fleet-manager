from pathlib import Path

from autocode.models import Chat
from autocode.scheduler import Scheduler
from autocode.store import Store


def test_failed_codex_falls_back_to_grok(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="codex:codex.rollout:abc",
        provider="codex",
        source="codex.rollout",
        provider_chat_id="abc",
        title="Fix tests",
        cwd="/tmp/project",
        updated_at="2026-05-21T00:00:00-05:00",
        latest_text="blocked",
        transcript_hash="h",
        alias="fix-tests",
        continuation="codex exec resume",
    )
    store.upsert_chat(chat, 5, "stalled", "fix tests")
    with store.connect() as con:
        con.execute("update chats set failure_count=2 where id=?", (chat.id,))
    row = store.find_chat("fix-tests")
    plan = Scheduler(store).fallback_plan(row, "prompt", tmp_path / "job-abc")
    assert plan.provider == "grok"
    assert "--prompt-file" in plan.cmd

