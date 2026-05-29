# Grok Watchdog as AutoCode Intelligence Layer

**Date:** 2026-05-28  
**Status:** Research only — no large implementation in this pass.  
**Related:** [MULTITASK_RESEARCH.md](./MULTITASK_RESEARCH.md) (Fleet Control Plane), `~/bin/autocode-grok-watchdog`, `autocode/grok_watchdog.py`, `autocode/fleet_report.py`

---

## Executive summary

AutoCode today splits **execution** (daemon tick, scheduler, recovery, remediation) from **observation** (Grok watchdog → Hermes Signal). The deterministic stack handles ~80% of fleet hygiene; Grok adds narrative synthesis and optional deep investigation. This doc proposes evolving the watchdog from **read-only reporter** into a tiered **Watchdog Brain**: observe → recommend → act (with guardrails), without duplicating or fighting the scheduler.

**Recommended Phase 1:** Structured **recommendations** (`state/watchdog-recommendations.json`) consumed by daemon tick — smarter completion/overdelivery/milestone judgments and richer Signal, still **zero autonomous writes** except pre-approved remediation hooks.

---

## 1. Current state

### 1.1 Two watchdogs, two roles

| Component | Role | Writes fleet? |
|-----------|------|-------------|
| `~/bin/autocode-watchdog` | Kill silent/idle jobs; `autocode drive` restart | **Yes** — kills PIDs, drives chats |
| `~/bin/autocode-grok-watchdog` | Fleet analysis → Hermes Signal | **No** — SELECT/logs only; prompt forbids `drive` |

Grok watchdog is explicitly **advisory**. Deterministic recovery owns stall resolution.

### 1.2 Grok watchdog pipeline (today)

```
Daemon tick / dispatch / job finish / autocode-watchdog
        │
        ▼
autocode.grok_watchdog.request(reason)   ← 30s debounce, coalesce reasons
        │
        ▼
~/bin/autocode-grok-watchdog --trigger <reasons>
        │
        ├─ Collect: status, queue, throughput, goal_verification,
        │           recent_jobs, stuck_patterns, needs_luke (fleet_report)
        │
        ├─ Persistent Grok session (resume; reset if FLEET_DONE stale)
        │
        └─ WATCHDOG_PROMPT → Grok (≤4 tool calls) → ===SIGNAL=== … ===END===
                                    │
                                    ▼
                          hermes send --to signal (≤2000 chars)
```

**Event triggers** (`autocode/grok_watchdog.py`):

| Reason | Source |
|--------|--------|
| `daemon_tick` | Every daemon loop |
| `dispatch`, `prompt_due` | Scheduler before/after dispatch |
| `job_completed`, `job_failed`, `job_killed` | `JobRunner` on finish |
| `tick_cli` | Manual `autocode tick` |
| `fallback` | No run in `AUTOCODE_GROK_WATCHDOG_INTERVAL` (default 900s) |

Fast paths (no Grok): `--stall-check`, `--daily-digest`.

### 1.3 Pre-classified context (deterministic)

Before Grok sees anything, the shell script and `fleet_report.py` compute:

- **`needs_luke_summary()`** — delegates to `remediation.needs_luke()` per queued chat:
  - User paused
  - Remediation exhausted (`DEFAULT_MAX_REMEDIATION_ATTEMPTS=2`, action ≠ `decompose_impossible`)
  - Max retries exhausted (`recovery.max_failure_count`)
  - Chat state `blocked`
- **`collect_stuck_patterns_enriched()`** — job evidence histograms with auto-fix hints:
  - `running_silent` / `running_external_idle` → `[auto-remediation queued]`
  - `worked` ×8+ → `OVERDELIVERY [auto-complete on tick]`
  - `silent_failed` ×3+ → `REPEATED_FAIL`
- **`collect_goal_verification()`** — parses last `FLEET_DONE` JSON from stdout; flags missing marker on `worked` jobs.

Grok is instructed to **echo NEEDS LUKE** unless it finds stronger evidence of a new blocker. Silent/overdelivery/done-queue issues should **not** ask Luke — daemon handles them.

### 1.4 What the scheduler does deterministically (every 2s tick)

```python
# scheduler.tick() — simplified
refresh jobs
reconcile false done / archive verified done
remediation_pass()          # overdelivery auto-complete, silent kill+retry
reconcile_killed_chats()
discover (if due)
dispatch up to capacity() slots (FIFO queue, lease per repo)
```

**Goal completion** (`goals.py` + `policy.py` + `runner.py`):

- Regex/heuristic `assess_output_state()` — FLEET_DONE, FLEET_MILESTONE, hard-requirement gaps, verification words
- `verify_goal_complete()` — min chars + optional `require_fleet_done`
- `detect_overdelivery()` — repeated FLEET_DONE / stable evidence keys (txid, deployment_active, simplicity_0xbe)
- `auto_complete_overdelivery()` in remediation pass
- False completes reopened via `reconcile_false_done_chats`

**Stall/failure** (`recovery.py` + `remediation.py`):

- Exponential backoff retries by failure kind
- Provider fallback after N failures (`scheduler.fallback_plan` — Codex stall → Grok takeover)
- Silent/external_idle: kill after threshold, inject remediation prompt, `schedule_retry(immediate=True)`
- Cursor auth chats: `kickstart_my_machines_worker`; after 2 attempts → `decompose_impossible_goal` (handoff doc + mark complete)

**Scheduling**:

- Global `max_active` (default 5), load/mem/disk caps, yolo single slot
- One lease per repo (`resource` = cwd)
- No per-provider slot caps in scheduler (weights only in `autocode-capacity` reporting)
- `chat_dependencies` block until dependency chat is done

### 1.5 Gap: observation vs control

| Question | Deterministic answer | Grok today | Ideal intelligence |
|----------|---------------------|------------|-------------------|
| Is goal truly done? | Regex + evidence keys | Reads FLEET_DONE in prompt | Semantic match goal ↔ evidence |
| Partial FLEET_MILESTONE? | `should_continue_after_output` | Mentioned in verification | Judge milestone sufficiency vs goal scope |
| Silent_failed root cause? | Retry + fallback provider | Can SELECT/tail logs | Diagnose + suggest targeted retry prompt |
| Over-delivery? | Count + fingerprint keys | Flags in stuck_patterns | Confirm "same proof, new txid" vs new work |
| Impossible goal? | `decompose_impossible_goal` after 2 remediation | Reports handoff doc path | Recommend goal rewrite / human gate |
| Parallel speed-up? | Squad (manual, 4 lanes) | Not involved | Recommend workstreams |
| Queue order / provider? | FIFO + failure backoff | Read-only queue list | Recommend dispatch hints |

Grok **narrates**; scheduler **acts**. No closed loop yet.

---

## 2. Industry patterns (brief survey)

| Pattern | Fit for AutoCode |
|---------|------------------|
| **Heuristics first** (regex, counters, markers) | Already core — keep for latency, cost, auditability |
| **LLM-as-judge** | Grok evaluates goal↔output when heuristics disagree or evidence is ambiguous |
| **Planner / executor split** | Scheduler = executor; Watchdog Brain = planner/reviewer on tick boundary |
| **Supervisor / compliance layer** (OrgAgent, OpenAI Agents guardrails) | Daemon applies caps; watchdog proposes; human gate for destructive ops |
| **Agent-as-judge** (trace-level) | Phase 2+: judge intermediate job turns, not just final stdout |
| **CI triage bots** | Map to `silent_failed` / `provider_error` — classify flake vs real blocker, suggest retry vs fix |

**Design principle:** Fast path = rules; slow path = Grok; **never** let LLM bypass leases, retry caps, or `needs_luke` gates.

---

## 3. Proposed architecture: Watchdog Brain

Three tiers with increasing authority:

```
┌─────────────────────────────────────────────────────────────┐
│  Tier 0 — OBSERVE (today + enrich)                          │
│  Event debounce, fleet_report, goal_verification, Signal    │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│  Tier 1 — RECOMMEND (Phase 1)                                 │
│  Structured JSON: judgments, suggested actions, confidence    │
│  Written to state/watchdog-recommendations.json               │
│  Daemon tick reads; applies only rule-approved actions        │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│  Tier 2 — ACT (Phase 2–3, guardrailed)                        │
│  state/watchdog-actions.json queue → daemon executor          │
│  Cap: N actions/hour, no kill without retry budget, audit log │
└───────────────────────────────────────────────────────────────┘
```

### 3.1 Tier 0 — Observe (current + small enrichments)

Keep read-only Grok for Signal. Add deterministic enrichments to WATCHDOG_PROMPT (no LLM cost):

- Last remediation action + attempt count per chat
- Active `task_plans` / FLEET_PLAN subtask status
- Provider backoff state
- Queue snapshot id + delta since last run

### 3.2 Tier 1 — Recommend (Phase 1 target)

Extend Grok output with a machine-readable block (in addition to Signal):

```json
===WATCHDOG_JSON===
{
  "version": 1,
  "generated_at": "2026-05-28T12:00:00Z",
  "trigger": "job_completed,dispatch",
  "judgments": [
    {
      "chat_id": "cursor:…",
      "goal_status": "blocked_impossible|complete|milestone|active|false_complete|overdelivery",
      "confidence": 0.85,
      "reason": "Bulk IDE→cloud sync not API-supported; handoff doc exists",
      "evidence_refs": ["job-abc123", "state/remediation/cursor-handoff-….md"]
    }
  ],
  "recommendations": [
    {
      "action": "complete_chat",
      "chat_id": "cursor:…",
      "params": {"reason": "documented milestone satisfies decomposed goal"},
      "priority": 1,
      "requires_human": false
    }
  ]
}
===END_JSON===
```

Daemon tick (or a thin `watchdog_executor.py`) validates schema, checks caps, applies **only** actions on an allowlist that mirror existing Python functions.

### 3.3 Tier 2 — Act (Phase 2–3)

Same recommendations promoted to **`state/watchdog-actions.json`** queue if confidence ≥ threshold and action ∈ auto_apply_set. Executor runs after `remediation_pass`, before dispatch — same tick ordering as today’s deterministic fixes.

Human gates (always `requires_human: true`):

- `kill_job` when chat has active priority
- `change_goal` (objective rewrite)
- `dispatch_provider` to a provider in backoff
- Any action when `needs_luke` is true for that chat

---

## 4. Decision matrix: rules vs Grok

| Decision | Keep rule-based | Add Grok judgment when |
|----------|-----------------|------------------------|
| Parse FLEET_DONE / FLEET_MILESTONE | ✅ Always | Never replace parser |
| Mark worked / silent_failed | ✅ Runner evidence | — |
| Auto-complete overdelivery (2+ verified) | ✅ `detect_overdelivery` | Grok confirms "same milestone re-proven" vs genuinely new scope |
| Reopen false done | ✅ `should_reopen_done_chat` | Grok flags "FLEET_DONE summary contradicts goal" |
| Silent kill + retry | ✅ `attempt_silent_remediation` | Grok suggests **retry prompt** content only |
| Provider fallback | ✅ `should_use_fallback` | Grok recommends *which* provider for task type (hint → scheduler metadata) |
| Impossible goal decomposition | ✅ After 2 remediation | Grok early-detect "API impossible" → recommend `decompose_impossible` before wasted retries |
| Queue ordering | ✅ FIFO + bump on retry | Grok: "unblock dependency X first" (recommend `queue_move`) |
| Same-repo parallelism | ✅ Leases | Grok: recommend worktree/squad lane for read-only investigate |
| Multitask / workstreams | ⚠️ Squad manual | Grok: spawn explore/review lanes when goal decomposed |
| Needs Luke | ✅ `remediation.needs_luke` | Grok adds narrative; must not clear Luke flag |

**Rule of thumb:** If the action mutates SQLite or kills a PID, the **executor** must call existing `Store` / `JobRunner` / `recovery` / `goals` APIs — not shell out to `autocode drive` from Grok.

---

## 5. Concrete action vocabulary

Proposed JSON commands (daemon API / future MCP mirror):

| Action | Maps to | Auto-apply? | Notes |
|--------|---------|-------------|-------|
| `complete_chat` | `goals.mark_goal_complete` | Phase 2, if overdelivery or decomposed impossible | Requires confidence + matching evidence keys |
| `archive_chat` | `store.queue_archive` | Phase 2 | Only when `done=1` verified |
| `retry_with_prompt` | `metadata.remediation_prompt_prefix` + `recovery.schedule_retry(immediate=True)` | Phase 2 | Prompt text from Grok; capped length |
| `dispatch_provider` | Set `provider_hint` in chat metadata; next dispatch uses it | Phase 3 | Cannot override lease |
| `spawn_workstream` | Future `workstream_create` or `squad launch` | Phase 3 | Max children per parent |
| `kill_job` | `JobRunner.kill_chat_jobs` | Phase 3, rare | Only if remediation would do same |
| `change_goal` | `store.set_goal` / objective update | **Human gate** | Luke confirms via Signal |
| `pause_chat` | `pause` | Human gate | |
| `queue_move` | `queue.move` | Phase 2 | Bounded position change |
| `noop` | — | Always | Log judgment only |

All actions append to `state/audit.jsonl` (`AUDIT_LOG` already defined in config).

---

## 6. Safety and governance

1. **Separation of powers** — Grok session remains read-only for SQL/files; **only** the daemon executor writes fleet state.
2. **Allowlist + caps** — e.g. max 3 auto actions per tick, max 20/hour; `AUTOCODE_WATCHDOG_AUTO=on|off`.
3. **Idempotency** — Action id = hash(chat_id, action, job_id); skip duplicates within 1h.
4. **Audit** — Every recommendation (applied or skipped) → `audit.jsonl` + `events` table kind `watchdog_action`.
5. **Human gates** — Signal shows "Recommended: change_goal … Reply YES to apply"; Hermes inbound Phase 3.
6. **No destructive without caps** — No mass `kill_job`; no `change_goal` auto; respect `needs_luke`.
7. **Fail closed** — Invalid JSON → Signal only, no executor run. Grok timeout → existing fallback summary.

---

## 7. Phased roadmap

### Phase 0 — Smarter reports only (days)

- [ ] Enrich WATCHDOG_PROMPT with remediation metadata, task_plans, provider backoff (deterministic)
- [ ] Parse `===WATCHDOG_JSON===` in shell script; write `state/watchdog-recommendations.json` (no executor)
- [ ] Dashboard/`autocode doctor` reads recommendations file
- [ ] Tune INIT_PROMPT: distinguish FLEET_MILESTONE vs FLEET_DONE in goal_verification echo

### Phase 1 — Recommend + selective auto-apply (1–2 weeks) ⭐ Recommended next

- [ ] `watchdog_executor.apply_recommendations(store)` called from `scheduler.tick` after `remediation_pass`
- [ ] Auto-apply **only**: `retry_with_prompt` (when remediation already scheduled), `noop` judgments logged
- [ ] Optional: `complete_chat` when `detect_overdelivery` would fire **and** Grok agrees (double confirmation)
- [ ] Extend `fleet_report.needs_luke` to include "pending watchdog recommendation requires_human"
- [ ] Unit tests: schema validation, cap enforcement, no action when `needs_luke`

### Phase 2 — Workstreams + dispatch hints (2–3 weeks)

- [ ] `spawn_workstream` → generalize squad lanes; tie to `task_plans` subtasks
- [ ] `dispatch_provider` metadata consumed in `scheduler.dispatch`
- [ ] Per-provider concurrency caps in scheduler (use capacity weights for real)
- [ ] POST control plane from [MULTITASK_RESEARCH.md](./MULTITASK_RESEARCH.md) — watchdog uses same API as MCP

### Phase 3 — Closed-loop control (ongoing)

- [ ] Hermes inbound: Luke approves gated actions
- [ ] Agent-as-judge on job turn traces (not just stdout tail)
- [ ] Cross-chat coordination ("sibling tasks" — two chats same repo, complementary subtasks)
- [ ] Deprecate overlap between `autocode-watchdog` kills and remediation (single owner)

---

## 8. Example flows (three real cases)

### 8.1 Cursor sync blocked (impossible bulk API)

**Symptoms:** `running_external_idle` or bridge errors; objective mentions cursor sync/authentication.

**Deterministic path today:**

1. `attempt_silent_remediation` → `restart_my_machines_worker`
2. After 2 attempts → `decompose_impossible_goal` writes `state/remediation/cursor-handoff-*.md`, `mark_goal_complete`
3. `needs_luke` = false (decomposed); Signal says auto-fixed

**Watchdog Brain:**

| Tier | Behavior |
|------|----------|
| 0 | Signal: "Cursor sync chat: impossible API — handoff doc at …; fleet auto-completed milestone" |
| 1 | Judgment: `goal_status=blocked_impossible`, recommend `complete_chat` if daemon hasn't yet |
| 2 | If stuck before decomposition: recommend `retry_with_prompt` with kickstart + handoff instructions once; never recommend infinite Cursor retries |

**Complements recovery:** Grok **accelerates** recognition of impossibility; rules **enforce** decomposition after cap.

### 8.2 Simplicity over-delivery (0xbe activation re-proven)

**Symptoms:** Many `worked` jobs; stdout has `txid=…`, `deployment active=true`, repeated `FLEET_DONE`.

**Deterministic path today:**

1. `goal_evidence_keys` → `fleet_done`, `txid:*`, `deployment_active`, `simplicity_0xbe`
2. `detect_overdelivery` → 2+ verified completions or stable proof churn
3. `auto_complete_overdelivery` in remediation pass → kill running, archive queue

**Watchdog Brain:**

| Tier | Behavior |
|------|----------|
| 0 | stuck_patterns: `OVERDELIVERY [auto-complete on tick]`; Signal: "Simplicity chat looping — daemon will archive" |
| 1 | Judgment: `overdelivery`, confidence 0.95; recommend `complete_chat` |
| 2 | Auto-apply `complete_chat` when keys match AND Grok summary aligns (optional double-check) |

**Complements recovery:** Heuristics fire reliably; Grok reduces false negatives when evidence keys are novel (new goal templates).

### 8.3 Activation done (deployment active, goal satisfied)

**Symptoms:** Single strong `FLEET_DONE` with `active=true`; chat still queued because assessor said `milestone` or missing verification words.

**Deterministic path today:**

1. `assess_output_state` may return `active` if milestone marker present
2. `verify_goal_complete` fails → `schedule_goal_incomplete`, another turn
3. Eventually overdelivery path or manual `autocode done`

**Watchdog Brain:**

| Tier | Behavior |
|------|----------|
| 0 | goal_verification: "✓ Simplicity deployment active on signet" |
| 1 | Judgment: `complete` — "Goal asked for activation; getdeploymentinfo shows active=true" |
| 2 | Recommend `complete_chat` with evidence_refs; executor calls `mark_goal_complete` if `verify_goal_complete` would pass with Grok-augmented assessment **or** Luke pre-approved semantic completion policy |

**Complements recovery:** Fills gap between rigid regex and semantic "good enough for signet activation" — without removing FLEET_DONE requirement.

---

## 9. Integration map

```
                    ┌─────────────────┐
                    │  Watchdog Brain │
                    │  (Grok + JSON)  │
                    └────────┬────────┘
                             │ recommendations
                             ▼
┌──────────────┐    ┌─────────────────┐    ┌──────────────┐
│  goals.py    │◄───│ watchdog_executor│───►│  recovery.py │
│  verify/     │    │  (Phase 1+)      │    │  retry/      │
│  overdelivery│    └────────┬────────┘    │  backoff     │
└──────────────┘             │             └──────────────┘
                             ▼
                    ┌─────────────────┐
                    │  scheduler.tick │
                    │  remediation_pass│
                    │  dispatch        │
                    └─────────────────┘
```

- **`goals.py`** — Source of truth for completion; executor must not bypass `verify_goal_complete` unless policy flag `AUTOCODE_WATCHDOG_SEMANTIC_COMPLETE=on` and double confirmation.
- **`recovery.py`** — Retries remain exponential; watchdog only sets `remediation_prompt_prefix` and `immediate` flag.
- **`remediation.py`** — `needs_luke` remains authoritative for Signal; watchdog cannot clear escalation.
- **`scheduler.py`** — Dispatch order unchanged unless `queue_move` / `provider_hint` action applied.
- **`grok_watchdog.py`** — Unchanged trigger debounce; optional second flush for recommendations-only on `job_completed`.

**Do not** merge `autocode-watchdog` into Grok — keep kill/restart deterministic and local; Grok diagnoses *why* a kill happened for Signal.

---

## 10. Optional: MCP tools & action queue sketch

### 10.1 MCP tools (Phase 2+, aligns with Fleet Control Plane)

| Tool | Purpose |
|------|---------|
| `autocode_watchdog_status` | Last Signal, recommendations, pending actions |
| `autocode_watchdog_recommend` | Trigger analysis on demand (Luke/Hermes) |
| `autocode_apply_action` | Execute one gated action (human confirmed) |

### 10.2 `state/watchdog-actions.json` (Phase 2)

```json
{
  "version": 1,
  "pending": [
    {
      "id": "wa-20260528-001",
      "created_at": "2026-05-28T12:00:05Z",
      "source": "grok_watchdog",
      "action": "retry_with_prompt",
      "chat_id": "codex:…",
      "params": {
        "prompt_prefix": "RECOVERY: prior run hit provider 429. Wait 60s, retry with smaller context…"
      },
      "confidence": 0.78,
      "requires_human": false,
      "expires_at": "2026-05-28T12:15:00Z"
    }
  ],
  "applied": [],
  "rejected": []
}
```

Daemon tick pseudocode:

```python
def process_watchdog_actions(store):
    queue = load_actions()
    for item in queue.pending[:MAX_ACTIONS_PER_TICK]:
        if item.expired or not validate(item):
            queue.rejected.append(item); continue
        if item.requires_human or needs_luke(store, item.chat_id):
            continue  # leave pending for Signal approval
        if apply_action(store, item):  # calls goals/recovery/scheduler helpers
            queue.applied.append(item)
            audit_log(item)
    save_actions(queue)
```

---

## 11. Risks

| Risk | Mitigation |
|------|------------|
| Grok hallucinates completion | Executor always re-runs `verify_goal_complete`; semantic mode off by default |
| Fight with autocode-watchdog | Watchdog never kills; remediation owns kill timing |
| Action storm after fleet event | Debounce + per-hour cap + idempotency keys |
| Stale persistent Grok session | Existing `needs_session_reset`; reset recommendations on session reset |
| Luke alert fatigue | Keep NEEDS LUKE deterministic; Grok adds detail not new flags |
| Cost/latency | Tier 1 JSON optional on `job_completed` + `fallback` only; skip on pure `daemon_tick` |

---

## 12. How this complements deterministic recovery

The codebase already implements a solid **reactive control loop**: evidence assessment → retry/backoff → remediation → overdelivery complete → provider fallback. That loop is fast, testable, and cheap.

The Watchdog Brain adds a **slow thinking layer** at event boundaries:

1. **Interpretation** — Semantic goal↔evidence when regex is brittle (activation done, partial milestones).
2. **Communication** — Signal that explains *why* auto-fix ran, reducing Luke's need to read SQLite.
3. **Anticipation** — Recommend decomposition before retry budget exhaustion.
4. **Coordination** — Future workstream hints for squad-like parallelism without manual `squad launch`.

Deterministic recovery remains the **safety net** that runs even when Grok is down, rate-limited, or wrong. Grok never replaces `needs_luke`, leases, or retry caps — it advises and, in later phases, proposes actions the daemon may apply through existing functions.

---

## Appendix A — File reference

| File | Role |
|------|------|
| `~/bin/autocode-grok-watchdog` | Shell orchestrator, prompts, Signal delivery |
| `autocode/grok_watchdog.py` | Event debounce, spawn watchdog |
| `autocode/fleet_report.py` | `needs_luke_*`, enriched stuck patterns |
| `autocode/remediation.py` | Silent remediation, impossible goal, needs_luke rules |
| `autocode/goals.py` | Completion, overdelivery, false done |
| `autocode/recovery.py` | Retry, backoff, fallback triggers |
| `autocode/scheduler.py` | Tick, dispatch, capacity |
| `autocode/policy.py` | Output assessment heuristics |
| `~/bin/autocode-watchdog` | Kill + drive (deterministic, separate) |

---

*End of research document.*
