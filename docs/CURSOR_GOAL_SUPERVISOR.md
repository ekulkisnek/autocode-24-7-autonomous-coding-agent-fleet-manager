# Cursor + Autocode goal supervisor

Autocode drives final goals autonomously. This doc explains the **self-healing stack** so you do not need to manually nudge this Cursor chat.

## Three layers (this chat)

| Layer | Mechanism | Cadence | What it does |
|-------|-----------|---------|--------------|
| **Stop hook** | `~/.cursor/hooks/goal-supervisor-stop.sh` | After every agent turn (while goals incomplete) | Re-injects supervisor prompt via `followup_message` |
| **Background loop** | `scripts/cursor-goal-supervisor-loop.sh` | Every **120s (2m)** | Emits `AGENT_LOOP_TICK_GOAL_SUPERVISOR` for monitored shell |
| **Cursor Automation** | Glass Automations UI | Every **2m** (after you save) | Scheduled agent prompt in redwallet workspace |

Underlying autocode infra (daemon, goal_fleets, meta-supervisor) runs independently — see [GOAL_FLEETS.md](./GOAL_FLEETS.md).

## Final goals (verify script)

```bash
python3 ~/autocode/scripts/verify-goal-status.py --json
```

| Goal | Done when |
|------|-----------|
| `l1-e2e-verified` | `L1_VERIFIED_EVIDENCE.md` has 2 mainchain txids + verify=ok |
| `windows-remote-health` | ping + bench + remote worked job |
| `liquid-utreexo-windows` | Mac probes + liquid progress |
| `github-sync-ekulkisnek` | ekulkisnek forks synced |

## Enable

### 1. Stop hook (enabled by default)

Already configured in `~/.cursor/hooks.json`:

```json
{
  "version": 1,
  "hooks": {
    "stop": [
      {
        "command": "hooks/goal-supervisor-stop.sh",
        "loop_limit": 9999,
        "timeout": 120
      }
    ]
  }
}
```

Test manually:

```bash
echo '{}' | ~/.cursor/hooks/goal-supervisor-stop.sh
# {} when all_complete=true; {"followup_message":"..."} when incomplete
```

### 2. Background loop

```bash
bash ~/autocode/scripts/cursor-goal-supervisor-loop.sh &
# PID written to ~/autocode/state/cursor-goal-supervisor-loop.pid
# Default interval: 120s (override with CURSOR_GOAL_SUPERVISOR_INTERVAL)
```

Restart after interval change:

```bash
kill $(cat ~/autocode/state/cursor-goal-supervisor-loop.pid) 2>/dev/null || true
CURSOR_GOAL_SUPERVISOR_INTERVAL=120 bash ~/autocode/scripts/cursor-goal-supervisor-loop.sh &
```

### 3. Cursor Automation (optional cloud schedule)

Open Glass → Automations → create **RedWallet Goal Supervisor** (every **2 minutes**, redwallet workspace).

Or ask Cursor to `open_automation` with the goal-supervisor prefill (120s / `every 2 minutes` schedule).

**If you already have a saved automation at 10m:** edit it in Glass → Automations → change schedule to **every 2 minutes** → Save.

### 4. `/loop` in this chat (manual fallback)

Paste in chat:

```text
/loop 2m Goal supervisor tick: Run python3 ~/autocode/scripts/autocode-infra-supervisor.py --json and python3 ~/autocode/scripts/verify-goal-status.py. If all_complete is false, read latest L1 logs under /Volumes/T705/redwallet-logs/, fix autocode/redwallet blockers, ensure run-l1-e2e-until-verified.sh and daemon running, dispatch python3 ~/autocode/scripts/dispatch-meta-supervisor.py if needed. Keep driving until verify-goal-status shows all_complete=true.
```

## Disable

| Mechanism | How to disable |
|-----------|----------------|
| **Stop hook** | `export CURSOR_GOAL_SUPERVISOR_HOOK=0` (session), or remove the `stop` entry from `~/.cursor/hooks.json` |
| **Background loop** | `kill $(cat ~/autocode/state/cursor-goal-supervisor-loop.pid)` |
| **Cursor Automation** | Disable or delete in Glass → Automations |
| **`/loop`** | Ask agent to stop the loop, or kill the loop shell PID |

All mechanisms auto-stop injecting prompts when `verify-goal-status.py` reports `all_complete=true`.

## What runs without you (autocode daemon)

1. **Daemon** (`autocode.cli daemon run`) — yolo on, scheduler tick every 2s
2. **`goal_fleets.tick`** — verify → infra fix → L1 loop → fleet dispatch → L1 workers → meta-supervisor (every **120s**)
3. **`run-l1-e2e-until-verified.sh`** — 9999 attempts, simulator ↔ Android, 45s retry
4. **`dispatch-l1-goal-workers.py`** — parallel fix workers when Detox idle
5. **`autocode-meta-supervisor`** — Cursor job every **~2min** (or immediately after infra repairs)

## Adaptive supervisor (improving over time)

State file: `~/autocode/state/goal-supervisor-state.json`

Each **120s** tick (goal_fleets + meta-supervisor dispatch):

1. Extracts a **failure signature** from latest Detox logs (e.g. `TransactionValue`, `WalletsList`, `ResetWedge`)
2. Tracks the last 10 signatures and supervisor **attempt count**
3. After **3 consecutive identical signatures**, escalates:
   - Stop-hook / fleet prompts: “prior fix did not work — try a different approach”
   - Meta-supervisor prompt: **ESCALATION** block with alternate-hypothesis instructions
   - `goal_fleets`: temporarily bumps `l1_max_mac_fix_workers` to 2 when cap was lower

The stop hook **reads** this state on every turn (does not increment attempt count — that would fire too often).

## Manual commands

```bash
# Infra check + auto-repair
python3 ~/autocode/scripts/autocode-infra-supervisor.py --json

# Force meta-supervisor Cursor job now
python3 ~/autocode/scripts/dispatch-meta-supervisor.py

# Full goal fleet dispatch
python3 ~/autocode/scripts/dispatch-goal-fleets.py
```

## Files

| Path | Role |
|------|------|
| `~/.cursor/hooks.json` | User-level stop hook config |
| `~/.cursor/hooks/goal-supervisor-stop.sh` | Shell wrapper → Python hook |
| `~/autocode/scripts/cursor-goal-supervisor-stop-hook.py` | verify + infra + adaptive context → followup JSON |
| `~/autocode/scripts/cursor-goal-supervisor-loop.sh` | 2m AGENT_LOOP_TICK emitter |
| `~/autocode/state/cursor-goal-supervisor-loop.pid` | Background loop PID |
| `~/autocode/state/goal-supervisor-state.json` | Failure signature history + attempt count |
| `~/autocode/autocode/goal_supervisor_adaptive.py` | Shared adaptive logic |

## Meta-supervisor behavior

The Cursor job receives:
- Current goal percentages from `verify-goal-status.py`
- Infra supervisor actions (what was already fixed deterministically)
- Last 40 lines of detox.log / SUMMARY.txt / until-verified log
- **ESCALATION** block when the same failure signature repeats 3+ times

It is instructed to:
- Fix autocode + redwallet code for the active blocker
- Avoid duplicate orchestrators during Detox
- Push to ekulkisnek forks
- **Not** mark FLEET_DONE until external verify passes

## Tuning

| Env var | Default | Effect |
|---------|---------|--------|
| `CURSOR_GOAL_SUPERVISOR_HOOK` | `1` | Set `0` to disable stop hook |
| `CURSOR_GOAL_SUPERVISOR_INTERVAL` | **120** | Background loop seconds |
| `AUTOCODE_GOAL_TICK_INTERVAL` | **120** | Seconds between goal_fleets ticks |
| `AUTOCODE_META_SUPERVISOR_INTERVAL` | **120** | Min seconds between meta-supervisor dispatches |
| `L1_E2E_MAX_ATTEMPTS` | 9999 | L1 retry cap |
| `L1_E2E_RETRY_SLEEP` | 45 | Seconds between L1 attempts |

## Human gates (rare)

- Grok OAuth login on Mac/Windows (cursor fallback handles L1)
- Physical device unplugged (simulator fallback active for L1)
