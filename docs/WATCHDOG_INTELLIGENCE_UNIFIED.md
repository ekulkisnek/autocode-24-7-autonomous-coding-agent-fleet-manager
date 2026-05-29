# Autocode Watchdog Intelligence — Unified Design

**Date:** 2026-05-28  
**Synthesizes:** GROK_WATCHDOG_INTELLIGENCE.md (Angle A), GROK_WATCHDOG_INTEL_B_ACTIONS.md (Angle B), live codebase research, MULTITASK_RESEARCH.md §4-5  
**Status:** Design reference — implementation order in §9

---

## TL;DR

The grok watchdog today is a **read-only fleet narrator**. This document designs it as a **closed-loop intelligence layer** that classifies failures semantically, influences dispatch and retry decisions, and queues concrete fleet mutations — all without fighting the deterministic stack or requiring Luke's attention for routine events.

Three complementary mechanisms work together:

| Mechanism | Latency | How it works |
|-----------|---------|--------------|
| **Metadata hints** (Layer 1) | ~5s | Watchdog writes `gw_*` fields to `metadata_json`; scheduler/recovery read them passively on every tick |
| **Action queue** (Layer 2) | ~30s–2min | Watchdog emits structured JSON; `watchdog_executor` applies via existing Python APIs after `remediation_pass` |
| **Dispatch briefing** (Layer 3) | per-dispatch | Accumulated watchdog notes injected into `build_prompt()` so each job turn starts with better context |

The deterministic stack (runner, recovery, remediation, scheduler) is **unchanged and always wins**. Watchdog influence is subordinate, gated, and fully audited.

---

## 1. What's Wrong With the Current System

The daemon makes all decisions via hard-coded heuristics. These are the specific gaps where LLM judgment would most reduce wasted cycles:

### 1.1 Failure classification is too coarse

`evidence_status` collapses radically different failure modes into the same bucket. `silent_failed` covers: auth wall (needs manual login), rate limit (wait 60s), hung process (kill and retry immediately), network outage (wait and retry), and task genuinely impossible. All get the same static exponential backoff. The right response to each is completely different.

**Impact:** Auth walls keep retrying pointlessly. Rate limits sometimes get killed before they recover. Impossible tasks accumulate retries instead of being escalated.

### 1.2 "Done" detection fires on false positives and rejects valid completions

The regex gate (`COMPLETION_CLAIM_WORDS` + `VERIFICATION_WORDS`) is both too permissive and too strict:
- A job describing "once you deploy, everything will work" gets `worked` because it has "everything" and "work"
- A valid FLEET_DONE JSON wrapped in markdown bold gets rejected
- A job that completes a subtask (not the full objective) reports "done" and trips overdelivery detection

**Impact:** Chats get auto-completed when they shouldn't; others stay in the loop when they've genuinely finished.

### 1.3 Retry strategy doesn't learn

A chat that has failed eight times with the same error signature gets the same backoff curve as its first failure. There's no "this approach isn't working, suggest a pivot" logic. The kill-loop detection only counts kills, not failure patterns across them.

**Impact:** Chats burn hours on an unproductive retry cycle that a single look at the stderr would resolve.

### 1.4 Queue ranking is static

Candidates are dispatched by position (FIFO). A chat at position #3 that's 90% complete and blocked only on a single test sits behind a chat at #2 that's stuck in an auth loop with six failures. Nothing adjusts ordering based on observed forward-progress velocity.

**Impact:** High-confidence work waits behind low-confidence work.

### 1.5 No semantic coordination for parallel sessions

When two grok sessions work in the same repo, the scheduler injects raw `git status` and a list of parallel session chat IDs. There's no understanding of whether their tasks are complementary, conflicting, or redundant. Two sessions can simultaneously refactor the same module in incompatible directions, and neither is told to avoid the other's files.

**Impact:** Merge conflicts, wasted work, silently divergent state.

### 1.6 Overdelivery detection has a hard-coded window

The 3600s window with N=3 worked jobs is the same for a 10-second unit test loop and a 45-minute e2e chain. Fast chats trip it too early; slow chats stay looping long after they've provably finished.

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    DETERMINISTIC STACK                      │
│  scheduler.tick → runner → recovery → remediation_pass      │
│  (unchanged; always authoritative)                          │
└───────────────────┬─────────────────────────────────────────┘
                    │ reads / is influenced by
        ┌───────────┴──────────────────────────┐
        │           WATCHDOG INTELLIGENCE      │
        │                                      │
        │  ┌─────────────────────────────────┐ │
        │  │  LAYER 1: Metadata Hints        │ │
        │  │  Fast path, ~5s, no grok call   │ │
        │  │  gw_failure_class               │ │
        │  │  gw_suggested_timeout           │ │
        │  │  gw_candidate_priority_boost    │ │
        │  │  gw_completion_override         │ │
        │  └─────────────────────────────────┘ │
        │                                      │
        │  ┌─────────────────────────────────┐ │
        │  │  LAYER 2: Action Queue          │ │
        │  │  Full grok analysis, ~2min      │ │
        │  │  watchdog-actions.json          │ │
        │  │  watchdog_executor (daemon)     │ │
        │  └─────────────────────────────────┘ │
        │                                      │
        │  ┌─────────────────────────────────┐ │
        │  │  LAYER 3: Dispatch Briefing     │ │
        │  │  Per-dispatch, ~0s             │ │
        │  │  injected into build_prompt()   │ │
        │  └─────────────────────────────────┘ │
        └──────────────────────────────────────┘
                    │
        ┌───────────┴──────────────────────────┐
        │           FLEET CONTROL PLANE        │
        │  fleet_actions.py (shared)           │
        │  MCP tools / HTTP API (future)       │
        └──────────────────────────────────────┘
```

**Separation of powers (critical):**

| Component | Writes SQLite / kills PIDs | Notes |
|-----------|---------------------------|-------|
| `~/bin/autocode-grok-watchdog` | **No** — SELECT + log + JSON file append only | Grok cannot call autocode CLI |
| Layer 1 fast-path classifier | **Yes** — `metadata_json` only | No job kills, no queue changes |
| `watchdog_executor.py` (daemon) | **Yes** — via `Store` / `JobRunner` / `goals` / `recovery` | After `remediation_pass`, before dispatch |
| `~/bin/autocode-watchdog` | **Yes** — kills PIDs, `autocode drive` | Deterministic; unchanged |

---

## 3. Layer 1 — Metadata Hints (Fast-Path Classification)

The lowest-risk influence mechanism: watchdog writes structured fields into `chats.metadata_json`. Scheduler, runner, and recovery read these passively — they don't block on watchdog and don't call it. Watchdog is purely advisory at this layer.

### 3.1 Hint fields

```json
{
  "gw_failure_class":           "auth_wall",
  "gw_failure_note":            "Cursor terminal idle 900s; last output asks for login",
  "gw_suggested_timeout":       1800,
  "gw_candidate_priority_boost": -2,
  "gw_completion_override":     "reject",
  "gw_completion_reason":       "claims done but output describes a future deploy step, not completed work",
  "gw_provider_hint":           "grok",
  "gw_updated_at":              "2026-05-28T17:00:00Z"
}
```

### 3.2 Failure classes and their dispatch effects

| `gw_failure_class` | Meaning | Recovery effect |
|--------------------|---------|-----------------|
| `auth_wall` | Login required; retries will not help | Skip backoff; set `requires_human=true` |
| `rate_limit` | API rate limit; fixed wait will resolve | Short fixed wait (60s), no failure_count bump |
| `hung_process` | Process alive, no output, no progress | Immediate kill + retry; no backoff |
| `compilation_error` | Build fails; needs code fix | Normal backoff; inject error in next prompt |
| `env_not_ready` | Docker/service not running | Short wait, single retry, then needs_luke |
| `impossible` | Task cannot be completed as stated | Trigger `decompose_impossible_goal` early |
| `overdelivered` | Task done; loop is churn | Trigger `mark_goal_complete` |

**Where to read this:** `recovery.handle_job_failure()` checks `gw_failure_class` before computing backoff. `scheduler.candidates()` skips `auth_wall` chats (treats as if paused). `runner._refresh_one()` uses `gw_suggested_timeout` as the stall floor.

### 3.3 Completion override

`gw_completion_override` can be `"reject"` or `"confirm"`:

- `"reject"` — `goals.verify_goal_complete()` returns `(False, gw_completion_reason)` regardless of regex match. Prevents false positives from being marked done.
- `"confirm"` — `goals.assess_for_completion()` returns `done=True` when watchdog has verified evidence even if the regex assessment would say `active`. Used when FLEET_DONE is present but the format was rejected by the parser.

This field expires after 30 minutes (watchdog re-assesses each run); stale overrides are ignored.

### 3.4 Priority boost

`gw_candidate_priority_boost` is a float added to effective queue position in `candidates()`. Negative means "dispatch sooner". The scheduler sums the stored position and this boost for ranking only — the position column in the queue table is not changed.

```python
# In candidates():
effective_pos = row["position"] + float(meta.get("gw_candidate_priority_boost", 0))
```

Watchdog sets boost based on observed forward-progress velocity:
- Chat made measurable progress last 3 turns → boost -2
- Chat looping with same evidence → boost +3  
- Chat is `auth_wall` → boost +99 (effectively excluded)

### 3.5 How hints are written

The fast-path classifier runs in `autocode-grok-watchdog` **without** calling grok — it reads the last 600 chars of stderr and the last 3 evidence_status records from the DB, runs a local pattern match, and writes the result. Total latency: ~5s. This fires on every `job_failed` and `job_killed` event.

The full grok analysis updates all hint fields (including completion_override and provider_hint) on its normal trigger cycle. The fast-path result is a bootstrap that the full analysis refines.

---

## 4. Layer 2 — Action Queue (Gated Execution)

### 4.1 How it works

The grok watchdog appends structured actions to `state/watchdog-actions.json` inside a `===WATCHDOG_JSON===` block in its output. A new `watchdog_executor.py` module drains this queue each daemon tick, applies actions via existing Python APIs, and records every decision to the audit log.

```
autocode-grok-watchdog (--trigger …)
        │  ===WATCHDOG_JSON=== { "actions": [...] }
        ▼
state/watchdog-actions.json  ← pending | applied | rejected
        │
        ▼
watchdog_executor.process_actions(store, scheduler)
  ├─ allowlist check
  ├─ expiry check (default TTL: 15min)
  ├─ idempotency check (1h window)
  ├─ needs_luke gate (blocks everything)
  ├─ per-tick cap (AUTOCODE_WATCHDOG_MAX_PER_TICK=3)
  ├─ per-hour cap (AUTOCODE_WATCHDOG_MAX_PER_HOUR=20)
  └─ apply via fleet_actions.py
        │
        ▼
  goals · recovery · remediation · JobRunner · Store
```

### 4.2 Action schema

```json
{
  "id": "wa-20260528-cursor-auth-001",
  "created_at": "2026-05-28T18:04:12Z",
  "expires_at": "2026-05-28T18:19:12Z",
  "source": "grok_watchdog",
  "trigger": "job_failed,running_external_idle",
  "action": "retry_with_prompt",
  "chat_id": "cursor:cli:sync-handoff",
  "job_id": "job-abc123",
  "params": { ... },
  "confidence": 0.82,
  "requires_human": false,
  "idempotency_key": "sha256(chat_id|action|job_id)"
}
```

Human-gated actions stay in `pending` until Hermes/Signal reply or MCP `autocode_apply_action`.

### 4.3 Daemon tick insertion point

```python
# scheduler.tick() — after remediation, before reconcile/dispatch
auto_fix = remediation.remediation_pass(self.store)
action_result = watchdog_executor.process_actions(self.store, self)  # NEW
auto_fix["watchdog_actions"] = action_result
# then: reconcile_killed_chats → discover → dispatch
```

Rationale: remediation may already kill/retry the same job. The executor checks whether `remediation_attempts` incremented in this tick and skips `kill_job` / `retry_with_prompt` for the same chat. Running before dispatch means `complete_chat` and `change_goal` affect this tick's candidate set.

### 4.4 Action vocabulary

**`complete_chat`**
- Target: `goals.mark_goal_complete(store, chat_id, reason, kill_running=True, archive=True)`
- Auto-applies when: `detect_overdelivery()` confirms, OR `decompose_impossible_goal` has already fired, OR gw_completion_override="confirm" with high confidence
- Guards: re-runs `verify_goal_complete()` unless `force_reason="impossible_handoff"`; blocked by `needs_luke()`

**`retry_with_prompt`**
- Target: set `metadata_json.remediation_prompt_prefix` + call `recovery.schedule_retry(immediate=True)` + `store.queue_bump_front()`
- Does NOT call grok/codex subprocess directly — next tick dispatch builds the full plan
- Auto-applies when: failure_kind is known, failure is not `auth_wall`, `remediation_attempts < DEFAULT_MAX_REMEDIATION_ATTEMPTS`
- Example: cursor bridge stall → inject "REMEDIATION: verify launchctl kickstart, document handoff artifact"

**`dispatch_provider`**
- Target: set `metadata_json.provider_hint` (soft preference); optionally update `chats.provider` column; call `store.queue_bump_front()`
- Scheduler reads `provider_hint` in `fallback_plan()` — if hint disagrees with computed fallback and confidence ≥ threshold, use hint
- Auto-applies Phase 2 only; never when `provider_in_backoff` for the hinted provider

**`kill_job`**
- Target: `JobRunner(store).kill_chat_jobs(chat_id, reason=f"watchdog_{reason}")`
- Auto-applies only when evidence is `running_silent` / `running_external_idle` past threshold AND `attempt_silent_remediation` would have killed anyway — watchdog accelerates, not bypasses
- Never mass-kill fleet (single job scope only)

**`change_goal`**
- Target: `store.set_goal(chat_id, objective, source="watchdog_approved")`
- **Always `requires_human: true`** — Signal reply or MCP `autocode_apply_action` required
- Bumps queue, resets `done=0`, supersedes prior active goal

**`spawn_workstream`** *(Phase 2)*
- Target: `store.add_priority(...)` + `cli.squad_launch` pattern → new `squad:<priority_id>:<lane>` chats
- Near-term: maps to existing squad infrastructure (max 4 lanes)
- Future: dedicated `workstreams(id, parent_chat_id, provider, depends_on_json, status)` table with DAG-aware scheduler fill
- `requires_human: true` by default; auto only when parent chat is `complete_chat`-bound and a specific review lane is configured

**`block_completion`** *(new, not in Angle B)*
- Target: set `gw_completion_override="reject"` in metadata_json with reason and expiry
- Stops `verify_goal_complete()` from returning True for this chat until watchdog clears it or re-confirms
- Auto-applies when grok identifies a false positive completion claim in the output
- Lightweight — metadata only, no job state changes

**`reposition_queue`** *(new, lightweight alternative to dispatch_provider)*
- Target: `store.queue_move(chat_id, new_position)` directly
- Used when watchdog wants to deprioritize a stuck chat without changing its provider
- Simpler and less risky than dispatch_provider for pure ordering changes

### 4.5 Safety model

**Master switch:** `AUTOCODE_WATCHDOG_AUTO=off` by default. No actions applied until explicitly enabled.

**Caps:**
- `AUTOCODE_WATCHDOG_MAX_PER_TICK=3` — never more than 3 actions per daemon cycle
- `AUTOCODE_WATCHDOG_MAX_PER_HOUR=20` — rolling window cap
- `AUTOCODE_WATCHDOG_AUTO_THRESHOLD=0.9` — minimum confidence for auto-promote from recommendations

**Fail-closed:**
- Invalid schema → reject, log, continue
- Expired action → reject silently
- `needs_luke()` returns True → reject all actions for that chat regardless of type
- Any SQLite write fails → full rollback; action stays pending

**Idempotency:** same `idempotency_key` within 1 hour → no-op apply; logged as "duplicate_skip"

**Coexistence with autocode-watchdog:** The deterministic watchdog (kill + drive) remains unchanged. If both would act on the same job in the same tick, `remediation_pass` runs first — watchdog_executor skips actions where remediation already acted.

**Audit trail:** every apply / skip / reject → `store.event("watchdog_action", chat_id, action_id=..., result=..., reason=...)` + `state/audit.jsonl`

---

## 5. Layer 3 — Dispatch Briefing (Prompt Enrichment)

When a chat has accumulated watchdog notes (failure history, what's been tried, what's semantically missing), these are injected into `build_prompt()` as a "watchdog briefing" block before the normal prior job context.

```python
# In scheduler._row_with_plan(), after workspace context injection:
gw_notes = meta.get("gw_briefing_notes", "")
if gw_notes:
    data["prior_job_context"] = (
        f"Watchdog analysis (last {meta.get('gw_briefing_turns', '?')} turns):\n"
        f"{gw_notes}\n\n"
        + (data.get("prior_job_context") or "")
    )
```

**What goes in gw_briefing_notes** (watchdog writes this during full analysis):
```
- Auth wall detected on turns 1-2; Cursor login refreshed by user at 16:45
- Turn 3 claims completion but output describes future deploy step, not completed work
- Missing: smoke test against live endpoint returning 200
- Suggested focus: run deploy script, hit endpoint, emit FLEET_DONE only after 200 response
```

This replaces thin "prior job context" (usually just the last turn summary) with semantically-relevant history that the agent can act on immediately.

**TTL:** Notes are refreshed each watchdog run and expire after 60 minutes or when `gw_updated_at` is stale.

---

## 6. Two-Speed Analysis

The current watchdog runs a full grok session on every job event (30s debounce). This is expensive and slow for simple decisions. The right model is two speeds:

**Fast path (~5s, no grok call):**
- Triggered on every `job_failed` / `job_killed` event
- Reads last 600 bytes of stderr + last 3 evidence_status records
- Pattern-matches to `gw_failure_class` (regex + keyword list, no LLM)
- Writes classification to metadata_json immediately
- Updates `gw_suggested_timeout` based on failure class
- Sets `gw_candidate_priority_boost` based on consecutive failures

**Slow path (~2-3 min, full grok session):**
- Triggered by: 3+ consecutive failures, overdelivery suspicion, large priority job completion, manual `--trigger analysis`
- Reads last 4KB of output, full objective, task plan, parallel session state
- Produces: `gw_completion_override`, full `gw_briefing_notes`, action queue entries, `gw_provider_hint`
- Persistent grok session carries context across runs

The fast path ensures failure class is available within seconds (before the next tick retry). The slow path provides richer intelligence when needed without burning analysis time on routine events.

---

## 7. Semantic Multi-Task Coordination

The scheduler currently injects raw `git status` and chat IDs for parallel sessions in the same workspace. This should be extended with semantic coordination from the watchdog.

### 7.1 Parallel session compatibility check

Before dispatching a second job into a workspace with an existing running job, watchdog runs a cached compatibility assessment:

```
Job A: "implement utreexo bridge in lib/node/mod.rs"
Job B: "add logging middleware to lib/node/mod.rs"

→ CONFLICT: both target lib/node/mod.rs
   Suggested: A takes mod.rs, B works in logging.rs (new file)
   Or: make B depend on A (autocode depend add)
```

Assessment is cached per (chat_id_A, chat_id_B, git_HEAD) for 10 minutes. If cached "safe to parallelize", scheduler proceeds. If "conflict", scheduler skips B this tick and adds it back next tick with the conflict note injected into the next dispatch prompt.

### 7.2 Duplicate objective detection

Watchdog periodically scans queued chats for semantic goal overlap ("add tests for the auth module" and "write auth module test coverage" are the same task). When found:
- Emits Signal note: "Possible duplicate: chat A and chat B share objective"
- `requires_human: true` action `change_goal` to consolidate, or manual `autocode done`
- Does not auto-complete either — too risky without Luke's confirmation

### 7.3 Spawn workstream for natural decomposition

When watchdog detects a chat's FLEET_PLAN has subtasks that are clearly independent (different files, different providers, no shared state), it can propose spawning the subtasks as parallel chats with sequential dependencies:

```json
{
  "action": "spawn_workstream",
  "params": {
    "parent_chat_id": "grok:main:feature-x",
    "lanes": [
      { "provider": "grok", "objective": "implement core API", "depends_on": [] },
      { "provider": "codex", "objective": "write tests", "depends_on": ["lane-0"] },
      { "provider": "grok", "objective": "update docs", "depends_on": ["lane-1"] }
    ]
  },
  "requires_human": true
}
```

Human approval creates the chats, wires `chat_dependencies`, and the scheduler DAG manages ordering.

---

## 8. Fleet Control Plane Alignment

Angle B correctly identifies that watchdog_executor and future MCP/HTTP handlers should share the same `fleet_actions.py` layer:

```python
# fleet_actions.py — shared by watchdog_executor, MCP tools, HTTP handlers
def apply_complete_chat(store, chat_id, reason, *, force_reason=None): ...
def apply_retry_with_prompt(store, chat_id, *, prompt_prefix, immediate=True): ...
def apply_dispatch_provider(store, chat_id, provider, *, set_hard=False): ...
def apply_kill_job(store, chat_id, reason): ...
def apply_change_goal(store, chat_id, objective): ...
def apply_spawn_workstream(store, parent_chat_id, lanes): ...
def apply_block_completion(store, chat_id, reason, *, expires_minutes=30): ...
def apply_reposition_queue(store, chat_id, new_position): ...
```

**Design rule:** `watchdog_executor` and MCP handlers call only `fleet_actions.*` functions. Neither calls the other, and neither shells out to `autocode` CLI. This ensures Signal-approved actions and API clients cannot diverge in behavior.

**MCP surface (Phase 1):**

| MCP tool | fleet_actions call |
|----------|-------------------|
| `autocode_complete_chat` | `apply_complete_chat` |
| `autocode_retry_with_prompt` | `apply_retry_with_prompt` |
| `autocode_apply_action` | generic dispatcher by action.id |
| `autocode_reject_action` | moves pending → rejected |
| `autocode_watchdog_status` | reads watchdog-actions.json + last recommendations |

---

## 9. Example Flows

### Flow A — Cursor auth stall (end-to-end)

**Symptoms:** `running_external_idle`, objective mentions cursor auth / bridge; stderr has `CURSOR_BRIDGE_CLOSED`.

**Fast path (within 5s of job_killed):**
- Classifier reads stderr → sets `gw_failure_class = "auth_wall"`, `gw_candidate_priority_boost = +3`
- Scheduler skips this chat next tick (priority_boost effectively deprioritizes it)

**Deterministic path (unchanged):**
1. `attempt_silent_remediation` → `kickstart_my_machines_worker` + `schedule_retry(immediate=True)`
2. If `remediation_attempts >= 2` → `decompose_impossible_goal` → handoff markdown + `mark_goal_complete`

**Watchdog action queue (if deterministic path already fired):**
```json
{
  "action": "retry_with_prompt",
  "chat_id": "cursor:cli:auth-sync",
  "params": {
    "prompt_prefix": "WATCHDOG: Bridge closed. Verify kickstart worked. If Cursor login still required, document the per-chat handoff steps and emit FLEET_DONE with handoff artifact path.",
    "immediate": true,
    "failure_kind": "auth_wall"
  },
  "confidence": 0.82,
  "requires_human": false
}
```

**If 2 remediation attempts exhausted and grok detects impossibility before daemon does:**
```json
{
  "action": "complete_chat",
  "params": { "reason": "impossible bulk API — handoff documented", "force_reason": "impossible_handoff" },
  "confidence": 0.90
}
```
Executor guardrail: only applies if handoff file exists under `state/remediation/cursor-handoff-*.md` OR `decompose_impossible_goal` already fired.

### Flow B — Simplicity overdelivery

**Symptoms:** 6 `worked` jobs; stdout has `txid=...` + `deployment active=true` + repeated FLEET_DONE; `detect_overdelivery` fires.

**Deterministic (already):** `auto_complete_overdelivery` → `mark_goal_complete` + archive.

**Watchdog accelerates:** emits `complete_chat` with confidence 0.96 before the 3-job threshold is reached (it sees txid + deployment_active in job 4, not job 6). Executor validates with `detect_overdelivery()` before applying.

**Optional follow-up (human-gated):**
```json
{
  "action": "spawn_workstream",
  "params": { "lane": "diff-reviewer", "provider": "codex", "objective": "Review whether txid is net-new or duplicate proof" },
  "requires_human": true
}
```

### Flow C — Forward-progress ranking

**Symptoms:** Chat A at position #2 has been `silent_failed` 5 times. Chat C at position #4 has made clean progress last 3 turns.

**Fast path:** Sets `gw_candidate_priority_boost = +5` on A (deprioritize), `-2` on C (promote).

**Effect:** C dispatches before A next tick without changing stored queue positions. No queue mutation needed.

---

## 10. Implementation Phases

### Phase 1 — Metadata hints + fast-path classifier (2-3 days)

**New code:**
- Fast-path classifier in `autocode-grok-watchdog` (no grok call, 5s execution)
- `gw_failure_class` + `gw_suggested_timeout` + `gw_candidate_priority_boost` written to metadata_json
- Read in `recovery.handle_job_failure()` for class-specific backoff
- Read in `scheduler.candidates()` for priority boost
- Read in `runner._refresh_one()` for stall timeout floor
- `gw_completion_override` honored in `goals.verify_goal_complete()`

**What doesn't change:** action queue, watchdog_executor, daemon tick ordering, grok session

**Unlock:** auth walls stop retrying pointlessly; fast chats get priority; false-positive completions blocked.

### Phase 2 — Action queue + watchdog_executor (3-4 days)

**New code:**
- `autocode/watchdog_executor.py` — `process_actions()`, allowlist, caps, audit
- Hook in `scheduler.tick()` after `remediation_pass`
- `state/watchdog-actions.json` schema and parser
- `===WATCHDOG_JSON===` block parsing in `autocode-grok-watchdog`
- `autocode/fleet_actions.py` — shared action functions
- `AUTOCODE_WATCHDOG_AUTO` master switch (default off)

**First actions to enable (low risk):**
- `complete_chat` (gated by `detect_overdelivery`)
- `block_completion` (metadata only)
- `retry_with_prompt` (mirrors existing remediation)
- `reposition_queue` (simple position update)

**Enable later:**
- `dispatch_provider` (Phase 2 gate: never override active backoff)
- `kill_job` (only when remediation would have anyway)

**Unlock:** watchdog can complete overdelivered chats faster; retry with smarter prompts; route away from failing providers.

### Phase 3 — Dispatch briefing + semantic coordination (2-3 days)

**New code:**
- `gw_briefing_notes` written by full grok analysis
- Injected in `scheduler._row_with_plan()` before prior_job_context
- Parallel session compatibility check cache (per git HEAD)
- Semantic workspace collision detection in `_workspace_context()`

**Unlock:** each job turn starts with semantically-relevant history; parallel sessions avoid file conflicts.

### Phase 4 — Multi-task spawning + Fleet Control Plane (1 week)

**New code:**
- `spawn_workstream` action (human-gated initially)
- `workstreams` table in store.py
- DAG-aware candidate selection (multi-parent dependency tracking)
- MCP tools via `autocode web` HTTP server
- `autocode watchdog apply <action-id>` CLI (one-shot drain for testing)

**Unlock:** watchdog can propose decomposing large goals into parallel tracked workstreams; remote control via Claude or other MCP client.

---

## 11. What Watchdog's Grok Session Needs to Become

The current prompt asks Grok to be a **fleet reporter** ("tell me how things are going"). For the intelligence layer to work, the session needs to shift to being a **decision model** with three outputs:

1. **Signal** (unchanged) — ===SIGNAL=== block for human Signal notification
2. **Metadata hints** — `===WATCHDOG_HINTS===` block with `gw_*` fields per chat (fast path writes these without grok; slow path refines them)
3. **Action queue** — `===WATCHDOG_JSON===` block with actions array

The grok session should accumulate a per-chat memory across runs using its persistent session context. Rather than re-assessing from scratch each time, it should maintain a running model: "chat A tried auth kickstart twice, failed both times, handoff doc exists → ready to complete_chat." This is why the persistent session (which already exists) is valuable — it retains trajectory, not just snapshot.

The INIT_PROMPT should be updated to explain all three output blocks and that the session's job is to maintain this per-chat model across runs, not to describe the current state fresh each time.

---

## 12. Implementation Checklist

**Phase 1:**
- [ ] Fast-path classifier in `autocode-grok-watchdog` (no grok; reads stderr + events)
- [ ] Write `gw_failure_class`, `gw_suggested_timeout`, `gw_candidate_priority_boost` to metadata_json
- [ ] Read `gw_failure_class` in `recovery.handle_job_failure()` for class-specific backoff
- [ ] Read `gw_candidate_priority_boost` in `scheduler.candidates()` (effective position sort)
- [ ] Read `gw_suggested_timeout` in `runner._refresh_one()` as stall timeout floor
- [ ] Honor `gw_completion_override` in `goals.verify_goal_complete()`

**Phase 2:**
- [ ] `autocode/watchdog_executor.py` — `process_actions`, `apply_action`, allowlist, caps
- [ ] `autocode/fleet_actions.py` — shared action functions
- [ ] Hook `watchdog_executor.process_actions()` in `scheduler.tick()` after `remediation_pass`
- [ ] `===WATCHDOG_JSON===` parser in `autocode-grok-watchdog`
- [ ] `state/watchdog-actions.json` read/write/rotate
- [ ] `AUTOCODE_WATCHDOG_AUTO` env knob (default off)
- [ ] `AUTOCODE_WATCHDOG_MAX_PER_TICK`, `_PER_HOUR`, `_AUTO_THRESHOLD` knobs
- [ ] Idempotency key checking (1h window)
- [ ] `needs_luke` gate (blocks all actions for that chat)
- [ ] Tests: cursor-auth stall sequence, overdelivery double-confirm, needs_luke blocks apply

**Phase 3:**
- [ ] `gw_briefing_notes` written in full grok analysis pass
- [ ] Inject in `scheduler._row_with_plan()` before prior_job_context
- [ ] Parallel session compatibility check in `_workspace_context()`
- [ ] Compatibility cache (per git HEAD, 10 min TTL)

**Phase 4:**
- [ ] `spawn_workstream` action + `workstreams` table
- [ ] MCP tools via `autocode web` HTTP
- [ ] `autocode watchdog apply <action-id>` CLI subcommand
- [ ] Update INIT_PROMPT for three-output model
- [ ] Update grok session to accumulate per-chat trajectory model

---

*The goal is a watchdog that makes the fleet smarter on its own — not by replacing the deterministic stack, but by seeing what it can't: the semantics of why a job failed, whether a completion claim is real, and whether two parallel sessions are about to step on each other.*
