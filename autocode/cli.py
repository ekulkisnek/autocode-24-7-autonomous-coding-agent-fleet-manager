from __future__ import annotations

import argparse
import json
import os
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
from .util import command_exists, compact, json_loads, load1, rel_time


def print_status(store: Store, limit: int) -> None:
    Scheduler(store).runner.refresh()
    chats = store.rows("select count(*) c from chats")
    active = store.rows("select count(*) c from chats where adopted=1 and done=0 and paused=0 and coding_score>0")
    jobs = store.rows("select * from jobs order by created_at desc limit ?", (limit,))
    running = [j for j in jobs if j["status"] == "running"]
    goals = store.rows("select g.*,c.alias,c.provider from goals g join chats c on c.id=g.chat_id where g.status='active' order by g.updated_at desc limit ?", (limit,))
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


def cmd_chats(args: argparse.Namespace) -> None:
    store = Store()
    Scheduler(store).force_discover()
    cutoff = time.time() - parse_recent(args.recent)
    rows = store.rows("select * from chats where updated_at!='' order by updated_at desc limit ?", (args.limit * 8,))
    shown = 0
    for r in rows:
        from .util import parse_ts
        if parse_ts(r["updated_at"]) < cutoff:
            continue
        shown += 1
        print(f"- {rel_time(r['updated_at'])} {r['provider']} {short(r['alias'])} state={r['state']} score={r['coding_score']}")
        print(f"  {compact(r['title'] or r['latest_text'], 180)}")
        if shown >= args.limit:
            break
    if shown == 0:
        print("No chats in that window.")


def cmd_drive(args: argparse.Namespace) -> None:
    store = Store()
    Scheduler(store).force_discover()
    row = store.find_chat(args.query)
    if not row:
        raise SystemExit(f"No chat matched: {args.query}")
    store.set_goal(row["id"], args.goal, "user")
    row = store.row("select * from chats where id=?", (row["id"],))
    job_id = None
    if args.no_start:
        print(f"Goal attached: {row['alias']}")
        return
    sched = Scheduler(store)
    if not sched.has_active_job(row["id"]) and not sched.has_active_lease(row):
        job_id = sched.dispatch(row)
    print(f"Driving {row['alias']}")
    print(f"goal: {args.goal}")
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
    g = sub.add_parser("goals"); g.add_argument("--limit", type=int, default=20); g.set_defaults(func=cmd_goals)
    c = sub.add_parser("chats"); c.add_argument("--recent", default="24h"); c.add_argument("--limit", type=int, default=20); c.set_defaults(func=cmd_chats)
    d = sub.add_parser("drive"); d.add_argument("query"); d.add_argument("--goal", required=True); d.add_argument("--no-start", action="store_true"); d.set_defaults(func=cmd_drive)
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

