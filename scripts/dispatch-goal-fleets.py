#!/usr/bin/env python3
"""Goal-driven autocode dispatch: re-queue incomplete goals until success or hard blocker."""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autocode.goal_fleets import PARALLEL_WITH_L1_GOAL_IDS
from autocode.models import Chat
from autocode.scheduler import Scheduler
from autocode.store import Store
from autocode.util import now_iso, sha

REDWALLET = "/Volumes/T705/code/work-on-something-to-do-with/redwallet"
LOG_ROOT = "/Volumes/T705/redwallet-logs"

GOAL_FLEETS = {
    "l1-e2e-verified": {
        "alias": "l1-e2e-until-verified",
        "provider": "cursor",
        "fallback_provider": "grok",
        "cwd": REDWALLET,
        "position": -10000.0,
        "goal": f"""RedWallet L1 E2E until verified (Goal 1).

CURRENT MODE: LiPhone unplugged — simulator paths ONLY.
Do NOT run run-l1-physical-bidirectional-e2e.sh or run-l1-ios-phone-* orchestrators.

SUCCESS CRITERIA (stop with FLEET_DONE only when ALL met):
1. Autocode runs scripts/run-l1-e2e-until-verified.sh which picks ONE path:
   - physical: ./scripts/run-l1-physical-bidirectional-e2e.sh (ONLY when LiPhone USB connected)
   - simulator: run-l1-ios-simulator-to-android-phone-e2e.sh THEN run-l1-android-phone-to-ios-simulator-e2e.sh (ACTIVE NOW)
2. Update {LOG_ROOT}/L1_VERIFIED_EVIDENCE.md with TWO mainchain txids, detox_exit=0, verify=ok for BOTH directions.
3. Do NOT start parallel Detox/orchestrator runs — lock is enforced via scripts/l1-e2e-lock.sh.
4. Do NOT spawn Cursor Task subagents — autocode fleet job only.

If lock is held, wait or inspect current run logs under {LOG_ROOT}/current-l1-simulator-bidirectional-e2e.
Known simulator blockers to fix: L1SendE2E not found after fund (openWalletSendScreen), iPhone 16e-Detox in .detoxrc.json, Detox app busy after fund.

Devices: Android 0A201JECB03306 (connected), LiPhone 00008020-0011204911F3002E (UNPLUGGED — use iOS Simulator iPhone 16e-Detox).
REDWALLET_SKIP_ANDROID_SEED=1 with known address tb1qewdkqej3xc6hh2v5q88rnaekd2zkccf0zq6kdf when seed already done.
Commit redwallet fixes to fork branch codex/redwallet-utreexo-quic-sync; push to ekulkisnek/BlueWallet.

End with FLEET_DONE only after verify-goal-status shows l1-e2e-verified complete.""",
    },
    "windows-remote-health": {
        "alias": "windows-remote-health",
        "provider": "grok",
        "cwd": "/Users/lukekensik/autocode",
        "position": 8000.0,
        "goal": """AutoCode Windows remote health (Goal 2).

SUCCESS:
- python3 -m autocode worker ping windows-main → ok
- python3 -m autocode worker bench windows-main → all *_ok=1
- Dispatch ONE grok job to windows-main that completes with evidence_status=worked (real stdout, not API limit false positive)

Fix if needed: grok OAuth on Windows, cursor-agent path C:\\Users\\Luke\\AppData\\Local\\cursor-agent\\cursor-agent.cmd,
sequential dispatch (weight_capacity=1), repos at C:\\Users\\Luke\\redwallet and drivechain subrepos.

Run: python3 -m autocode coord set-windows-sequential
Probe: python3 -c "from autocode.store import Store; from autocode import remote_ssh; print(remote_ssh.probe_worker(dict(Store().row('select * from remote_workers where id=?', ('windows-main',)))))"

End with FLEET_DONE when remote worked job exists.""",
    },
    "liquid-utreexo-windows": {
        "alias": "liquid-utreexo-windows-fleet",
        "provider": "grok",
        "cwd": "C:/Users/Luke",
        "position": 9000.0,
        "dispatch_script": "scripts/dispatch-liquid-utreexo-jobs.py",
    },
    "github-sync-ekulkisnek": {
        "alias": "github-sync-ekulkisnek",
        "provider": "grok",
        "cwd": REDWALLET,
        "position": 8500.0,
        "goal": """Sync ekulkisnek GitHub forks (Goal 4).

Repos/branches:
- github.com/ekulkisnek/BlueWallet branch codex/redwallet-utreexo-quic-sync
- github.com/ekulkisnek/Floresta branch codex/mobile-utreexo-quic-sync
- github.com/ekulkisnek/plain-bitassets branch codex/floresta-utreexo-anchors
- autocode fixes to github.com/ekulkisnek/autocode if fork exists

Push meaningful commits; do NOT force push. Verify `git status -sb` shows no ahead on fork remote.
End with FLEET_DONE and push evidence (git log -1 --oneline per repo).""",
    },
    "gamecube-tcg-falsebound": {
        "alias": "gamecube-tcg-falsebound-fleet",
        "provider": "cursor",
        "fallback_provider": "grok",
        "cwd": "/Users/lukekensik/coding/fbk-tcg-mod",
        "position": 7500.0,
        "dispatch_script": "scripts/dispatch-gamecube-tcg-workers.py",
        "goal": """GameCube Yu-Gi-Oh Falsebound Kingdom → TCG conversion (optional parallel goal).

Project: /Users/lukekensik/coding/fbk-tcg-mod
ROM: ~/Downloads/Yu-Gi-Oh! The Falsebound Kingdom (USA).7z (NKit ISO)
Evidence: state/EVIDENCE.md (6 milestones must reach =ok)

Workers dispatch via dispatch-gamecube-tcg-workers.py:
- gamecube-tcg-rom-analysis (extract + NKit convert)
- gamecube-tcg-battle-mapping (3v3 → TCG plan)
- gamecube-tcg-rules-engine (LP, phases, zones)
- gamecube-tcg-card-mechanics (decks + emulator POC)

Runs in PARALLEL with L1 — do not pause for RedWallet E2E.
End FLEET_DONE only when verify-goal-status.py gamecube-tcg-falsebound is complete.""",
    },
}


def load_status() -> dict:
    script = Path(__file__).resolve().parent / "verify-goal-status.py"
    r = subprocess.run([sys.executable, str(script), "--json"], capture_output=True, text=True)
    if r.returncode not in (0, 1) or not r.stdout.strip():
        return {"all_complete": False, "goals": []}
    return json.loads(r.stdout)


def ensure_chat(store: Store, spec: dict) -> str:
    from autocode import recovery

    goal = spec.get("goal", "")
    alias = spec["alias"]
    provider = spec["provider"]
    fallback = spec.get("fallback_provider", "")
    if fallback and recovery.provider_in_backoff(store, provider):
        if not recovery.provider_in_backoff(store, fallback):
            provider = fallback
    cwd = spec["cwd"]
    chat_id = f"{provider}:goal-fleet:{alias}:{sha(goal or alias)[:8]}"
    chat = Chat(
        id=chat_id,
        provider=provider,
        source=f"{provider}.sqlite" if provider != "cursor" else "cursor.new",
        provider_chat_id=f"goal-{uuid.uuid4().hex[:12]}",
        title=alias,
        cwd=cwd,
        updated_at=now_iso(),
        latest_text=(goal or alias)[:500],
        transcript_hash=sha(goal or alias),
        alias=alias,
        continuation=provider,
    )
    store.upsert_chat(chat, coding_score=20, state="active", objective=goal or alias)
    if goal:
        store.set_goal(chat.id, goal, "user")
    pos = float(spec.get("position", 0))
    store.queue_add(chat.id, pos)
    store.queue_bump_front(chat.id) if pos < 0 else None
    row = store.row("select paused from chats where id=?", (chat.id,))
    if row and int(row["paused"] or 0):
        with store.connect() as con:
            con.execute("update chats set paused=0 where id=?", (chat.id,))
    return chat.id


def main() -> None:
    store = Store()
    sched = Scheduler(store)

    # Coordination defaults
    subprocess.run([sys.executable, "-m", "autocode", "coord", "set-windows-sequential"], check=False)
    subprocess.run([sys.executable, "-m", "autocode", "yolo", "on"], check=False)

    status = load_status()
    incomplete = [g for g in status.get("goals", []) if not g.get("complete")]
    if not incomplete:
        print("All goals complete.")
        return

    print(f"Incomplete goals: {len(incomplete)}")
    dispatched: list[str] = []

    l1_active = any(g["id"] == "l1-e2e-verified" for g in incomplete)
    if l1_active:
        from autocode import coordination
        from autocode.goal_fleets import (
            _l1_loop_running,
            _l1_orchestrator_running,
            pause_duplicate_l1_fleet_chats,
            pause_l1_competitors_no_lock,
        )

        pause_duplicate_l1_fleet_chats(store, sched)
        if (
            not coordination.l1_lock_active()
            and not _l1_orchestrator_running()
            and not _l1_loop_running()
        ):
            coordination.kill_duplicate_l1_processes()
        if not coordination.l1_lock_active() and not _l1_orchestrator_running():
            print("L1 incomplete — pausing non-goal competitors (no lock acquire)")
            pause_l1_competitors_no_lock(store, sched)

    l1_incomplete = any(g["id"] == "l1-e2e-verified" for g in incomplete)
    for g in incomplete:
        gid = g["id"]
        if l1_incomplete and gid not in PARALLEL_WITH_L1_GOAL_IDS and gid != "l1-e2e-verified":
            print(f"SKIP {gid}: Goal 1 (l1-e2e-verified) incomplete — defer until verified")
            continue
        spec = GOAL_FLEETS.get(gid)
        if not spec:
            print(f"SKIP no fleet spec for {gid}")
            continue
        if gid == "l1-e2e-verified":
            if _l1_loop_running() or _l1_orchestrator_running():
                print(f"SKIP {gid}: shell orchestrator active — fleet dispatch deferred")
                continue
            alias = spec["alias"]
            active = store.row(
                """
                select count(*) c from jobs j
                join chats c on c.id=j.chat_id
                where j.status='running' and (c.alias=? or c.alias like ?)
                """,
                (alias, f"%{alias}%"),
            )
            if active and int(active["c"] or 0) > 0:
                print(f"SKIP {gid}: l1 fleet job already running")
                continue
        if spec.get("dispatch_script"):
            script = Path(__file__).resolve().parent / Path(spec["dispatch_script"]).name
            print(f"RUN {script} for {gid}")
            subprocess.run([sys.executable, str(script)], check=False)
            dispatched.append(gid)
            continue
        chat_id = ensure_chat(store, spec)
        row = store.row("select * from chats where id=?", (chat_id,))
        if not row:
            continue
        if sched.has_active_job(chat_id):
            print(f"SKIP {gid}: job already running for {chat_id}")
            continue
        job_id = sched.dispatch(row)
        if job_id:
            print(f"DISPATCHED {gid} -> {job_id}")
            dispatched.append(gid)
        else:
            print(f"FAILED dispatch {gid}")

    print(json.dumps({"dispatched": dispatched, "incomplete": [g["id"] for g in incomplete]}, indent=2))


if __name__ == "__main__":
    main()
