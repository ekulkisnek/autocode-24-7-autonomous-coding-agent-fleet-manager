# Goal Fleets

## Operating model: autocode only

All four goals are driven **only** by the autocode daemon (`goal_fleets.tick` every ~90s + `run-l1-e2e-until-verified.sh`). Do **not** start parallel Cursor parent agents, manual `dispatch-goal-fleets.py` runs, or duplicate L1 orchestrators outside autocode. Cursor/Grok fleet jobs fix blockers (iOS command server, BitAssets RPC); the shell loop owns Detox/orchestrator execution.

Autocode goal-driven loops for the four final goals. Run status check:

```bash
python3 scripts/verify-goal-status.py
python3 scripts/verify-goal-status.py --json
```

See also: [AUTOCODE_GOAL_DRIVING.md](AUTOCODE_GOAL_DRIVING.md)

## Autonomous loop (no manual intervention)

The daemon scheduler calls `goal_fleets.tick()` every ~90s (`AUTOCODE_GOAL_TICK_INTERVAL`).

Each tick:

1. Runs `scripts/verify-goal-status.py --json`
2. Re-opens fleet chats falsely marked `done`
3. Pauses non-goal Mac fleets during L1; dedupes to one L1 fleet chat
4. Spawns `run-l1-e2e-until-verified.sh` when L1 incomplete and idle
5. Dispatches incomplete goal fleets when no active fleet job

Goal fleet chats cannot mark `done` until verify-goal-status passes (`autocode/goal_fleets.py` + `runner.py`).

Manual dispatch (also enables yolo):

```bash
python3 scripts/dispatch-goal-fleets.py
```

## Goals

| ID | Success criteria | Fleet |
|----|------------------|-------|
| `l1-e2e-verified` | `L1_VERIFIED_EVIDENCE.md` has 2 txids + verify=ok + detox_exit=0 | `l1-e2e-until-verified` cursor fleet (+ grok fallback) + shell loop |
| `windows-remote-health` | ping + bench OK + remote job `worked` | `windows-remote-health` grok chat |
| `liquid-utreexo-windows` | Mac signet probes + liquid chat progress | `dispatch-liquid-utreexo-jobs.py` |
| `github-sync-ekulkisnek` | ekulkisnek forks pushed, no ahead | `github-sync-ekulkisnek` grok chat |

## L1 path (ONE at a time)

`scripts/pick-l1-e2e-path.sh` selects:

- **physical** (preferred): iPhone + Android + BitAssets RPC + Electrum all reachable
- **simulator** (fallback): iOS simulator + Android when physical blocked (e.g. LiPhone BitAssets RPC Host down)

`scripts/run-l1-e2e-until-verified.sh` runs the chosen path in a retry loop until verify passes.

## Coordination

- L1 lock: `scripts/l1-e2e-lock.sh` — lock pid is bash `$$`, not Python subprocess
- Mac pause during L1: `pause_l1_competitors_no_lock` (does NOT acquire lock; does NOT kill lock holder tree)
- Windows sequential: `python3 -m autocode coord set-windows-sequential`

```bash
python3 -m autocode coord l1-status
python3 -m autocode coord pause-l1-competitors
python3 -m autocode coord release-l1
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| grok `provider_error` OAuth URL | Grok login required; autocode falls back to cursor for goal-fleet |
| orchestrator SIGKILL exit 137 | Was lock pid bug — ensure redwallet `l1-e2e-lock.sh` uses bash pid |
| simulator + physical concurrent | Autocode kills conflicting path based on lock holder |
| competing liquid/patreon jobs | Paused automatically during L1 |
| BitAssets RPC down | Simulator path auto-selected |

## Hard human gates

- Grok OAuth login on Mac/Windows (if probe shows auth errors)
- Cursor API billing / rate limits (provider_error with API message)
- Physical devices unplugged (simulator fallback may still progress)
