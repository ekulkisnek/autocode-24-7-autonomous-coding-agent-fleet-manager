from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


MARKER_RE = re.compile(r"(?ims)^\s*(FLEET_DONE|FLEET_MILESTONE|FLEET_PLAN)\s*:\s*(\{.*?\})\s*$")


@dataclass(frozen=True)
class FleetMarker:
    kind: str
    payload: dict[str, Any]
    raw: str

    @property
    def complete(self) -> bool:
        return self.kind == "FLEET_DONE"

    @property
    def status(self) -> str:
        return str(self.payload.get("status") or ("done" if self.complete else "active"))

    @property
    def summary(self) -> str:
        for key in ("summary", "evidence_summary", "done", "current_evidence"):
            value = self.payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return self.kind

    @property
    def blockers(self) -> list[str]:
        value = self.payload.get("blockers") or self.payload.get("blocked_on") or []
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        return []


def parse_fleet_marker(text: str) -> FleetMarker | None:
    """Return the last valid structured fleet marker in provider output."""
    last: FleetMarker | None = None
    for match in MARKER_RE.finditer(text or ""):
        raw = match.group(0).strip()
        try:
            payload = json.loads(match.group(2))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        last = FleetMarker(match.group(1), payload, raw)
    return last
