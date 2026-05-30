# Goal Fleets

## Operating model: autocode only

All four goals are driven **only** by the autocode daemon (`goal_fleets.tick` every ~90s + `run-l1-e2e-until-verified.sh`). Do **not** start parallel Cursor parent agents, manual `dispatch-goal-fleets.py` runs, or duplicate L1 orchestrators outside autocode. Cursor/Grok fleet jobs fix blockers (iOS command server, BitAssets RPC); the shell loop owns Detox/orchestrator execution.

Autocode goal-driven loops for the four final goals. Run status check:

```bash
python3 scripts/verify-goal-status.py
python3 scripts/verify-goal-status.py --json
```

See also: [AUTOCODE_GOAL_DRIVING.md](AUTOCODE_GOAL_DRIVING.md), [CURSOR_GOAL_SUPERVISOR.md](CURSOR_GOAL_SUPERVISOR.md)

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
| `liquid-utreexo-windows` | Mac signet probes + liquid chat progress | `dispatch-liquid-utreexo-jobs.py` (grok on windows-main) |
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

## L1 send completion checklist

Use this when Goal 1 (`l1-e2e-verified`) is incomplete. Autocode runs these automatically; manual runs should follow the same order.

### Preflight (before each attempt)

1. **florestad Electrum** — `127.0.0.1:60101` reachable; height within ~3 blocks of docker mainchain
2. **Docker mainchain** — `docker compose -f local-dev/docker-compose.local-minimal.yml ps mainchain` shows Up
3. **Metro** — `:8081/status` (orchestrator starts if missing)
4. **adb** — Android `0A201JECB03306` in `device` state
5. **Detox simulator** — boot `FC7DDD6B-DFCB-432A-98CE-48C453E6EF48` (iPhone 16e-Detox)
6. **Autocode preflight script** — `scripts/l1-e2e-autocode-preflight.sh`

### Orchestrator env (simulator path — LiPhone unplugged)

```bash
L1_E2E_FORCE_PATH=simulator
L1_E2E_SKIP_PHYSICAL_IOS=1
REDWALLET_SKIP_ANDROID_SEED=1
REDWALLET_SKIP_IOS_SEED=1
L1_E2E_BALANCE_WAIT_MS=120000
L1_E2E_POST_FUND_RELAUNCH=1
L1_E2E_MAX_ATTEMPTS=9999
```

### Send-phase gate

After fund + mine, **before Detox send UI**:

```bash
bash scripts/preflight-electrum-balance.sh <sender_address> <send_sats>
```

Detox spec calls this via `waitForElectrumBalance()` in `l1SignetShared.js`.

### Success evidence

Update `/Volumes/T705/redwallet-logs/L1_VERIFIED_EVIDENCE.md` with:

- Two mainchain txids (simulator→Android, Android→simulator)
- `verify=ok` for both directions
- `detox_exit=0` for both legs

`verify-goal-status.py` marks complete when evidence has ≥2 txid rows + ≥2 verify=ok hits.

### Failure → fix → retry (automatic)

On `goal_incomplete`:

1. `goal_fleets.tick` injects last 50 lines of `detox.log` / `SUMMARY.txt` + latest run dir into fleet prompt
2. Dispatches `l1-sim-detox-fix` (TransactionValue/app-busy) or `l1-electrum-sync-fix` (balance 0) workers in parallel with runner
3. Shell loop `run-l1-e2e-until-verified.sh` retries after 90s (does **not** kill active runner unless stuck >45min)
4. Goals 2–4 deferred until Goal 1 complete (Windows workers not used for L1 sim path)
