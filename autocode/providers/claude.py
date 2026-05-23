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
            text = read_text(path, limit=24000)
            first, latest = self._summarize(text)
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
                transcript_hash=sha(text),
                alias=slug(f"{Path(workspace).name} {first or sid}", sid),
                continuation="claude --resume",
                metadata={"file": str(path)},
            ))
        return chats

    def _workspace(self, encoded: str) -> str:
        return "/" + encoded[1:].replace("-", "/") if encoded.startswith("-") else encoded.replace("-", "/")

    def _summarize(self, text: str) -> tuple[str, str]:
        first = ""
        latest = []
        for line in text.splitlines():
            try:
                obj = json.loads(line)
            except Exception:
                continue
            typ = obj.get("type")
            msg = obj.get("message") or {}
            content = msg.get("content") or obj.get("content") or ""
            if isinstance(content, list):
                content = " ".join(str(x.get("text") if isinstance(x, dict) else x) for x in content)
            if typ == "user" and not first and content:
                first = str(content)
            if content:
                latest.append(str(content))
        return first, "\n".join(latest[-12:])

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
