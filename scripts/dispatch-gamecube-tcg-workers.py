#!/usr/bin/env python3
"""Dispatch parallel GameCube TCG Falsebound conversion workers."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autocode.models import Chat
from autocode.scheduler import Scheduler
from autocode.store import Store
from autocode.util import now_iso, sha

PROJECT = Path("/Users/lukekensik/coding/fbk-tcg-mod")
ROM_ARCHIVE = Path.home() / "Downloads/Yu-Gi-Oh! The Falsebound Kingdom (USA).7z"
NKIT_TOOLING = Path("/Users/lukekensik/coding/gamecubenkit")
EVIDENCE = PROJECT / "state" / "EVIDENCE.md"
GOAL_ID = "gamecube-tcg-falsebound"

WORKER_SPECS: dict[str, dict] = {
    "gamecube-tcg-rom-analysis": {
        "provider": "cursor",
        "fallback_provider": "grok",
        "cwd": str(PROJECT),
        "position": 7000.0,
        "milestones": ("rom_extracted", "iso_converted"),
        "goal_template": """GameCube TCG Falsebound — ROM analysis worker.

Project: {project}
ROM archive: {rom_archive}
NKit tooling: {nkit_tooling}
Evidence file: {evidence}

TASKS:
1. Run `bash scripts/extract-rom.sh` if rom/ is empty
2. Convert NKit ISO to playable ISO/GCM using {nkit_tooling}/convert_nkit2.sh or convert_nkit_1.4.sh
3. Document file layout in docs/ROM_LAYOUT.md (disc structure, main.dol, file list)
4. Verify Dolphin can boot to title screen (document command + result)
5. Update state/EVIDENCE.md markers: rom_extracted=ok, iso_converted=ok when done

Do NOT distribute ROM files. Personal modding only.
End FLEET_DONE when evidence markers updated and docs committed to fbk-tcg-mod git.""",
    },
    "gamecube-tcg-battle-mapping": {
        "provider": "cursor",
        "fallback_provider": "grok",
        "cwd": str(PROJECT),
        "position": 7010.0,
        "milestones": ("battle_system_mapped", "tcg_design_doc"),
        "goal_template": """GameCube TCG Falsebound — battle system mapping worker.

Project: {project}
Evidence: {evidence}

CONTEXT: Falsebound Kingdom uses simplified 3v3 battles (3 monsters per side, no full TCG rules).
Goal: map current battle flow to enable TCG conversion (life points, phases, 5 monster zones, spell/trap).

TASKS:
1. Read docs/ROM_LAYOUT.md and any extracted assets
2. Research/document battle state machine: init → turn → attack → win/lose
3. Write docs/BATTLE_SYSTEM.md (current mechanics, data structures, known addresses if found)
4. Write docs/TCG_CONVERSION_PLAN.md: feasibility, mod vs reimplementation, phased milestones
5. Update evidence: battle_system_mapped=ok, tcg_design_doc=ok

Use Dolphin memory maps / community docs where available. No ROM redistribution.
End FLEET_DONE when both docs exist and evidence updated.""",
    },
    "gamecube-tcg-rules-engine": {
        "provider": "cursor",
        "fallback_provider": "grok",
        "cwd": str(PROJECT),
        "position": 7020.0,
        "milestones": ("rules_engine_scaffold",),
        "goal_template": """GameCube TCG Falsebound — TCG rules engine scaffold.

Project: {project}
Read first: docs/TCG_CONVERSION_PLAN.md, docs/BATTLE_SYSTEM.md

Build a language-agnostic or Python TCG rules engine scaffold under src/tcg/:
- Life points (8000 default)
- Phases: draw, standby, main, battle, end
- Zones: monster (5), spell/trap (5), field, graveyard, deck, extra deck
- Turn state machine with legal action validation
- Unit tests for basic flow (summon, attack, LP damage)

This scaffold drives future ROM hook integration or standalone battle reimplementation.
Update evidence: rules_engine_scaffold=ok
End FLEET_DONE when tests pass and evidence updated.""",
    },
    "gamecube-tcg-card-mechanics": {
        "provider": "cursor",
        "fallback_provider": "grok",
        "cwd": str(PROJECT),
        "position": 7030.0,
        "milestones": ("emulator_poc",),
        "goal_template": """GameCube TCG Falsebound — card/monster mechanics + emulator POC.

Project: {project}
Read: docs/TCG_CONVERSION_PLAN.md, src/tcg/

TASKS:
1. Define card data schema (JSON/YAML): id, name, type, atk, def, level, effect text
2. Create data/cards/starter_deck.json with representative Falsebound monsters remapped as TCG cards
3. Wire rules engine to load decks and run one simulated duel (stdout log)
4. Document Dolphin POC path: what would need patching for in-game TCG (docs/EMULATOR_POC.md)
5. If feasible, create minimal gecko code hook stub or document why not yet
6. Update evidence: emulator_poc=ok when simulated duel runs OR POC doc complete with next steps

End FLEET_DONE when evidence updated.""",
    },
    "gamecube-tcg-fleet-supervisor": {
        "provider": "cursor",
        "fallback_provider": "grok",
        "cwd": str(PROJECT),
        "position": 6990.0,
        "milestones": tuple(),
        "goal_template": """GameCube TCG Falsebound — fleet supervisor.

Project: {project}
Evidence: {evidence}
Goal ID: {goal_id}

1. Run: PYTHONPATH=~/autocode python3 ~/autocode/scripts/verify-goal-status.py {goal_id}
2. Read state/EVIDENCE.md — list incomplete milestones
3. Dispatch or unblock the right worker (re-run dispatch-gamecube-tcg-workers.py if needed)
4. Fix cross-worker blockers (missing NKit convert, git, tooling)
5. Update docs/AUTOCODE_LOOP.md if process changes

Do NOT mark FLEET_DONE until verify-goal-status shows gamecube-tcg-falsebound complete.
If blocked on human gate (.NET 6, ROM legal), document in state/BLOCKERS.md.""",
    },
}

MILESTONE_RE = re.compile(r"(\w+)=ok")


def load_goal_status() -> dict:
    script = Path(__file__).resolve().parent / "verify-goal-status.py"
    r = subprocess.run(
        [sys.executable, str(script), "--json"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if r.stdout.strip():
        data = json.loads(r.stdout)
        for g in data.get("goals", []):
            if g.get("id") == GOAL_ID:
                return g
    return {"id": GOAL_ID, "complete": False, "pct": 0, "milestones": {}}


def incomplete_milestones() -> list[str]:
    goal = load_goal_status()
    ms = goal.get("milestones") or {}
    return [k for k, v in ms.items() if not v]


def workers_for_milestones(missing: list[str], *, dispatch_all: bool) -> list[str]:
    if dispatch_all or not missing:
        return list(WORKER_SPECS.keys())
    aliases: list[str] = []
    for alias, spec in WORKER_SPECS.items():
        if alias == "gamecube-tcg-fleet-supervisor":
            continue
        if any(m in missing for m in spec.get("milestones", ())):
            aliases.append(alias)
    if aliases:
        aliases.append("gamecube-tcg-fleet-supervisor")
    elif not missing:
        return []
    else:
        aliases.append("gamecube-tcg-fleet-supervisor")
    return aliases


def pick_provider(store: Store, spec: dict) -> str:
    from autocode import recovery

    primary = spec["provider"]
    fallback = spec.get("fallback_provider", "")
    if recovery.provider_in_backoff(store, primary) and fallback:
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
    chat_id = f"{provider}:gamecube-worker:{alias}:{sha(goal)[:8]}"
    chat = Chat(
        id=chat_id,
        provider=provider,
        source=f"{provider}.sqlite" if provider != "cursor" else "cursor.new",
        provider_chat_id=f"gc-{uuid.uuid4().hex[:12]}",
        title=alias,
        cwd=spec["cwd"],
        updated_at=now_iso(),
        latest_text=goal[:500],
        transcript_hash=sha(goal),
        alias=alias,
        continuation=provider,
    )
    store.upsert_chat(chat, coding_score=12, state="active", objective=goal)
    store.set_goal(chat.id, goal, "user")
    store.queue_add(chat.id, float(spec.get("position", 0)))
    row = store.row("select paused from chats where id=?", (chat.id,))
    if row and int(row["paused"] or 0):
        with store.connect() as con:
            con.execute("update chats set paused=0 where id=?", (chat.id,))
    return chat.id


def main() -> None:
    parser = argparse.ArgumentParser(description="Dispatch GameCube TCG Falsebound workers")
    parser.add_argument("--all", action="store_true", help="Dispatch all workers")
    parser.add_argument("--one", action="store_true", help="Dispatch at most one worker")
    args = parser.parse_args()

    goal = load_goal_status()
    if goal.get("complete"):
        print(f"{GOAL_ID} complete — no workers dispatched.")
        return

    missing = incomplete_milestones()
    aliases = workers_for_milestones(missing, dispatch_all=args.all)
    if not aliases:
        aliases = ["gamecube-tcg-fleet-supervisor"]

    store = Store()
    sched = Scheduler(store)
    coord = sched.coordination_snapshot()
    mac_can_take = bool(coord.get("mac_can_take_more"))
    max_workers = 2 if not args.all else len(aliases)
    dispatched: list[dict] = []
    count = 0

    context = {
        "project": str(PROJECT),
        "rom_archive": str(ROM_ARCHIVE),
        "nkit_tooling": str(NKIT_TOOLING),
        "evidence": str(EVIDENCE),
        "goal_id": GOAL_ID,
    }

    for alias in aliases:
        if alias_has_running_job(store, alias):
            print(f"SKIP {alias}: job running")
            continue
        spec = WORKER_SPECS[alias]
        goal_text = spec["goal_template"].format(**context)
        chat_id = ensure_worker(store, alias, spec, goal_text)
        row = store.row("select * from chats where id=?", (chat_id,))
        if sched.has_active_job(chat_id):
            print(f"SKIP {alias}: active job")
            continue
        if count >= max_workers and not mac_can_take:
            print(f"QUEUE {alias}: mac at capacity")
            dispatched.append({"alias": alias, "job_id": None, "queued": True})
            continue
        if count >= max_workers:
            print(f"QUEUE {alias}: worker cap ({max_workers})")
            dispatched.append({"alias": alias, "job_id": None, "queued": True})
            continue
        job_id = sched.dispatch(row)
        if job_id:
            print(f"DISPATCHED {alias} -> {job_id}")
            dispatched.append({"alias": alias, "job_id": job_id})
            count += 1
        else:
            print(f"QUEUE {alias}: dispatch deferred")
            dispatched.append({"alias": alias, "job_id": None, "queued": True})
        if args.one and count >= 1:
            break

    print(
        json.dumps(
            {
                "goal_id": GOAL_ID,
                "goal_pct": goal.get("pct"),
                "missing_milestones": missing,
                "dispatched": dispatched,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
