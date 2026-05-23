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


def test_scheduler_repairs_done_priority_target(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="codex:codex.rollout:redwallet",
        provider="codex",
        source="codex.rollout",
        provider_chat_id="redwallet",
        title="Implement wallet persistence",
        cwd="/tmp/redwallet",
        updated_at="2026-05-21T00:00:00-05:00",
        latest_text="old done state",
        transcript_hash="h",
        alias="redwallet",
        continuation="codex exec resume",
    )
    goal = "Keep RedWallet working in this exact Codex chat until production ready."
    store.upsert_chat(chat, 5, "active", goal)
    store.add_priority("redwallet", goal, 1001, "/tmp/redwallet", chat.id, 3)
    with store.connect() as con:
        con.execute("update chats set done=1,state='done',paused=1,adopted=0 where id=?", (chat.id,))

    repaired = Scheduler(store).enforce_priority_invariants()

    row = store.row("select * from chats where id=?", (chat.id,))
    assert repaired == 1
    assert row["done"] == 0
    assert row["paused"] == 0
    assert row["adopted"] == 1
    assert row["state"] == "active"
    assert row["objective"] == goal
    goals = store.rows("select * from goals where chat_id=? and status='active'", (chat.id,))
    assert len(goals) == 1
    assert goals[0]["source"] == "priority"


def test_scheduler_skips_repeatedly_failed_non_priority(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="cursor:cursor.transcript:noisy",
        provider="cursor",
        source="cursor.transcript",
        provider_chat_id="noisy",
        title="Old transcript",
        cwd="/tmp/missing-project",
        updated_at="2026-05-21T00:00:00-05:00",
        latest_text="fix code",
        transcript_hash="h",
        alias="noisy",
        continuation="fork-to-codex",
    )
    store.upsert_chat(chat, 5, "active", "fix code")
    with store.connect() as con:
        con.execute("update chats set failure_count=3 where id=?", (chat.id,))

    assert Scheduler(store).candidates(10) == []


def test_priority_candidate_can_override_failure_backoff(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="codex:codex.rollout:redwallet",
        provider="codex",
        source="codex.rollout",
        provider_chat_id="redwallet",
        title="Implement wallet persistence",
        cwd="/tmp/redwallet",
        updated_at="2026-05-21T00:00:00-05:00",
        latest_text="fix code",
        transcript_hash="h",
        alias="redwallet",
        continuation="codex exec resume",
    )
    store.upsert_chat(chat, 5, "active", "fix code")
    store.add_priority("redwallet", "finish redwallet", 1001, "/tmp/redwallet", chat.id, 1)
    with store.connect() as con:
        con.execute("update chats set failure_count=99 where id=?", (chat.id,))

    candidates = Scheduler(store).candidates(10)
    assert [row["id"] for row in candidates] == [chat.id]


def test_priority_only_mode_excludes_general_backlog(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    priority_chat = Chat(
        id="codex:codex.rollout:redwallet",
        provider="codex",
        source="codex.rollout",
        provider_chat_id="redwallet",
        title="Implement wallet persistence",
        cwd="/tmp/redwallet",
        updated_at="2026-05-21T00:00:00-05:00",
        latest_text="fix code",
        transcript_hash="h1",
        alias="redwallet",
        continuation="codex exec resume",
    )
    backlog_chat = Chat(
        id="codex:codex.rollout:other",
        provider="codex",
        source="codex.rollout",
        provider_chat_id="other",
        title="Other project",
        cwd="/tmp/other",
        updated_at="2026-05-22T00:00:00-05:00",
        latest_text="fix code",
        transcript_hash="h2",
        alias="other",
        continuation="codex exec resume",
    )
    store.upsert_chat(priority_chat, 5, "active", "fix redwallet")
    store.upsert_chat(backlog_chat, 5, "active", "fix other")
    store.add_priority("redwallet", "finish redwallet", 1001, "/tmp/redwallet", priority_chat.id, 1)
    store.set_config("priority_only", "on")

    candidates = Scheduler(store).candidates(10)
    assert [row["id"] for row in candidates] == [priority_chat.id]
