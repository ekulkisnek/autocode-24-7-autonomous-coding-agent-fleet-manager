from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_workflow(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        try:
            import yaml  # type: ignore
        except Exception as exc:
            raise RuntimeError("YAML workflows require PyYAML; use JSON workflow files in this lightweight install") from exc
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("workflow must be a mapping")
    steps = data.get("steps", [])
    if not isinstance(steps, list):
        raise ValueError("workflow steps must be a list")
    return data
