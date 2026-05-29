#!/usr/bin/env python3
"""Dispatch parallel L1 fix workers (code/analysis) without touching the exclusive E2E runner."""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autocode.models import Chat
from autocode.scheduler import Scheduler
from autocode.store import Store
from autocode.util import now_iso, sha

REDWALLET = "/Volumes/T705/code/work-on-something-to-do-with/redwallet"
LOG_ROOT = "/Volumes/T705/redwallet-logs"

WORKERS = [
    {
        "alias": "l1-sim-detox-fix",
        "provider": "grok",
        "cwd": REDWALLET,
        "position": -5000.0,
        "goal": f"""L1 simulator Detox send fix (Goal 1 worker — code only, NO orchestrator).

Fix tests/e2e/l1_ios_simulator_to_android.spec.js and l1_android_phone_to_ios_simulator.spec.js:
- TransactionValue timeout after CreateTransactionButton
- Detox app-busy after fund (avoid Skip/Continue alert loops; use resetToWalletsList)
- L1_E2E_BALANCE_WAIT_MS=120000, test timeout 1200000ms

Do NOT run run-l1-* orchestrators (exclusive shell loop owns that).
Read latest: {LOG_ROOT}/current-l1-simulator-bidirectional-e2e/ios-to-android/detox.log
Commit to codex/redwallet-utreexo-quic-sync; push ekulkisnek/BlueWallet.
End FLEET_DONE when changes pushed.""",
    },
    {
        "alias": "l1-electrum-sync-fix",
        "provider": "grok",
        "cwd": REDWALLET,
        "position": -4990.0,
        "goal": f"""L1 Electrum balance sync fix (Goal 1 worker — code only).

Wallets report 0 balance despite on-chain UTXOs on signet florestad :60101.
Verify blockchain.scripthash.get_balance for funded addresses in scripts or tests/e2e/l1SignetShared.js.
Check blue_modules/BlueElectrum.ts sync path; florestad electrum at 127.0.0.1:60101.

Do NOT run L1 orchestrators. Commit/push fork branch codex/redwallet-utreexo-quic-sync.
End FLEET_DONE when fix pushed.""",
    },
    {
        "alias": "l1-log-analysis",
        "provider": "grok",
        "cwd": REDWALLET,
        "position": -4980.0,
        "goal": f"""L1 log analyst (read-only recommendations).

Read:
- {LOG_ROOT}/current-l1-simulator-bidirectional-e2e/ios-to-android/detox.log
- {LOG_ROOT}/l1-e2e-until-verified-autocode.log
- {LOG_ROOT}/L1_VERIFIED_EVIDENCE.md

Output: top 3 blockers + exact file/line fixes. Do NOT run orchestrators or edit unless trivial one-liner.
End FLEET_DONE with written analysis in job stdout.""",
    },
]


def load_status() -> dict:
    script = Path(__file__).resolve().parent / "verify-goal-status.py"
    r = subprocess.run([sys.executable, str(script), "--json"], capture_output=True, text=True)
    if r.stdout.strip():
        return json.loads(r.stdout)
    return {"all_complete": False, "goals": []}


def ensure_worker(store: Store, spec: dict) -> str:
    goal = spec["goal"]
    alias = spec["alias"]
    provider = spec["provider"]
    chat_id = f"{provider}:goal1-worker:{alias}:{sha(goal)[:8]}"
    chat = Chat(
        id=chat_id,
        provider=provider,
        source=f"{provider}.sqlite",
        provider_chat_id=f"goal1-{uuid.uuid4().hex[:12]}",
        title=alias,
        cwd=spec["cwd"],
        updated_at=now_iso(),
        latest_text=goal[:500],
        transcript_hash=sha(goal),
        alias=alias,
        continuation=provider,
    )
    store.upsert_chat(chat, coding_score=15, state="active", objective=goal)
    store.queue_add(chat.id, float(spec.get("position", 0)))
    row = store.row("select paused from chats where id=?", (chat.id,))
    if row and int(row["paused"] or 0):
        with store.connect() as con:
            con.execute("update chats set paused=0 where id=?", (chat.id,))
    return chat.id


def main() -> None:
    status = load_status()
    l1 = next((g for g in status.get("goals", []) if g.get("id") == "l1-e2e-verified"), None)
    if not l1 or l1.get("complete"):
        print("Goal 1 complete — no L1 workers dispatched.")
        return

    store = Store()
    sched = Scheduler(store)
    dispatched: list[str] = []

    for spec in WORKERS:
        alias = spec["alias"]
        active = store.row(
            """
            select count(*) c from jobs j
            join chats c on c.id=j.chat_id
            where j.status='running' and c.alias=?
            """,
            (alias,),
        )
        if active and int(active["c"] or 0) > 0:
            print(f"SKIP {alias}: job running")
            continue
        chat_id = ensure_worker(store, spec)
        row = store.row("select * from chats where id=?", (chat_id,))
        if sched.has_active_job(chat_id):
            print(f"SKIP {alias}: active job")
            continue
        job_id = sched.dispatch(row)
        if job_id:
            print(f"DISPATCHED {alias} -> {job_id}")
            dispatched.append(job_id)

    print(json.dumps({"dispatched": dispatched}, indent=2))


if __name__ == "__main__":
    main()
