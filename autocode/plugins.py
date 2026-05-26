from __future__ import annotations

import json
from pathlib import Path

from .config import ROOT


PLUGIN_DIR = ROOT / "plugins"


def list_plugins() -> list[dict]:
    PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
    plugins: list[dict] = []
    for manifest in sorted(PLUGIN_DIR.glob("*/plugin.json")):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception as exc:
            data = {"name": manifest.parent.name, "error": str(exc)}
        data.setdefault("id", manifest.parent.name)
        data.setdefault("path", str(manifest.parent))
        plugins.append(data)
    return plugins


def validate_plugin(manifest: dict) -> list[str]:
    errors: list[str] = []
    if not manifest.get("id"):
        errors.append("missing id")
    for key in ("providers", "reactions", "workflows"):
        if key in manifest and not isinstance(manifest[key], list):
            errors.append(f"{key} must be a list")
    return errors


def scaffold_plugin(plugin_id: str) -> Path:
    safe = "".join(ch for ch in plugin_id if ch.isalnum() or ch in {"-", "_"}).strip("-_")
    if not safe:
        raise ValueError("plugin id must contain letters, numbers, dash, or underscore")
    root = PLUGIN_DIR / safe
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "plugin.json"
    if not manifest.exists():
        manifest.write_text(
            json.dumps(
                {
                    "id": safe,
                    "name": safe,
                    "version": "0.1.0",
                    "providers": [],
                    "reactions": [],
                    "workflows": [],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return root
