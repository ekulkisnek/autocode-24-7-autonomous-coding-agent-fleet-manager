# Fleet coordination mutexes

## L1 E2E exclusive lock

File: `/Volumes/T705/redwallet-logs/.l1-e2e-lock` (override with `REDWALLET_L1_E2E_LOCK`).

Physical/simulator L1 orchestrators acquire this lock before Detox runs:

```bash
./scripts/l1-e2e-lock.sh acquire /path/to/run-dir physical-bidirectional
```

While held:

- Autocode **blocks Mac dispatch** for liquid, patreon, l1-detox, and run-l1 chats
- `autocode coord pause-l1-competitors` pauses/kills competing Mac jobs
- Duplicate `run-l1-*` / detox processes are killed on acquire

```bash
python3 -m autocode coord l1-status
python3 -m autocode coord pause-l1-competitors
python3 -m autocode coord release-l1
```

## Windows remote sequential dispatch

- `windows-main` should use `weight_capacity=1.0` for one SSH job at a time
- Scheduler dispatches at most **one remote job per worker per tick**
- `scripts/dispatch-liquid-utreexo-jobs.py` waits 30s between dispatches and for slot clearance
- SSH connect timeouts: 30–45s; scp/mkdir: 45–60s (`autocode/remote_ssh.py`)

```bash
python3 -m autocode coord set-windows-sequential
python3 -m autocode worker ping windows-main
python3 -m autocode worker smoke windows-main
```
