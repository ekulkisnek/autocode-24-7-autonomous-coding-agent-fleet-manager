# Autocode goal driving

Autocode daemon + goal fleets autonomously drive four final goals to completion.
No manual Cursor subagents or parallel orchestrators.

## Loop (every ~90s)

1. `goal_fleets.tick()` runs `scripts/verify-goal-status.py --json`
2. If all complete → stop
3. For incomplete goals:
   - Re-open fleet chats marked `done` while external verify still fails
   - Inject failure context into fleet chat metadata for re-dispatch
   - Pause non-goal Mac fleets during L1 (liquid, patreon, manual l1-* chats)
   - Dedupe to ONE `l1-e2e-until-verified` fleet chat (cursor preferred when grok in backoff)
   - Spawn `scripts/run-l1-e2e-until-verified.sh` if L1 incomplete and no orchestrator running
   - Dispatch idle goal fleets via `scripts/dispatch-goal-fleets.py`

## Workers

| Goal | Mac | Windows |
|------|-----|---------|
| L1 E2E | cursor goal-fleet (grok fallback) + shell orchestrator loop | — |
| Windows health | — | grok on windows-main (sequential) |
| Liquid/Floresta | grok fleet dispatch (Mac orchestrator) | grok jobs via dispatch-liquid-utreexo-jobs.py on windows-main |
| GitHub sync | grok goal-fleet on redwallet | — |

## L1 path selection

`scripts/pick-l1-e2e-path.sh` chooses **one** path:

- **physical** — Android adb + iPhone devicectl + BitAssets RPC (127.0.0.1:6004) + Electrum (60101)
- **simulator** — fallback when physical blocked (e.g. LiPhone BitAssets RPC Host down)

Override: `L1_E2E_FORCE_PATH=physical|simulator`

## No manual intervention model

- Daemon yolo=on keeps dispatching until verify passes
- Fleet chats cannot mark `done` until `verify-goal-status.py` passes external criteria
- `goal_incomplete` / `provider_error` → backoff retry with failure context prefix
- L1 lock uses bash `$$` (not Python subprocess pid) so dedup does not SIGKILL the live orchestrator

## Troubleshooting

| Symptom | Cause | Autocode response |
|---------|-------|-------------------|
| `provider_error` grok OAuth URL in stderr | Grok not logged in | Backoff + cursor fallback for goal-fleet |
| orchestrator exit 137 | Lock pid mismatch killed orchestrator | Fixed: l1-e2e-lock.sh uses bash pid |
| simulator + physical concurrent | Two paths without mutex | pick-l1-e2e-path + kill conflicting path |
| liquid/patreon steal Mac slots | Competing fleets | pause_l1_competitors_no_lock during L1 |
| BitAssets RPC down on phone | Environment blocker | Auto-fallback to simulator path |
| Windows jobs pile up | Parallel remote dispatch | weight_capacity=1 + one job/worker/tick |

## Commands

```bash
python3 scripts/verify-goal-status.py
python3 scripts/dispatch-goal-fleets.py
python3 -m autocode coord l1-status
python3 -m autocode status
```

Force goal tick:

```bash
PYTHONPATH=. python3 -c "from autocode.store import Store; from autocode.scheduler import Scheduler; from autocode import goal_fleets; print(goal_fleets.tick(Store(), Scheduler(Store()), force=True))"
```
