from __future__ import annotations

from .antigravity import AntigravityProvider
from .base import Provider
from .claude import ClaudeProvider
from .codex import CodexProvider
from .cursor import CursorProvider
from .grok import GrokProvider


def providers() -> list[Provider]:
    return [GrokProvider(), CodexProvider(), ClaudeProvider(), CursorProvider(), AntigravityProvider()]


def provider_map() -> dict[str, Provider]:
    return {p.name: p for p in providers()}

