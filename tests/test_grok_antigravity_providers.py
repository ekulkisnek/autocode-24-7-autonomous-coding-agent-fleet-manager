import sqlite3
from pathlib import Path

from autocode.models import Chat
from autocode.providers.antigravity import AntigravityProvider
from autocode.providers.grok import GrokProvider


def test_grok_continue_resumes_existing_session_with_prompt_file(tmp_path: Path):
    provider = GrokProvider()
    chat = Chat(
        id="grok:grok.sqlite:session-1",
        provider="grok",
        source="grok.sqlite",
        provider_chat_id="session-1",
        cwd=str(tmp_path),
    )

    plan = provider.continue_plan(chat, "keep going", tmp_path)

    assert plan.supported is True
    assert plan.same_chat is True
    assert plan.prompt_file is True
    assert plan.cmd[:4] == ["grok", "--resume", "session-1", "--prompt-file"]
    assert "--permission-mode" in plan.cmd


def test_grok_discovers_sessions_from_sqlite(tmp_path: Path):
    db = tmp_path / ".grok" / "sessions" / "session_search.sqlite"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute("create table session_docs(session_id text,cwd text,updated_at real,title text,content text)")
    con.execute(
        "insert into session_docs values(?,?,?,?,?)",
        ("session-1", str(tmp_path), 1779000000.0, "Fix API", "User: fix API\nAssistant: ok"),
    )
    con.commit()
    con.close()
    provider = GrokProvider()
    provider.db = db

    chats = provider.discover()

    assert len(chats) == 1
    assert chats[0].id == "grok:grok.sqlite:session-1"
    assert chats[0].updated_at.startswith("2026-")
    assert chats[0].continuation == "grok --resume"


def test_antigravity_discovery_uses_iso_timestamp_and_fallback_when_agentapi_unavailable(tmp_path: Path, monkeypatch):
    root = tmp_path / ".gemini" / "antigravity"
    transcript = root / "brain" / "conv-1" / ".system_generated" / "logs" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"role":"user","content":"Please fix this coding task with enough context to title it."}\n', encoding="utf-8")
    monkeypatch.delenv("ANTIGRAVITY_LS_ADDRESS", raising=False)
    provider = AntigravityProvider()
    provider.root = root
    provider.brain = root / "brain"
    provider.agentapi = root / "bin" / "agentapi"

    chats = provider.discover()
    plan = provider.continue_plan(chats[0], "continue safely", tmp_path)

    assert chats[0].updated_at.startswith("20")
    assert chats[0].continuation == "fork-to-codex"
    assert chats[0].metadata["agentapi_ready"] is False
    assert plan.provider == "codex"
    assert plan.same_chat is False
    assert "Continue this Antigravity conversation" in (plan.stdin or "")


def test_antigravity_discovers_conversation_storage_without_brain_transcript(tmp_path: Path, monkeypatch):
    root = tmp_path / ".gemini" / "antigravity"
    conv = root / "conversations" / "conv-2.db-wal"
    conv.parent.mkdir(parents=True)
    conv.write_bytes(
        b"\x00\x01"
        b"1. find the redwallet project and improve the UI for the bitassets wallet send and receive actions\n"
        b"2. make wallet creation distinct for BitAssets sidechain vs main chain\n"
    )
    monkeypatch.delenv("ANTIGRAVITY_LS_ADDRESS", raising=False)
    provider = AntigravityProvider()
    provider.root = root
    provider.brain = root / "brain"
    provider.conversations = root / "conversations"
    provider.agentapi = root / "bin" / "agentapi"

    chats = provider.discover()

    assert len(chats) == 1
    assert chats[0].id == "antigravity:antigravity.conversation:conv-2"
    assert chats[0].source == "antigravity.conversation"
    assert "bitassets" in chats[0].title.lower()
    assert chats[0].metadata["conversation_storage"] is True


def test_antigravity_continue_uses_agentapi_when_ready(tmp_path: Path, monkeypatch):
    agentapi = tmp_path / "agentapi"
    agentapi.write_text("#!/bin/sh\n", encoding="utf-8")
    agentapi.chmod(0o755)
    monkeypatch.setenv("ANTIGRAVITY_LS_ADDRESS", "127.0.0.1:1234")
    provider = AntigravityProvider()
    provider.agentapi = agentapi
    chat = Chat(
        id="antigravity:antigravity.brain:conv-1",
        provider="antigravity",
        source="antigravity.brain",
        provider_chat_id="conv-1",
    )

    plan = provider.continue_plan(chat, "continue safely", tmp_path)

    assert plan.provider == "antigravity"
    assert plan.same_chat is True
    assert plan.cmd == [str(agentapi), "send-message", "conv-1", "continue safely"]
    assert plan.env.get("ANTIGRAVITY_LS_ADDRESS") == "127.0.0.1:1234"
