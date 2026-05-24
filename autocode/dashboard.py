from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Row
from typing import Any

from .config import DB, HOME
from .launchd import status as launchd_status
from .scheduler import Scheduler
from .store import Store
from .util import command_exists, compact, json_loads, load1, memory_free_percent, now_iso, parse_ts, read_text, rel_time


PROVIDERS = ("codex", "claude", "antigravity", "grok", "cursor")


@dataclass(frozen=True)
class ModelInfo:
    model: str = "?"
    effort: str = "?"
    speed: str = "normal"

    def label(self) -> str:
        parts = [self.model or "?"]
        if self.effort and self.effort != "?":
            parts.append(f"effort={self.effort}")
        if self.speed and self.speed != "normal":
            parts.append(self.speed)
        return "/".join(parts)


def render_dashboard(store: Store | None = None, *, width: int | None = None, limit: int = 12, refresh_jobs: bool = True) -> str:
    store = store or Store()
    if refresh_jobs:
        Scheduler(store).runner.refresh()
    width = width or shutil.get_terminal_size((132, 40)).columns
    width = max(92, min(width, 180))
    daemon_ok, _ = launchd_status()
    active_jobs = _count(store, "select count(*) c from jobs where status='running'")
    cap = Scheduler(store).capacity()
    active_chats = _count(store, "select count(*) c from chats where adopted=1 and done=0 and paused=0 and coding_score>0")
    total_chats = _count(store, "select count(*) c from chats")
    yolo = store.get_config("yolo", "off")
    priority_only = store.get_config("priority_only", "off")
    mem = memory_free_percent()
    mem_label = f"{mem}% free" if mem is not None else "unknown"

    lines: list[str] = []
    lines.append(_bar("AutoCode Dashboard", width))
    lines.append(
        f"{now_iso()} | daemon={'on' if daemon_ok else 'off'} | yolo={yolo} | "
        f"priority_only={priority_only} | jobs={active_jobs}/{cap} | load1={load1():.2f} | mem={mem_label}"
    )
    lines.append(f"db={DB} | chats={total_chats} total, {active_chats} active/adopted")
    lines.append("")
    lines.extend(_running_section(store, width, limit))
    lines.append("")
    lines.extend(_queue_section(store, width, limit))
    lines.append("")
    lines.extend(_usage_section(store, width))
    lines.append("")
    lines.extend(_recent_section(store, width, min(limit, 8)))
    lines.append("")
    lines.append("Quota note: remaining subscription/API quota is shown as unknown unless a provider exposes an exact local endpoint.")
    lines.append("Usage columns are observed AutoCode jobs, not provider billing totals.")
    return "\n".join(lines).rstrip() + "\n"


def run_dashboard(interval: float = 2.0, limit: int = 12, once: bool = False) -> None:
    store = Store()
    while True:
        text = render_dashboard(store, limit=limit)
        if once:
            print(text, end="")
            return
        print("\033[2J\033[H", end="")
        print(text, end="", flush=True)
        time.sleep(max(0.5, interval))


def _running_section(store: Store, width: int, limit: int) -> list[str]:
    rows = store.rows(
        """
        select j.*, c.alias, c.title, c.source, c.provider_chat_id, c.objective, c.metadata_json
        from jobs j left join chats c on c.id=j.chat_id
        where j.status='running'
        order by j.created_at asc
        limit ?
        """,
        (limit,),
    )
    lines = [_title("Driving Now", width)]
    if not rows:
        lines.append("  none running")
        return lines
    for row in rows:
        label = row["alias"] or row["chat_id"]
        model = model_info(row).label()
        working = _job_working_text(row, 900)
        lines.append(
            f"  {rel_time(row['created_at']):>4}  {row['provider']:<11} {model:<24} "
            f"{row['evidence_status']:<16} {row['id']}  {_fit(label, 42)}"
        )
        if row["cwd"]:
            lines.append(f"        cwd: {_fit(row['cwd'], width - 13)}")
        lines.append(f"        doing: {_fit(working, width - 15)}")
    return lines


def _queue_section(store: Store, width: int, limit: int) -> list[str]:
    scheduler = Scheduler(store)
    rows = scheduler.candidates(limit)
    priorities = store.rows(
        """
        select * from project_priorities
        where status='active'
        order by priority desc, updated_at desc
        limit ?
        """,
        (limit,),
    )
    lines = [_title("Watched / Next Up", width)]
    if priorities:
        lines.append("  priority projects:")
        for p in priorities[: min(5, limit)]:
            target = f" -> {_fit(p['target_chat_id'], 34)}" if p["target_chat_id"] else ""
            lines.append(f"    p{p['priority']} {_fit(p['query'], 32)}{target} lanes={p['worker_lanes']}")
            lines.append(f"      goal: {_fit(p['objective'], width - 14)}")
    if not rows:
        if not priorities and store.get_config("priority_only", "off").lower() in {"1", "true", "yes", "on"}:
            lines.append("  no schedulable chats right now because priority_only=on and no active priority projects exist")
            lines.append("  add one with: autocode drive <chat-query> --goal \"...\" --priority --exact")
        else:
            lines.append("  no schedulable chats right now")
        return lines
    lines.append("  next scheduler candidates:")
    for row in rows[:limit]:
        meta = json_loads(row["metadata_json"], {})
        model = chat_model_info(row, meta).label()
        title = row["alias"] or row["title"] or row["provider_chat_id"]
        state = row["state"]
        flag = "P" if _is_priority(store, row["id"]) else "-"
        objective = row["objective"] or row["title"] or row["latest_text"]
        lines.append(
            f"    {flag} {rel_time(row['updated_at']):>4} {row['provider']:<11} {model:<22} "
            f"{state:<11} {_fit(title, 44)}"
        )
        lines.append(f"      next: {_fit(objective, width - 12)}")
    return lines


def _usage_section(store: Store, width: int) -> list[str]:
    now = time.time()
    jobs = store.rows("select provider,status,created_at,updated_at,evidence_status from jobs")
    lines = [_title("Provider Usage / Health", width)]
    lines.append("  provider     health        running  1h   24h  7d   fail24  default/model       remaining")
    for provider in PROVIDERS:
        provider_jobs = [j for j in jobs if j["provider"] == provider]
        running = sum(1 for j in provider_jobs if j["status"] == "running")
        one_h = _count_since(provider_jobs, now - 3600)
        day = _count_since(provider_jobs, now - 86400)
        week = _count_since(provider_jobs, now - 7 * 86400)
        fail24 = sum(1 for j in provider_jobs if j["status"] == "failed" and parse_ts(j["updated_at"]) >= now - 86400)
        health = _provider_health(provider)
        default = _provider_default(store, provider)
        remaining = "unknown"
        lines.append(
            f"  {provider:<12} {health:<13} {running:>7} {one_h:>4} {day:>5} {week:>4} "
            f"{fail24:>7}  {_fit(default, 18):<18} {remaining}"
        )
    return lines


def _recent_section(store: Store, width: int, limit: int) -> list[str]:
    rows = store.rows(
        """
        select j.*, c.alias, c.title
        from jobs j left join chats c on c.id=j.chat_id
        where j.status!='running'
        order by j.updated_at desc
        limit ?
        """,
        (limit,),
    )
    lines = [_title("Recent Evidence", width)]
    if not rows:
        lines.append("  no recent finished jobs")
        return lines
    for row in rows:
        label = row["alias"] or row["title"] or row["chat_id"]
        reason = row["evidence_reason"] or _job_working_text(row, 260)
        lines.append(
            f"  {rel_time(row['updated_at']):>4} {row['provider']:<11} {row['status']:<9} "
            f"{row['evidence_status']:<16} {_fit(label, 42)}"
        )
        if reason:
            lines.append(f"       {_fit(reason, width - 8)}")
    return lines


def model_info(row: Row) -> ModelInfo:
    cmd = _job_cmd(row)
    meta = json_loads(row["metadata_json"] if _row_has(row, "metadata_json") else "", {})
    provider = str(row["provider"] or "")
    model = _arg_after(cmd, "--model") or meta.get("model") or _stderr_model(row) or _provider_default_from_cmd(provider)
    effort = _arg_after(cmd, "--effort") or _arg_after(cmd, "-c", prefix="model_reasoning_effort=") or "?"
    speed = "fast" if str(model).endswith("-fast") or "fast" in " ".join(cmd).lower() else "normal"
    return ModelInfo(str(model or "?"), str(effort or "?"), speed)


def chat_model_info(row: Row, meta: dict[str, Any]) -> ModelInfo:
    provider = str(row["provider"] or "")
    model = meta.get("model") or _provider_default_from_cmd(provider)
    if provider == "cursor":
        model = meta.get("model") or "auto"
    return ModelInfo(str(model or "?"), "?", "fast" if str(model).endswith("-fast") else "normal")


def _job_working_text(row: Row, limit: int) -> str:
    prompt = compact(row["prompt"], limit)
    stdout = read_text(Path(row["stdout_path"]), limit=limit * 4).strip() if row["stdout_path"] else ""
    stderr = read_text(Path(row["stderr_path"]), limit=limit * 4).strip() if row["stderr_path"] else ""
    text = stdout or stderr
    if text:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        useful = [line for line in lines if not line.startswith(("diff --git", "index ", "@@ "))]
        if useful:
            return compact(" ".join(useful[-8:]), limit)
    return prompt


def _stderr_model(row: Row) -> str:
    stderr = read_text(Path(row["stderr_path"]), limit=20000) if row["stderr_path"] else ""
    for pattern in (r"\bmodel:\s*([A-Za-z0-9._:-]+)", r"\bmodel=([A-Za-z0-9._:-]+)"):
        match = re.search(pattern, stderr, re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def _job_cmd(row: Row) -> list[str]:
    try:
        parsed = json.loads(row["cmd_json"] or "[]")
        return [str(x) for x in parsed] if isinstance(parsed, list) else []
    except Exception:
        return []


def _arg_after(cmd: list[str], flag: str, prefix: str = "") -> str:
    for i, item in enumerate(cmd):
        if prefix and item.startswith(prefix):
            return item.split("=", 1)[1]
        if item == flag and i + 1 < len(cmd):
            return cmd[i + 1]
        if item.startswith(flag + "="):
            return item.split("=", 1)[1]
    return ""


def _provider_default(store: Store, provider: str) -> str:
    if provider == "cursor":
        return store.get_config("cursor_model", "auto")
    return _provider_default_from_cmd(provider)


def _provider_default_from_cmd(provider: str) -> str:
    return {
        "codex": "configured",
        "claude": "configured",
        "antigravity": "configured",
        "grok": "grok-build",
        "cursor": "auto",
    }.get(provider, "?")


def _provider_health(provider: str) -> str:
    if provider == "antigravity":
        return "agentapi-ok" if (HOME / ".gemini" / "antigravity" / "bin" / "agentapi").exists() else "agentapi-missing"
    command = {
        "cursor": "cursor-agent",
    }.get(provider, provider)
    return "cmd-ok" if command_exists(command) else "cmd-missing"


def _count(store: Store, sql: str) -> int:
    row = store.row(sql)
    return int(row["c"] if row else 0)


def _count_since(rows: list[Row], cutoff: float) -> int:
    return sum(1 for row in rows if parse_ts(row["created_at"]) >= cutoff)


def _is_priority(store: Store, chat_id: str) -> bool:
    row = store.row(
        "select count(*) c from project_priorities where status='active' and target_chat_id=?",
        (chat_id,),
    )
    return int(row["c"] if row else 0) > 0


def _row_has(row: Row, key: str) -> bool:
    try:
        return key in row.keys()
    except Exception:
        return False


def _fit(text: Any, width: int) -> str:
    width = max(4, width)
    return compact(text, width)


def _title(text: str, width: int) -> str:
    return f"-- {text} " + "-" * max(0, width - len(text) - 4)


def _bar(text: str, width: int) -> str:
    label = f" {text} "
    side = max(0, (width - len(label)) // 2)
    right = max(0, width - len(label) - side)
    return "=" * side + label + "=" * right
