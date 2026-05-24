import json
import sqlite3
from pathlib import Path

from autocode.models import Chat
from autocode.providers.cursor import CursorProvider


def make_cli_store(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("create table meta (key text primary key, value text)")
    con.execute("create table blobs (id text primary key, data blob)")
    meta = {
        "agentId": "agent-123",
        "name": "Fix checkout bug",
        "createdAt": 1779000000000,
    }
    con.execute("insert into meta(key,value) values(?,?)", ("0", json.dumps(meta).encode().hex()))
    con.execute(
        "insert into blobs(id,data) values(?,?)",
        ("u1", json.dumps({"role": "user", "content": "<user_info>\nWorkspace Path: /tmp/shop\n</user_info>\nFix checkout bug in the app."}).encode()),
    )
    con.execute(
        "insert into blobs(id,data) values(?,?)",
        ("a1", json.dumps({"role": "assistant", "content": [{"type": "text", "text": "I inspected the cart flow."}]}).encode()),
    )
    con.commit()
    con.close()


def make_state_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("create table ItemTable (key text unique on conflict replace, value blob)")
    con.execute("create table cursorDiskKV (key text unique on conflict replace, value blob)")
    data = {
        "allComposers": [
            {
                "type": "head",
                "composerId": "composer-1",
                "name": "Local IDE task",
                "lastUpdatedAt": 1779100000000,
                "unifiedMode": "agent",
                "forceMode": "edit",
                "hasUnreadMessages": True,
                "workspaceIdentifier": {"uri": {"fsPath": "/tmp/project"}},
                "subtitle": "Edited app.ts",
            },
            {
                "type": "head",
                "composerId": "cloud-1",
                "name": "Cloud task",
                "lastUpdatedAt": 1779200000000,
                "unifiedMode": "agent",
                "workspaceIdentifier": {
                    "uri": {
                        "fsPath": "/workspace",
                        "external": "vscode-remote://background-composer%2Bbc-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/workspace",
                    }
                },
            },
        ],
        "selectedComposerIds": ["composer-1"],
    }
    con.execute("insert into ItemTable(key,value) values(?,?)", ("composer.composerData", json.dumps(data)))
    con.commit()
    con.close()


def test_cursor_provider_reads_cli_store_and_marks_direct_continue(tmp_path: Path):
    db = tmp_path / ".cursor" / "chats" / "workspace" / "chat" / "store.db"
    make_cli_store(db)
    provider = CursorProvider()
    provider.chats_root = tmp_path / ".cursor" / "chats"
    provider.projects = tmp_path / ".cursor" / "projects"
    provider.user_root = tmp_path / "Cursor" / "User"
    provider._cursor_api_key = lambda: ""

    chats = provider.discover()

    assert len(chats) == 1
    chat = chats[0]
    assert chat.source == "cursor.cli"
    assert chat.provider_chat_id == "agent-123"
    assert chat.cwd == "/tmp/shop"
    assert chat.metadata["direct_continue"] is True
    assert "Fix checkout bug" in chat.latest_text


def test_cursor_provider_reads_ide_and_cloud_composers(tmp_path: Path):
    make_state_db(tmp_path / "Cursor" / "User" / "workspaceStorage" / "abc" / "state.vscdb")
    provider = CursorProvider()
    provider.chats_root = tmp_path / ".cursor" / "chats"
    provider.projects = tmp_path / ".cursor" / "projects"
    provider.user_root = tmp_path / "Cursor" / "User"
    provider._cursor_api_key = lambda: ""

    chats = {c.source: c for c in provider.discover()}

    assert "cursor.ide" in chats
    assert "cursor.cloud" in chats
    assert chats["cursor.ide"].metadata["has_unread"] is True
    assert chats["cursor.cloud"].metadata["cloud_agent_id"] == "bc-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def test_cursor_cli_continue_uses_cursor_agent_resume(tmp_path: Path):
    provider = CursorProvider()
    provider._cursor_api_key = lambda: "secret"
    chat = Chat(
        id="cursor:cursor.cli:agent-123",
        provider="cursor",
        source="cursor.cli",
        provider_chat_id="agent-123",
        cwd=str(tmp_path),
    )

    plan = provider.continue_plan(chat, "continue safely", tmp_path)

    assert plan.supported is True
    assert plan.same_chat is True
    assert plan.cmd[:2] == ["cursor-agent", "--resume"]
    assert "agent-123" in plan.cmd
    assert "--model" in plan.cmd
    assert "auto" in plan.cmd
    assert "Do not run commands that can wait" in plan.cmd[-1]
    assert "finish with a concise status summary" in plan.cmd[-1]
    assert plan.env == {"CURSOR_API_KEY": "secret"}


def test_cursor_cloud_continue_uses_api_followup(tmp_path: Path):
    provider = CursorProvider()
    provider._cursor_api_key = lambda: "secret"
    chat = Chat(
        id="cursor:cursor.cloud:bc-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        provider="cursor",
        source="cursor.cloud",
        provider_chat_id="bc-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        cwd="cloud (cursor)",
    )

    plan = provider.continue_plan(chat, "continue safely", tmp_path)

    assert plan.supported is True
    assert plan.same_chat is True
    assert plan.prompt_file is True
    assert plan.cmd[:4] == ["python3", "-m", "autocode.cursor_cloud", "followup"]
    assert plan.cmd[-1] == "auto"
    assert plan.env == {"CURSOR_API_KEY": "secret"}
