#!/usr/bin/env python3
"""Dispatch Cursor meta-supervisor: reads goal status + infra report, fixes blockers autonomously."""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autocode.models import Chat
from autocode.goal_supervisor_adaptive import adaptive_context_for_dispatch, format_escalation_block
from autocode.scheduler import Scheduler
from autocode.store import Store
from autocode.util import now_iso, sha

AUTOCODE_ROOT = Path(__file__).resolve().parents[1]
REDWALLET = "/Volumes/T705/code/work-on-something-to-do-with/redwallet"
LOG_ROOT = "/Volumes/T705/redwallet-logs"
META_ALIAS = "autocode-meta-supervisor"
DEFAULT_INTERVAL_SEC = int(__import__("os").environ.get("AUTOCODE_META_SUPERVISOR_INTERVAL", "120"))


def load_infra_report() -> dict:
    script = AUTOCODE_ROOT / "scripts" / "autocode-infra-supervisor.py"
    r = subprocess.run([sys.executable, str(script), "--json"], capture_output=True, text=True, timeout=120)
    if r.stdout.strip():
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            pass
    return {"actions": [], "goals": {}}


def load_status() -> dict:
    script = AUTOCODE_ROOT / "scripts" / "verify-goal-status.py"
    r = subprocess.run([sys.executable, str(script), "--json"], capture_output=True, text=True, timeout=120)
    if r.stdout.strip():
        return json.loads(r.stdout)
    return {"all_complete": False, "goals": []}


def failure_context() -> str:
    parts: list[str] = []
    for name in (
        "current-l1-simulator-bidirectional-e2e",
        "current-l1-ios-android-e2e",
    ):
        link = Path(LOG_ROOT) / name
        if link.is_symlink():
            run_dir = link.resolve()
            for rel in ("ios-to-android/detox.log", "android-to-ios/detox.log", "SUMMARY.txt"):
                p = run_dir / rel
                if p.is_file():
                    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                    parts.append(f"--- {p} ---\n" + "\n".join(lines[-40:]))
    log = Path(LOG_ROOT) / "l1-e2e-until-verified-autocode.log"
    if log.is_file():
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        parts.append("--- until-verified log ---\n" + "\n".join(lines[-30:]))
    return "\n\n".join(parts)[:6000] or "(no recent logs)"


def meta_supervisor_due(store: Store, *, infra_actions: list[str]) -> bool:
    if infra_actions:
        return True
    row = store.row("select value from config where key='last_meta_supervisor_at'")
    if not row or not row["value"]:
        return True
    try:
        from datetime import datetime, timezone

        last = datetime.strptime(str(row["value"]), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age = (__import__("time").time() - last.timestamp())
        return age >= DEFAULT_INTERVAL_SEC
    except ValueError:
        return True


def build_goal(status: dict, infra: dict, ctx: str, adaptive_ctx: dict | None = None) -> str:
    goals_txt = json.dumps(status.get("goals", []), indent=2)
    infra_txt = json.dumps(infra, indent=2)
    escalation = format_escalation_block(adaptive_ctx or {})
    escalation_block = f"\n\n{escalation}" if escalation else ""
    return f"""AutoCode meta-supervisor (self-healing control plane).

You supervise autocode until ALL final goals pass verify-goal-status.py.
Do NOT stop driving until `all_complete=true`.

## Current goals
{goals_txt}

## Infra supervisor report (deterministic fixes already applied this tick)
{infra_txt}

## Primary goal (L1)
Success = {LOG_ROOT}/L1_VERIFIED_EVIDENCE.md has TWO mainchain txids with verify=ok (simulator ↔ Android).
LiPhone unplugged — simulator path ONLY. Android 0A201JECB03306 connected.

## Your job this cycle
1. Read verify-goal-status + infra report + failure logs below.
2. Fix blockers in autocode ({AUTOCODE_ROOT}) and redwallet ({REDWALLET}):
   - Detox send failures (WalletsList, TransactionValue, app-busy, L1SendE2E)
   - Orchestrator/env bugs (skip seed, IOS_L1_RECEIVE_ADDRESS propagation, ANDROID_SDK_ROOT)
   - Autocode coordination (goal_fleets, dispatch-l1-goal-workers, provider backoff)
3. Ensure running: daemon, run-l1-e2e-until-verified.sh, electrum :60101.
4. Do NOT start duplicate run-l1-* orchestrators while lock held or Detox active.
5. When L1 incomplete and Detox idle: run `python3 scripts/dispatch-l1-goal-workers.py`.
6. Commit + push meaningful fixes to ekulkisnek forks.

## Failure context
{ctx}{escalation_block}

## End condition for THIS job
- If you made fixes: summarize what changed and what should run next.
- If goals still incomplete: do NOT mark FLEET_DONE — leave chat active for re-dispatch.
- If all goals complete: end FLEET_DONE.

Never ask the human to monitor — autocode loop owns retries."""


def has_running_meta_job(store: Store) -> bool:
    row = store.row(
        """
        select count(*) c from jobs j
        join chats c on c.id=j.chat_id
        where j.status='running' and c.alias=?
        """,
        (META_ALIAS,),
    )
    return bool(row and int(row["c"] or 0) > 0)


def main() -> None:
    status = load_status()
    if status.get("all_complete"):
        print("All goals complete — meta-supervisor idle.")
        return

    infra = load_infra_report()
    store = Store()
    if has_running_meta_job(store):
        print("SKIP: meta-supervisor job already running")
        return
    if not meta_supervisor_due(store, infra_actions=list(infra.get("actions") or [])):
        print(f"SKIP: meta-supervisor interval ({DEFAULT_INTERVAL_SEC}s) not elapsed")
        return

    ctx = failure_context()
    adaptive_ctx = adaptive_context_for_dispatch()
    goal = build_goal(status, infra, ctx, adaptive_ctx)
    chat_id = f"cursor:meta-supervisor:{sha(goal)[:8]}"
    chat = Chat(
        id=chat_id,
        provider="cursor",
        source="cursor.new",
        provider_chat_id=f"meta-{uuid.uuid4().hex[:12]}",
        title=META_ALIAS,
        cwd=str(AUTOCODE_ROOT),
        updated_at=now_iso(),
        latest_text=goal[:500],
        transcript_hash=sha(goal),
        alias=META_ALIAS,
        continuation="cursor",
    )
    store.upsert_chat(chat, coding_score=25, state="active", objective=goal)
    store.set_goal(chat.id, goal, "user")
    store.queue_add(chat.id, -9500.0)

    sched = Scheduler(store)
    row = store.row("select * from chats where id=?", (chat.id,))
    job_id = sched.dispatch(row) if row else None

    with store.connect() as con:
        con.execute(
            "insert or replace into config(key,value) values('last_meta_supervisor_at',?)",
            (now_iso(),),
        )

    print(json.dumps({"dispatched": job_id, "chat_id": chat.id, "goals": status.get("goals")}, indent=2))


if __name__ == "__main__":
    main()
