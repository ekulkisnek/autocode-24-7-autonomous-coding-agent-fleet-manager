#!/usr/bin/env python3
"""Dispatch parallel L1 fix workers (code/analysis) without touching the exclusive E2E runner."""
from __future__ import annotations

import argparse
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
WIN_REDWALLET = "C:/Users/Luke/redwallet"
LOG_ROOT = Path("/Volumes/T705/redwallet-logs")

# Mac-local code fix workers (never run run-l1-* orchestrators).
MAC_WORKER_SPECS: dict[str, dict] = {
    "l1-sim-detox-fix": {
        "provider": "cursor",
        "fallback_provider": "grok",
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
            "walletslist",
        ),
        "goal_template": """L1 simulator Detox send fix (Goal 1 worker — code only, NO orchestrator).

Latest failure context:
{failure_context}

Fix tests/e2e/l1_ios_simulator_to_android.spec.js and l1_android_phone_to_ios_simulator.spec.js:
- TransactionValue timeout after CreateTransactionButton
- Detox app-busy after fund (avoid Skip/Continue alert loops; use resetToWalletsList)
- WalletsList timeout after L1_E2E_POST_FUND_RELAUNCH (fix relaunch/sync path)
- L1_E2E_BALANCE_WAIT_MS=120000, test timeout 1200000ms

Do NOT run run-l1-* orchestrators (exclusive shell loop owns that).
Read latest: {run_dir}/ios-to-android/detox.log
Commit to codex/redwallet-utreexo-quic-sync; push ekulkisnek/BlueWallet.
End FLEET_DONE when changes pushed.""",
    },
    "l1-electrum-sync-fix": {
        "provider": "cursor",
        "fallback_provider": "grok",
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
    "l1-orchestrator-hardening": {
        "provider": "cursor",
        "fallback_provider": "grok",
        "cwd": REDWALLET,
        "position": -4985.0,
        "patterns": ("fund", "verify", "skip-seed", "lock", "orchestrator"),
        "goal_template": """L1 orchestrator hardening (Goal 1 worker — scripts only, NO live orchestrator).

Latest failure context:
{failure_context}

Improve RedWallet L1 scripts (do NOT run them):
- scripts/fund-l1-e2e-wallet.sh / send-ios-btc-l1.sh verifyTxPaysAddress checks
- REDWALLET_SKIP_ANDROID_SEED / REDWALLET_SKIP_IOS_SEED handling in orchestrators
- scripts/l1-e2e-lock.sh duplicate-kill safety

Also review autocode scripts/run-l1-e2e-until-verified.sh preflight + retry loop.
Commit redwallet + autocode fixes; push ekulkisnek forks.
End FLEET_DONE when pushed.""",
    },
    "l1-signet-shared-tests": {
        "provider": "cursor",
        "fallback_provider": "grok",
        "cwd": REDWALLET,
        "position": -4982.0,
        "patterns": ("verifytxpaysaddress", "sumpaidsats", "l1signetshared"),
        "goal_template": """L1 l1SignetShared unit tests (Goal 1 worker — tests only).

Latest failure context:
{failure_context}

Add/extend tests/unit/l1SignetShared.test.js for:
- verifyTxPaysAddress (known signet txids from {log_root}/L1_VERIFIED_EVIDENCE.md)
- sumPaidSatsToAddress edge cases

Run: npm run unit -- tests/unit/l1SignetShared.test.js
Do NOT run L1 orchestrators. Commit/push codex/redwallet-utreexo-quic-sync.
End FLEET_DONE when tests pass and pushed.""",
    },
    "l1-log-analysis": {
        "provider": "cursor",
        "fallback_provider": "grok",
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

# Windows spill workers — review/docs only (no Mac Detox).
WINDOWS_WORKER_SPECS: dict[str, dict] = {
    "l1-detox-spec-review": {
        "provider": "cursor",
        "fallback_provider": "grok",
        "cwd": WIN_REDWALLET,
        "position": -4975.0,
        "goal_template": """RedWallet L1 Detox spec review (Windows worker — read/fix spec only).

Latest failure context:
{failure_context}

Review tests/e2e/l1_ios_simulator_to_android.spec.js and l1_android_phone_to_ios_simulator.spec.js:
- post-funding navigation (WalletsList, openWalletSendScreen, L1SendE2E)
- L1_E2E_POST_FUND_RELAUNCH handling
- resetToWalletsList / alert dismissal patterns

Repo: C:\\Users\\Luke\\redwallet (sync from Mac if stale).
Do NOT run Detox or run-l1-* on Windows. Commit/push if fixes made.
End FLEET_DONE with review summary or pushed fixes.""",
    },
    "l1-blueelectrum-signet": {
        "provider": "cursor",
        "fallback_provider": "grok",
        "cwd": WIN_REDWALLET,
        "position": -4970.0,
        "goal_template": """BlueElectrum dev peer config for signet (Windows worker — code review).

Latest failure context:
{failure_context}

Review blue_modules/BlueElectrum.ts default signet peer list and florestad :60101 usage.
Document recommended dev peer config in docs/L1_IOS_ANDROID_E2E.md if missing.
Do NOT run L1 orchestrators. Commit/push if changes made.
End FLEET_DONE with summary or pushed doc.""",
    },
    "l1-docs-e2e": {
        "provider": "cursor",
        "fallback_provider": "grok",
        "cwd": WIN_REDWALLET,
        "position": -4965.0,
        "goal_template": """L1 E2E documentation (Windows worker).

Latest failure context:
{failure_context}

Update or create docs/L1_IOS_ANDROID_E2E.md covering:
- simulator vs physical paths
- run-l1-ios-simulator-to-android-phone-e2e.sh commands
- verify=ok evidence format for L1_VERIFIED_EVIDENCE.md
- known blockers (WalletsList, balance sync, skip-seed flags)

Do NOT run orchestrators. Commit/push if doc updated.
End FLEET_DONE when doc committed.""",
    },
}

WORKER_SPECS = {**MAC_WORKER_SPECS, **WINDOWS_WORKER_SPECS}


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


def workers_for_failure(blob: str, *, dispatch_all: bool) -> tuple[list[str], list[str]]:
    """Return (mac_aliases, windows_aliases) to dispatch."""
    if dispatch_all:
        mac = list(MAC_WORKER_SPECS.keys())
        windows = list(WINDOWS_WORKER_SPECS.keys())
        return mac, windows

    matched_mac: list[str] = []
    for alias, spec in MAC_WORKER_SPECS.items():
        if alias == "l1-log-analysis":
            continue
        if any(p in blob for p in spec.get("patterns", ())):
            matched_mac.append(alias)
    if not matched_mac:
        matched_mac.append("l1-log-analysis")
    elif "l1-log-analysis" not in matched_mac:
        matched_mac.append("l1-log-analysis")
    return matched_mac, []


def pick_provider(store: Store, spec: dict) -> str:
    from autocode import recovery

    primary = spec["provider"]
    fallback = spec.get("fallback_provider", "")
    if recovery.provider_in_backoff(store, primary) and fallback:
        if not recovery.provider_in_backoff(store, fallback):
            return fallback
    # Grok goal1 workers hit provider_error repeatedly — prefer cursor when grok unhealthy.
    if primary == "grok" and fallback == "cursor" and recovery.provider_in_backoff(store, "grok"):
        return "cursor"
    if primary == "cursor" and recovery.provider_in_backoff(store, "cursor") and fallback:
        if not recovery.provider_in_backoff(store, fallback):
            return fallback
    return primary


def alias_has_running_job(store: Store, alias: str) -> bool:
    row = store.row(
        """
        select count(*) c from jobs j
        join chats c on c.id=j.chat_id
        where j.status='running' and c.alias=?
        """,
        (alias,),
    )
    return bool(row and int(row["c"] or 0) > 0)


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


def windows_slot_available(store: Store) -> bool:
    worker = store.row("select * from remote_workers where id='windows-main' and enabled=1")
    if not worker:
        return False
    row = store.row(
        "select count(*) c from jobs where status='running' and worker_id='windows-main'"
    )
    cap = float(worker["weight_capacity"] or 1.0)
    used = int(row["c"] or 0) if row else 0
    return used < cap


def l1_detox_or_shell_active() -> bool:
    """True when the exclusive L1 shell loop or a run-l1-* orchestrator is running."""
    from autocode import coordination
    from autocode.goal_fleets import _l1_loop_running, _l1_orchestrator_running

    return (
        coordination.l1_lock_active()
        or _l1_orchestrator_running()
        or _l1_loop_running()
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Dispatch L1 parallel fix workers")
    parser.add_argument(
        "--all",
        action="store_true",
        default=True,
        help="Dispatch all fix workers when Goal 1 incomplete (default: on)",
    )
    parser.add_argument(
        "--pattern-only",
        action="store_true",
        help="Only dispatch workers matching failure log patterns",
    )
    args = parser.parse_args()
    dispatch_all = not args.pattern_only

    status = load_status()
    l1 = next((g for g in status.get("goals", []) if g.get("id") == "l1-e2e-verified"), None)
    if not l1 or l1.get("complete"):
        print("Goal 1 complete — no L1 workers dispatched.")
        return

    run_dir = find_latest_run_dir()
    blob = failure_blob(run_dir)
    mac_aliases, windows_aliases = workers_for_failure(blob, dispatch_all=dispatch_all)
    failure_context = blob[:3000] if blob else "(no recent detox.log tail)"
    run_dir_str = str(run_dir or LOG_ROOT / "current-l1-simulator-bidirectional-e2e")

    store = Store()
    sched = Scheduler(store)
    l1_busy = l1_detox_or_shell_active()
    if l1_busy:
        # Mac fix workers compete with Detox CPU; Windows review workers do not.
        mac_aliases = []
        print("L1 Detox/shell active — Mac fix workers deferred; Windows workers OK")

    coord = sched.coordination_snapshot()
    mac_can_take = bool(coord.get("mac_can_take_more"))
    dispatched: list[dict] = []
    mac_dispatched = 0
    max_mac_fix_workers = int(store.get_config("l1_max_mac_fix_workers", "2") or 2)

    for alias in mac_aliases:
        if alias_has_running_job(store, alias):
            print(f"SKIP {alias}: job running")
            continue
        spec = MAC_WORKER_SPECS[alias]
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
        if mac_dispatched >= max_mac_fix_workers:
            print(f"QUEUE {alias}: mac fix worker cap ({max_mac_fix_workers})")
            dispatched.append({"alias": alias, "job_id": None, "target": "mac", "queued": True})
            continue
        if not mac_can_take:
            print(f"QUEUE {alias}: mac at capacity (queued for tick spill)")
            dispatched.append({"alias": alias, "job_id": None, "target": "mac", "queued": True})
            continue
        job_id = sched.dispatch(row)
        if job_id:
            print(f"DISPATCHED {alias} -> {job_id} (mac)")
            dispatched.append({"alias": alias, "job_id": job_id, "target": "mac"})
            mac_dispatched += 1
        else:
            print(f"QUEUE {alias}: dispatch deferred")
            dispatched.append({"alias": alias, "job_id": None, "target": "mac", "queued": True})

    if windows_aliases and (l1_busy or not mac_can_take or dispatch_all):
        worker = store.row("select * from remote_workers where id='windows-main' and enabled=1")
        from autocode import recovery

        if recovery.provider_in_backoff(store, "grok"):
            grok_err = store.row("select last_error from provider_health where provider='grok'")
            err_blob = str(grok_err["last_error"] or "").lower() if grok_err else ""
            if any(m in err_blob for m in ("sign in", "oauth", "authorize", "open this url")):
                print("SKIP windows grok workers: grok OAuth required on windows-main")
                windows_aliases = []
        for alias in windows_aliases:
            if alias_has_running_job(store, alias):
                print(f"SKIP {alias}: job running")
                continue
            if not windows_slot_available(store):
                print(f"QUEUE {alias}: windows-main busy")
                dispatched.append({"alias": alias, "job_id": None, "target": "windows", "queued": True})
                continue
            spec = WINDOWS_WORKER_SPECS[alias]
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
            if worker:
                job_id = sched.dispatch_remote(row, dict(worker))
                if job_id:
                    print(f"DISPATCHED {alias} -> {job_id} (windows-main)")
                    dispatched.append({"alias": alias, "job_id": job_id, "target": "windows-main"})
                else:
                    print(f"QUEUE {alias}: windows dispatch failed")
            else:
                print(f"SKIP {alias}: windows-main unavailable")

    print(
        json.dumps(
            {
                "dispatched": dispatched,
                "run_dir": run_dir_str,
                "mac_workers": mac_aliases,
                "windows_workers": windows_aliases,
                "mac_can_take_more": mac_can_take,
                "goal_pct": l1.get("pct"),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
