from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .config import DB, DEFAULT_PRESERVE_JOBS_ON_SHUTDOWN, LOG, PID_FILE, ROOT, ensure_dirs
from . import grok_watchdog
from .daemon import Daemon
from .dashboard import run_dashboard
from .discovery import discover
from .launchd import install as launchd_install
from .launchd import start as launchd_start
from .launchd import status as launchd_status
from .launchd import stop as launchd_stop
from .providers import providers
from .preprint_release import write_kit
from .scheduler import Scheduler
from .runner import JobRunner
from .store import Store
from .models import ContinuePlan
from .util import command_exists, compact, json_loads, load1, memory_free_percent, read_text, rel_time, sha


def print_status(store: Store, limit: int) -> None:
    Scheduler(store).runner.refresh()
    chats = store.rows("select count(*) c from chats")
    queue_rows = store.queue_list()
    finished_rows = store.queue_finished_list(limit)
    running = store.rows("select * from jobs where status='running' order by created_at desc limit ?", (limit,))
    recent = store.rows("select * from jobs where status!='running' order by updated_at desc limit ?", (limit,))
    daemon_ok, _ = launchd_status()
    working = f"WORKING ({len(running)} running)" if running else "IDLE"
    print(f"AutoCode | {working}")
    print(f"daemon: {'on' if daemon_ok else 'off'} | yolo={store.get_config('yolo','off')} | load={load1():.2f}")
    print(f"db: {DB}")
    print(f"chats: {int(chats[0]['c']) if chats else 0} discovered, {len(queue_rows)} in queue", end="")
    if finished_rows:
        print(f", {len(finished_rows)} finished (show: autocode queue finished)")
    else:
        print()
    if running:
        print(f"running ({len(running)}):")
        for j in running:
            print(f"- {rel_time(j['created_at'])} {j['provider']} {short(j['chat_id'])} {j['id']} {j['evidence_status']}")
    else:
        print("running: none")
    print(f"queue ({len(queue_rows)}):")
    for r in queue_rows[:limit]:
        pos = r["position"]
        pos_str = f"#{int(pos)}" if pos == int(pos) else f"#{pos:.1f}"
        print(f"- {pos_str} {r['provider']} {short(r['alias'] or r['title'])} {rel_time(r['updated_at'])}")
    if recent:
        print("recent jobs:")
        for j in recent[:limit]:
            print(f"- {rel_time(j['updated_at'])} {j['evidence_status']} {short(j['chat_id'])}")
            if j["evidence_reason"]:
                print(f"  {compact(j['evidence_reason'], 160)}")


def print_now(store: Store, limit: int) -> None:
    Scheduler(store).runner.refresh()
    running = store.rows("select * from jobs where status='running' order by created_at desc limit ?", (limit,))
    candidates = Scheduler(store).candidates(limit)
    if running:
        print(f"Running now ({len(running)}):")
        for j in running:
            print(f"- {rel_time(j['created_at'])} {j['provider']} {short(j['chat_id'])} {j['id']} {j['evidence_status']}")
    else:
        print("Running now: none (IDLE)")
    print(f"Queue ({len(candidates)} items):")
    for c in candidates:
        pos = c["queue_position"]
        pos_str = f"#{int(pos)}" if pos == int(pos) else f"#{pos:.1f}"
        print(f"- {pos_str} {rel_time(c['updated_at'])} {c['provider']} {short(c['alias'])} state={c['state']}")
        print(f"  {compact(c['objective'], 180)}")


def short(text: str, n: int = 58) -> str:
    return compact(text, n)


def mask_contacts(text: str) -> str:
    return re.sub(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+)", r"\1***\2", text or "")


def priority_label(store: Store, chat_id: str, fallback: str) -> str:
    row = store.row(
        """
        select query from project_priorities
        where status='active' and target_chat_id=?
        order by priority desc, updated_at desc
        limit 1
        """,
        (chat_id,),
    )
    return str(row["query"]) if row else fallback


def active_job_for(store: Store, chat_id: str):
    return store.row(
        "select * from jobs where chat_id=? and status='running' order by created_at desc limit 1",
        (chat_id,),
    )


def job_tail(job, limit: int = 260) -> str:
    if not job:
        return ""
    messages = job_messages(job)
    if messages:
        return compact(messages[-1], limit)
    stdout = read_text(Path(job["stdout_path"]), limit=limit * 4).strip()
    stderr = read_text(Path(job["stderr_path"]), limit=max(50000, limit * 30)).strip()
    text = stdout or stderr or job["evidence_reason"] or ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    codex_messages: list[str] = []
    for i, line in enumerate(lines):
        if line != "codex":
            continue
        msg: list[str] = []
        for nxt in lines[i + 1:]:
            if nxt in {"codex", "exec"} or nxt.startswith(("diff --git", "@@ ")):
                break
            if nxt.startswith(("+", "-")):
                continue
            msg.append(nxt)
            if len(" ".join(msg)) >= limit:
                break
        if msg:
            codex_messages.append(" ".join(msg))
    if codex_messages:
        return compact(codex_messages[-1], limit)
    interesting = []
    for line in reversed(lines):
        if line.startswith(("+", "-", "@@")):
            continue
        low = line.lower()
        if "warning:" in low or "generated." in low:
            continue
        interesting.append(line)
        if len(" ".join(reversed(interesting))) >= limit:
            break
    return compact(" ".join(reversed(interesting)) or text, limit)


def job_messages(job, limit_bytes: int = 180000) -> list[str]:
    if not job:
        return []
    text = "\n".join(
        part for part in [
            read_text(Path(job["stdout_path"]), limit=limit_bytes).strip(),
            read_text(Path(job["stderr_path"]), limit=limit_bytes).strip(),
        ] if part
    )
    lines = [line.rstrip() for line in text.splitlines()]
    messages: list[str] = []
    i = 0
    stop_prefixes = (
        "diff --git",
        "@@ ",
        "exec",
        "apply patch",
        "patch:",
        "/bin/",
        "error:",
        "warning:",
        "** ",
    )
    while i < len(lines):
        if lines[i].strip() != "codex":
            i += 1
            continue
        i += 1
        body: list[str] = []
        while i < len(lines):
            line = lines[i].strip()
            if line == "codex":
                break
            if line in {"exec", "apply patch"} or line.startswith(stop_prefixes):
                break
            if line and not line.startswith(("+", "-", "index ")):
                body.append(line)
            i += 1
        text_body = compact(" ".join(body), 2000)
        if text_body:
            messages.append(text_body)
    return messages


def cmd_last(args: argparse.Namespace) -> None:
    store = Store()
    Scheduler(store).runner.refresh()
    row = None
    priority = store.find_priority(args.query)
    if priority and priority["target_chat_id"]:
        row = store.row("select * from chats where id=?", (priority["target_chat_id"],))
    if not row:
        row = store.find_chat(args.query)
    if not row:
        raise SystemExit(f"No chat matched: {args.query}")
    job = active_job_for(store, row["id"])
    label = priority_label(store, row["id"], row["alias"])
    print(f"{label} [{row['provider']}]")
    if job:
        messages = job_messages(job)
        print(f"state: running | job: {job['id']} | updated: {rel_time(job['updated_at'])}")
        if messages:
            print(messages[-1])
        else:
            print(job_tail(job, 800))
        return
    print(f"state: {row['state']} | updated: {rel_time(row['updated_at'])}")
    print(compact(row["latest_text"] or row["title"], 2000))


def parse_recent(value: str) -> float:
    text = (value or "24h").strip().lower()
    if text.endswith("m"):
        return float(text[:-1]) * 60
    if text.endswith("h"):
        return float(text[:-1]) * 3600
    if text.endswith("d"):
        return float(text[:-1]) * 86400
    return float(text) * 3600


def cmd_status(args: argparse.Namespace) -> None:
    print_status(Store(), args.limit)


def cmd_now(args: argparse.Namespace) -> None:
    print_now(Store(), args.limit)


def cmd_goals(args: argparse.Namespace) -> None:
    store = Store()
    rows = store.rows("select g.*,c.alias,c.provider,c.updated_at from goals g join chats c on c.id=g.chat_id where g.status='active' order by g.updated_at desc limit ?", (args.limit,))
    if not rows:
        print("No active goals.")
        return
    for r in rows:
        print(f"- {r['provider']} {short(r['alias'])} {rel_time(r['updated_at'])}")
        print(f"  {r['objective']}")


def cmd_priority(args: argparse.Namespace) -> None:
    store = Store()
    if args.priority_cmd == "list":
        rows = store.rows("select * from project_priorities where status='active' order by priority desc, updated_at desc limit ?", (args.limit,))
        if not rows:
            print("No active priority projects.")
            return
        for r in rows:
            matches = store.rows(
                """
                select provider,alias,state,updated_at from chats
                where paused=0 and done=0 and coding_score>0 and (
                  lower(id)=lower(?) or lower(alias)=lower(?) or lower(provider_chat_id)=lower(?)
                  or lower(title) like ? or lower(alias) like ? or lower(cwd) like ?
                )
                order by updated_at desc limit 5
                """,
                (r["query"], r["query"], r["query"], f"%{r['query'].lower()}%", f"%{r['query'].lower()}%", f"%{r['query'].lower()}%"),
            )
            resource = f" resource={r['resource_path']}" if r["resource_path"] else ""
            target = f" target={short(r['target_chat_id'])}" if r["target_chat_id"] else ""
            lanes = f" lanes={r['worker_lanes']}" if int(r["worker_lanes"] or 1) > 1 else ""
            print(f"- p{r['priority']} {r['query']} [{r['id']}]{resource}{target}{lanes}")
            print(f"  goal: {compact(r['objective'], 220)}")
            if matches:
                print("  matches:")
                for m in matches:
                    print(f"  - {rel_time(m['updated_at'])} {m['provider']} {short(m['alias'])} state={m['state']}")
            else:
                print("  matches: none currently discovered")
    elif args.priority_cmd == "add":
        target_chat_id = args.chat_id
        if args.exact:
            found = store.find_chat(args.query)
            if not found:
                raise SystemExit(f"No chat matched for --exact: {args.query}")
            target_chat_id = found["id"]
        pid = store.add_priority(args.query, args.goal, args.rank, args.path, target_chat_id, args.lanes)
        Scheduler(store).force_discover()
        print(f"Priority added: {pid}")
        print(f"query: {args.query}")
        print(f"rank: {args.rank}")
        if args.path:
            print(f"path: {args.path}")
        if target_chat_id:
            print(f"target_chat_id: {target_chat_id}")
        if args.lanes > 1:
            print(f"lanes: {args.lanes}")
        print(f"goal: {args.goal}")
    elif args.priority_cmd == "remove":
        killed = Scheduler(store).runner.kill_priority_jobs(args.query, "priority_removed")
        count = store.remove_priority(args.query)
        print(f"Paused {count} priority project(s); killed {killed} running job(s).")


def squad_lanes(priority) -> list[dict[str, str]]:
    objective = priority["objective"]
    path = priority["resource_path"] or "/Users/lukekensik"
    target = priority["target_chat_id"] or ""
    base = {
        "objective": objective,
        "path": path,
        "target": target,
    }
    return [
        {
            **base,
            "name": "ios-build-fixer",
            "provider": "grok",
            "mode": "read_only",
            "task": (
                "Inspect the RedWallet iOS/Hermes/native build blocker and identify the smallest safe fix. "
                "Focus on stale paths, generated Pods config, native bridge signatures, and commands the writer should run next."
            ),
        },
        {
            **base,
            "name": "diff-reviewer",
            "provider": "codex",
            "mode": "read_only",
            "task": (
                "Review the current dirty diff for correctness, production readiness, and regression risk. "
                "Identify missing tests, risky native lifecycle behavior, and the safest commit sequence."
            ),
        },
        {
            **base,
            "name": "e2e-planner",
            "provider": "claude",
            "mode": "read_only",
            "task": (
                "Design the fastest verification path from the current state to production-ready completion. "
                "Focus on iOS Detox, funded BitAssets smoke, Docker/signet prerequisites, and how to prove persistence is truly fixed."
            ),
        },
        {
            **base,
            "name": "worktree-experiment",
            "provider": "grok",
            "mode": "worktree",
            "task": (
                "Optional isolated experiment lane. Only use a separate worktree/copy, never the main checkout. "
                "Prototype a fix if the primary writer remains blocked, then report a patch plan for the writer to adopt."
            ),
        },
    ]


def squad_prompt(lane: dict[str, str]) -> str:
    snapshot = repo_snapshot(lane["path"])
    return (
        f"AutoCode squad helper lane: {lane['name']}\n"
        f"Provider lane: {lane['provider']}\n"
        f"Mode: {lane['mode']}\n"
        f"Primary objective: {lane['objective']}\n"
        f"Repository: {lane['path']}\n"
        f"Primary writer chat: {lane['target'] or 'not pinned'}\n\n"
        "Rules:\n"
        "- You are a helper, not the primary writer.\n"
        "- Do not edit files, commit, push, run destructive commands, or start long GUI workflows in read_only mode.\n"
        "- Keep your work bounded: inspect, reason, and produce actionable findings for the primary writer.\n"
        "- If you need to run commands, prefer read-only commands or short checks under 2 minutes.\n"
        "- The primary writer remains the exact Codex chat; your output will be collected and fed there.\n\n"
        f"Task:\n{lane['task']}\n\n"
        f"Current repo snapshot:\n{snapshot}\n\n"
        "Return this shape:\n"
        "SQUAD_FINDINGS:\n"
        "- lane:\n"
        "- verdict:\n"
        "- evidence inspected:\n"
        "- highest-confidence next action for primary writer:\n"
        "- exact command/file pointers:\n"
        "- risks or blockers:\n"
    )


def repo_snapshot(path: str) -> str:
    root = Path(path).expanduser()
    if not root.exists():
        return f"path does not exist: {path}"
    commands = [
        ["git", "-C", str(root), "status", "--short"],
        ["git", "-C", str(root), "diff", "--stat"],
        ["git", "-C", str(root), "branch", "--show-current"],
    ]
    chunks = []
    for cmd in commands:
        try:
            res = subprocess.run(cmd, text=True, capture_output=True, timeout=8)
            label = " ".join(cmd[2:])
            text = (res.stdout + res.stderr).strip()
            chunks.append(f"$ {label}\n{text[:4000] or '(no output)'}")
        except Exception as exc:
            chunks.append(f"$ {' '.join(cmd[2:])}\nfailed: {exc}")
    return "\n\n".join(chunks)


def squad_plan_for(lane: dict[str, str], prompt: str, job_dir: Path) -> ContinuePlan | None:
    provider = lane["provider"]
    cwd = lane["path"] if Path(lane["path"]).exists() else "/Users/lukekensik"
    if provider == "grok":
        return ContinuePlan(
            True,
            "grok",
            cwd,
            cmd=[
                "grok",
                "--cwd",
                cwd,
                "--prompt-file",
                str(job_dir / "prompt.txt"),
                "--no-alt-screen",
                "--permission-mode",
                "bypassPermissions",
            ],
            prompt_file=True,
            same_chat=False,
            reason="Squad helper lane.",
        )
    if provider == "codex":
        return ContinuePlan(
            True,
            "codex",
            cwd,
            cmd=["codex", "exec", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "-C", cwd, "-"],
            stdin=prompt,
            same_chat=False,
            reason="Squad helper lane.",
        )
    if provider == "claude":
        return ContinuePlan(
            True,
            "claude",
            cwd,
            cmd=["claude", "--print", "--output-format", "text"],
            stdin=prompt,
            same_chat=False,
            reason="Squad helper lane.",
        )
    return None


def cmd_squad(args: argparse.Namespace) -> None:
    store = Store()
    priority = store.find_priority(args.query)
    if not priority:
        raise SystemExit(f"No active priority project matched: {args.query}")
    sched = Scheduler(store)
    lanes = squad_lanes(priority)
    if args.squad_cmd == "plan":
        print(f"Squad plan for p{priority['priority']} {priority['query']}")
        print(f"repo: {priority['resource_path'] or '(none)'}")
        print(f"writer: {priority['target_chat_id'] or '(not pinned)'}")
        for lane in lanes:
            available = command_exists(lane["provider"])
            print(f"- {lane['name']} [{lane['provider']}/{lane['mode']}] {'available' if available else 'missing'}")
            print(f"  {compact(lane['task'], 180)}")
        return
    if args.squad_cmd == "launch":
        active = sched.active_job_count()
        cap = sched.capacity()
        headroom = max(0, cap - active)
        requested = headroom if args.limit <= 0 else min(args.limit, headroom)
        mem = memory_free_percent()
        mem_text = f", mem_free={mem}%" if mem is not None else ""
        if not args.dry_run and active >= cap and not args.force:
            print(f"Capacity full: active_jobs={active}/{cap}, load={load1():.2f}{mem_text}. Not launching squad helpers.")
            print("Use --force only if you intentionally want to exceed the current resource cap.")
            return
        if not args.force:
            print(f"Squad resource budget: active_jobs={active}/{cap}, launch_slots={requested}, load={load1():.2f}{mem_text}")
        launched = 0
        launched_chat_ids: list[str] = []
        for lane in lanes:
            if args.mode != "all" and lane["mode"] != args.mode:
                continue
            if not args.force and launched >= requested:
                break
            if args.force and args.limit > 0 and launched >= args.limit:
                break
            if not command_exists(lane["provider"]):
                print(f"skip {lane['name']}: provider command missing ({lane['provider']})")
                continue
            chat_id = f"squad:{priority['id']}:{lane['name']}"
            existing = store.row("select * from jobs where chat_id=? and status='running' order by created_at desc limit 1", (chat_id,))
            if existing and not args.force:
                print(f"skip {lane['name']}: already running {existing['id']}")
                continue
            prompt = squad_prompt(lane)
            job_dir = sched._planned_job_dir()
            plan = squad_plan_for(lane, prompt, job_dir)
            if not plan:
                print(f"skip {lane['name']}: unsupported provider")
                continue
            if args.dry_run:
                print(f"would launch {lane['name']} [{lane['provider']}/{lane['mode']}]")
                print(f"cmd: {' '.join(plan.cmd)}")
                launched_chat_ids.append(chat_id)
                launched += 1
                continue
            lease = lane["path"] if lane["mode"] == "worktree" else ""
            job_id = sched.runner.start_aux(chat_id, lane["path"], plan, prompt, job_dir, lease_resource=lease)
            store.event("squad_lane_started", chat_id, job_id, priority_id=priority["id"], lane=lane["name"], mode=lane["mode"])
            print(f"launched {lane['name']}: {job_id}")
            launched_chat_ids.append(chat_id)
            launched += 1
        if launched == 0 and not args.dry_run:
            print("No squad lanes launched.")
        # Note: squad lanes are parallel by design (helper lenses).
        # --sequential records the launch order as a dependency hint in the event log only.
        if getattr(args, "sequential", False) and len(launched_chat_ids) > 1:
            order = " → ".join(cid.split(":")[-1] for cid in launched_chat_ids)
            print(f"  sequential order recorded: {order}")
            store.event("squad_sequential_hint", priority["id"], lane_order=order)
        return
    if args.squad_cmd == "collect":
        pattern = f"squad:{priority['id']}:%"
        rows = store.rows("select * from jobs where chat_id like ? order by updated_at desc limit ?", (pattern, args.limit))
        if not rows:
            print("No squad lane jobs found.")
            return
        summaries = []
        for j in rows:
            lane = j["chat_id"].split(":")[-1]
            stdout = read_text(Path(j["stdout_path"]), limit=2500)
            stderr = read_text(Path(j["stderr_path"]), limit=3500)
            body = (stdout.strip() or stderr.strip() or j["evidence_reason"] or "").strip()
            summary = f"{lane} ({j['status']}, {rel_time(j['updated_at'])}):\n{compact(body, 1400)}"
            summaries.append(summary)
            print(f"- {summary}\n")
        if args.send_writer:
            target = priority["target_chat_id"]
            if not target:
                print("Cannot send to writer: priority has no target_chat_id.")
                return
            writer = store.row("select * from chats where id=?", (target,))
            if not writer:
                print(f"Cannot send to writer: target chat not discovered: {target}")
                return
            if sched.has_active_job(target):
                print("Writer chat is currently running; findings not sent yet. Run collect --send-writer after it finishes.")
                return
            prompt = (
                "AutoCode squad findings for your current RedWallet objective.\n"
                "Use these as advisory input. Continue making the actual repo changes in this exact Codex chat until production ready.\n\n"
                + "\n\n".join(summaries)
            )
            job_id = sched.dispatch_with_prompt(writer, prompt)
            print(f"sent findings to writer: {job_id or 'not sent'}")


def cmd_chats(args: argparse.Namespace) -> None:
    store = Store()
    Scheduler(store).force_discover()
    cutoff = time.time() - parse_recent(args.recent)
    # Prefer non-transcript sources when a provider_chat_id has multiple entries
    rows = store.rows(
        """
        select c.* from chats c
        where c.updated_at != ''
          and (
            c.source not like '%.transcript'
            or not exists (
              select 1 from chats c2
              where c2.provider = c.provider
                and c2.provider_chat_id = c.provider_chat_id
                and c2.source not like '%.transcript'
            )
          )
        order by c.updated_at desc
        limit ?
        """,
        (args.limit * 8,),
    )
    shown = 0
    in_queue = {r["chat_id"] for r in store.rows("select chat_id from queue")}
    for r in rows:
        from .util import parse_ts
        job = active_job_for(store, r["id"])
        display_at = job["updated_at"] if job else r["updated_at"]
        if parse_ts(display_at) < cutoff:
            continue
        shown += 1
        state = "running" if job else r["state"]
        queued = " [Q]" if r["id"] in in_queue else ""
        title = r["title"] or r["latest_text"] or r["provider_chat_id"]
        print(f"- {rel_time(display_at):>5}  {r['provider']:<11} {state:<13} {short(title)}{queued}")
        if job:
            print(f"        job {job['id']} {job['evidence_status']}: {job_tail(job, 200)}")
        if shown >= args.limit:
            break
    if shown == 0:
        print("No chats in that window.")


def cmd_cursor(args: argparse.Namespace) -> None:
    store = Store()
    if args.cursor_cmd not in {"model", "models"}:
        Scheduler(store).force_discover()
    if args.cursor_cmd == "status":
        rows = store.rows(
            """
            select source,count(*) c,
              sum(case when json_extract(metadata_json,'$.active') then 1 else 0 end) active,
              sum(case when json_extract(metadata_json,'$.direct_continue') then 1 else 0 end) direct
            from chats
            where provider='cursor'
            group by source
            order by source
            """
        )
        total = sum(int(r["c"]) for r in rows)
        print(f"Cursor in AutoCode: {total} chats")
        for r in rows:
            print(f"- {r['source']}: {r['c']} seen, {r['active'] or 0} active/recent, {r['direct'] or 0} direct same-chat sends")
        from .providers.cursor import CursorProvider
        cursor_provider = CursorProvider()
        cursor_env = os.environ.copy()
        cursor_env.update(cursor_provider.cursor_env())
        auth = subprocess.run(["/usr/bin/env", "bash", "-lc", "cursor-agent status 2>/dev/null | head -n 1"], text=True, capture_output=True, timeout=8, env=cursor_env)
        line = mask_contacts(auth.stdout.strip() or auth.stderr.strip())
        print(f"- cursor-agent account: {line or 'unknown'}")
        print(f"- headless API key: {'configured' if cursor_provider.cursor_env().get('CURSOR_API_KEY') else 'missing'}")
        print(f"- default model: {cursor_provider.cursor_model()}")
        print("- direct drive: cursor.cli resumes same chat with cursor-agent --resume")
        print("- cloud drive: cursor.cloud posts same-agent follow-ups through Cursor Cloud API when the agent id is bc-*")
        print("- IDE drive: readable now; continued through a new Cursor Agent worker unless Cursor exposes a stable same-chat IDE composer API")
        return
    if args.cursor_cmd == "chats":
        where = "provider='cursor'"
        vals: list[object] = []
        if args.source:
            where += " and source=?"
            vals.append(args.source)
        rows = store.rows(
            f"""
            select * from chats
            where {where}
            order by updated_at desc
            limit ?
            """,
            tuple(vals + [args.limit]),
        )
        if not rows:
            print("No Cursor chats found.")
            return
        for r in rows:
            meta = json_loads(r["metadata_json"], {})
            direct = "same-chat" if meta.get("direct_continue") else "worker"
            active = meta.get("activity_status") or ("active" if meta.get("active") else "idle")
            provider_status = f", status={meta.get('status')}" if meta.get("status") else ""
            model = f", model={meta.get('model')}" if meta.get("model") else ""
            print(f"- {rel_time(r['updated_at'])} {r['source']} {short(r['alias'])} [{active}, {direct}{provider_status}{model}]")
            print(f"  {compact(r['title'] or r['latest_text'], 180)}")
        return
    if args.cursor_cmd == "history":
        row = store.find_chat(args.query)
        if not row or row["provider"] != "cursor":
            raise SystemExit(f"No Cursor chat matched: {args.query}")
        meta = json_loads(row["metadata_json"], {})
        print(f"{row['alias']} [{row['source']}]")
        print(f"updated: {rel_time(row['updated_at'])} | state: {row['state']} | continue: {row['continuation']}")
        print(f"activity: {meta.get('activity_status', 'unknown')} | history: {meta.get('history_quality', 'unknown')}")
        print(f"model: {meta.get('model') or 'auto'}")
        print()
        print(compact(row["latest_text"] or row["title"], args.chars))
        return
    if args.cursor_cmd == "models":
        from .providers.cursor import CursorProvider
        cursor_env = os.environ.copy()
        cursor_env.update(CursorProvider().cursor_env())
        res = subprocess.run(["cursor-agent", "models"], text=True, capture_output=True, timeout=20, env=cursor_env)
        print(res.stdout.strip() or res.stderr.strip())
        return
    if args.cursor_cmd == "model":
        from .providers.cursor import CursorProvider
        current = CursorProvider().cursor_model()
        if not args.model:
            print(f"cursor_model={current}")
            return
        available = cursor_models()
        if available and args.model not in available:
            raise SystemExit(f"Unknown Cursor model: {args.model}\nRun `autocode cursor models` to list valid ids.")
        store.set_config("cursor_model", args.model)
        print(f"cursor_model={args.model}")
        return
    if args.cursor_cmd == "new":
        from .models import ContinuePlan
        from .providers.cursor import CursorProvider
        workspace = Path(args.workspace).expanduser()
        cwd = str(workspace if workspace.exists() else Path.home())
        provider = CursorProvider()
        model = args.model or provider.cursor_model()
        goal = provider._cursor_safe_prompt(args.goal)
        plan = ContinuePlan(
            True,
            "cursor",
            cwd,
            cmd=[
                *provider.cursor_agent_cmd([
                    "cursor-agent",
                    "--print",
                    "--output-format",
                    "text",
                    "--force",
                    "--trust",
                    "--workspace",
                    cwd,
                ], model),
                goal,
            ],
            env=provider.cursor_env(),
            same_chat=False,
            reason="Start a new Cursor Agent chat.",
        )
        job_id = Scheduler(store).runner.start_aux(f"cursor:new:{sha(cwd + args.goal)[:16]}", cwd, plan, goal)
        print(f"Started Cursor Agent chat job: {job_id}")
        print(f"workspace: {cwd}")
        print(f"model: {model}")
        print(f"goal: {args.goal}")


def cmd_grok(args: argparse.Namespace) -> None:
    import tempfile
    import urllib.parse
    store = Store()
    if args.grok_cmd == "new":
        workspace = Path(args.workspace).expanduser()
        cwd = str(workspace if workspace.exists() else Path.home())

        grok_home = Path.home() / ".grok"
        grok_bin = grok_home / "bin" / "grok"
        encoded_cwd = urllib.parse.quote(cwd, safe="")
        sessions_dir = grok_home / "sessions" / encoded_cwd

        existing: set[str] = set()
        if sessions_dir.exists():
            existing = {d.name for d in sessions_dir.iterdir() if d.is_dir() and len(d.name) > 10}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(args.goal)
            prompt_path = f.name

        try:
            subprocess.run(
                [str(grok_bin), "--cwd", cwd, "--no-alt-screen",
                 "--output-format", "plain", "--permission-mode", "bypassPermissions",
                 "--max-turns", str(args.grok_message_ceiling),
                 "--prompt-file", prompt_path],
                timeout=300,
            )
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            pass
        finally:
            Path(prompt_path).unlink(missing_ok=True)

        new_session_id = None
        if sessions_dir.exists():
            new_dirs = [d for d in sessions_dir.iterdir()
                        if d.is_dir() and d.name not in existing and len(d.name) > 10]
            if new_dirs:
                newest = max(new_dirs, key=lambda d: d.stat().st_mtime)
                new_session_id = newest.name

        if not new_session_id:
            print("Warning: could not detect new Grok session — check ~/.grok/sessions/")
            return

        chat_id = f"grok:grok.sqlite:{new_session_id}"
        Scheduler(store).force_discover()

        print(f"chat_id: {chat_id}")
        print(f"workspace: {cwd}")
        print(f"goal: {args.goal}")

        if getattr(args, "queue", False):
            row = store.find_chat(new_session_id)
            position = float(args.position) if getattr(args, "position", None) is not None else None
            if row:
                store.queue_add(row["id"], position)
                store.set_goal(row["id"], args.goal, "user")
                pos = store.row("select position from queue where chat_id=?", (row["id"],))
                print(f"Added to queue at position {pos['position'] if pos else '?'}")
            else:
                print(f"Warning: use: autocode queue add {new_session_id} --goal ...")
    elif args.grok_cmd == "chats":
        from .providers.grok import GrokProvider
        chats = GrokProvider().discover()[: args.limit]
        if not chats:
            print("No Grok chats discovered.")
            return
        for chat in chats:
            print(f"- last_used={rel_time(chat.updated_at)} updated_at={chat.updated_at} {chat.id} {chat.alias}")
            print(f"  cwd: {chat.cwd or '(unknown)'}")
            print(f"  title: {compact(chat.title or chat.latest_text, 180)}")


def cmd_antigravity(args: argparse.Namespace) -> None:
    from .providers.antigravity import AntigravityProvider
    provider = AntigravityProvider()
    if args.antigravity_cmd == "status":
        print(f"agentapi: {'ready' if provider._agentapi_ready() else 'not ready'}")
        print(f"agentapi path: {provider.agentapi}")
        if not provider._agentapi_ready():
            print("same-chat send requires Antigravity running with ANTIGRAVITY_LS_ADDRESS in this environment")
        print(f"discovered chats: {len(provider.discover())}")
    elif args.antigravity_cmd == "chats":
        chats = provider.discover()[: args.limit]
        if not chats:
            print("No Antigravity chats discovered.")
            return
        for chat in chats:
            mode = "same-chat" if chat.metadata.get("agentapi_ready") else "codex-takeover"
            print(f"- last_used={rel_time(chat.updated_at)} updated_at={chat.updated_at} {chat.id} {chat.alias} [{mode}]")
            print(f"  title: {compact(chat.title or chat.latest_text, 180)}")
    elif args.antigravity_cmd == "new":
        if not provider._agentapi_ready():
            raise SystemExit(
                "Antigravity agentapi is not reachable. Open Antigravity and run this from an environment "
                "with ANTIGRAVITY_LS_ADDRESS, or drive an existing discovered Antigravity chat for Codex takeover."
            )
        raise SystemExit("Antigravity agentapi new-conversation command is not exposed; use an existing Antigravity chat id.")


def cursor_models() -> set[str]:
    from .providers.cursor import CursorProvider
    cursor_env = os.environ.copy()
    cursor_env.update(CursorProvider().cursor_env())
    res = subprocess.run(["cursor-agent", "models"], text=True, capture_output=True, timeout=20, env=cursor_env)
    text = res.stdout or res.stderr
    models: set[str] = set()
    for line in text.splitlines():
        if " - " in line:
            models.add(line.split(" - ", 1)[0].strip())
    return models


def cmd_drive(args: argparse.Namespace) -> None:
    store = Store()
    Scheduler(store).force_discover()
    row = store.find_chat(args.query)
    if not row:
        raise SystemExit(f"No chat matched: {args.query}")
    store.set_goal(row["id"], args.goal, "user")
    store.queue_add(row["id"])
    row = store.row("select * from chats where id=?", (row["id"],))
    job_id = None
    if args.no_start:
        print(f"Goal attached and queued: {row['alias']}")
        return
    sched = Scheduler(store)
    if not sched.has_active_job(row["id"]) and not sched.has_active_lease(row):
        old_model = store.get_config("cursor_model", "auto")
        if args.model and row["provider"] == "cursor":
            store.set_config("cursor_model", args.model)
        try:
            job_id = sched.dispatch(row)
        finally:
            if args.model and row["provider"] == "cursor":
                store.set_config("cursor_model", old_model)
    print(f"Driving {row['alias']}")
    print(f"goal: {args.goal}")
    if args.model and row["provider"] == "cursor":
        print(f"model: {args.model}")
    print(f"job: {job_id or 'queued'}")


def cmd_yolo(args: argparse.Namespace) -> None:
    store = Store()
    store.set_config("yolo", args.state)
    print(f"yolo={args.state}")


def cmd_pause(args: argparse.Namespace) -> None:
    store = Store()
    row = store.find_chat(args.query)
    if not row:
        raise SystemExit(f"No chat matched: {args.query}")
    killed = Scheduler(store).runner.kill_chat_jobs(row["id"], "chat_paused")
    store.pause_chat(row["id"])
    print(f"Paused {row['alias']}; killed {killed} running job(s)")


def cmd_coord(args: argparse.Namespace) -> None:
    from . import coordination

    store = Store()
    sched = Scheduler(store)
    if args.coord_cmd == "l1-status":
        info = coordination.read_l1_lock()
        active = coordination.l1_lock_active()
        if getattr(args, "json", False):
            print(json.dumps({"active": active, "lock": info}, indent=2, sort_keys=True))
            return
        if active and info:
            print(f"L1 lock ACTIVE pid={info.get('pid')} holder={info.get('holder')} run_dir={info.get('run_dir')}")
        else:
            print("L1 lock: none")
    elif args.coord_cmd == "pause-l1-competitors":
        from . import goal_fleets

        paused, killed = goal_fleets.pause_l1_competitors_no_lock(store, sched)
        sim_killed = goal_fleets.kill_simulator_l1_runs()
        print(
            f"Paused {paused} competing chat(s); killed {killed} job(s); "
            f"sim_killed={len(sim_killed)}"
        )
    elif args.coord_cmd == "kill-physical-l1":
        from . import goal_fleets

        killed = goal_fleets.kill_physical_l1_runs()
        coordination.release_l1_lock()
        print(f"killed_physical_pids={killed}")
    elif args.coord_cmd == "release-l1":
        coordination.release_l1_lock()
        print("L1 lock released")
    elif args.coord_cmd == "set-windows-sequential":
        with store.connect() as con:
            con.execute("update remote_workers set weight_capacity=1.0 where id='windows-main'")
        print("windows-main weight_capacity=1.0 (one remote job at a time)")


def cmd_done(args: argparse.Namespace) -> None:
    store = Store()
    row = store.find_chat(args.query)
    if not row:
        raise SystemExit(f"No chat matched: {args.query}")
    store.done_chat(row["id"])
    print(f"Done {row['alias']}")


def cmd_depend(args: argparse.Namespace) -> None:
    store = Store()
    chat = store.find_chat(args.query)
    if not chat:
        raise SystemExit(f"No chat matched: {args.query}")
    if args.depend_cmd == "list":
        deps = store.get_dependencies(chat["id"])
        if not deps:
            print(f"{chat['alias']} has no dependencies.")
        else:
            print(f"Dependencies of {chat['alias']}:")
            for d in deps:
                state = "done" if d["done"] else d["state"]
                print(f"  [{state}] {d['depends_on'][:8]}  {d['title'] or '(no title)'}")
    elif args.depend_cmd == "add":
        on_chat = store.find_chat(args.on)
        if not on_chat:
            raise SystemExit(f"No chat matched for --on: {args.on}")
        if store.add_dependency(chat["id"], on_chat["id"]):
            print(f"Added: {chat['alias']} depends on {on_chat['alias']}")
        else:
            print("Dependency already exists or self-reference rejected.")
    elif args.depend_cmd == "remove":
        on_chat = store.find_chat(args.on)
        if not on_chat:
            raise SystemExit(f"No chat matched for --on: {args.on}")
        store.remove_dependency(chat["id"], on_chat["id"])
        print(f"Removed dependency: {chat['alias']} no longer waits on {on_chat['alias']}")


def cmd_logs(args: argparse.Namespace) -> None:
    if not LOG.exists():
        print("No log yet.")
        return
    print("\n".join(LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-args.lines:]))


def cmd_doctor(args: argparse.Namespace) -> None:
    ensure_dirs()
    store = Store()
    daemon_ok, daemon_text = launchd_status()
    checks = [
        ("root", ROOT.exists()),
        ("db", DB.exists()),
        ("daemon", daemon_ok),
        ("grok", command_exists("grok")),
        ("codex", command_exists("codex")),
        ("claude", command_exists("claude")),
        ("cursor", command_exists("cursor")),
        ("yolo", store.get_config("yolo") == "on"),
    ]
    ok = all(v for _, v in checks if _ not in {"daemon"})
    print(f"AutoCode doctor: {'ok' if ok else 'needs attention'}")
    for name, value in checks:
        print(f"- {'ok' if value else 'missing'} {name}")
    print(f"- db {DB}")
    if daemon_ok:
        print("- launchd loaded")
    else:
        print("- launchd not loaded; run `autocode daemon install` then `autocode daemon start`")

    if getattr(args, "auto_fix", False):
        from . import goals
        from . import remediation
        from .fleet_report import needs_luke_summary

        Scheduler(store).runner.refresh()
        archived = goals.reconcile_done_still_in_queue(store)
        archived.extend(store.queue_archive_done())
        result = remediation.remediation_pass(store)
        result["queue_archived"] = archived
        print("- auto-fix pass:")
        for key in ("overdelivery_completed", "queue_archived", "remediated", "decomposed"):
            items = result.get(key) or []
            if items:
                print(f"  {key}: {len(items)}")
                for chat_id in items[:5]:
                    row = store.row("select alias from chats where id=?", (chat_id,))
                    print(f"    - {row['alias'] if row else chat_id}")
        needs = needs_luke_summary(store)
        print(f"- needs Luke after auto-fix: {needs}")


def cmd_discover(args: argparse.Namespace) -> None:
    stats = Scheduler(Store()).force_discover()
    print(json.dumps(stats, indent=2, sort_keys=True))


def cmd_tick(args: argparse.Namespace) -> None:
    result = Scheduler(Store()).tick(dispatch=not args.dry_run, max_projects=args.max_projects)
    if not args.dry_run:
        grok_watchdog.request("tick_cli")
    print(json.dumps(result, indent=2, sort_keys=True))


def cmd_queue(args: argparse.Namespace) -> None:
    store = Store()
    if args.queue_cmd == "list":
        rows = store.queue_list()
        stale = store.rows(
            """
            select q.position, q.chat_id, c.alias, c.title, c.provider, c.state, c.updated_at, c.objective
            from queue q join chats c on c.id=q.chat_id
            where c.done=1
            order by q.position asc
            """
        )
        if not rows and not stale:
            print("Queue is empty.")
            print("Add with: autocode queue add <chat-query> [--position N]")
            return
        active = store.rows("select chat_id from jobs where status='running'")
        running_ids = {r["chat_id"] for r in active}
        show_deps = getattr(args, "deps", False)
        # Pre-fetch all dependency info when --deps is set
        dep_map: dict = {}
        if show_deps:
            for r in rows:
                deps = store.get_dependencies(r["chat_id"])
                dep_map[r["chat_id"]] = deps
        print(f"Active queue ({len(rows)} items):")
        for r in rows:
            pos = r["position"]
            pos_str = f"#{int(pos)}" if pos == int(pos) else f"#{pos:.1f}"
            state = r["state"]
            is_running = r["chat_id"] in running_ids
            flag = " [RUNNING]" if is_running else ""
            title = r["alias"] or r["title"] or r["chat_id"]
            # Show [BLOCKED] if any unfinished dep exists
            blocking = []
            if show_deps and r["chat_id"] in dep_map:
                blocking = [d for d in dep_map[r["chat_id"]] if not d["done"]]
            blocked_flag = " [BLOCKED]" if blocking else ""
            print(f"  {pos_str:<6} {rel_time(r['updated_at']):>5}  {r['provider']:<11} {state:<13} {short(title)}{flag}{blocked_flag}")
            if r["objective"]:
                print(f"         goal: {compact(r['objective'], 160)}")
            if show_deps and r["chat_id"] in dep_map:
                all_deps = dep_map[r["chat_id"]]
                for d in all_deps:
                    dep_state = "done" if d["done"] else d["state"]
                    dep_title = short(d["title"] or d["depends_on"][:8])
                    dep_mark = "✓" if d["done"] else "⏳"
                    print(f"         {dep_mark} depends on: {dep_title} [{dep_state}]")
        if stale:
            print(f"\nDone but still in queue ({len(stale)} — run: autocode queue archive --all-done):")
            for r in stale:
                title = r["alias"] or r["title"] or r["chat_id"]
                print(f"  - {r['provider']:<11} {short(title)}")
    elif args.queue_cmd == "finished":
        rows = store.queue_finished_list(args.limit)
        if not rows:
            print("Finished pile is empty.")
            return
        print(f"Finished ({len(rows)} items):")
        for r in rows:
            title = r["alias"] or r["chat_id"]
            print(f"  {rel_time(r['archived_at']):>5}  {r['provider']:<11} {short(title)}  ({r['reason']})")
            if r["objective"]:
                print(f"         goal: {compact(r['objective'], 160)}")
    elif args.queue_cmd == "archive":
        if args.all_done:
            archived = store.queue_archive_done()
            if not archived:
                print("No done chats left in the active queue.")
                return
            print(f"Archived {len(archived)} done chat(s) to the finished pile.")
            return
        row = store.find_chat(args.query)
        if not row:
            raise SystemExit(f"No chat matched: {args.query}")
        if store.queue_archive(row["id"], reason=args.reason):
            print(f"Archived to finished pile: {row['alias']}")
        else:
            print(f"Not in active queue: {row['alias']}")
    elif args.queue_cmd == "add":
        Scheduler(store).force_discover()
        row = store.find_chat(args.query)
        if not row:
            raise SystemExit(f"No chat matched: {args.query}")
        store.queue_add(row["id"], float(args.position) if args.position is not None else None)
        if args.goal:
            store.set_goal(row["id"], args.goal, "user")
        pos = store.row("select position from queue where chat_id=?", (row["id"],))
        print(f"Added to queue: {row['alias']}")
        print(f"position: {pos['position'] if pos else '?'}")
        if args.goal:
            print(f"goal: {args.goal}")
    elif args.queue_cmd == "remove":
        row = store.find_chat(args.query)
        if not row:
            raise SystemExit(f"No chat matched: {args.query}")
        removed = store.queue_remove(row["id"])
        print(f"{'Removed from queue' if removed else 'Not in queue'}: {row['alias']}")
    elif args.queue_cmd == "move":
        row = store.find_chat(args.query)
        if not row:
            raise SystemExit(f"No chat matched: {args.query}")
        moved = store.queue_move(row["id"], float(args.position))
        if moved:
            print(f"Moved {row['alias']} to position {args.position}")
        else:
            print(f"Not in queue: {row['alias']}")


def cmd_audit(args: argparse.Namespace) -> None:
    from .audit import replay_summary
    from .config import AUDIT_LOG
    if not AUDIT_LOG.exists():
        print("No audit log yet.")
        return
    if args.replay:
        print(json.dumps(replay_summary(AUDIT_LOG), indent=2, sort_keys=True))
        return
    lines = AUDIT_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-args.limit:]
    for line in lines:
        if args.raw:
            print(line)
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            print(line)
            continue
        print(f"{event.get('ts')} {event.get('kind')} chat={short(str(event.get('chat_id') or ''), 40)} job={event.get('job_id') or ''}")


def cmd_plugins(args: argparse.Namespace) -> None:
    from .plugins import list_plugins, scaffold_plugin, validate_plugin
    if args.plugins_cmd == "list":
        plugins = list_plugins()
        if not plugins:
            print("No plugins installed.")
            return
        for plugin in plugins:
            errors = validate_plugin(plugin)
            suffix = f" errors={','.join(errors)}" if errors else ""
            print(f"- {plugin.get('id')} {plugin.get('version','')} {plugin.get('path','')}{suffix}")
    elif args.plugins_cmd == "create":
        path = scaffold_plugin(args.id)
        print(f"created plugin scaffold: {path}")


def cmd_reactions(args: argparse.Namespace) -> None:
    from .reactions import evaluate_reactions
    print(json.dumps(evaluate_reactions(Store()), indent=2, sort_keys=True))


def cmd_ledger(args: argparse.Namespace) -> None:
    store = Store()
    rows = store.rows(
        """
        select provider,count(*) jobs,sum(token_input) input_tokens,
          sum(token_output) output_tokens,sum(cost_estimate) cost_usd
        from jobs
        group by provider
        order by cost_usd desc, jobs desc, provider
        """
    )
    total_cost = 0.0
    print("provider     jobs  input_tokens  output_tokens  cost_usd")
    for row in rows:
        cost = float(row["cost_usd"] or 0)
        total_cost += cost
        print(f"{row['provider']:<12} {row['jobs']:>4} {int(row['input_tokens'] or 0):>13} {int(row['output_tokens'] or 0):>14} {cost:>8.4f}")
    print(f"total_cost_usd={total_cost:.4f}")


def cmd_workflow(args: argparse.Namespace) -> None:
    from .workflows import apply_workflow, load_workflow
    workflow = load_workflow(args.path)
    if args.apply:
        created = apply_workflow(Store(), workflow)
        print(json.dumps({"created_priorities": created}, indent=2, sort_keys=True))
    else:
        print(json.dumps(workflow, indent=2, sort_keys=True))


def cmd_web(args: argparse.Namespace) -> None:
    from .web import run_web
    run_web(args.host, args.port)


def _shutdown_job_action(store: Store, reason: str) -> tuple[str, int]:
    preserve = store.get_config("preserve_jobs_on_shutdown", "on" if DEFAULT_PRESERVE_JOBS_ON_SHUTDOWN else "off").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    runner = Scheduler(store).runner
    if preserve:
        count = runner.detach_all(reason)
        return "detached", count
    count = runner.kill_all(reason)
    return "killed", count


def cmd_daemon(args: argparse.Namespace) -> None:
    if args.daemon_cmd == "run":
        Daemon(args.interval).run()
    elif args.daemon_cmd == "install":
        launchd_install()
        print("installed launchd plist")
    elif args.daemon_cmd == "start":
        code, out, err = launchd_start()
        print((out + err).strip() or f"started ({code})")
    elif args.daemon_cmd == "stop":
        action, count = _shutdown_job_action(Store(), "daemon_stop")
        code, out, err = launchd_stop()
        print((out + err).strip() or f"stopped ({code})")
        if count:
            print(f"{action} {count} running job(s)")
    elif args.daemon_cmd == "restart":
        action, count = _shutdown_job_action(Store(), "daemon_restart")
        launchd_stop()
        code, out, err = launchd_start()
        print((out + err).strip() or f"restarted ({code})")
        if count:
            print(f"{action} {count} running job(s)")
    elif args.daemon_cmd == "status":
        ok, text = launchd_status()
        print("loaded" if ok else "not loaded")
        if args.verbose:
            print(text[:4000])


def cmd_dashboard(args: argparse.Namespace) -> None:
    run_dashboard(
        interval=args.interval,
        limit=args.limit,
        once=args.once,
        alt_screen=args.alt_screen,
        append_history=args.append_history,
    )


def cmd_preprint_kit(args: argparse.Namespace) -> None:
    paths = write_kit(
        Path(args.output).expanduser(),
        title=args.title,
        pseudonym=args.pseudonym,
        technique=args.technique,
        pdf=Path(args.pdf).expanduser() if args.pdf else None,
    )
    print(f"preprint kit: {Path(args.output).expanduser()}")
    for key, value in paths.items():
        if key.endswith("_result"):
            continue
        print(f"{key}: {value}")
    audit = paths.get("pdf_audit_result")
    if audit:
        print(f"pdf audit: {'review-ok' if audit.ok_to_review else 'needs-review'}")
        for finding in audit.findings:
            print(f"- {finding}")


def cmd_ipc(args: argparse.Namespace) -> None:
    from . import self_improve
    store = Store()
    if getattr(args, "scan", False):
        result = self_improve.scan(store, force=True)
        print(json.dumps(result, indent=2))
        return
    print(self_improve.format_ipc_report(store))


def cmd_watchdog(args: argparse.Namespace) -> None:
    from . import watchdog_executor
    from .store import Store

    if args.watchdog_cmd == "list":
        actions = watchdog_executor._load_actions()
        status_filter = getattr(args, "status", None)
        if status_filter:
            actions = [a for a in actions if a.get("status") == status_filter]
        if not actions:
            print("no actions" + (f" with status={status_filter}" if status_filter else ""))
            return
        for a in actions:
            aid = a.get("id", "?")
            atype = a.get("type", "?")
            chat = a.get("chat_id", "")
            status = a.get("status", "?")
            conf = a.get("confidence", 1.0)
            needs = " [needs_luke]" if a.get("needs_luke") else ""
            reason = a.get("reject_reason", "")
            print(f"  {aid:<20} {atype:<22} {chat:<20} {status}{needs}  conf={conf:.2f}{'  '+reason if reason else ''}")

    elif args.watchdog_cmd == "apply":
        action_id = args.action_id
        actions = watchdog_executor._load_actions()
        target = next((a for a in actions if str(a.get("id") or "") == action_id), None)
        if not target:
            print(f"action {action_id!r} not found")
            return
        if target.get("status") == "applied":
            print(f"action {action_id} already applied")
            return
        store = Store()
        try:
            result = watchdog_executor._apply_action(store, target)
        except Exception as exc:
            print(f"error: {exc}")
            return
        if result:
            target["status"] = "applied"
            from .util import now_iso, now_ts
            target["applied_at"] = now_iso()
            target["applied_at_ts"] = now_ts()
            watchdog_executor._save_actions(actions)
            store.event("watchdog_action_applied_manual", action_id=action_id, action_type=target.get("type", ""))
            print(f"applied: {action_id} ({target.get('type')})")
        else:
            print(f"action returned False (no-op or precondition failed)")

    elif args.watchdog_cmd == "clear":
        actions = watchdog_executor._load_actions()
        before = len(actions)
        keep = [a for a in actions if a.get("status") != "rejected"]
        watchdog_executor._save_actions(keep)
        print(f"cleared {before - len(keep)} rejected actions ({len(keep)} remain)")


def _worker_refresh(store: Store) -> JobRunner:
    runner = JobRunner(store)
    runner.refresh()
    return runner


def _print_coord_human(coord: dict, sched: Scheduler, queued_count: int) -> None:
    print(
        f"Mac: {coord['mac_running_weight']:.1f}/{coord['mac_capacity']} used, "
        f"{coord['mac_available']:.1f} free, can_take_more={coord['mac_can_take_more']}"
    )
    print(
        f"Remote: {coord['remote_running_jobs']} job(s), weight {coord['remote_running_weight']:.1f}, "
        f"dispatch_budget={coord['remote_dispatch_budget']:.1f}"
    )
    for w in coord.get("workers") or []:
        flag = "on" if w["enabled"] else "off"
        providers = w.get("providers") or ""
        prov_tag = f" [{providers}]" if providers else ""
        print(
            f"  {w['id']}@{w['host']}{prov_tag} load={w['load']:.1f}/{w['capacity']:.1f} "
            f"headroom={w['headroom']:.1f} [{flag}]"
        )
    jobs = coord.get("running_jobs") or []
    if jobs:
        print("Running jobs:")
        for j in jobs[:10]:
            where = j["worker_id"] or "mac"
            print(f"  {j['id']} {where} {j['provider']} {j['evidence_status']} {j.get('alias') or ''}")
    print(f"queued_candidates={queued_count}")


def cmd_worker(args: argparse.Namespace) -> None:
    import subprocess as sp
    import uuid

    from . import remote_ssh

    store = Store()
    refresh_cmds = {"list", "coord", "jobs", "reap", "bench"}
    runner = _worker_refresh(store) if args.worker_cmd in refresh_cmds else JobRunner(store)

    if args.worker_cmd == "list":
        workers = store.rows("select * from remote_workers order by id asc")
        if not workers:
            print("No remote workers configured. Use `autocode worker add`.")
            return
        sched = Scheduler(store)
        for w in workers:
            used = sched._remote_worker_weight(str(w["id"]))
            cap = float(w["weight_capacity"] or 4.0)
            status = "enabled" if w["enabled"] else "disabled"
            shell = remote_ssh.worker_shell(w)
            job_count = store.row(
                "select count(*) c from jobs where status='running' and worker_id=?",
                (str(w["id"]),),
            )
            jobs_n = int(job_count["c"] if job_count else 0)
            seen = str(w["last_seen_at"] or "") or "never"
            providers = str(w["provider_types"] or "").replace(" ", "")
            print(
                f"{w['id']}  {w['ssh_user']}@{w['host']}  providers=[{providers}]  "
                f"load={used:.1f}/{cap:.1f} jobs={jobs_n} shell={shell} seen={seen} {status}"
            )
            if w["notes"]:
                print(f"  notes: {w['notes']}")

    elif args.worker_cmd == "add":
        store.init()
        with store.connect() as con:
            con.execute(
                """insert into remote_workers(id,host,ssh_user,provider_types,weight_capacity,default_cwd,ssh_key_path,enabled,notes,remote_shell)
                   values(?,?,?,?,?,?,?,1,?,?)
                   on conflict(id) do update set host=excluded.host,ssh_user=excluded.ssh_user,
                   provider_types=excluded.provider_types,weight_capacity=excluded.weight_capacity,
                   default_cwd=excluded.default_cwd,ssh_key_path=excluded.ssh_key_path,enabled=1,
                   notes=excluded.notes,remote_shell=excluded.remote_shell""",
                (args.id, args.host, args.user, args.providers, float(args.capacity),
                 args.cwd, args.key or "", args.notes or "", args.shell or ""),
            )
        print(f"added remote worker: {args.id} ({args.user}@{args.host})")

    elif args.worker_cmd == "remove":
        active = store.row(
            "select count(*) c from jobs where status='running' and worker_id=?",
            (args.id,),
        )
        if active and int(active["c"] or 0) > 0:
            print(f"refusing remove: {active['c']} running job(s) on {args.id}; run `autocode worker reap {args.id}` first")
            return
        with store.connect() as con:
            con.execute("delete from remote_workers where id=?", (args.id,))
        print(f"removed remote worker: {args.id}")

    elif args.worker_cmd == "enable":
        with store.connect() as con:
            con.execute("update remote_workers set enabled=? where id=?", (0 if args.disable else 1, args.id))
        print(f"{'disabled' if args.disable else 'enabled'}: {args.id}")

    elif args.worker_cmd == "ping":
        worker = store.row("select * from remote_workers where id=?", (args.id,))
        if not worker:
            print(f"no worker with id: {args.id}")
            return
        cmd = remote_ssh.build_ping_command(worker)
        result = sp.run(cmd, capture_output=True, text=True, timeout=15)
        remote_ssh.touch_worker_seen(store, args.id)
        print(result.stdout or "(no output)")
        if result.returncode != 0:
            print(f"STDERR: {result.stderr.strip()}")
            print(f"exit code: {result.returncode}")

    elif args.worker_cmd == "probe":
        worker = store.row("select * from remote_workers where id=?", (args.id,))
        if not worker:
            print(f"no worker with id: {args.id}")
            return
        worker = dict(worker)
        worker["id"] = args.id
        report = remote_ssh.probe_worker(worker)
        remote_ssh.touch_worker_seen(store, args.id)
        if getattr(args, "json", False):
            print(json.dumps(report, indent=2, sort_keys=True))
            return
        res = report.get("resources") or {}
        prov = report.get("providers") or {}
        print(f"{args.id}@{worker['host']}")
        print(f"  RAM: {res.get('ram_gb', '?')} GB ({res.get('ram_bytes', 0)} bytes)")
        print(f"  CPU cores: {res.get('cpu_cores', '?')}")
        print(f"  grok: {prov.get('grok', 'unknown')}")
        print(f"  cursor-agent: {prov.get('cursor_agent', 'unknown')}")
        suggested = float(report.get("suggested_capacity") or 4.0)
        current = float(worker.get("weight_capacity") or 4.0)
        print(f"  suggested capacity: {suggested:.1f} (configured: {current:.1f})")
        if getattr(args, "apply_capacity", False):
            with store.connect() as con:
                con.execute("update remote_workers set weight_capacity=? where id=?", (suggested, args.id))
            print(f"  applied capacity={suggested:.1f}")

    elif args.worker_cmd == "set-capacity":
        with store.connect() as con:
            con.execute("update remote_workers set weight_capacity=? where id=?", (float(args.capacity), args.id))
        print(f"set {args.id} capacity={float(args.capacity):.1f}")

    elif args.worker_cmd == "smoke":
        worker = store.row("select * from remote_workers where id=?", (args.id,))
        if not worker:
            print(f"no worker with id: {args.id}")
            return
        job_id = f"smoke-{uuid.uuid4().hex[:8]}"
        prompt = "autocode remote worker smoke test"
        print(f"smoke job_id={job_id}")
        mkdir = remote_ssh.ensure_remote_job_dir(worker, job_id)
        if mkdir.returncode != 0:
            print(f"mkdir failed: {(mkdir.stderr or mkdir.stdout).strip()}")
            return
        local_prompt = store.path.parent / "jobs" / job_id / "prompt.txt"
        local_prompt.parent.mkdir(parents=True, exist_ok=True)
        local_prompt.write_text(prompt, encoding="utf-8")
        copied = remote_ssh.scp_prompt_file(worker, str(local_prompt), job_id)
        if copied.returncode != 0:
            print(f"scp failed: {(copied.stderr or copied.stdout).strip()}")
            return
        cmd = remote_ssh.build_smoke_command(worker, job_id)
        result = sp.run(cmd, capture_output=True, text=True, timeout=30)
        print(result.stdout or "(no output)")
        if result.returncode != 0:
            print(f"STDERR: {result.stderr.strip()}")
            print(f"exit code: {result.returncode}")
            return
        grok_cmd = remote_ssh.build_remote_exec_command(
            worker,
            worker["default_cwd"] or "~",
            ["grok", "--version"],
            job_id,
        )
        print("remote exec preview:", " ".join(grok_cmd[-4:]))
        remote_ssh.touch_worker_seen(store, args.id)
        print("smoke ok")

    elif args.worker_cmd == "bench":
        worker = store.row("select * from remote_workers where id=?", (args.id,))
        if not worker:
            print(f"no worker with id: {args.id}")
            return
        print(f"benchmarking {args.id} ({worker['ssh_user']}@{worker['host']})...")
        result = remote_ssh.bench_remote_worker(worker)
        remote_ssh.touch_worker_seen(store, args.id)
        print(json.dumps(result, indent=2, sort_keys=True))

    elif args.worker_cmd == "jobs":
        worker_filter = getattr(args, "id", "") or ""
        query = (
            "select j.*, c.alias from jobs j left join chats c on c.id=j.chat_id "
            "where j.status='running' and coalesce(j.worker_id,'')!=''"
        )
        params: tuple = ()
        if worker_filter:
            query += " and j.worker_id=?"
            params = (worker_filter,)
        query += " order by j.created_at asc"
        rows = store.rows(query, params)
        if not rows:
            print("no running remote jobs")
            return
        for row in rows:
            print(
                f"{row['id']}  worker={row['worker_id']}  {row['provider']}  "
                f"pid={row['pid']}  {row['evidence_status']}  {row['alias'] or row['chat_id']}"
            )

    elif args.worker_cmd == "reap":
        worker_filter = getattr(args, "id", "") or ""
        if worker_filter:
            jobs = store.rows(
                "select * from jobs where status='running' and worker_id=?",
                (worker_filter,),
            )
            reaped = []
            for job in jobs:
                if not runner._pid_running(int(job["pid"] or 0)):
                    if not args.dry_run:
                        runner._refresh_one(job)
                    reaped.append(str(job["id"]))
        else:
            reaped = runner.reap_stale_remote_jobs(dry_run=args.dry_run)
        verb = "would reap" if args.dry_run else "reaped"
        print(f"{verb} {len(reaped)} remote job(s)")
        for job_id in reaped:
            print(f"  {job_id}")

    elif args.worker_cmd == "coord":
        sched = Scheduler(store)
        coord = sched.coordination_snapshot()
        queued = sched.candidates(5)
        if getattr(args, "json", False):
            print(json.dumps(coord, indent=2, sort_keys=True))
        else:
            _print_coord_human(coord, sched, len(queued))
        if queued and not coord["mac_can_take_more"]:
            row = queued[0]
            worker = sched._pick_remote_worker(str(row["provider"] or ""), sched._job_weight(row))
            if worker:
                print(
                    f"next_remote_target={worker['id']} for {row['alias'] or row['id']} "
                    f"provider={row['provider']} weight={sched._job_weight(row):.1f}"
                )
            else:
                print("next_remote_target=none (no worker headroom or unsupported provider)")
        elif queued:
            print("next_local_dispatch=yes (mac has capacity)")
        else:
            print("queue_empty")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autocode")
    p.add_argument("--version", action="version", version="autocode 0.1.0")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status"); s.add_argument("--limit", type=int, default=10); s.set_defaults(func=cmd_status)
    n = sub.add_parser("now"); n.add_argument("--limit", type=int, default=10); n.set_defaults(func=cmd_now)
    dash = sub.add_parser("dashboard")
    dash.add_argument("--interval", type=float, default=2.0, help="refresh interval in seconds")
    dash.add_argument("--limit", type=int, default=12, help="rows per dashboard section")
    dash.add_argument("--once", action="store_true", help="render one snapshot and exit")
    dash.add_argument("--append-history", action="store_true", help="append every refresh frame to terminal scrollback")
    dash.add_argument("--alt-screen", action="store_true", help="use the terminal alternate screen")
    dash.add_argument("--no-alt-screen", action="store_true", help=argparse.SUPPRESS)
    dash.set_defaults(func=cmd_dashboard)
    pk = sub.add_parser("preprint-kit", help="create an anonymous preprint release checklist and metadata kit")
    pk.add_argument("--output", default=str(ROOT / "state" / "preprint-anonymity-kit"))
    pk.add_argument("--title", default="Untitled anonymized preprint")
    pk.add_argument("--pseudonym", default="SN Org")
    pk.add_argument("--technique", default="Trace elemental analysis")
    pk.add_argument("--pdf", default="", help="optional PDF path to scan for common identity leaks")
    pk.set_defaults(func=cmd_preprint_kit)
    last = sub.add_parser("last"); last.add_argument("query"); last.set_defaults(func=cmd_last)
    g = sub.add_parser("goals"); g.add_argument("--limit", type=int, default=20); g.set_defaults(func=cmd_goals)
    pr = sub.add_parser("priority")
    prsub = pr.add_subparsers(dest="priority_cmd", required=True)
    prl = prsub.add_parser("list"); prl.add_argument("--limit", type=int, default=20); prl.set_defaults(func=cmd_priority)
    pra = prsub.add_parser("add"); pra.add_argument("query"); pra.add_argument("--goal", required=True); pra.add_argument("--rank", type=int, default=100); pra.add_argument("--path", default=""); pra.add_argument("--chat-id", default=""); pra.add_argument("--exact", action="store_true"); pra.add_argument("--lanes", type=int, default=1); pra.set_defaults(func=cmd_priority)
    prr = prsub.add_parser("remove"); prr.add_argument("query"); prr.set_defaults(func=cmd_priority)
    c = sub.add_parser("chats"); c.add_argument("--recent", default="30d"); c.add_argument("--limit", type=int, default=50); c.set_defaults(func=cmd_chats)
    cu = sub.add_parser("cursor")
    cusub = cu.add_subparsers(dest="cursor_cmd", required=True)
    cust = cusub.add_parser("status"); cust.set_defaults(func=cmd_cursor)
    cuch = cusub.add_parser("chats"); cuch.add_argument("--limit", type=int, default=30); cuch.add_argument("--source", choices=["cursor.cli", "cursor.transcript", "cursor.ide", "cursor.cloud"], default=""); cuch.set_defaults(func=cmd_cursor)
    cuhi = cusub.add_parser("history"); cuhi.add_argument("query"); cuhi.add_argument("--chars", type=int, default=3000); cuhi.set_defaults(func=cmd_cursor)
    cumo = cusub.add_parser("model"); cumo.add_argument("model", nargs="?"); cumo.set_defaults(func=cmd_cursor)
    cumos = cusub.add_parser("models"); cumos.set_defaults(func=cmd_cursor)
    cune = cusub.add_parser("new"); cune.add_argument("--workspace", default=str(Path.home())); cune.add_argument("--goal", required=True); cune.add_argument("--model", default=""); cune.set_defaults(func=cmd_cursor)
    gr = sub.add_parser("grok")
    grsub = gr.add_subparsers(dest="grok_cmd", required=True)
    grnew = grsub.add_parser("new"); grnew.add_argument("--workspace", default=str(Path.home())); grnew.add_argument("--goal", required=True); grnew.add_argument("--grok-message-ceiling", type=int, default=60, help="Internal Grok CLI message ceiling; AutoCode turns are interventions, not provider messages."); grnew.add_argument("--queue", action="store_true", help="Add to autocode queue after creation"); grnew.add_argument("--position", type=float, default=None, help="Queue position (lower = higher priority)"); grnew.set_defaults(func=cmd_grok)
    grch = grsub.add_parser("chats"); grch.add_argument("--limit", type=int, default=30); grch.set_defaults(func=cmd_grok)
    ag = sub.add_parser("antigravity")
    agsub = ag.add_subparsers(dest="antigravity_cmd", required=True)
    agst = agsub.add_parser("status"); agst.set_defaults(func=cmd_antigravity)
    agch = agsub.add_parser("chats"); agch.add_argument("--limit", type=int, default=30); agch.set_defaults(func=cmd_antigravity)
    agnew = agsub.add_parser("new"); agnew.add_argument("--goal", required=True); agnew.set_defaults(func=cmd_antigravity)
    d = sub.add_parser("drive"); d.add_argument("query"); d.add_argument("--goal", required=True); d.add_argument("--no-start", action="store_true"); d.add_argument("--model", default="", help="Cursor-only per-send model override, e.g. auto or composer-2.5"); d.set_defaults(func=cmd_drive)
    sq = sub.add_parser("squad")
    sqsub = sq.add_subparsers(dest="squad_cmd", required=True)
    sqp = sqsub.add_parser("plan"); sqp.add_argument("query"); sqp.set_defaults(func=cmd_squad)
    sql = sqsub.add_parser("launch"); sql.add_argument("query"); sql.add_argument("--limit", type=int, default=0, help="maximum lanes to launch; 0 means use current resource headroom"); sql.add_argument("--mode", choices=["read_only", "worktree", "all"], default="read_only"); sql.add_argument("--dry-run", action="store_true"); sql.add_argument("--force", action="store_true"); sql.add_argument("--sequential", action="store_true", help="record lane launch order as a sequential hint event"); sql.set_defaults(func=cmd_squad)
    sqc = sqsub.add_parser("collect"); sqc.add_argument("query"); sqc.add_argument("--limit", type=int, default=8); sqc.add_argument("--send-writer", action="store_true"); sqc.set_defaults(func=cmd_squad)
    y = sub.add_parser("yolo"); y.add_argument("state", choices=["on", "off"]); y.set_defaults(func=cmd_yolo)
    pa = sub.add_parser("pause"); pa.add_argument("query"); pa.set_defaults(func=cmd_pause)

    co = sub.add_parser("coord", help="L1 E2E mutex and fleet coordination")
    cosub = co.add_subparsers(dest="coord_cmd", required=True)
    col = cosub.add_parser("l1-status", help="show L1 exclusive lock state")
    col.add_argument("--json", action="store_true")
    col.set_defaults(func=cmd_coord)
    cop = cosub.add_parser("pause-l1-competitors", help="pause liquid/patreon/l1 Mac jobs during Detox")
    cop.set_defaults(func=cmd_coord)
    cor = cosub.add_parser("release-l1", help="release L1 E2E lock file")
    cor.set_defaults(func=cmd_coord)
    cok = cosub.add_parser("kill-physical-l1", help="kill physical iPhone L1 orchestrators and release lock")
    cok.set_defaults(func=cmd_coord)
    cow = cosub.add_parser("set-windows-sequential", help="set windows-main capacity=1")
    cow.set_defaults(func=cmd_coord)
    dn = sub.add_parser("done"); dn.add_argument("query"); dn.set_defaults(func=cmd_done)
    dep = sub.add_parser("depend")
    dep.add_argument("query", help="chat to inspect or modify dependencies for")
    depsub = dep.add_subparsers(dest="depend_cmd", required=True)
    depl = depsub.add_parser("list"); depl.set_defaults(func=cmd_depend)
    depa = depsub.add_parser("add"); depa.add_argument("--on", required=True, help="chat that must be done first"); depa.set_defaults(func=cmd_depend)
    depr = depsub.add_parser("remove"); depr.add_argument("--on", required=True); depr.set_defaults(func=cmd_depend)
    l = sub.add_parser("logs"); l.add_argument("--lines", type=int, default=80); l.set_defaults(func=cmd_logs)
    doc = sub.add_parser("doctor")
    doc.add_argument("--auto-fix", action="store_true", help="run remediation pass on live DB")
    doc.set_defaults(func=cmd_doctor)
    disc = sub.add_parser("discover"); disc.set_defaults(func=cmd_discover)
    tick = sub.add_parser("tick"); tick.add_argument("--dry-run", action="store_true"); tick.add_argument("--max-projects", type=int, default=None); tick.set_defaults(func=cmd_tick)
    q = sub.add_parser("queue")
    qsub = q.add_subparsers(dest="queue_cmd", required=True)
    ql = qsub.add_parser("list"); ql.add_argument("--deps", action="store_true", help="show dependency tree for each queued chat"); ql.set_defaults(func=cmd_queue)
    qf = qsub.add_parser("finished"); qf.add_argument("--limit", type=int, default=30); qf.set_defaults(func=cmd_queue)
    qarch = qsub.add_parser("archive"); qarch.add_argument("query", nargs="?", default=""); qarch.add_argument("--all-done", action="store_true", help="archive every done chat still in the active queue"); qarch.add_argument("--reason", default="done"); qarch.set_defaults(func=cmd_queue)
    qa = qsub.add_parser("add"); qa.add_argument("query"); qa.add_argument("--position", type=float, default=None, help="queue position (float; lower = higher priority)"); qa.add_argument("--goal", default="", help="optional goal to attach"); qa.set_defaults(func=cmd_queue)
    qr = qsub.add_parser("remove"); qr.add_argument("query"); qr.set_defaults(func=cmd_queue)
    qm = qsub.add_parser("move"); qm.add_argument("query"); qm.add_argument("position", type=float); qm.set_defaults(func=cmd_queue)
    au = sub.add_parser("audit"); au.add_argument("--limit", type=int, default=40); au.add_argument("--raw", action="store_true"); au.add_argument("--replay", action="store_true"); au.set_defaults(func=cmd_audit)
    plug = sub.add_parser("plugins")
    plugsub = plug.add_subparsers(dest="plugins_cmd", required=True)
    plugl = plugsub.add_parser("list"); plugl.set_defaults(func=cmd_plugins)
    plugc = plugsub.add_parser("create"); plugc.add_argument("id"); plugc.set_defaults(func=cmd_plugins)
    react = sub.add_parser("reactions"); react.set_defaults(func=cmd_reactions)
    ledger = sub.add_parser("ledger"); ledger.set_defaults(func=cmd_ledger)
    wf = sub.add_parser("workflow"); wf.add_argument("path"); wf.add_argument("--apply", action="store_true"); wf.set_defaults(func=cmd_workflow)
    web = sub.add_parser("web"); web.add_argument("--host", default="127.0.0.1"); web.add_argument("--port", type=int, default=8765); web.set_defaults(func=cmd_web)
    dm = sub.add_parser("daemon")
    dsub = dm.add_subparsers(dest="daemon_cmd", required=True)
    run = dsub.add_parser("run"); run.add_argument("--interval", type=int, default=2); run.set_defaults(func=cmd_daemon)
    for name in ("install", "start", "stop", "restart"):
        x = dsub.add_parser(name); x.set_defaults(func=cmd_daemon)
    st = dsub.add_parser("status"); st.add_argument("--verbose", action="store_true"); st.set_defaults(func=cmd_daemon)
    ipc = sub.add_parser("ipc", help="show IPC (jobs-per-completion) metric and self-improvement status")
    ipc.add_argument("--scan", action="store_true", help="force a self-improvement scan now")
    ipc.set_defaults(func=cmd_ipc)
    wd = sub.add_parser("watchdog", help="manage watchdog action queue")
    wdsub = wd.add_subparsers(dest="watchdog_cmd", required=True)
    wdl = wdsub.add_parser("list", help="list watchdog actions"); wdl.add_argument("--status", default="", help="filter by status: pending, applied, rejected"); wdl.set_defaults(func=cmd_watchdog)
    wda = wdsub.add_parser("apply", help="manually apply a watchdog action (for needs_luke actions)"); wda.add_argument("action_id", help="action id to apply"); wda.set_defaults(func=cmd_watchdog)
    wdc = wdsub.add_parser("clear", help="remove rejected actions from the queue file"); wdc.set_defaults(func=cmd_watchdog)

    wk = sub.add_parser("worker", help="manage remote worker machines")
    wksub = wk.add_subparsers(dest="worker_cmd", required=True)
    wkl = wksub.add_parser("list", help="list configured remote workers"); wkl.set_defaults(func=cmd_worker)
    wka = wksub.add_parser("add", help="add or update a remote worker")
    wka.add_argument("id", help="short name for this worker, e.g. windows-main")
    wka.add_argument("host", help="Tailscale hostname or IP")
    wka.add_argument("user", help="SSH username on the remote machine")
    wka.add_argument("--providers", default="grok", help="comma-separated provider types this worker supports")
    wka.add_argument("--capacity", type=float, default=4.0, help="weight capacity (default 4.0)")
    wka.add_argument("--cwd", default="~", help="default working directory on the remote machine")
    wka.add_argument("--key", default="", help="path to SSH private key (leave blank to use default)")
    wka.add_argument("--shell", default="", help="remote shell style: powershell, bash, or blank for auto")
    wka.add_argument("--notes", default="", help="optional notes")
    wka.set_defaults(func=cmd_worker)
    wkr = wksub.add_parser("remove", help="remove a remote worker"); wkr.add_argument("id"); wkr.set_defaults(func=cmd_worker)
    wke = wksub.add_parser("enable", help="enable or disable a remote worker")
    wke.add_argument("id"); wke.add_argument("--disable", action="store_true"); wke.set_defaults(func=cmd_worker)
    wkp = wksub.add_parser("ping", help="test SSH connectivity and grok/cursor-agent on a remote worker")
    wkp.add_argument("id"); wkp.set_defaults(func=cmd_worker)
    wkpr = wksub.add_parser("probe", help="report RAM/CPU, provider binaries, and suggested capacity")
    wkpr.add_argument("id")
    wkpr.add_argument("--json", action="store_true", help="emit raw JSON")
    wkpr.add_argument("--apply-capacity", action="store_true", help="set weight_capacity to suggested value")
    wkpr.set_defaults(func=cmd_worker)
    wksc = wksub.add_parser("set-capacity", help="set remote worker weight capacity")
    wksc.add_argument("id")
    wksc.add_argument("capacity", type=float)
    wksc.set_defaults(func=cmd_worker)
    wks = wksub.add_parser("smoke", help="test remote mkdir/scp/exec path used by start_remote()")
    wks.add_argument("id"); wks.set_defaults(func=cmd_worker)
    wkc = wksub.add_parser("coord", help="show mac/remote coordination state and next spill target")
    wkc.add_argument("--json", action="store_true", help="emit raw JSON")
    wkc.set_defaults(func=cmd_worker)
    wkj = wksub.add_parser("jobs", help="list running remote jobs")
    wkj.add_argument("id", nargs="?", default="", help="optional worker id filter")
    wkj.set_defaults(func=cmd_worker)
    wkrp = wksub.add_parser("reap", help="finalize stale remote jobs with dead SSH wrappers")
    wkrp.add_argument("id", nargs="?", default="", help="optional worker id filter")
    wkrp.add_argument("--dry-run", action="store_true")
    wkrp.set_defaults(func=cmd_worker)
    wkb = wksub.add_parser("bench", help="benchmark SSH ping/mkdir/scp/smoke latency")
    wkb.add_argument("id")
    wkb.set_defaults(func=cmd_worker)
    return p


def main(argv: list[str] | None = None) -> None:
    ensure_dirs()
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
