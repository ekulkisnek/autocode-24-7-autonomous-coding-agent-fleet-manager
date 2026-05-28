from __future__ import annotations

import json
import re
from pathlib import Path

from ..config import HOME
from ..models import Chat, ContinuePlan
from ..util import iso_from_ts, read_text, sha, slug
from .base import Provider


class ClaudeProvider(Provider):
    name = "claude"
    root = HOME / ".claude" / "projects"

    def discover(self) -> list[Chat]:
        if not self.root.exists():
            return []
        chats: list[Chat] = []
        files = sorted(self.root.glob("**/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files:
            head = self._read_head(path, 32000)
            tail = read_text(path, limit=24000)
            first = self._first_user_msg(head)
            latest = self._latest_text(tail)
            workspace = self._workspace(path.parent.name)
            sid = path.stem
            chats.append(Chat(
                id=f"claude:claude.jsonl:{sid}",
                provider=self.name,
                source="claude.jsonl",
                provider_chat_id=sid,
                title=first[:160] or "Claude session",
                cwd=workspace,
                updated_at=iso_from_ts(path.stat().st_mtime),
                latest_text=latest[-6000:],
                transcript_hash=sha(tail),
                alias=slug(f"{Path(workspace).name} {first or sid}", sid),
                continuation="claude --resume",
                metadata={"file": str(path)},
            ))
        return chats

    def _read_head(self, path: Path, limit: int) -> str:
        try:
            with path.open("rb") as f:
                return f.read(limit).decode("utf-8", errors="replace")
        except Exception:
            return ""

    def _workspace(self, encoded: str) -> str:
        return "/" + encoded[1:].replace("-", "/") if encoded.startswith("-") else encoded.replace("-", "/")

    def _first_user_msg(self, text: str) -> str:
        skip_prefixes = (
            "<local-command-caveat>",
            "<system-reminder>",
        )
        continuation_prefix = "This session is being continued"
        continuation_title = ""
        for line in text.splitlines():
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("type") != "user":
                continue
            msg = obj.get("message") or {}
            raw = msg.get("content") or ""
            content = self._extract_text(raw)
            if not content:
                continue
            if any(content.startswith(p) for p in skip_prefixes):
                continue
            if content.startswith(continuation_prefix):
                if not continuation_title:
                    continuation_title = self._title_from_summary(content)
                continue
            return content
        return continuation_title

    def _title_from_summary(self, continuation_text: str) -> str:
        # Try "Explicit requests this session:" bullets first — most specific
        for marker in ("Explicit requests this session:", "Primary Request and Intent:", "Summary:"):
            idx = continuation_text.find(marker)
            if idx == -1:
                continue
            snippet = continuation_text[idx + len(marker):]
            for line in snippet.splitlines():
                stripped = line.strip().lstrip("- •").strip()
                if stripped and not stripped.startswith("("):
                    return stripped[:140]
        return ""

    def _latest_text(self, text: str) -> str:
        parts: list[str] = []
        for line in text.splitlines():
            try:
                obj = json.loads(line)
            except Exception:
                continue
            typ = obj.get("type")
            msg = obj.get("message") or {}
            raw = msg.get("content") or obj.get("content") or ""
            content = self._extract_text(raw)
            if typ in {"user", "assistant"} and content:
                parts.append(content)
        return "\n".join(parts[-12:])

    def _extract_text(self, raw) -> str:
        if isinstance(raw, list):
            parts = [x["text"] for x in raw if isinstance(x, dict) and x.get("type") == "text" and x.get("text")]
            return " ".join(parts)
        return str(raw) if raw else ""

    def continue_plan(self, chat: Chat, prompt: str, job_dir: Path) -> ContinuePlan:
        cwd = chat.cwd if chat.cwd and Path(chat.cwd).exists() else str(HOME)
        if not re.match(r"^[0-9a-fA-F-]{36}$", chat.provider_chat_id):
            context = read_text(Path(chat.metadata.get("file", "")), limit=12000)
            combined = (
                "Continue this Claude transcript by taking over the project work in Codex. "
                "Preserve existing files and do not undo unrelated user changes.\n\n"
                f"Claude transcript context:\n{context[-10000:]}\n\nAutoCode instruction:\n{prompt}\n"
            )
            return ContinuePlan(
                True,
                "codex",
                cwd,
                cmd=["codex", "exec", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "-C", cwd, "-"],
                stdin=combined,
                same_chat=False,
                reason="Claude transcript is not directly resumable; using Codex takeover.",
            )
        return ContinuePlan(
            True,
            self.name,
            cwd,
            cmd=[
                "claude",
                "--print",
                "--output-format",
                "text",
                "--permission-mode",
                "bypassPermissions",
                "--resume",
                chat.provider_chat_id,
            ],
            stdin=prompt,
            same_chat=True,
        )
