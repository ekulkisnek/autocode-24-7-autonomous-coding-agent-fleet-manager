# AutoCode ops improvements

**Dated:** 2026-05-28 (health pass refreshed 2026-05-29; fixes landed 2026-05-29)

## Fixed 2026-05-29 (Phase 1)

- **SI loop breaker** — `self_improve.reconcile_stalled_self_improvement()` archives `si-*` chats at max retries with `max_turns exceeded`; clears grok provider backoff via `watchdog_executor.process_deterministic_unblock()` each tick.
- **Deterministic unblock** — Runs in `scheduler.tick()` after remediation; no Grok / `AUTOCODE_WATCHDOG_AUTO` required.
- **RAM 15–25%** — `scheduler.capacity()` keeps ≥1 slot when queue has work and mem free is 15–25%.
- **Docs** — `docs/WATCHDOG_WHY_NO_AUTO_UNBLOCK.md` explains why Signal-only watchdog could not unblock.

## Issues found today (historical / monitor)

- **RAM capacity floor** — Older `capacity.log` lines referenced a 1.5GB floor; current gate uses `memory_pressure` % with soft curve + yolo min slot.
- **Grok `max_turns` loop** — Was: 5 stalled `si-*` items at 8 failures (`NEEDS_LUKE`). Now auto-archived by loop breaker when stderr shows `max_turns exceeded`.
- **Codex missing** — `autocode doctor` flags `missing codex` (other providers OK).
- **Cursor auth / dispatch** — Multiple cursor fleet-check chats active; some user-paused; watch for login-loop jobs burning turns without stdout.
- **Watchdog concurrency** — Frequent `Another grok-watchdog run is in progress — skipping` (overlap on ~30s daemon_tick cadence).
- **Watchdog Grok rejects** — 19× `Grok response rejected` (400 Bad Request / CLI error text leaked into response) in current log; Hermes still delivers on successful runs (~1349 ok deliveries).
- **High system load** — Dashboard load ~6.1 with 19% mem free; may amplify RAM gate and slow job starts.

## Recommended next engineering work

1. **Watchdog Phase 1 JSON** — Emit `===WATCHDOG_JSON===` recommendations to `state/watchdog-recommendations.json`; daemon consumes with `AUTOCODE_WATCHDOG_AUTO` guardrails (see `GROK_WATCHDOG_INTELLIGENCE.md`).
2. **Fleet Control Plane** — Unify deterministic tick + watchdog signals + remediation (`MULTITASK_RESEARCH.md`); reduce duplicate Grok SI queue items.
3. **Fix SI failure root causes** — Raise or tune `max_turns` for grok-build SI jobs; fix `assess-output-state` strictness causing `goal_incomplete`; backoff when stderr is only `max_turns exceeded`.
4. **RAM gate tuning** — Revisit 1.5GB floor vs actual free memory metric; log when queue blocked solely by capacity.
5. **Watchdog debounce / lock** — Extend coalesce window or skip tick-trigger when run &gt;25s old to cut skip spam and API 400s.
6. **Install / wire Codex** — Satisfy doctor so codex quota and dispatch are available as fallback.

## Operational tips for Luke

- **Clear stalled SI queue** when max retries exhausted: `autocode queue remove` or reprioritize after code fix — otherwise daemon keeps re-attempting dead letters.
- **Check capacity before blaming providers:** `tail ~/autocode/logs/capacity.log` and dashboard mem line.
- **Watchdog smoke test:** `~/bin/autocode-grok-watchdog --dry-run --trigger test` (includes `fleet_report` block).
- **Signal inbox:** Successful runs end with `Hermes delivery: hermes send ok`; pending file `~/autocode/state/grok-watchdog-pending.json` should have empty `reasons` when idle.
- **Wiki work** — Pseudodoxology wiki lives under `patreon-transcribe/pseudodoxology-wiki/`; commit wiki + SCHEMA pages, exclude `.env` and large `tmp/extracts` unless intentionally versioned.
