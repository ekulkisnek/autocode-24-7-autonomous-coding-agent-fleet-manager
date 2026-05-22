from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Chat:
    id: str
    provider: str
    source: str
    provider_chat_id: str
    title: str = ""
    cwd: str = ""
    updated_at: str = ""
    latest_text: str = ""
    transcript_hash: str = ""
    alias: str = ""
    continuation: str = "unsupported"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ContinuePlan:
    supported: bool
    provider: str
    cwd: str
    cmd: list[str] = field(default_factory=list)
    stdin: str | None = None
    prompt_file: bool = False
    env: dict[str, str] = field(default_factory=dict)
    reason: str = ""
    same_chat: bool = True

