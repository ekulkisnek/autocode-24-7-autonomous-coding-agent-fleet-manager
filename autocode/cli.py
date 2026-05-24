from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .config import DB, LOG, PID_FILE, ROOT, ensure_dirs
from .daemon import Daemon
from .discovery import discover
from .launchd import install as launchd_install
from .launchd import start as launchd_start
from .launchd import status as launchd_status
from .launchd import stop as launchd_stop
from .providers import providers
from .scheduler import Scheduler
from .store import Store
from .models import ContinuePlan
from .util import command_exists, compact, json_loads, load1, memory_free_percent, read_text, rel_time, sha


def print_status(store: Store, limit: int) -> None:
    Scheduler(store).runner.refresh()
    chats = store.rows("select count(*) c from chats")
    active = store.rows("select count(*) c from chats where adopted=1 and done=0 and paused=0 and coding_score>0")
    running = store.rows("select * from jobs where status='running' order by created_at desc limit ?", (limit,))
    goals = store.rows("select g.*,c.alias,c.provider from goals g join chats c on c.id=g.chat_id where g.status='active' order by g.updated_at desc limit ?", (limit,))
    priorities = store.rows("select * from project_priorities where status='active' order by priority desc, updated_at desc limit ?", (limit,))
    recent = store.rows("select * from jobs where status!='running' order by updated_at desc limit ?", (limit,))
    daemon_ok, _ = launchd_status()
    print("AutoCode")
    print(f"daemon: {'on' if daemon_ok else 'off'} | yolo={store.get_config('yolo','off')} | load={load1():.2f}")
    print(f"db: {DB}")
    print(f"chats: {int(chats[0]['c']) if chats else 0} total, {int(active[0]['c']) if active else 0} active/adopted")
    print(f"running: {len(running)}")
    if running:
        for j in running:
            print(f"- {rel_time(j['created_at'])} {j['provider']} {short(j['chat_id'])} {j['id']} {j['evidence_status']}")
    print(f"goals: {len(goals)}")
    for g in goals[:limit]:
        print(f"- {g['provider']} {short(g['alias'])}: {compact(g['objective'], 180)}")
    print(f"priority projects: {len(priorities)}")
    for p in priorities[:limit]:
        print(f"- p{p['priority']} {short(p['query'])}: {compact(p['objective'], 160)}")
    if recent:
        print("recent:")
        for j in recent[:limit]:
            print(f"- {rel_time(j['updated_at'])} {j['evidence_status']} {short(j['chat_id'])}")
            if j["evidence_reason"]:
                print(f"  {compact(j['evidence_reason'], 160)}")


def print_now(store: Store, limit: int) -> None:
    Scheduler(store).runner.refresh()
    running = store.rows("select * from jobs where status='running' order by created_at desc limit ?", (limit,))
    candidates = Scheduler(store).candidates(limit)
    if running:
        print("Running now:")
        for j in running:
            print(f"- {rel_time(j['created_at'])} {j['provider']} {short(j['chat_id'])} {j['id']} {j['evidence_status']}")
    else:
        print("Running now: none")
    print("Next up:")
    for c in candidates:
        print(f"- {rel_time(c['updated_at'])} {c['provider']} {short(c['alias'])} state={c['state']}")
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
        count = store.remove_priority(args.query)
        print(f"Paused {count} priority project(s).")


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
                launched += 1
                continue
            lease = lane["path"] if lane["mode"] == "worktree" else ""
            job_id = sched.runner.start_aux(chat_id, lane["path"], plan, prompt, job_dir, lease_resource=lease)
            store.event("squad_lane_started", chat_id, job_id, priority_id=priority["id"], lane=lane["name"], mode=lane["mode"])
            print(f"launched {lane['name']}: {job_id}")
            launched += 1
        if launched == 0 and not args.dry_run:
            print("No squad lanes launched.")
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
    rows = store.rows("select * from chats where updated_at!='' order by updated_at desc limit ?", (args.limit * 8,))
    shown = 0
    for r in rows:
        from .util import parse_ts
        job = active_job_for(store, r["id"])
        display_at = job["updated_at"] if job else r["updated_at"]
        if parse_ts(display_at) < cutoff:
            continue
        shown += 1
        state = "running" if job else r["state"]
        label = priority_label(store, r["id"], r["alias"])
        print(f"- {rel_time(display_at)} {r['provider']} {short(label)} state={state} score={r['coding_score']}")
        if job:
            print(f"  job {job['id']} {job['evidence_status']}: {job_tail(job, 220)}")
        else:
            print(f"  {compact(r['title'] or r['latest_text'], 180)}")
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
                args.goal,
            ],
            env=provider.cursor_env(),
            same_chat=False,
            reason="Start a new Cursor Agent chat.",
        )
        job_id = Scheduler(store).runner.start_aux(f"cursor:new:{sha(cwd + args.goal)[:16]}", cwd, plan, args.goal)
        print(f"Started Cursor Agent chat job: {job_id}")
        print(f"workspace: {cwd}")
        print(f"model: {model}")
        print(f"goal: {args.goal}")


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
    if args.priority:
        store.add_priority(args.query, args.goal, args.rank, args.path, row["id"] if args.exact else "", args.lanes)
    row = store.row("select * from chats where id=?", (row["id"],))
    job_id = None
    if args.no_start:
        print(f"Goal attached: {row['alias']}")
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
    if args.priority:
        print(f"priority: p{args.rank}")
        if args.path:
            print(f"priority path: {args.path}")
        if args.exact:
            print(f"priority target: {row['id']}")
        if args.lanes > 1:
            print(f"priority lanes: {args.lanes}")
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
    store.pause_chat(row["id"])
    print(f"Paused {row['alias']}")


def cmd_done(args: argparse.Namespace) -> None:
    store = Store()
    row = store.find_chat(args.query)
    if not row:
        raise SystemExit(f"No chat matched: {args.query}")
    store.done_chat(row["id"])
    print(f"Done {row['alias']}")


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


def cmd_discover(args: argparse.Namespace) -> None:
    stats = Scheduler(Store()).force_discover()
    print(json.dumps(stats, indent=2, sort_keys=True))


def cmd_tick(args: argparse.Namespace) -> None:
    result = Scheduler(Store()).tick(dispatch=not args.dry_run, max_projects=args.max_projects)
    print(json.dumps(result, indent=2, sort_keys=True))


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
        code, out, err = launchd_stop()
        print((out + err).strip() or f"stopped ({code})")
    elif args.daemon_cmd == "restart":
        launchd_stop()
        code, out, err = launchd_start()
        print((out + err).strip() or f"restarted ({code})")
    elif args.daemon_cmd == "status":
        ok, text = launchd_status()
        print("loaded" if ok else "not loaded")
        if args.verbose:
            print(text[:4000])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autocode")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status"); s.add_argument("--limit", type=int, default=10); s.set_defaults(func=cmd_status)
    n = sub.add_parser("now"); n.add_argument("--limit", type=int, default=10); n.set_defaults(func=cmd_now)
    last = sub.add_parser("last"); last.add_argument("query"); last.set_defaults(func=cmd_last)
    g = sub.add_parser("goals"); g.add_argument("--limit", type=int, default=20); g.set_defaults(func=cmd_goals)
    pr = sub.add_parser("priority")
    prsub = pr.add_subparsers(dest="priority_cmd", required=True)
    prl = prsub.add_parser("list"); prl.add_argument("--limit", type=int, default=20); prl.set_defaults(func=cmd_priority)
    pra = prsub.add_parser("add"); pra.add_argument("query"); pra.add_argument("--goal", required=True); pra.add_argument("--rank", type=int, default=100); pra.add_argument("--path", default=""); pra.add_argument("--chat-id", default=""); pra.add_argument("--exact", action="store_true"); pra.add_argument("--lanes", type=int, default=1); pra.set_defaults(func=cmd_priority)
    prr = prsub.add_parser("remove"); prr.add_argument("query"); prr.set_defaults(func=cmd_priority)
    c = sub.add_parser("chats"); c.add_argument("--recent", default="24h"); c.add_argument("--limit", type=int, default=20); c.set_defaults(func=cmd_chats)
    cu = sub.add_parser("cursor")
    cusub = cu.add_subparsers(dest="cursor_cmd", required=True)
    cust = cusub.add_parser("status"); cust.set_defaults(func=cmd_cursor)
    cuch = cusub.add_parser("chats"); cuch.add_argument("--limit", type=int, default=30); cuch.add_argument("--source", choices=["cursor.cli", "cursor.transcript", "cursor.ide", "cursor.cloud"], default=""); cuch.set_defaults(func=cmd_cursor)
    cuhi = cusub.add_parser("history"); cuhi.add_argument("query"); cuhi.add_argument("--chars", type=int, default=3000); cuhi.set_defaults(func=cmd_cursor)
    cumo = cusub.add_parser("model"); cumo.add_argument("model", nargs="?"); cumo.set_defaults(func=cmd_cursor)
    cumos = cusub.add_parser("models"); cumos.set_defaults(func=cmd_cursor)
    cune = cusub.add_parser("new"); cune.add_argument("--workspace", default=str(Path.home())); cune.add_argument("--goal", required=True); cune.add_argument("--model", default=""); cune.set_defaults(func=cmd_cursor)
    d = sub.add_parser("drive"); d.add_argument("query"); d.add_argument("--goal", required=True); d.add_argument("--no-start", action="store_true"); d.add_argument("--priority", action="store_true"); d.add_argument("--rank", type=int, default=100); d.add_argument("--path", default=""); d.add_argument("--exact", action="store_true"); d.add_argument("--lanes", type=int, default=1); d.add_argument("--model", default="", help="Cursor-only per-send model override, e.g. auto or composer-2.5"); d.set_defaults(func=cmd_drive)
    sq = sub.add_parser("squad")
    sqsub = sq.add_subparsers(dest="squad_cmd", required=True)
    sqp = sqsub.add_parser("plan"); sqp.add_argument("query"); sqp.set_defaults(func=cmd_squad)
    sql = sqsub.add_parser("launch"); sql.add_argument("query"); sql.add_argument("--limit", type=int, default=0, help="maximum lanes to launch; 0 means use current resource headroom"); sql.add_argument("--mode", choices=["read_only", "worktree", "all"], default="read_only"); sql.add_argument("--dry-run", action="store_true"); sql.add_argument("--force", action="store_true"); sql.set_defaults(func=cmd_squad)
    sqc = sqsub.add_parser("collect"); sqc.add_argument("query"); sqc.add_argument("--limit", type=int, default=8); sqc.add_argument("--send-writer", action="store_true"); sqc.set_defaults(func=cmd_squad)
    y = sub.add_parser("yolo"); y.add_argument("state", choices=["on", "off"]); y.set_defaults(func=cmd_yolo)
    pa = sub.add_parser("pause"); pa.add_argument("query"); pa.set_defaults(func=cmd_pause)
    dn = sub.add_parser("done"); dn.add_argument("query"); dn.set_defaults(func=cmd_done)
    l = sub.add_parser("logs"); l.add_argument("--lines", type=int, default=80); l.set_defaults(func=cmd_logs)
    doc = sub.add_parser("doctor"); doc.set_defaults(func=cmd_doctor)
    disc = sub.add_parser("discover"); disc.set_defaults(func=cmd_discover)
    tick = sub.add_parser("tick"); tick.add_argument("--dry-run", action="store_true"); tick.add_argument("--max-projects", type=int, default=None); tick.set_defaults(func=cmd_tick)
    dm = sub.add_parser("daemon")
    dsub = dm.add_subparsers(dest="daemon_cmd", required=True)
    run = dsub.add_parser("run"); run.add_argument("--interval", type=int, default=20); run.set_defaults(func=cmd_daemon)
    for name in ("install", "start", "stop", "restart"):
        x = dsub.add_parser(name); x.set_defaults(func=cmd_daemon)
    st = dsub.add_parser("status"); st.add_argument("--verbose", action="store_true"); st.set_defaults(func=cmd_daemon)
    return p


def main(argv: list[str] | None = None) -> None:
    ensure_dirs()
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
