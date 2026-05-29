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
LOG_ROOT = Path("/Volumes/T705/redwallet-logs")

WORKER_SPECS = {
    "l1-sim-detox-fix": {
        "provider": "grok",
        "fallback_provider": "cursor",
        "cwd": REDWALLET,
        "position": -5000.0,
        "patterns": (
            "transactionvalue",
            "app busy",
            "app-busy",
            "skip",
            "createtransactionbutton",
            "l1sende2e",
            "detox",
        ),
        "goal_template": """L1 simulator Detox send fix (Goal 1 worker — code only, NO orchestrator).

Latest failure context:
{failure_context}

Fix tests/e2e/l1_ios_simulator_to_android.spec.js and l1_android_phone_to_ios_simulator.spec.js:
- TransactionValue timeout after CreateTransactionButton
- Detox app-busy after fund (avoid Skip/Continue alert loops; use resetToWalletsList)
- L1_E2E_BALANCE_WAIT_MS=120000, test timeout 1200000ms

Do NOT run run-l1-* orchestrators (exclusive shell loop owns that).
Read latest: {run_dir}/ios-to-android/detox.log
Commit to codex/redwallet-utreexo-quic-sync; push ekulkisnek/BlueWallet.
End FLEET_DONE when changes pushed.""",
    },
    "l1-electrum-sync-fix": {
        "provider": "grok",
        "fallback_provider": "cursor",
        "cwd": REDWALLET,
        "position": -4990.0,
        "patterns": (
            "balance",
            "electrum",
            "scripthash",
            "insufficient balance",
            "0 sats",
            "0 <",
        ),
        "goal_template": """L1 Electrum balance sync fix (Goal 1 worker — code only).

Latest failure context:
{failure_context}

Wallets report 0 balance despite on-chain UTXOs on signet florestad :60101.
Verify blockchain.scripthash.get_balance for funded addresses via scripts/preflight-electrum-balance.sh.
Check blue_modules/BlueElectrum.ts sync path; florestad electrum at 127.0.0.1:60101.

Do NOT run L1 orchestrators. Commit/push fork branch codex/redwallet-utreexo-quic-sync.
End FLEET_DONE when fix pushed.""",
    },
    "l1-log-analysis": {
        "provider": "grok",
        "fallback_provider": "cursor",
        "cwd": REDWALLET,
        "position": -4980.0,
        "patterns": tuple(),
        "goal_template": """L1 log analyst (read-only recommendations).

Latest failure context:
{failure_context}

Read:
- {run_dir}/ios-to-android/detox.log
- {log_root}/l1-e2e-until-verified-autocode.log
- {log_root}/L1_VERIFIED_EVIDENCE.md

Output: top 3 blockers + exact file/line fixes. Do NOT run orchestrators or edit unless trivial one-liner.
End FLEET_DONE with written analysis in job stdout.""",
    },
}


def load_status() -> dict:
    script = Path(__file__).resolve().parent / "verify-goal-status.py"
    r = subprocess.run([sys.executable, str(script), "--json"], capture_output=True, text=True)
    if r.stdout.strip():
        return json.loads(r.stdout)
    return {"all_complete": False, "goals": []}


def find_latest_run_dir() -> Path | None:
    for name in (
        "current-l1-simulator-bidirectional-e2e",
        "current-l1-ios-android-e2e",
        "current-l1-android-ios-e2e",
    ):
        link = LOG_ROOT / name
        if link.is_symlink():
            target = link.resolve()
            if target.is_dir():
                return target
    candidates = sorted(
        LOG_ROOT.glob("l1-simulator-bidirectional-e2e-*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def failure_blob(run_dir: Path | None) -> str:
    parts: list[str] = []
    if run_dir:
        for rel in ("ios-to-android/detox.log", "android-to-ios/detox.log", "SUMMARY.txt"):
            path = run_dir / rel
            if path.is_file():
                try:
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                    parts.append("\n".join(lines[-50:]))
                except OSError:
                    pass
    autocode_log = LOG_ROOT / "l1-e2e-until-verified-autocode.log"
    if autocode_log.is_file():
        try:
            lines = autocode_log.read_text(encoding="utf-8", errors="replace").splitlines()
            parts.append("\n".join(lines[-30:]))
        except OSError:
            pass
    return "\n".join(parts).lower()


def workers_for_failure(blob: str) -> list[str]:
    matched: list[str] = []
    for alias, spec in WORKER_SPECS.items():
        if alias == "l1-log-analysis":
            continue
        if any(p in blob for p in spec["patterns"]):
            matched.append(alias)
    if not matched:
        matched.append("l1-log-analysis")
    elif "l1-log-analysis" not in matched and len(matched) < 2:
        matched.append("l1-log-analysis")
    return matched


def pick_provider(store: Store, spec: dict) -> str:
    from autocode import recovery

    provider = spec["provider"]
    fallback = spec.get("fallback_provider", "")
    if fallback and recovery.provider_in_backoff(store, provider):
        if not recovery.provider_in_backoff(store, fallback):
            return fallback
    return provider


def ensure_worker(store: Store, alias: str, spec: dict, goal: str) -> str:
    provider = pick_provider(store, spec)
    chat_id = f"{provider}:goal1-worker:{alias}:{sha(goal)[:8]}"
    chat = Chat(
        id=chat_id,
        provider=provider,
        source=f"{provider}.sqlite" if provider != "cursor" else "cursor.new",
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

    run_dir = find_latest_run_dir()
    blob = failure_blob(run_dir)
    aliases = workers_for_failure(blob)
    failure_context = blob[:3000] if blob else "(no recent detox.log tail)"
    run_dir_str = str(run_dir or LOG_ROOT / "current-l1-simulator-bidirectional-e2e")

    store = Store()
    sched = Scheduler(store)
    dispatched: list[str] = []

    for alias in aliases:
        spec = WORKER_SPECS[alias]
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
        goal = spec["goal_template"].format(
            failure_context=failure_context,
            run_dir=run_dir_str,
            log_root=str(LOG_ROOT),
        )
        chat_id = ensure_worker(store, alias, spec, goal)
        row = store.row("select * from chats where id=?", (chat_id,))
        if sched.has_active_job(chat_id):
            print(f"SKIP {alias}: active job")
            continue
        job_id = sched.dispatch(row)
        if job_id:
            print(f"DISPATCHED {alias} -> {job_id}")
            dispatched.append(job_id)

    print(json.dumps({"dispatched": dispatched, "run_dir": run_dir_str, "workers": aliases}, indent=2))


if __name__ == "__main__":
    main()
