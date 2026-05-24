from __future__ import annotations

import os
import json
from pathlib import Path

from ..config import HOME
from ..models import Chat, ContinuePlan
from ..util import iso_from_ts, read_text, sha, slug
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
            title = self._metadata_title(conv) or self._title(text) or sid
            chats.append(Chat(
                id=f"antigravity:antigravity.brain:{sid}",
                provider=self.name,
                source="antigravity.brain",
                provider_chat_id=sid,
                title=title,
                cwd=str(HOME),
                updated_at=iso_from_ts(transcript.stat().st_mtime),
                latest_text=text[-6000:],
                transcript_hash=sha(text),
                alias=slug(f"antigravity {title}", sid),
                continuation="antigravity agentapi" if self._agentapi_ready() else "fork-to-codex",
                metadata={"transcript": str(transcript), "agentapi_ready": self._agentapi_ready()},
            ))
        return chats

    def _title(self, text: str) -> str:
        for line in text.splitlines()[:60]:
            content = self._json_content(line)
            if "<USER_REQUEST>" in content:
                content = content.split("<USER_REQUEST>", 1)[1].split("</USER_REQUEST>", 1)[0]
            clean = " ".join(content.split())
            if len(clean) > 20 and not clean.startswith("# Conversation History"):
                return clean[:160]
        return ""

    def _metadata_title(self, conv: Path) -> str:
        for name in ("task.md.metadata.json", "implementation_plan.md.metadata.json", "walkthrough.md.metadata.json"):
            path = conv / name
            if not path.exists():
                continue
            try:
                summary = json.loads(read_text(path, limit=4000)).get("summary") or ""
            except Exception:
                continue
            clean = " ".join(str(summary).split())
            if clean:
                return clean[:160]
        return ""

    def _json_content(self, line: str) -> str:
        try:
            obj = json.loads(line)
        except Exception:
            return line
        return str(obj.get("content") or "")

    def continue_plan(self, chat: Chat, prompt: str, job_dir: Path) -> ContinuePlan:
        if self._agentapi_ready():
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

    def _agentapi_ready(self) -> bool:
        return self.agentapi.exists() and bool(os.environ.get("ANTIGRAVITY_LS_ADDRESS"))
