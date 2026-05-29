# Why the Grok watchdog could not auto-unblock AutoCode

Short reference for operators. Code paths are under `~/autocode/autocode/`.

## What the Grok watchdog actually does

- **Entry:** `grok_watchdog.on_daemon_tick()` → `~/bin/autocode-grok-watchdog` (`grok_watchdog.py`).
- **Effect:** Read-only fleet context → Grok narrative → **Signal/Hermes** delivery (`grok_watchdog._fire_pending_locked`).
- **Does not:** Write SQLite queue/jobs, clear `provider_health`, pause/archive chats, or call `scheduler.dispatch`.

Until Phase 1, recommendations were advisory only. `watchdog_executor.process_actions()` applies JSON actions only when `AUTOCODE_WATCHDOG_AUTO=on` and actions are in `state/watchdog-actions.json`.

## Why this fleet stayed blocked (2026-05-28/29)

1. **SI death loop + `needs_luke`** — Five `si-*` self-improvement chats hit `failure_count >= 8`. `remediation.needs_luke()` (`remediation.py`) returns true at max retries, so `scheduler.candidates()` skips them (`failure_count >= recovery.max_failure_count`). Grok jobs failed with `max_turns exceeded` (stderr), not fixable by another SI retry.

2. **Grok provider backoff** — Repeated `provider_error` incremented `provider_health.failure_count` for `grok`. `recovery.provider_in_backoff()` blocked all grok dispatches even for healthy queue items (`scheduler.candidates` line ~278).

3. **Watchdog has no executor hook** — The shell watchdog’s INIT prompt forbids mutating queue/DB. Overlap skips (`Another grok-watchdog run in progress`) reduced successful runs; **19× Grok API 400** meant `===WATCHDOG_JSON===` recommendations were often never persisted for any executor.

4. **RAM / capacity (secondary)** — `scheduler.capacity()` soft-limits slots below ~20% mem free. At ~18% free, yolo already grants `min(configured, 1)` when `cap == 0`; primary blocker was backoff + dead SI queue, not capacity alone.

## What fixes this (Phase 1, in daemon)

| Mechanism | Module | Behavior |
|-----------|--------|----------|
| SI loop breaker | `self_improve.reconcile_stalled_self_improvement()` | `si-*`, failures ≥ 8, last job `max_turns` → `pause_chat` + `queue_archive(..., reason=si_loop_breaker)` |
| Deterministic unblock | `watchdog_executor.process_deterministic_unblock()` | Runs each `scheduler.tick()` after `remediation_pass`; clears grok backoff when SI archives |
| Mem 15–25% floor | `scheduler.capacity()` | At least 1 slot when queue has non-paused work |
| Optional Grok actions | `watchdog_executor.process_actions()` | Requires `AUTOCODE_WATCHDOG_AUTO=on` + allowlisted `watchdog-actions.json` |

Real project goals (`require_fleet_done`, `goals.verify_goal_complete`) are unchanged.
