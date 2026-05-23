from __future__ import annotations

from pathlib import Path

from ..config import HOME
from ..models import Chat, ContinuePlan
from ..util import read_text, sha, slug
from .base import Provider


class CursorProvider(Provider):
    name = "cursor"
    projects = HOME / ".cursor" / "projects"

    def discover(self) -> list[Chat]:
        if not self.projects.exists():
            return []
        chats: list[Chat] = []
        files = sorted(self.projects.glob("**/agent-transcripts/**/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files:
            sid = path.stem
            text = read_text(path, limit=24000)
            project = self._project_path(path)
            latest = text[-6000:]
            chats.append(Chat(
                id=f"cursor:cursor.transcript:{sid}",
                provider=self.name,
                source="cursor.transcript",
                provider_chat_id=sid,
                title=self._title(text) or sid,
                cwd=project,
                updated_at=path.stat().st_mtime,
                latest_text=latest,
                transcript_hash=sha(text),
                alias=slug(f"{Path(project).name} {self._title(text) or sid}", sid),
                continuation="fork-to-codex",
                metadata={"file": str(path), "direct_continue": False},
            ))
        return chats

    def _project_path(self, path: Path) -> str:
        try:
            rel = path.relative_to(self.projects)
            project_key = rel.parts[0]
            if project_key.startswith("Users-"):
                return "/" + project_key.replace("-", "/")
            return str(self.projects / project_key)
        except Exception:
            return str(HOME)

    def _title(self, text: str) -> str:
        for line in text.splitlines()[:50]:
            if "user" in line.lower() and len(line) > 40:
                return line[:180]
        return ""

    def continue_plan(self, chat: Chat, prompt: str, job_dir: Path) -> ContinuePlan:
        cwd = chat.cwd if chat.cwd and Path(chat.cwd).exists() else str(HOME)
        context = read_text(Path(chat.metadata.get("file", "")), limit=12000)
        combined = (
            "Continue this Cursor conversation by taking over the project work in Codex. "
            "Preserve existing files and do not undo unrelated user changes.\n\n"
            f"Cursor transcript context:\n{context[-10000:]}\n\nAutoCode instruction:\n{prompt}\n"
        )
        return ContinuePlan(
            True,
            "codex",
            cwd,
            cmd=["codex", "exec", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "-C", cwd, "-"],
            stdin=combined,
            same_chat=False,
            reason="Cursor local transcript is continued by Codex takeover.",
        )
