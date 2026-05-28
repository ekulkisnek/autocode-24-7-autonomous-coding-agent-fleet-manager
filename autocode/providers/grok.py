from __future__ import annotations

import sqlite3
from pathlib import Path

from ..config import HOME
from ..models import Chat, ContinuePlan
from ..util import iso_from_ts, sha, slug
from .base import Provider


class GrokProvider(Provider):
    name = "grok"
    db = HOME / ".grok" / "sessions" / "session_search.sqlite"

    def discover(self) -> list[Chat]:
        if not self.db.exists():
            return []
        try:
            con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True, timeout=3)
            rows = con.execute("select session_id,cwd,updated_at,title,content from session_docs order by updated_at desc").fetchall()
            con.close()
        except Exception:
            return []
        chats: list[Chat] = []
        for sid, cwd, updated, title, content in rows:
            stable = f"grok:grok.sqlite:{sid}"
            text = content or ""
            chats.append(Chat(
                id=stable,
                provider=self.name,
                source="grok.sqlite",
                provider_chat_id=str(sid),
                title=title or "",
                cwd=cwd or "",
                updated_at=iso_from_ts(updated),
                latest_text=text[-6000:],
                transcript_hash=sha(text),
                alias=slug(f"{Path(cwd or '').name} {title or sid}", sid),
                continuation="grok --resume",
                metadata={},
            ))
        return chats

    def continue_plan(self, chat: Chat, prompt: str, job_dir: Path) -> ContinuePlan:
        prompt_path = job_dir / "prompt.txt"
        return ContinuePlan(
            True,
            self.name,
            chat.cwd or str(HOME),
            cmd=[
                "grok",
                "--resume",
                chat.provider_chat_id,
                "--prompt-file",
                str(prompt_path),
                "--no-alt-screen",
                "--permission-mode",
                "bypassPermissions",
                "--max-turns",
                "40",
                "--output-format",
                "plain",
            ],
            stdin=None,
            prompt_file=True,
            same_chat=True,
        )
