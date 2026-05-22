from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_ts() -> float:
    return time.time()


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_ts(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 10_000_000_000 else v
    text = str(value)
    try:
        return parse_ts(float(text))
    except Exception:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def iso_from_ts(value: Any) -> str:
    ts = parse_ts(value)
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat(timespec="seconds")


def rel_time(value: Any) -> str:
    ts = parse_ts(value)
    if ts <= 0:
        return "-"
    seconds = max(0, int(time.time() - ts))
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def slug(text: str, fallback: str = "chat", limit: int = 70) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower()).strip("-")
    text = re.sub(r"-+", "-", text)
    return (text or fallback)[:limit].strip("-") or fallback


def compact(text: Any, limit: int = 220) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s if len(s) <= limit else s[: max(0, limit - 1)].rstrip() + "…"


def sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def json_loads(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def read_text(path: Path, limit: int | None = None) -> str:
    try:
        if limit and path.exists() and path.stat().st_size > limit:
            with path.open("rb") as f:
                f.seek(max(0, path.stat().st_size - limit))
                return f.read().decode("utf-8", errors="replace")
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def run_text(cmd: list[str], timeout: int = 10, cwd: str | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as exc:
        return 127, "", str(exc)


def command_exists(name: str) -> bool:
    return subprocess.run(["/usr/bin/env", "bash", "-lc", f"command -v {name} >/dev/null"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def load1() -> float:
    try:
        return float(os.getloadavg()[0])
    except Exception:
        return 0.0

