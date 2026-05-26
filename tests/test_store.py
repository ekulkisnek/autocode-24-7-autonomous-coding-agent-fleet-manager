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


def test_active_priority_target_cannot_be_marked_done_by_discovery(tmp_path: Path):
    db = tmp_path / "autocode.sqlite"
    store = Store(db)
    chat = Chat(
        id="codex:codex.rollout:redwallet",
        provider="codex",
        source="codex.rollout",
        provider_chat_id="redwallet",
        title="Implement wallet persistence",
        cwd="/tmp/redwallet",
        updated_at="2026-05-21T00:00:00-05:00",
        latest_text="work in progress",
        transcript_hash="h1",
        alias="redwallet",
        continuation="codex exec resume",
    )
    goal = "Make RedWallet production ready. HARD REQUIREMENT: prove Utreexo asset creation, sending, and receiving."
    store.upsert_chat(chat, 5, "active", "old goal")
    store.set_goal(chat.id, goal)
    store.add_priority("redwallet", goal, 1001, "/tmp/redwallet", chat.id, 3)

    chat.latest_text = "FLEET_DONE: unrelated handoff complete"
    chat.transcript_hash = "h2"
    store.upsert_chat(chat, 5, "done", "discovered done")

    row = store.row("select * from chats where id=?", (chat.id,))
    assert row["done"] == 0
    assert row["paused"] == 0
    assert row["adopted"] == 1
    assert row["state"] == "active"
    assert row["objective"] == goal


def test_add_priority_reopens_target_chat(tmp_path: Path):
    db = tmp_path / "autocode.sqlite"
    store = Store(db)
    chat = Chat(
        id="codex:codex.rollout:redwallet",
        provider="codex",
        source="codex.rollout",
        provider_chat_id="redwallet",
        title="Implement wallet persistence",
        cwd="/tmp/redwallet",
        updated_at="2026-05-21T00:00:00-05:00",
        latest_text="old done state",
        transcript_hash="h1",
        alias="redwallet",
        continuation="codex exec resume",
    )
    store.upsert_chat(chat, 5, "done", "old objective")
    goal = "Keep working in this exact chat until RedWallet is production ready."

    store.add_priority("redwallet", goal, 1001, "/tmp/redwallet", chat.id, 3)

    row = store.row("select * from chats where id=?", (chat.id,))
    assert row["done"] == 0
    assert row["paused"] == 0
    assert row["state"] == "active"
    assert row["objective"] == goal


def test_add_priority_deduplicates_by_target_chat(tmp_path: Path):
    db = tmp_path / "autocode.sqlite"
    store = Store(db)
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
    store.upsert_chat(chat, 5, "active", "old objective")

    first = store.add_priority("redwallet", "goal one", 1001, "/tmp/redwallet", chat.id, 3)
    second = store.add_priority(chat.id, "goal two", 1001, "/tmp/redwallet", chat.id, 3)

    rows = store.rows("select * from project_priorities where status='active' and target_chat_id=?", (chat.id,))
    row = store.row("select * from chats where id=?", (chat.id,))
    assert first == second
    assert len(rows) == 1
    assert rows[0]["query"] == chat.id
    assert rows[0]["objective"] == "goal two"
    assert row["objective"] == "goal two"


def test_add_priority_reactivates_completed_target_priority(tmp_path: Path):
    db = tmp_path / "autocode.sqlite"
    store = Store(db)
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
    store.upsert_chat(chat, 5, "active", "old objective")
    first = store.add_priority("redwallet", "goal one", 1001, "/tmp/redwallet", chat.id, 3)
    with store.connect() as con:
        con.execute("update project_priorities set status='complete' where id=?", (first,))

    second = store.add_priority(chat.id, "goal two", 1001, "/tmp/redwallet", chat.id, 3)

    rows = store.rows("select * from project_priorities where target_chat_id=?", (chat.id,))
    assert first == second
    assert len(rows) == 1
    assert rows[0]["status"] == "active"
    assert rows[0]["objective"] == "goal two"


def test_only_one_active_goal_per_chat(tmp_path: Path):
    db = tmp_path / "autocode.sqlite"
    store = Store(db)
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
    store.upsert_chat(chat, 5, "active", "old objective")

    store.set_goal(chat.id, "goal one")
    store.set_goal(chat.id, "goal two")
    store.add_priority("redwallet", "goal three", 1001, "/tmp/redwallet", chat.id, 3)

    rows = store.rows("select * from goals where chat_id=? and status='active'", (chat.id,))
    assert len(rows) == 1
    assert rows[0]["objective"] == "goal three"
    assert rows[0]["source"] == "priority"


def test_task_plan_starts_as_decomposition_request_not_hardcoded_list(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    plan_id = store.ensure_task_plan("chat-1", "Build the thing")

    row = store.row("select * from task_plans where id=?", (plan_id,))
    assert row["status"] == "needs_decomposition"
    assert row["subtasks_json"] == "[]"
    assert "FLEET_PLAN" in store.task_plan_summary("chat-1")
