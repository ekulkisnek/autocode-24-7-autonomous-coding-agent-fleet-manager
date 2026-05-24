from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ..config import DB, HOME
from ..models import Chat, ContinuePlan
from ..util import compact, iso_from_ts, json_loads, parse_ts, read_text, sha, slug
from .base import Provider


class CursorProvider(Provider):
    name = "cursor"
    projects = HOME / ".cursor" / "projects"
    chats_root = HOME / ".cursor" / "chats"
    user_root = HOME / "Library" / "Application Support" / "Cursor" / "User"
    recent_active_seconds = 10 * 60

    def discover(self) -> list[Chat]:
        by_id: dict[str, Chat] = {}
        for chat in self._discover_cloud_api_agents():
            by_id[chat.id] = chat
        for chat in self._discover_cli_chat_dbs():
            by_id[chat.id] = chat
        for chat in self._discover_agent_transcripts():
            by_id.setdefault(chat.id, chat)
        for chat in self._discover_ide_composers():
            old = by_id.get(chat.id)
            if not old or self._quality_rank(chat) > self._quality_rank(old):
                by_id[chat.id] = chat
        return sorted(by_id.values(), key=lambda c: parse_ts(c.updated_at), reverse=True)

    def _quality_rank(self, chat: Chat) -> int:
        quality = str(chat.metadata.get("history_quality", ""))
        if "full" in quality:
            return 3
        if "summary" in quality:
            return 2
        return 1

    def _discover_agent_transcripts(self) -> list[Chat]:
        if not self.projects.exists():
            return []
        chats: list[Chat] = []
        files = sorted(self.projects.glob("**/agent-transcripts/**/*.jsonl"), key=lambda p: self._mtime(p), reverse=True)
        for path in files:
            sid = path.stem
            text = read_text(path, limit=24000)
            project = self._project_path(path)
            latest = text[-6000:]
            title = self._title_from_text(text) or sid
            chats.append(Chat(
                id=f"cursor:cursor.transcript:{sid}",
                provider=self.name,
                source="cursor.transcript",
                provider_chat_id=sid,
                title=title,
                cwd=project,
                updated_at=iso_from_ts(self._mtime(path)),
                latest_text=latest,
                transcript_hash=sha(text),
                alias=slug(f"{Path(project).name} {title}", sid),
                continuation="fork-to-cursor-agent",
                metadata={
                    "file": str(path),
                    "direct_continue": False,
                    "same_chat_continue": False,
                    "history_quality": "full transcript jsonl",
                    "active": self._is_recent(self._mtime(path)),
                    "activity_status": self._activity_status(self._mtime(path)),
                    "model": self.cursor_model(),
                },
            ))
        return chats

    def _discover_cli_chat_dbs(self) -> list[Chat]:
        if not self.chats_root.exists():
            return []
        chats: list[Chat] = []
        for db in sorted(self.chats_root.glob("**/store.db"), key=lambda p: self._mtime(p), reverse=True):
            parsed = self._read_cursor_store_db(db)
            if not parsed:
                continue
            sid = parsed["agent_id"] or db.parent.name
            project = self._workspace_from_messages(parsed["messages"]) or self._chatdb_workspace(db)
            title = parsed["title"] or self._title_from_messages(parsed["messages"]) or sid
            latest = self._messages_text(parsed["messages"], limit=9000)
            updated_ts = max(self._mtime(db), parsed["updated_ts"] or 0)
            chats.append(Chat(
                id=f"cursor:cursor.cli:{sid}",
                provider=self.name,
                source="cursor.cli",
                provider_chat_id=sid,
                title=title,
                cwd=project,
                updated_at=iso_from_ts(updated_ts),
                latest_text=latest,
                transcript_hash=sha(latest + str(parsed["message_count"])),
                alias=slug(f"{Path(project).name} {title}", sid),
                continuation="cursor-agent --resume",
                metadata={
                    "db": str(db),
                    "workspace_hash": db.parent.parent.name if len(db.parents) > 1 else "",
                    "message_count": parsed["message_count"],
                    "created_at": iso_from_ts(parsed["created_ts"]),
                    "updated_at": iso_from_ts(updated_ts),
                    "direct_continue": True,
                    "same_chat_continue": True,
                    "history_quality": "full local Cursor Agent store.db transcript",
                    "active": self._is_recent(updated_ts),
                    "activity_status": self._activity_status(updated_ts),
                    "model": self.cursor_model(),
                },
            ))
        return chats

    def _discover_ide_composers(self) -> list[Chat]:
        chats: dict[str, Chat] = {}
        for db in self._state_dbs():
            data = self._read_composer_data(db)
            if not data:
                continue
            workspace = self._workspace_for_state_db(db)
            selected = set(data.get("selectedComposerIds") or [])
            focused = set(data.get("lastFocusedComposerIds") or [])
            for item in data.get("allComposers") or []:
                if not isinstance(item, dict):
                    continue
                composer_id = str(item.get("composerId") or "")
                if not composer_id or composer_id == "empty-state-draft":
                    continue
                updated = item.get("lastUpdatedAt") or item.get("conversationCheckpointLastUpdatedAt") or item.get("createdAt") or self._mtime(db)
                source = "cursor.cloud" if self._is_cloud_composer(item) else "cursor.ide"
                cloud_id = self._cloud_agent_id(item)
                sid = cloud_id if source == "cursor.cloud" and cloud_id else composer_id
                title = str(item.get("name") or item.get("subtitle") or sid)
                latest = self._composer_latest_text(item)
                cwd = self._composer_workspace_path(item) or workspace
                active = bool(item.get("hasBlockingPendingActions") or item.get("hasPendingPlan") or item.get("hasUnreadMessages") or self._is_recent(updated))
                chat = Chat(
                    id=f"cursor:{source}:{sid}",
                    provider=self.name,
                    source=source,
                    provider_chat_id=sid,
                    title=title,
                    cwd=cwd,
                    updated_at=iso_from_ts(updated),
                    latest_text=latest,
                    transcript_hash=sha(json.dumps(item, sort_keys=True, default=str)),
                    alias=slug(f"{Path(cwd).name if cwd else 'cursor'} {title}", sid),
                    continuation="fork-to-cursor-agent",
                    metadata={
                        "db": str(db),
                        "composer_id": composer_id,
                        "unified_mode": item.get("unifiedMode", ""),
                        "force_mode": item.get("forceMode", ""),
                        "archived": bool(item.get("isArchived")),
                        "draft": bool(item.get("isDraft")),
                        "selected": sid in selected,
                        "focused": sid in focused,
                        "has_unread": bool(item.get("hasUnreadMessages")),
                        "has_pending_plan": bool(item.get("hasPendingPlan")),
                        "has_blocking_pending_actions": bool(item.get("hasBlockingPendingActions")),
                        "lines_added": int(item.get("totalLinesAdded") or 0),
                        "lines_removed": int(item.get("totalLinesRemoved") or 0),
                        "files_changed": int(item.get("filesChangedCount") or 0),
                        "context_usage_percent": item.get("contextUsagePercent"),
                        "direct_continue": False,
                        "same_chat_continue": False,
                        "history_quality": "IDE composer metadata and latest subtitle",
                        "active": active,
                        "activity_status": self._activity_status(updated, active=active),
                        "cloud_agent_id": cloud_id,
                        "model": item.get("model") or self.cursor_model(),
                    },
                )
                old = chats.get(chat.id)
                if not old or parse_ts(chat.updated_at) >= parse_ts(old.updated_at):
                    chats[chat.id] = chat
        return list(chats.values())

    def _discover_cloud_api_agents(self) -> list[Chat]:
        api_key = self._cursor_api_key()
        if not api_key:
            return []
        try:
            data = self._cloud_request("GET", "/v1/agents", query={"limit": 100, "includeArchived": "true"}, timeout=15)
        except Exception:
            return []
        chats: list[Chat] = []
        for item in data.get("items") or []:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("id") or "")
            if not sid:
                continue
            updated = item.get("updatedAt") or item.get("createdAt") or 0
            title = str(item.get("name") or sid)
            status = str(item.get("status") or "")
            latest = "\n".join(part for part in [
                title,
                f"status: {status}" if status else "",
                f"latestRunId: {item.get('latestRunId')}" if item.get("latestRunId") else "",
                f"url: {item.get('url')}" if item.get("url") else "",
            ] if part)
            env = item.get("env") if isinstance(item.get("env"), dict) else {}
            chats.append(Chat(
                id=f"cursor:cursor.cloud:{sid}",
                provider=self.name,
                source="cursor.cloud",
                provider_chat_id=sid,
                title=title,
                cwd=f"cloud ({env.get('type') or 'cursor'})",
                updated_at=iso_from_ts(updated),
                latest_text=latest,
                transcript_hash=sha(json.dumps(item, sort_keys=True, default=str)),
                alias=slug(f"cursor cloud {title}", sid),
                continuation="cursor cloud followup",
                metadata={
                    "cloud_agent_id": sid,
                    "cloud_url": item.get("url") or f"https://cursor.com/agents?id={sid}",
                    "status": status,
                    "latest_run_id": item.get("latestRunId") or "",
                    "direct_continue": True,
                    "same_chat_continue": True,
                    "history_quality": "Cursor Cloud API metadata",
                    "active": self._cloud_status_active(status),
                    "activity_status": self._cloud_activity_status(status, updated),
                    "model": item.get("model") or self.cursor_model(),
                },
            ))
        return chats

    def _state_dbs(self) -> list[Path]:
        paths: list[Path] = []
        global_db = self.user_root / "globalStorage" / "state.vscdb"
        if global_db.exists():
            paths.append(global_db)
        storage = self.user_root / "workspaceStorage"
        if storage.exists():
            paths.extend(sorted(storage.glob("*/state.vscdb"), key=lambda p: self._mtime(p), reverse=True))
        return paths

    def _read_cursor_store_db(self, db: Path) -> dict[str, Any] | None:
        try:
            con = self._connect_ro(db)
            meta: dict[str, Any] = {}
            if self._has_table(con, "meta"):
                for key, value in con.execute("select key,value from meta"):
                    meta[str(key)] = self._decode_jsonish(value)
            messages: list[dict[str, str]] = []
            if self._has_table(con, "blobs"):
                for _, data in con.execute("select id,data from blobs"):
                    msg = self._message_from_blob(data)
                    if msg:
                        messages.append(msg)
            con.close()
        except Exception:
            return None
        root = meta.get("0") if isinstance(meta.get("0"), dict) else {}
        created = root.get("createdAt") or self._mtime(db)
        updated = self._latest_message_ts(messages) or self._mtime(db)
        return {
            "agent_id": root.get("agentId") or db.parent.name,
            "title": root.get("name") or "",
            "created_ts": parse_ts(created),
            "updated_ts": parse_ts(updated),
            "messages": messages,
            "message_count": len(messages),
        }

    def _read_composer_data(self, db: Path) -> dict[str, Any] | None:
        try:
            con = self._connect_ro(db)
            data = None
            for key in ("composer.composerHeaders", "composer.composerData"):
                row = con.execute("select value from ItemTable where key=?", (key,)).fetchone()
                if row:
                    data = self._decode_jsonish(row[0])
                    break
            con.close()
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _connect_ro(self, db: Path) -> sqlite3.Connection:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
        return con

    def _cloud_request(self, method: str, path: str, query: dict[str, Any] | None = None, body: dict[str, Any] | None = None, timeout: int = 20) -> dict[str, Any]:
        api_key = self._cursor_api_key()
        if not api_key:
            raise RuntimeError("CURSOR_API_KEY is not configured")
        url = "https://api.cursor.com" + path
        if query:
            clean = {k: v for k, v in query.items() if v is not None and v != ""}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=payload, method=method.upper())
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{api_key}:".encode()).decode())
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "autocode/1.0")
        if payload is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return json_loads(raw.decode("utf-8", errors="replace"), {})

    def _has_table(self, con: sqlite3.Connection, name: str) -> bool:
        row = con.execute("select 1 from sqlite_master where type='table' and name=?", (name,)).fetchone()
        return bool(row)

    def _decode_jsonish(self, value: Any) -> Any:
        if isinstance(value, bytes):
            text = value.decode("utf-8", errors="replace")
        else:
            text = str(value or "")
        stripped = text.strip()
        if re.fullmatch(r"[0-9a-fA-F]+", stripped or "") and len(stripped) % 2 == 0:
            try:
                stripped = bytes.fromhex(stripped).decode("utf-8", errors="replace").strip()
            except Exception:
                pass
        return json_loads(stripped, stripped)

    def _message_from_blob(self, data: bytes) -> dict[str, str] | None:
        text = data.decode("utf-8", errors="replace").strip("\x00\r\n\t ")
        obj = json_loads(text, None)
        if not isinstance(obj, dict):
            match = re.search(r'(\{"role"\s*:\s*"(?:user|assistant|tool|system)".*)', text, re.S)
            if match:
                obj = json_loads(match.group(1), None)
        if not isinstance(obj, dict) or "role" not in obj:
            return None
        role = str(obj.get("role") or "")
        content = self._content_text(obj.get("content"))
        if not content:
            return None
        return {
            "role": role,
            "content": content,
            "created_at": iso_from_ts(obj.get("createdAt") or obj.get("timestamp") or obj.get("time") or 0),
        }

    def _content_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") in {"text", "reasoning"}:
                        parts.append(str(item.get("text") or ""))
                    elif "toolName" in item or "toolCallId" in item:
                        parts.append(compact(item.get("result") or item.get("toolName") or "", 1200))
                else:
                    parts.append(str(item))
            return "\n".join(p for p in parts if p)
        if isinstance(content, dict):
            return str(content.get("text") or content.get("result") or "")
        return ""

    def _messages_text(self, messages: list[dict[str, str]], limit: int = 9000) -> str:
        chunks = []
        for m in messages[-60:]:
            role = m.get("role", "")
            if role == "system":
                continue
            text = compact(m.get("content", ""), 1800)
            if role and text:
                chunks.append(f"{role}: {text}")
        out = "\n".join(chunks)
        return out[-limit:]

    def _title_from_messages(self, messages: list[dict[str, str]]) -> str:
        for m in messages:
            if m.get("role") == "user":
                return compact(m.get("content", ""), 120)
        return ""

    def _latest_message_ts(self, messages: list[dict[str, str]]) -> float:
        values = [parse_ts(m.get("created_at")) for m in messages if m.get("created_at")]
        return max(values) if values else 0.0

    def _project_path(self, path: Path) -> str:
        try:
            rel = path.relative_to(self.projects)
            project_key = rel.parts[0]
            if project_key.startswith("Users-"):
                return "/" + project_key.replace("-", "/")
            if project_key.startswith("Volumes-"):
                return "/" + project_key.replace("-", "/")
            if project_key == "empty-window":
                return str(HOME)
            return str(self.projects / project_key)
        except Exception:
            return str(HOME)

    def _chatdb_workspace(self, db: Path) -> str:
        return str(HOME)

    def _workspace_from_messages(self, messages: list[dict[str, str]]) -> str:
        for m in messages[:8]:
            match = re.search(r"Workspace Path:\s*([^\n\r]+)", m.get("content", ""))
            if match:
                path = match.group(1).strip()
                if path:
                    return path
        return ""

    def _workspace_for_state_db(self, db: Path) -> str:
        workspace_json = db.parent / "workspace.json"
        obj = json_loads(read_text(workspace_json), {})
        if isinstance(obj, dict):
            folder = obj.get("folder") or obj.get("workspace")
            if isinstance(folder, str) and folder.startswith("file://"):
                return folder.replace("file://", "")
            if isinstance(folder, str):
                return folder
        return str(HOME)

    def _composer_workspace_path(self, item: dict[str, Any]) -> str:
        wi = item.get("workspaceIdentifier")
        if isinstance(wi, dict):
            uri = wi.get("uri")
            if isinstance(uri, dict):
                path = uri.get("fsPath") or uri.get("path")
                if isinstance(path, str) and path:
                    return path
        draft = item.get("draftTarget")
        if isinstance(draft, dict):
            env = draft.get("environment")
            if isinstance(env, dict):
                name = env.get("name") or env.get("id")
                if isinstance(name, str) and name:
                    return name
        return ""

    def _composer_latest_text(self, item: dict[str, Any]) -> str:
        parts = []
        for key in ("name", "subtitle"):
            if item.get(key):
                parts.append(str(item[key]))
        for branch_key in ("activeBranch", "branches"):
            if item.get(branch_key):
                parts.append(f"{branch_key}: {compact(item[branch_key], 500)}")
        if item.get("hasBlockingPendingActions"):
            parts.append("has blocking pending actions")
        if item.get("hasPendingPlan"):
            parts.append("has pending plan")
        return "\n".join(parts)

    def _is_cloud_composer(self, item: dict[str, Any]) -> bool:
        text = json.dumps(item, default=str)
        return "background-composer" in text or "cloudTargetKey" in text or bool(self._cloud_agent_id(item))

    def _cloud_agent_id(self, item: dict[str, Any]) -> str:
        text = json.dumps(item, default=str)
        match = re.search(r"bc-[0-9a-fA-F-]{36}", text)
        return match.group(0) if match else ""

    def _title_from_text(self, text: str) -> str:
        for line in text.splitlines()[:80]:
            if "user" in line.lower() and len(line) > 40:
                return compact(line, 180)
        return ""

    def _mtime(self, path: Path) -> float:
        try:
            return path.stat().st_mtime
        except Exception:
            return 0.0

    def _is_recent(self, value: Any) -> bool:
        ts = parse_ts(value)
        return ts > 0 and time.time() - ts <= self.recent_active_seconds

    def _activity_status(self, value: Any, active: bool | None = None) -> str:
        if active is True:
            return "active"
        ts = parse_ts(value)
        if ts <= 0:
            return "unknown"
        age = time.time() - ts
        if age <= self.recent_active_seconds:
            return "recent"
        if age <= 24 * 3600:
            return "idle_today"
        return "idle"

    def _cloud_status_active(self, status: str) -> bool:
        s = (status or "").lower()
        return s in {"running", "queued", "pending", "in_progress", "working"}

    def _cloud_activity_status(self, status: str, updated: Any) -> str:
        if self._cloud_status_active(status):
            return "active"
        return self._activity_status(updated)

    def _cursor_api_key(self) -> str:
        if os.environ.get("CURSOR_API_KEY"):
            return str(os.environ["CURSOR_API_KEY"]).strip()
        for path in (HOME / ".hermes" / ".env", HOME / "grok-cursor-bridge" / ".env"):
            text = read_text(path)
            match = re.search(r"(?m)^CURSOR_API_KEY=(.+)$", text)
            if match:
                return match.group(1).strip().strip("\"'")
        return ""

    def cursor_env(self) -> dict[str, str]:
        api_key = self._cursor_api_key()
        return {"CURSOR_API_KEY": api_key} if api_key else {}

    def cursor_model(self) -> str:
        env_model = os.environ.get("AUTOCODE_CURSOR_MODEL", "").strip()
        if env_model:
            return env_model
        try:
            con = sqlite3.connect(DB, timeout=1)
            row = con.execute("select value from config where key='cursor_model'").fetchone()
            con.close()
            if row and str(row[0]).strip():
                return str(row[0]).strip()
        except Exception:
            pass
        return "auto"

    def cursor_agent_cmd(self, base: list[str], model: str | None = None) -> list[str]:
        selected = (model or self.cursor_model() or "auto").strip()
        if selected:
            return base + ["--model", selected]
        return base

    def continue_plan(self, chat: Chat, prompt: str, job_dir: Path) -> ContinuePlan:
        cwd = chat.cwd if chat.cwd and Path(chat.cwd).exists() else str(HOME)
        if chat.source == "cursor.cli":
            same_chat_prompt = self._same_chat_prompt(prompt)
            return ContinuePlan(
                True,
                "cursor",
                cwd,
                cmd=[
                    *self.cursor_agent_cmd([
                        "cursor-agent",
                        "--resume",
                        chat.provider_chat_id,
                        "--print",
                        "--output-format",
                        "text",
                        "--force",
                        "--trust",
                        "--workspace",
                        cwd,
                    ]),
                    same_chat_prompt,
                ],
                env=self.cursor_env(),
                same_chat=True,
                reason="Resume existing Cursor Agent CLI chat.",
            )
        if chat.source == "cursor.cloud" and chat.provider_chat_id.startswith("bc-") and self._cursor_api_key():
            return ContinuePlan(
                True,
                "cursor",
                chat.cwd or str(HOME),
                cmd=[
                    "python3",
                    "-m",
                    "autocode.cursor_cloud",
                    "followup",
                    chat.provider_chat_id,
                    str(job_dir / "prompt.txt"),
                    self.cursor_model(),
                ],
                env=self.cursor_env(),
                prompt_file=True,
                same_chat=True,
                reason="Post follow-up to Cursor Cloud Agent via Cursor API.",
            )
        context = self._context_for_takeover(chat)
        combined = (
            "Continue this Cursor conversation with Cursor Agent as the worker. "
            "The original Cursor source is read-only from AutoCode unless it is a Cursor Agent CLI chat. "
            "Preserve existing files and do not undo unrelated user changes.\n\n"
            f"Cursor context:\n{context[-12000:]}\n\nAutoCode instruction:\n{prompt}\n"
        )
        return ContinuePlan(
            True,
            "cursor",
            cwd,
            cmd=[
                *self.cursor_agent_cmd([
                    "cursor-agent",
                    "--print",
                    "--output-format",
                    "text",
                    "--force",
                    "--trust",
                    "--workspace",
                    cwd,
                ]),
                combined,
            ],
            env=self.cursor_env(),
            same_chat=False,
            reason=f"{chat.source} is read directly but continued in a new Cursor Agent worker.",
        )

    def _context_for_takeover(self, chat: Chat) -> str:
        path = chat.metadata.get("file") or chat.metadata.get("db")
        if path and str(path).endswith(".jsonl"):
            return read_text(Path(path), limit=16000)
        return f"{chat.title}\n\n{chat.latest_text}"

    def _same_chat_prompt(self, prompt: str) -> str:
        text = prompt.split("\n\nLatest known context:", 1)[0].strip()
        return text or prompt.strip()
