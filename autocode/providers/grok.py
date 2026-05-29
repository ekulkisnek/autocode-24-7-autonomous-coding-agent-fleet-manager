from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from ..config import HOME
from ..models import Chat, ContinuePlan
from ..util import iso_from_ts, sha, slug
from .base import Provider

_GROK_SESSION_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def grok_session_resume_id(chat: Chat) -> str | None:
    """Return a Grok session id safe for ``--resume``, or None for a fresh session."""
    sid = str(chat.provider_chat_id or "").strip()
    if not sid or not _GROK_SESSION_ID.match(sid):
        return None
    if chat.source != "grok.sqlite":
        return None
    db = HOME / ".grok" / "sessions" / "session_search.sqlite"
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        row = con.execute("select 1 from session_docs where session_id=? limit 1", (sid,)).fetchone()
        con.close()
    except Exception:
        return None
    return sid if row else None


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

    def _max_turns(self, chat: Chat) -> str:
        if chat.source == "grok.wiki_squad":
            return "120"
        blob = f"{chat.id} {chat.alias or ''}".lower()
        if "goal-fleet" in blob or chat.alias in {
            "l1-e2e-until-verified",
            "windows-remote-health",
            "github-sync-ekulkisnek",
            "liquid-utreexo-windows-fleet",
        }:
            return "200" if "l1-e2e-until-verified" in blob or chat.alias == "l1-e2e-until-verified" else "120"
        if chat.alias in {
            "l1-sim-detox-fix",
            "l1-electrum-sync-fix",
            "l1-log-analysis",
            "l1-orchestrator-hardening",
            "l1-signet-shared-tests",
            "l1-detox-spec-review",
            "l1-blueelectrum-signet",
            "l1-docs-e2e",
        }:
            return "200"
        return "40"

    def continue_plan(self, chat: Chat, prompt: str, job_dir: Path) -> ContinuePlan:
        prompt_path = job_dir / "prompt.txt"
        cwd = chat.cwd or str(HOME)
        common_tail = [
            "--prompt-file",
            str(prompt_path),
            "--no-alt-screen",
            "--permission-mode",
            "bypassPermissions",
            "--max-turns",
            self._max_turns(chat),
            "--output-format",
            "plain",
        ]
        resume_id = grok_session_resume_id(chat)
        if resume_id:
            cmd = ["grok", "--resume", resume_id, *common_tail]
            same_chat = True
        else:
            cmd = ["grok", "--cwd", cwd, *common_tail]
            same_chat = False
        return ContinuePlan(
            True,
            self.name,
            cwd,
            cmd=cmd,
            stdin=None,
            prompt_file=True,
            same_chat=same_chat,
        )
