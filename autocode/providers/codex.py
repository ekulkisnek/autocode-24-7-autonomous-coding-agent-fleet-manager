from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..config import HOME
from ..models import Chat, ContinuePlan
from ..util import compact, iso_from_ts, read_text, sha, slug
from .base import Provider


class CodexProvider(Provider):
    name = "codex"
    db = HOME / ".codex" / "state_5.sqlite"

    def discover(self) -> list[Chat]:
        if not self.db.exists():
            return []
        try:
            con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True, timeout=3)
            rows = con.execute(
                "select id,title,cwd,updated_at,rollout_path,preview from threads where archived=0 order by updated_at desc"
            ).fetchall()
            con.close()
        except Exception:
            return []
        chats: list[Chat] = []
        for sid, title, cwd, updated, rollout, preview in rows:
            transcript = read_text(Path(rollout), limit=16000) if rollout else ""
            latest = self._latest_text(transcript) or preview or ""
            stable = f"codex:codex.rollout:{sid}"
            chats.append(Chat(
                id=stable,
                provider=self.name,
                source="codex.rollout",
                provider_chat_id=str(sid),
                title=title or preview or "",
                cwd=cwd or "",
                updated_at=iso_from_ts(updated),
                latest_text=latest[-6000:],
                transcript_hash=sha(transcript or latest),
                alias=slug(f"{Path(cwd or '').name} {title or preview or sid}", sid),
                continuation="codex exec resume",
                metadata={"rollout_path": rollout or ""},
            ))
        return chats

    def _latest_text(self, transcript: str) -> str:
        out = []
        for line in transcript.splitlines()[-80:]:
            try:
                obj = json.loads(line)
            except Exception:
                continue
            payload = obj.get("payload") or obj.get("item") or obj.get("message") or {}
            if isinstance(payload, dict):
                text = payload.get("message") or payload.get("text") or payload.get("content")
                if isinstance(text, list):
                    text = " ".join(str(x.get("text") if isinstance(x, dict) else x) for x in text)
                if text:
                    out.append(str(text))
        return compact("\n".join(out[-12:]), 6000)

    def continue_plan(self, chat: Chat, prompt: str, job_dir: Path) -> ContinuePlan:
        return ContinuePlan(
            True,
            self.name,
            chat.cwd or str(HOME),
            cmd=[
                "codex",
                "exec",
                "resume",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                chat.provider_chat_id,
                "-",
            ],
            stdin=prompt,
            same_chat=True,
        )

