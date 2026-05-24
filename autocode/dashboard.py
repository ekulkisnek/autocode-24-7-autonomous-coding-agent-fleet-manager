from __future__ import annotations

import json
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Row
from typing import Any

from .config import DB, HOME, LOG
from .launchd import status as launchd_status
from .resources import format_job_resource, format_system_resource, job_resource, system_resource
from .scheduler import Scheduler
from .store import Store
from .util import command_exists, compact, json_loads, now_iso, parse_ts, read_text, rel_time


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
    resources = system_resource()

    lines: list[str] = []
    lines.append(_bar("AutoCode Dashboard", width))
    lines.append(
        f"{now_iso()} | daemon={'on' if daemon_ok else 'off'} | yolo={yolo} | "
        f"priority_only={priority_only} | jobs={active_jobs}/{cap} | {format_system_resource(resources)}"
    )
    lines.append(f"db={DB} | chats={total_chats} total, {active_chats} active/adopted")
    lines.extend(_session_summary(store, width))
    lines.append("")
    lines.extend(_running_section(store, width, limit))
    lines.append("")
    lines.extend(_queue_section(store, width, limit))
    lines.append("")
    lines.extend(_usage_section(store, width))
    lines.append("")
    lines.extend(_recent_section(store, width, min(limit, 8)))
    lines.append("")
    lines.append("Quota note: exact counters are shown only when a provider exposes a reliable read-only endpoint.")
    lines.append("Usage columns are observed AutoCode jobs, not provider billing totals.")
    return "\n".join(lines).rstrip() + "\n"


def run_dashboard(
    interval: float = 2.0,
    limit: int = 12,
    once: bool = False,
    alt_screen: bool = False,
    append_history: bool = False,
) -> None:
    store = Store()
    if not sys.stdout.isatty():
        print(render_dashboard(store, limit=limit), end="")
        return
    if once:
        print(render_dashboard(store, limit=limit), end="")
        return
    if alt_screen:
        print("\033[?1049h\033[?25l", end="", flush=True)
    previous_lines = 0
    try:
        while True:
            text = render_dashboard(store, limit=limit)
            if alt_screen:
                print("\033[H\033[2J\033[3J", end="")
                print(text, end="", flush=True)
            elif append_history:
                print(text, end="\n", flush=True)
            else:
                if previous_lines:
                    print(f"\033[{previous_lines}F\033[J", end="")
                print(text, end="", flush=True)
                previous_lines = text.count("\n")
            time.sleep(max(0.5, interval))
    except KeyboardInterrupt:
        pass
    finally:
        if alt_screen:
            print("\033[?25h\033[?1049l", end="", flush=True)


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
        counts = _chat_drive_counts(store, row["chat_id"])
        lines.append(
            f"  {rel_time(row['created_at']):>4}  {row['provider']:<11} {model:<24} "
            f"{row['evidence_status']:<16} {row['id']}  {_fit(label, 42)}"
        )
        lines.append(f"        prompts: session={counts[0]} total={counts[1]}")
        if row["cwd"]:
            lines.append(f"        cwd: {_fit(row['cwd'], width - 13)}")
        resource = job_resource(row)
        lines.append(f"        resources: {_fit(format_job_resource(resource), width - 19)}")
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
            lines.append(f"      goal: {_fit(_objective_summary(p['objective']), width - 14)}")
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
        objective = _objective_summary(row["objective"] or row["title"] or row["latest_text"])
        counts = _chat_drive_counts(store, row["id"])
        lines.append(
            f"    {flag} {rel_time(row['updated_at']):>4} {row['provider']:<11} {model:<22} "
            f"{state:<11} prompts={counts[0]}/{counts[1]} {_fit(title, 34)}"
        )
        lines.append(f"      next: {_fit(objective, width - 12)}")
    return lines


def _session_summary(store: Store, width: int) -> list[str]:
    start = _session_started_at()
    rows = store.rows(
        """
        select provider,count(*) c
        from jobs
        where created_at>=?
        group by provider
        order by c desc, provider
        """,
        (start,),
    )
    total = sum(int(row["c"]) for row in rows)
    split = ", ".join(f"{row['provider']}={row['c']}" for row in rows) or "none"
    return [f"session prompts: {total} since {rel_time(start)} ({_fit(split, width - 38)})"]


def _usage_section(store: Store, width: int) -> list[str]:
    now = time.time()
    jobs = store.rows("select provider,status,created_at,updated_at,evidence_status from jobs")
    quota = _quota_results()
    lines = [_title("Provider Usage / Health", width)]
    lines.append("  provider     health        running  1h   24h  7d   fail24  default/model")
    for provider in PROVIDERS:
        provider_jobs = [j for j in jobs if j["provider"] == provider]
        running = sum(1 for j in provider_jobs if j["status"] == "running")
        one_h = _count_since(provider_jobs, now - 3600)
        day = _count_since(provider_jobs, now - 86400)
        week = _count_since(provider_jobs, now - 7 * 86400)
        fail24 = sum(1 for j in provider_jobs if j["status"] == "failed" and parse_ts(j["updated_at"]) >= now - 86400)
        health = _provider_health(provider)
        default = _provider_default(store, provider)
        lines.append(
            f"  {provider:<12} {health:<13} {running:>7} {one_h:>4} {day:>5} {week:>4} "
            f"{fail24:>7}  {_fit(default, 18):<18}"
        )
        lines.append(f"      quota: {_quota_remaining(provider, quota, width=width)}")
    return lines


def _objective_summary(text: str) -> str:
    clean = " ".join((text or "").split())
    if not clean:
        return ""
    for pattern in (
        r"\bHard completion definition:\s*[^.!?]+[.!?]?\s*",
        r"\bOperating rule:\s*[^.!?]+[.!?]?\s*",
    ):
        clean = re.sub(pattern, "", clean, flags=re.IGNORECASE).strip()
    if len(clean) < 40:
        return clean
    parts = re.split(r"(?<=[.!?])\s+", clean)
    for part in parts:
        if 25 <= len(part) <= 220:
            return part.strip()
    return clean[:220].rstrip()


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
        summary, byte_note = _job_done_summary(row, 260)
        lines.append(
            f"  {rel_time(row['updated_at']):>4} {row['provider']:<11} {row['status']:<9} "
            f"{row['evidence_status']:<16} {_fit(label, 42)}"
        )
        if summary:
            lines.append(f"        done: {_fit(summary, width - 15)}")
        if byte_note:
            lines.append(f"             {_fit(byte_note, width - 14)}")
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
    read_limit = limit * 4
    stdout = _read_job_log(Path(row["stdout_path"]), read_limit).strip() if row["stdout_path"] else ""
    stderr = _read_job_log(Path(row["stderr_path"]), read_limit).strip() if row["stderr_path"] else ""
    text = "\n".join(part for part in (stdout, stderr) if part)
    if text:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        useful = [_clean_working_line(line) for line in lines if _useful_working_line(line)]
        useful = [line for line in useful if line]
        if useful:
            build = _build_progress_summary(useful)
            if build:
                return compact(build, limit)
            status = _status_lines(useful)
            if status:
                return compact(" ".join(status[-3:]), limit)
            tests = _test_progress_summary(useful)
            if tests:
                return compact(tests, limit)
            return compact(" ".join(useful[-4:]), limit)
    return _prompt_summary(row["prompt"], limit)


def _read_job_log(path: Path, limit: int) -> str:
    text = read_text(path, limit=limit)
    try:
        truncated = path.exists() and path.stat().st_size > limit
    except OSError:
        truncated = False
    if truncated and "\n" in text:
        return text.split("\n", 1)[1]
    return text


def _prompt_summary(prompt: str, limit: int) -> str:
    lines = [line.strip() for line in (prompt or "").splitlines() if line.strip()]
    for line in lines:
        if line.lower().startswith(("current known next step:", "autocode instruction:", "latest known context:")):
            value = line.split(":", 1)[-1].strip()
            if value:
                return compact(f"Waiting for first agent output; assigned: {value}", limit)
    for line in lines:
        if _useful_prompt_line(line):
            return compact(f"Waiting for first agent output; assigned: {line}", limit)
    return "Waiting for first agent output"


def _useful_prompt_line(line: str) -> bool:
    lower = line.lower()
    if lower.startswith(("operating rule:", "rules:", "hard completion definition:", "do not ", "use the fastest", "if ", "when ", "output ", "- ")):
        return False
    if re.match(r"^\d+\.\s", line.strip()):
        return False
    if "autocode is driving this project" in lower or "maximum yolo mode" in lower:
        return False
    if "FLEET_DONE" in line or "FLEET_MILESTONE_COMPLETE" in line:
        return False
    return len(line) >= 20


def _job_done_summary(row: Row, limit: int) -> tuple[str, str]:
    working = _job_working_text(row, limit)
    if working and not working.startswith("Waiting for first agent output"):
        summary = working
    else:
        summary = _evidence_reason_summary(row["evidence_reason"] or "", limit)
    return summary, _evidence_byte_note(row["evidence_reason"] or "")


def _evidence_reason_summary(reason: str, limit: int) -> str:
    text = reason or ""
    text = re.sub(r"stdout_bytes=\d+;?\s*", "", text)
    text = re.sub(r"stderr_bytes=\d+;?\s*", "", text)
    text = re.sub(r"^process exited;?\s*", "", text, flags=re.IGNORECASE)
    text = text.strip(" ;")
    return compact(text, limit) if text else ""


def _evidence_byte_note(reason: str) -> str:
    if not reason:
        return ""
    parts: list[str] = []
    stdout_match = re.search(r"stdout_bytes=(\d+)", reason)
    stderr_match = re.search(r"stderr_bytes=(\d+)", reason)
    if stdout_match:
        parts.append(f"stdout {_human_bytes(int(stdout_match.group(1)))}")
    if stderr_match:
        parts.append(f"stderr {_human_bytes(int(stderr_match.group(1)))}")
    return ", ".join(parts)


def _human_bytes(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f}MB"
    if size >= 1024:
        return f"{size / 1024:.1f}kB"
    return f"{size}B"


def _build_progress_summary(lines: list[str]) -> str:
    gradle = [line for line in lines if line.startswith("> Task ")]
    if gradle:
        task = gradle[-1].replace("> Task ", "").strip()
        return f"Android/Gradle build running: latest task {task}"
    return ""


def _test_progress_summary(lines: list[str]) -> str:
    tests = [line for line in lines if re.search(r"\b(pass|passed|fail|failed|tests?)\b", line, re.IGNORECASE)]
    if tests:
        return tests[-1]
    return ""


def _clean_working_line(line: str) -> str:
    clean = line.strip()
    clean = re.sub(r"^(?:FLEET_MILESTONE_COMPLETE|FLEET_DONE)\b[:\s-]*", "", clean).strip()
    clean = re.split(r"\bCompleted evidence:\s*", clean, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    clean = re.sub(r"^\s*[-*]\s+", "", clean).strip()
    return clean


def _status_lines(lines: list[str]) -> list[str]:
    markers = (
        "FLEET_MILESTONE_COMPLETE",
        "FLEET_DONE",
        "current ",
        "progress",
        "next action",
        "next step",
        "fixed ",
        "verified",
        "passed",
        "failed",
        "running",
        "still running",
        "blocked",
        "latest evidence",
        "result:",
    )
    return [line for line in lines if any(marker in line.lower() for marker in markers)]


def _useful_working_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    lower = stripped.lower()
    noise_prefixes = (
        "diff --git",
        "index ",
        "new file mode ",
        "deleted file mode ",
        "similarity index ",
        "rename from ",
        "rename to ",
        "@@ ",
        "+++ ",
        "--- ",
        "+",
        "-",
        "exec",
        "codex",
        "tokens used",
        "set -",
        "export ",
        "if ",
        "fi",
        "done",
        "do",
        "then",
        "else",
        "for ",
        "while ",
        "elif ",
        "case ",
        "esac",
        "chmod ",
        "cat ",
        "echo ",
        "> Configure project",
    )
    if stripped in {"codex", "exec", "```", "```text", "```bash", "```sh"}:
        return False
    if re.match(r"^[{}\[\],:\s]+$", stripped):
        return False
    if lower.startswith(("operating rule:", "rules:", "hard completion definition:", "current status to assume:")):
        return False
    if lower.startswith(("current known next step:", "autocode instruction:", "latest known context:")):
        return False
    if lower.startswith((
        "handoff here:",
        "i also linked it from",
        "and logged the context/handoff",
        "the handoff includes",
        "short current checkpoint:",
    )):
        return False
    if any(
        token in lower
        for token in (
            "codex_handoff_native_signer.md",
            "agent_coordination.md",
            "local_development_notes.md",
            "the handoff includes",
            "short current checkpoint",
        )
    ):
        return False
    if stripped.startswith(noise_prefixes):
        return False
    if re.match(r"^\d+\.\s", stripped):
        return False
    if lower.startswith("(use `node --trace-deprecation"):
        return False
    if "WARN codex_core_skills::loader" in stripped:
        return False
    if stripped.startswith(("/bin/", "./", "docker compose ", "git diff ", "tail ", "TMUX_TMPDIR=")):
        return False
    if lower.startswith(("succeeded in ", "failed in ", "errored in ")):
        return False
    if " child-process:exec_cmd " in lower or " child-process:exec_try " in lower:
        return False
    if re.match(r"^\d+\s+\d+:\d+(?::\d+)?\s+", stripped):
        return False
    if re.match(r"^\d+:\d{2}:\d{2}:\d{2}\.\d+\s+", stripped):
        return False
    if re.match(r"^\d{2}:\d{2}:\d{2}\.\d+\s+detox\[\d+\]\s+", stripped):
        return False
    if re.match(r"^\d+\s+\d+:\d+\s+(node|python|ruby|java|gradle|bash|sh)\b", stripped):
        return False
    if re.match(r"^\d+\s+\d+:\d+(?::\d+)?\s+/.+\b(ps|rg|tmux|autocode)\b", stripped):
        return False
    if "node_modules/.bin/" in stripped or "/.bin/detox " in stripped:
        return False
    if any(path in stripped for path in ("$HOME/Library/Android/sdk", "/Volumes/T705/code/android-commandlinetools", "/opt/homebrew/share/android")):
        return False
    if "\\" in stripped and ("/" in stripped or "$" in stripped):
        return False
    if lower.startswith(("npx ", "npm run ", "ps -axo ", "rg ", "tmux ", "status commands:")):
        return False
    if re.match(r"^(?:/usr/bin/)?(?:ps|rg|tmux)\b", stripped):
        return False
    if re.match(r'^"[A-Z0-9_]+=.*",?$', stripped):
        return False
    if re.match(r'^"[A-Za-z0-9:_+.\-]+":\s*', stripped):
        return False
    if stripped.startswith("> Task "):
        return True
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", stripped):
        return False
    if re.match(r"^[+\-]\s", stripped):
        return False
    if stripped.startswith(("- Edit files", "- Do not spend", "- When a background", "- If this is only", "- Output FLEET_DONE", "- If blocked")):
        return False
    return True


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
    return "cmd-ok" if _command_available(command) else "cmd-missing"


def _quota_results() -> dict[str, Any]:
    try:
        from .quota_probes import probe_all
        return probe_all(use_cache=True)
    except Exception:
        return {}


def _quota_remaining(provider: str, quota: dict[str, Any], *, width: int = 120) -> str:
    result = quota.get(provider)
    if not result:
        return "not exposed"
    if getattr(result, "status", "") in {"ok", "partial"}:
        summary = str(getattr(result, "summary", "") or "not exposed")
        return _fit(summary, max(48, width - 15))
    return "not exposed"


def _command_available(command: str) -> bool:
    if command_exists(command) or shutil.which(command):
        return True
    common = {
        "codex": ["/Applications/Codex.app/Contents/Resources/codex"],
        "claude": [str(HOME / ".local" / "bin" / "claude")],
        "grok": [str(HOME / ".grok" / "bin" / "grok")],
        "cursor-agent": [str(HOME / ".local" / "bin" / "cursor-agent")],
    }
    return any(Path(path).exists() for path in common.get(command, []))


def _count(store: Store, sql: str) -> int:
    row = store.row(sql)
    return int(row["c"] if row else 0)


def _count_since(rows: list[Row], cutoff: float) -> int:
    return sum(1 for row in rows if parse_ts(row["created_at"]) >= cutoff)


def _chat_drive_counts(store: Store, chat_id: str) -> tuple[int, int]:
    start = _session_started_at()
    session_row = store.row(
        "select count(*) c from jobs where chat_id=? and created_at>=?",
        (chat_id, start),
    )
    total_row = store.row("select count(*) c from jobs where chat_id=?", (chat_id,))
    return int(session_row["c"] if session_row else 0), int(total_row["c"] if total_row else 0)


def _session_started_at() -> str:
    text = read_text(LOG, limit=300000)
    for line in reversed(text.splitlines()):
        if " daemon started" in line:
            return line.split(" daemon started", 1)[0].strip()
    row = Store().row("select min(created_at) c from jobs")
    return str(row["c"] if row and row["c"] else "1970-01-01T00:00:00+00:00")


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
