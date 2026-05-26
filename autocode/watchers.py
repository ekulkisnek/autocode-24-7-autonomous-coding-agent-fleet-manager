from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass

from .config import HOME


def provider_watch_paths() -> list[Path]:
    """Known local provider state paths to monitor for fast rediscovery."""
    candidates = [
        HOME / ".codex" / "state_5.sqlite",
        HOME / ".grok" / "sessions" / "session_search.sqlite",
        HOME / ".cursor" / "projects",
        HOME / ".config" / "Claude",
        HOME / "Library" / "Application Support" / "Claude",
    ]
    return [path for path in candidates if path.exists()]


def latest_mtime(path: Path) -> float:
    try:
        if path.is_file():
            return path.stat().st_mtime
        newest = path.stat().st_mtime
        for child in path.rglob("*"):
            try:
                newest = max(newest, child.stat().st_mtime)
            except OSError:
                continue
        return newest
    except OSError:
        return 0.0


def watch_signature() -> str:
    return "|".join(f"{path}:{latest_mtime(path):.6f}" for path in provider_watch_paths())


@dataclass
class WatchState:
    signature: str = ""

    def poll(self) -> tuple[bool, str]:
        current = watch_signature()
        changed = bool(current and current != self.signature)
        self.signature = current
        return changed, current
