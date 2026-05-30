#!/usr/bin/env python3
"""Dispatch parallel Windows Grok jobs for Liquid/Floresta utreexo work."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autocode.models import Chat
from autocode.scheduler import Scheduler
from autocode.store import Store
from autocode.util import now_iso, sha

MAC_TAILSCALE = "100.76.117.106"
MAC_LAN = "192.168.1.236"
WIN_CWD = "C:/Users/Luke"

COMMON = f"""
## Environment (read first)
- Mac signet L1 host Tailscale: {MAC_TAILSCALE} (LAN fallback: {MAC_LAN})
- Mac runs Colima docker stack: drivechain-wallet-dev/local-dev/docker-compose.local-minimal.yml
- Exposed ports on Mac (bind 0.0.0.0 unless noted): mainchain P2P 38333, bitassets RPC 6004, QUIC 6104/udp, Floresta Electrum 60101
- Windows worker: Luke@100.100.179.47, cwd {WIN_CWD}
- You may commit on Windows clones when work is complete.

## Repo sync (run if paths missing)
```powershell
$base = $env:USERPROFILE
if (-not (Test-Path "$base\\redwallet")) {{
  git clone -b codex/redwallet-utreexo-quic-sync https://github.com/ekulkisnek/BlueWallet.git "$base\\redwallet"
}}
if (-not (Test-Path "$base\\drivechain-wallet-dev")) {{ New-Item -ItemType Directory -Path "$base\\drivechain-wallet-dev" | Out-Null }}
if (-not (Test-Path "$base\\drivechain-wallet-dev\\floresta-bitassets")) {{
  git clone -b codex/mobile-utreexo-quic-sync https://github.com/ekulkisnek/Floresta.git "$base\\drivechain-wallet-dev\\floresta-bitassets"
}}
if (-not (Test-Path "$base\\drivechain-wallet-dev\\plain-bitassets")) {{
  git clone -b codex/floresta-utreexo-anchors https://github.com/ekulkisnek/plain-bitassets.git "$base\\drivechain-wallet-dev\\plain-bitassets"
}}
if (-not (Test-Path "$base\\drivechain-wallet-dev\\liquid-simplicity")) {{
  git clone -b codex/floresta-utreexo-anchors https://github.com/ekulkisnek/plain-bitassets.git "$base\\drivechain-wallet-dev\\liquid-simplicity"
}}
# pull latest
foreach ($repo in @('redwallet','drivechain-wallet-dev\\floresta-bitassets','drivechain-wallet-dev\\plain-bitassets','drivechain-wallet-dev\\liquid-simplicity')) {{
  $p = Join-Path $base $repo
  if (Test-Path (Join-Path $p '.git')) {{ git -C $p pull --ff-only 2>$null }}
}}
```

Branches:
- RedWallet: codex/redwallet-utreexo-quic-sync @ github.com/ekulkisnek/BlueWallet
- Floresta: codex/mobile-utreexo-quic-sync @ github.com/ekulkisnek/Floresta
- plain-bitassets + liquid-simplicity: codex/floresta-utreexo-anchors @ github.com/ekulkisnek/plain-bitassets

Utreexo/QUIC context: plain-bitassets exports private_signet_utreexo_anchors + QUIC lite-wallet updates with utreexo proofs; florestad syncs L1 via utreexo P2P and BitAssets wallet via QUIC/RPC; RedWallet embeds floresta-bitassets-wallet native module.
"""

JOBS = [
    {
        "alias": "liquid-floresta-drivechain-wiring",
        "cwd": f"{WIN_CWD}/drivechain-wallet-dev/floresta-bitassets",
        "goal": COMMON
        + """
## Task A: Floresta + drivechain-wallet-dev utreexo/Liquid wiring
Research and implement the smallest correct path for Floresta to sync Liquid sidechain blocks using utreexo.

Focus repos: floresta-bitassets (branch codex/mobile-utreexo-quic-sync), plain-bitassets/liquid-simplicity (codex/floresta-utreexo-anchors).

Deliverables:
1. Wire florestad to discover utreexo anchors from plain-bitassets RPC (private_signet_utreexo_peer_source / private_signet_active_utreexo_anchors) when connecting to Mac signet at """
        + MAC_TAILSCALE
        + """:6004
2. Add/adjust CLI flags or config for remote Mac L1 P2P anchor (38333) and bitassets RPC over Tailscale
3. Document startup command sequence for Windows Floresta → Mac signet L1
4. Run `cargo check -p florestad --features bitassets` and any focused tests you add
5. Commit on Windows if tests pass; push to fork branch codex/mobile-utreexo-quic-sync

End with FLEET_DONE and a short SUMMARY of what works vs TODO.
""",
    },
    {
        "alias": "liquid-redwallet-floresta-client",
        "cwd": f"{WIN_CWD}/redwallet",
        "goal": COMMON
        + """
## Task B: RedWallet Floresta/utreexo client for Liquid sidechain sync
Build on branch codex/redwallet-utreexo-quic-sync.

Focus: blue_modules/BitAssetsWallet.ts, class/wallets/bitassets-wallet.ts, embedded native module bridge, QUIC URL normalization.

Deliverables:
1. Ensure wallet can configure remote Mac bitassets RPC ("""
        + MAC_TAILSCALE
        + """:6004) and QUIC (6104) over Tailscale from Windows dev/testing
2. Surface utreexo sync status in BitAssetsWallet UI (quic connected, utreexo_leaf_hash on UTXOs) — extend if incomplete
3. Add/adjust unit test coverage for Tailscale host normalization (helpers/redwalletSignetEndpoints or BitAssetsWallet.ts)
4. Run `npm run lint` and `npx jest tests/unit/bitassets-wallet.test.ts --runInBand`
5. Commit on Windows if green; push to fork codex/redwallet-utreexo-quic-sync

End with FLEET_DONE and SUMMARY.
""",
    },
    {
        "alias": "liquid-tailscale-connectivity",
        "cwd": f"{WIN_CWD}/drivechain-wallet-dev",
        "goal": COMMON
        + """
## Task C: Tailscale connectivity Mac signet L1 ↔ Windows Floresta
Create scripts/docs for cross-machine signet access.

Deliverables:
1. Create local-dev/scripts/tailscale-signet-connectivity.md documenting:
   - Mac Tailscale """
        + MAC_TAILSCALE
        + """, LAN """
        + MAC_LAN
        + """
   - Required Mac docker ports exposed to Tailscale (6004, 6104/udp, 38333, 60101 if Floresta running)
   - Windows test commands: curl/TCP probes, florestad handshake, bitassets RPC get-blockcount
2. Add local-dev/scripts/tailscale-signet-probe.ps1 (Windows) and tailscale-signet-probe.sh (Mac) that verify connectivity
3. If Mac ports are localhost-only, document the minimal docker-compose change OR ssh tunnel alternative (do NOT modify Mac files unless you can verify via probe)
4. Run probes from Windows against """
        + MAC_TAILSCALE
        + """ and record results in the doc

End with FLEET_DONE, probe output, and connectivity verdict (PASS/FAIL/BLOCKED).
""",
    },
    {
        "alias": "liquid-utreexo-tests-docs",
        "cwd": f"{WIN_CWD}/drivechain-wallet-dev",
        "goal": COMMON
        + """
## Task D: Tests + architecture doc for Liquid utreexo sync path
Deliverables:
1. Write local-dev/docs/LIQUID_UTREEXO_FLORESTA_ARCHITECTURE.md covering:
   - Data flow: Mac L1 signet (mainchain+enforcer) → plain-bitassets sidechain → Floresta utreexo L1 sync + QUIC lite-wallet proofs → RedWallet embedded wallet
   - How Liquid sidechain (liquid-simplicity / liquid-signet-sidechain) fits vs current BitAssets plain-bitassets stack
   - Tailscale topology diagram (ascii or mermaid)
2. Add or extend smoke script local-dev/scripts/floresta-utreexo-tailscale-smoke.ps1 that:
   - Probes Mac """
        + MAC_TAILSCALE
        + """:6004 RPC
   - Optionally runs floresta-bitassets-electrum-smoke-test.sh equivalent steps adapted for remote Mac
3. Identify gaps for true Liquid (Elements) sidechain block sync vs current Drivechain BitAssets sidechain — be explicit about what's implemented vs TODO
4. Commit docs/scripts on Windows; push to appropriate fork branch if repo is a git clone

End with FLEET_DONE and gap list.
""",
    },
]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Dispatch Liquid/Floresta utreexo Windows jobs")
    parser.add_argument(
        "--one",
        action="store_true",
        help="Dispatch only the first job that has no active/running work",
    )
    args = parser.parse_args()

    store = Store()
    sched = Scheduler(store)
    worker = store.row("select * from remote_workers where id='windows-main' and enabled=1")
    if not worker:
        print("ERROR: windows-main worker not found or disabled")
        sys.exit(1)

    dispatched: list[tuple[str, str, str]] = []
    base_pos = 9000.0
    import time

    # Sequential: one Windows job at a time; wait for slot before next dispatch.
    def remote_busy() -> bool:
        row = store.row(
            "select count(*) c from jobs where status='running' and worker_id='windows-main'"
        )
        return int(row["c"] if row else 0) > 0

    def wait_for_remote_slot(max_wait_s: int = 7200) -> bool:
        deadline = time.time() + max_wait_s
        while remote_busy() and time.time() < deadline:
            print("waiting for windows-main slot...")
            time.sleep(30)
        return not remote_busy()

    for index, spec in enumerate(JOBS):
        if args.one:
            active = store.row(
                """
                select count(*) c from jobs j
                join chats c on c.id=j.chat_id
                where j.status in ('running','pending')
                  and c.alias=?
                """,
                (spec["alias"],),
            )
            if active and int(active["c"] or 0) > 0:
                print(f"SKIP {spec['alias']}: already active")
                continue
            done = store.row(
                "select done from chats where alias=? order by updated_at desc limit 1",
                (spec["alias"],),
            )
            if done and int(done["done"] or 0):
                print(f"SKIP {spec['alias']}: chat marked done")
                continue
        if index > 0 and not args.one and not wait_for_remote_slot():
            print(f"SKIP {spec['alias']}: windows-main still busy after timeout")
            continue
        chat_id = f"grok:liquid-utreexo:{spec['alias']}:{sha(spec['goal'])[:8]}"
        chat = Chat(
            id=chat_id,
            provider="grok",
            source="grok.sqlite",
            provider_chat_id=f"liquid-{uuid.uuid4().hex[:12]}",
            title=spec["alias"],
            cwd=spec["cwd"],
            updated_at=now_iso(),
            latest_text=spec["goal"][:500],
            transcript_hash=sha(spec["goal"]),
            alias=spec["alias"],
            continuation="grok",
        )
        store.upsert_chat(chat, coding_score=10, state="active", objective=spec["goal"])
        store.set_goal(chat.id, spec["goal"], "user")
        store.queue_add(chat.id, base_pos + index)
        row = store.row("select * from chats where id=?", (chat.id,))
        job_id = sched.dispatch_remote(row, dict(worker))
        if job_id:
            dispatched.append((spec["alias"], job_id, chat.id))
            print(f"DISPATCHED {spec['alias']} -> {job_id}")
            if args.one:
                break
            time.sleep(30)
        else:
            print(f"FAILED {spec['alias']}")

    print(f"\nTotal dispatched: {len(dispatched)}/{len(JOBS)}")
    for alias, job_id, chat_id in dispatched:
        print(f"  {alias}: job={job_id} chat={chat_id}")


if __name__ == "__main__":
    main()
