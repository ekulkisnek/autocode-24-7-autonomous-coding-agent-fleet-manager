from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .store import Store


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


def apply_workflow(store: Store, workflow: dict[str, Any]) -> list[str]:
    """Create priority entries from a declarative workflow.

    JSON shape:
      {"name":"...", "steps":[{"query":"...", "goal":"...", "rank":100, "path":"...", "chat_id":"..."}]}
    """
    created: list[str] = []
    for idx, step in enumerate(workflow.get("steps", [])):
        if not isinstance(step, dict):
            raise ValueError(f"workflow step {idx} must be a mapping")
        query = str(step.get("query") or f"{workflow.get('name','workflow')}-{idx + 1}")
        goal = str(step.get("goal") or step.get("objective") or "").strip()
        if not goal:
            raise ValueError(f"workflow step {idx} missing goal")
        rank = int(step.get("rank") or step.get("priority") or 100)
        path = str(step.get("path") or "")
        chat_id = str(step.get("chat_id") or "")
        lanes = int(step.get("lanes") or 1)
        created.append(store.add_priority(query, goal, rank, path, chat_id, lanes))
    return created
