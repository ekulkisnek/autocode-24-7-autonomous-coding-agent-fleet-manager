# Goal Fleets

Autocode goal-driven loops for the four final goals. Run status check:

```bash
python3 scripts/verify-goal-status.py
python3 scripts/verify-goal-status.py --json
```

Dispatch incomplete goals (daemon + yolo should be on):

```bash
python3 scripts/dispatch-goal-fleets.py
```

## Goals

| ID | Success criteria | Fleet |
|----|------------------|-------|
| `l1-e2e-verified` | `L1_VERIFIED_EVIDENCE.md` has 2 txids + verify=ok + detox_exit=0 | `l1-e2e-until-verified` grok chat |
| `windows-remote-health` | ping + bench OK + remote job `worked` | `windows-remote-health` grok chat |
| `liquid-utreexo-windows` | Mac signet probes + liquid chat progress | `dispatch-liquid-utreexo-jobs.py` |
| `github-sync-ekulkisnek` | ekulkisnek forks pushed, no ahead | `github-sync-ekulkisnek` grok chat |

## Coordination

- L1 lock: `scripts/l1-e2e-lock.sh` on all `run-l1-*.sh` in redwallet
- Mac pause during L1: `python3 -m autocode coord pause-l1-competitors`
- Windows sequential: `python3 -m autocode coord set-windows-sequential`

## L1 loop (standalone)

```bash
./scripts/run-l1-e2e-until-verified.sh
```

## Hard human gates

- Grok OAuth login on Windows (if probe shows auth errors)
- Cursor API billing / rate limits (provider_error with API message)
- Physical devices unplugged or adb/devicectl unavailable
