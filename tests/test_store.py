from pathlib import Path

from autocode.models import Chat
from autocode.store import Store


def test_chat_identity_is_stable(tmp_path: Path):
    db = tmp_path / "autocode.sqlite"
    store = Store(db)
    chat = Chat(
        id="grok:grok.sqlite:abc",
        provider="grok",
        source="grok.sqlite",
        provider_chat_id="abc",
        title="Build thing",
        cwd="/tmp/project",
        updated_at="2026-05-21T00:00:00-05:00",
        latest_text="write code",
        transcript_hash="h1",
        alias="project-build-thing",
        continuation="grok --resume",
    )
    store.upsert_chat(chat, 5, "active", "goal")
    chat.title = "Build renamed thing"
    chat.transcript_hash = "h2"
    store.upsert_chat(chat, 5, "active", "goal")
    rows = store.rows("select * from chats")
    assert len(rows) == 1
    assert rows[0]["id"] == "grok:grok.sqlite:abc"


def test_goal_survives_milestone_style_active_chat(tmp_path: Path):
    db = tmp_path / "autocode.sqlite"
    store = Store(db)
    chat = Chat(
        id="codex:codex.rollout:abc",
        provider="codex",
        source="codex.rollout",
        provider_chat_id="abc",
        title="Wallet",
        cwd="/tmp/wallet",
        updated_at="2026-05-21T00:00:00-05:00",
        latest_text="FLEET_MILESTONE_COMPLETE",
        transcript_hash="h",
        alias="wallet",
        continuation="codex exec resume",
    )
    store.upsert_chat(chat, 5, "active", "goal")
    store.set_goal(chat.id, "finish wallet")
    row = store.find_chat("wallet")
    assert row["done"] == 0
    assert row["objective"] == "finish wallet"

