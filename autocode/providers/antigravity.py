from __future__ import annotations

import os
from pathlib import Path

from ..config import HOME
from ..models import Chat, ContinuePlan
from ..util import read_text, sha, slug
from .base import Provider


class AntigravityProvider(Provider):
    name = "antigravity"
    root = HOME / ".gemini" / "antigravity"
    brain = root / "brain"
    agentapi = root / "bin" / "agentapi"

    def discover(self) -> list[Chat]:
        if not self.brain.exists():
            return []
        chats: list[Chat] = []
        for conv in sorted(self.brain.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            transcript = conv / ".system_generated" / "logs" / "transcript.jsonl"
            if not transcript.exists():
                continue
            text = read_text(transcript, limit=24000)
            sid = conv.name
            chats.append(Chat(
                id=f"antigravity:antigravity.brain:{sid}",
                provider=self.name,
                source="antigravity.brain",
                provider_chat_id=sid,
                title=self._title(text) or sid,
                cwd=str(HOME),
                updated_at=transcript.stat().st_mtime,
                latest_text=text[-6000:],
                transcript_hash=sha(text),
                alias=slug(f"antigravity {self._title(text) or sid}", sid),
                continuation="antigravity agentapi" if self.agentapi.exists() else "fork-to-codex",
                metadata={"transcript": str(transcript)},
            ))
        return chats

    def _title(self, text: str) -> str:
        for line in text.splitlines()[:60]:
            if len(line) > 50:
                return line[:160]
        return ""

    def continue_plan(self, chat: Chat, prompt: str, job_dir: Path) -> ContinuePlan:
        if self.agentapi.exists():
            return ContinuePlan(
                True,
                self.name,
                str(HOME),
                cmd=[str(self.agentapi), "send-message", chat.provider_chat_id, prompt],
                env=os.environ.copy(),
                same_chat=True,
            )
        context = read_text(Path(chat.metadata.get("transcript", "")), limit=12000)
        combined = f"Continue this Antigravity conversation by taking over in Codex.\n\nContext:\n{context[-10000:]}\n\nInstruction:\n{prompt}\n"
        return ContinuePlan(
            True,
            "codex",
            str(HOME),
            cmd=["codex", "exec", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "-C", str(HOME), "-"],
            stdin=combined,
            same_chat=False,
        )

