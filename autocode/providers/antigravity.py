from __future__ import annotations

import os
import json
import re
from pathlib import Path

from ..config import HOME
from ..models import Chat, ContinuePlan
from ..util import iso_from_ts, read_text, sha, slug
from .base import Provider


class AntigravityProvider(Provider):
    name = "antigravity"
    root = HOME / ".gemini" / "antigravity"
    brain = root / "brain"
    conversations = root / "conversations"
    agentapi = root / "bin" / "agentapi"

    def discover(self) -> list[Chat]:
        if not self.brain.exists() and not self.conversations.exists():
            return []
        chats: list[Chat] = []
        seen: set[str] = set()
        storage = self._conversation_storage()
        brain_dirs = []
        if self.brain.exists():
            brain_dirs = sorted(self.brain.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        for conv in brain_dirs:
            if not conv.is_dir():
                continue
            sid = conv.name
            transcript = conv / ".system_generated" / "logs" / "transcript.jsonl"
            stored_text, stored_mtime = storage.get(sid, ("", 0.0))
            if not transcript.exists() and not stored_text:
                continue
            text = read_text(transcript, limit=24000) if transcript.exists() else ""
            latest_text = stored_text[-6000:] if stored_mtime >= (transcript.stat().st_mtime if transcript.exists() else 0.0) and stored_text else text[-6000:]
            title = self._title_from_recent_text(stored_text) or self._metadata_title(conv) or self._title(text) or sid
            updated_ts = max(transcript.stat().st_mtime if transcript.exists() else 0.0, stored_mtime)
            chats.append(Chat(
                id=f"antigravity:antigravity.brain:{sid}",
                provider=self.name,
                source="antigravity.brain",
                provider_chat_id=sid,
                title=title,
                cwd=str(HOME),
                updated_at=iso_from_ts(updated_ts),
                latest_text=latest_text,
                transcript_hash=sha(text + stored_text),
                alias=slug(f"antigravity {title}", sid),
                continuation="antigravity agentapi" if self._agentapi_ready() else "fork-to-codex",
                metadata={"transcript": str(transcript), "agentapi_ready": self._agentapi_ready(), "conversation_storage": bool(stored_text)},
            ))
            seen.add(sid)
        for sid, (text, mtime) in storage.items():
            if sid in seen or not text:
                continue
            title = self._title_from_recent_text(text) or self._title(text) or sid
            chats.append(Chat(
                id=f"antigravity:antigravity.conversation:{sid}",
                provider=self.name,
                source="antigravity.conversation",
                provider_chat_id=sid,
                title=title,
                cwd=str(HOME),
                updated_at=iso_from_ts(mtime),
                latest_text=text[-6000:],
                transcript_hash=sha(text),
                alias=slug(f"antigravity {title}", sid),
                continuation="antigravity agentapi" if self._agentapi_ready() else "fork-to-codex",
                metadata={"agentapi_ready": self._agentapi_ready(), "conversation_storage": True},
            ))
        chats.sort(key=lambda c: c.updated_at, reverse=True)
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

    def _conversation_storage(self) -> dict[str, tuple[str, float]]:
        if not self.conversations.exists():
            return {}
        out: dict[str, tuple[str, float]] = {}
        for path in self.conversations.iterdir():
            if not path.is_file():
                continue
            sid = self._conversation_sid(path)
            if not sid:
                continue
            text = self._printable_binary_text(path)
            if not text:
                continue
            old_text, old_mtime = out.get(sid, ("", 0.0))
            mtime = path.stat().st_mtime
            combined = old_text + "\n" + text
            keyword_lines = [
                line for line in combined.splitlines()
                if any(word in line.lower() for word in ("redwallet", "bitassets", "wallet"))
            ]
            out[sid] = (("\n".join(keyword_lines[-300:]) + "\n" + combined[-128000:])[-192000:], max(old_mtime, mtime))
        return out

    def _conversation_sid(self, path: Path) -> str:
        name = path.name
        for suffix in (".db-wal", ".db-shm", ".db", ".pb"):
            if name.endswith(suffix):
                return name[: -len(suffix)]
        return ""

    def _printable_binary_text(self, path: Path) -> str:
        try:
            data = path.read_bytes()
        except Exception:
            return ""
        if len(data) > 20_000_000:
            data = data[-20_000_000:]
        chunks = re.findall(rb"[\x09\x0a\x0d\x20-\x7e]{8,}", data)
        lines = []
        keyword_lines = []
        for chunk in chunks:
            line = chunk.decode("utf-8", errors="ignore")
            lines.append(line)
            lower = line.lower()
            if "redwallet" in lower or "bitassets" in lower or "wallet" in lower:
                keyword_lines.append(line)
        text = "\n".join(keyword_lines[-200:] + lines[-800:])
        return text[-128000:]

    def _title_from_recent_text(self, text: str) -> str:
        best: tuple[int, str] = (0, "")
        for line in reversed(text.splitlines()):
            clean = " ".join(line.split())
            if len(clean) < 24:
                continue
            if clean.startswith(("{", "[", "/", "File Path:", "tool", "LineContent", "AbsolutePath", "DirectoryPath")):
                continue
            lower = clean.lower()
            score = 0
            if "redwallet" in lower:
                score += 8
            if "bitassets" in lower:
                score += 6
            if "wallet" in lower:
                score += 2
            if lower.startswith(("github.com/", "lib/", "screen/", "class/", "navigation/")):
                score -= 4
            if any(token in lower for token in ("linenumber", "linecontent", "toolaction", "toolsummary")):
                score -= 6
            if score > best[0]:
                best = (score, clean[:160])
        return best[1]

    def _json_content(self, line: str) -> str:
        try:
            obj = json.loads(line)
        except Exception:
            return line
        if not isinstance(obj, dict):
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
        transcript = str(chat.metadata.get("transcript") or "")
        context = read_text(Path(transcript), limit=12000) if transcript else (chat.latest_text or "")
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
