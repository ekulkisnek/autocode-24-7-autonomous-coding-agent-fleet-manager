from __future__ import annotations

from pathlib import Path

from ..models import Chat, ContinuePlan


class Provider:
    name = "unknown"

    def discover(self) -> list[Chat]:
        return []

    def continue_plan(self, chat: Chat, prompt: str, job_dir: Path) -> ContinuePlan:
        return ContinuePlan(False, self.name, chat.cwd, reason="Continuation unsupported")

