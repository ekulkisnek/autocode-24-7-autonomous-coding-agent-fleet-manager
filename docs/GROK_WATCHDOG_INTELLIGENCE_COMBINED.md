# Grok Watchdog Intelligence — Combined Master Doc

**Date:** 2026-05-28  
**Status:** Research synthesis — no implementation in this pass  
**Output of:** parallel angle research + `grok_watchdog.py` / `fleet_report.py` review  
**Gaps:** `GROK_WATCHDOG_INTEL_A_SAFETY.md` and `GROK_WATCHDOG_INTEL_C_MULTITASK.md` were not produced; safety and multitask sections below are integrated from the main doc, Angle B, and [MULTITASK_RESEARCH.md](./MULTITASK_RESEARCH.md).

---

## 1. Executive summary

- AutoCode already splits **execution** (daemon tick, scheduler, remediation, recovery) from **observation** (Grok watchdog → Hermes Signal). The deterministic stack handles most fleet hygiene; Grok adds narrative synthesis and optional investigation — today **read-only**, no fleet writes.
- Two watchdogs coexist by design: `autocode-watchdog` **kills and drives**; `autocode-grok-watchdog` **SELECT/logs only** and must not shell to `autocode drive`.
- The proposed **Watchdog Brain** evolves Grok in three tiers: **observe** (Signal + enriched context) → **recommend** (`state/watchdog-recommendations.json`) → **act** (`state/watchdog-actions.json` + `watchdog_executor` on daemon tick).
- **Recommended Phase 1:** structured recommendations consumed after `remediation_pass`, with **zero or minimal auto-apply** (`retry_with_prompt` when remediation already scheduled; optional `complete_chat` only when `detect_overdelivery` agrees). Richer Signal; still no Grok-initiated SQLite/PID mutations.
- **Rules stay authoritative** for parsers (`FLEET_DONE` / `FLEET_MILESTONE`), evidence status, retry caps, leases, and `remediation.needs_luke()`. Grok advises on ambiguity; the executor calls existing `goals` / `recovery` / `remediation` / `JobRunner` APIs.
- **Fleet Control Plane** ([MULTITASK_RESEARCH.md](./MULTITASK_RESEARCH.md) Option A) is the long-term client surface (MCP + HTTP POST). Watchdog actions and MCP tools should share **`fleet_actions`** helpers so Signal approval and IDE agents cannot diverge.
- **Multitask alignment:** Cursor Multitask is conversation-scoped; AutoCode is machine-scoped. `spawn_workstream` maps to squad lanes now, workstreams + scheduler DAG in Phase 2.
- **Real cases** (cursor sync impossible API, Simplicity 0xbe overdelivery, activation-done semantic gap) show Grok **accelerates interpretation** while deterministic paths remain the safety net when Grok is down or wrong.

---

## 2. Unified architecture: Watchdog Brain tiers

### 2.1 Today (Tier 0 only)

```
Daemon tick / dispatch / job finish / autocode-watchdog
        │
        ▼
autocode.grok_watchdog.request(reason)     ← 30s debounce, coalesce (max 12 reasons)
        │
        ▼
~/bin/autocode-grok-watchdog --trigger <reasons>
        │
        ├─ fleet_report: needs_luke_summary, stuck_patterns_enriched, goal_verification
        ├─ Persistent Grok session (resume; reset if FLEET_DONE stale)
        └─ WATCHDOG_PROMPT → Grok (≤4 tool calls) → ===SIGNAL=== … ===END===
                                    │
                                    ▼
                          hermes send --to signal (≤2000 chars)
```

**Triggers** (`grok_watchdog.py`): `daemon_tick`, `dispatch`, `prompt_due`, `job_completed`, `job_failed`, `job_killed`, `tick_cli`, `fallback` (default 900s). Fast paths: `--stall-check`, `--daily-digest` (no Grok).

**Pre-classified context** (`fleet_report.py` + shell):

| Signal | Source |
|--------|--------|
| NEEDS LUKE | `remediation.needs_luke()` — paused, remediation exhausted, max retries, `blocked` |
| Auto-fix hints | `running_silent` / `running_external_idle` → remediation queued; 8+ `worked` → OVERDELIVERY; 3+ `silent_failed` → REPEATED_FAIL |
| Goal verification | Last `FLEET_DONE` JSON from stdout; missing marker on `worked` jobs |

Grok is instructed to **echo NEEDS LUKE** unless stronger evidence of a new blocker; silent/overdelivery/done-queue issues should **not** ask Luke.

### 2.2 Target tiers

```
┌─────────────────────────────────────────────────────────────┐
│  Tier 0 — OBSERVE (current + enrich)                        │
│  Debounced triggers, fleet_report, Signal, optional JSON log  │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│  Tier 1 — RECOMMEND (Phase 1 ⭐)                              │
│  ===WATCHDOG_JSON=== → state/watchdog-recommendations.json    │
│  Daemon reads; applies only rule-approved / allowlisted ops   │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│  Tier 2 — ACT (Phase 2–3)                                     │
│  Promote high-confidence items → watchdog-actions.json        │
│  watchdog_executor after remediation_pass, before dispatch    │
└─────────────────────────────────────────────────────────────┘
```

**Separation of powers**

| Layer | Writes SQLite / kills PIDs? |
|-------|-----------------------------|
| `autocode-grok-watchdog` | **No** — read-only; may append JSON state files only |
| `watchdog_executor` (daemon) | **Yes** — via `Store` / `JobRunner` / `goals` / `recovery` |
| `autocode-watchdog` | **Yes** — deterministic kill + `drive`; **not** merged into Grok |

### 2.3 Daemon tick ordering (unified)

**Today:**

```text
runner.refresh()
goals.reconcile_done / reconcile_false_done
remediation.remediation_pass()
recovery.reconcile_killed_chats()
cleanup_stale_leases → discover → dispatch
grok_watchdog.on_daemon_tick()          # advisory only
```

**Target (Phase 1+):**

```text
remediation.remediation_pass()
watchdog_executor.process_actions()     # NEW — after remediation, before reconcile_killed
recovery.reconcile_killed_chats()
discover → dispatch
grok_watchdog.on_daemon_tick()          # Signal; optional flush on job_completed
```

Rationale: remediation may already kill/retry; executor must **not fight** same-tick remediation. `complete_chat` / queue changes affect the same tick’s dispatch candidates.

### 2.4 Integration map

```
                    ┌─────────────────┐
                    │  Watchdog Brain │
                    │  (Grok + JSON)  │
                    └────────┬────────┘
                             │ recommendations / actions
                             ▼
┌──────────────┐    ┌─────────────────┐    ┌──────────────┐
│  goals.py    │◄───│ watchdog_executor│───►│  recovery.py │
│  verify/     │    │  + fleet_actions │    │  retry/      │
│  overdelivery│    └────────┬────────┘    │  backoff     │
└──────────────┘             │             └──────────────┘
                             ▼
                    ┌─────────────────┐
                    │  scheduler.tick │
                    │  remediation_pass│
                    │  dispatch        │
                    └─────────────────┘
         ▲                                    ▲
         │                                    │
   Hermes / Cursor MCP              autocode-watchdog (kill+drive)
   (Fleet Control Plane)            (deterministic, separate)
```

---

## 3. Merged decision matrix (rules vs Grok)

| Decision | Keep rule-based | Add Grok judgment when |
|----------|-----------------|------------------------|
| Parse `FLEET_DONE` / `FLEET_MILESTONE` | Always | Never replace parser |
| Mark `worked` / `silent_failed` | `JobRunner` evidence | — |
| Auto-complete overdelivery (2+ verified) | `detect_overdelivery` | Confirm “same milestone re-proven” vs new scope |
| Reopen false done | `should_reopen_done_chat` | `FLEET_DONE` contradicts goal |
| Silent kill + retry | `attempt_silent_remediation` | Suggest **retry prompt content** only |
| Provider fallback | `should_use_fallback` | Recommend provider for task type → `provider_hint` metadata |
| Impossible goal | After 2 remediation attempts | Early “API impossible” → recommend decompose before budget exhausted |
| Queue ordering | FIFO + retry bump | “Unblock dependency X first” → `queue_move` |
| Same-repo parallelism | Leases | Recommend worktree / squad lane for read-only investigate |
| Multitask / workstreams | Squad manual (4 lanes) | Recommend explore/review lanes when goal decomposed |
| Needs Luke | `remediation.needs_luke` | Narrative detail only; **cannot clear** Luke flag |

**Rule of thumb:** Any action that mutates SQLite or kills a PID goes through **`watchdog_executor` → existing Python APIs**, never `autocode drive` from Grok.

**Human gates (always `requires_human: true`):** `change_goal`, `pause_chat`, `kill_job` when chat has active priority, `dispatch_provider` while provider in backoff, any action when `needs_luke(chat_id)`.

---

## 4. Action API + multitask coordination (single phased roadmap)

### 4.1 Machine-readable outputs

**Tier 1 — recommendations** (`state/watchdog-recommendations.json`):

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
      "reason": "…",
      "evidence_refs": ["job-abc123", "state/remediation/cursor-handoff-….md"]
    }
  ],
  "recommendations": [
    {
      "action": "complete_chat",
      "chat_id": "cursor:…",
      "params": { "reason": "…" },
      "priority": 1,
      "requires_human": false
    }
  ]
}
===END_JSON===
```

**Tier 2 — action queue** (`state/watchdog-actions.json`): durable `pending` / `applied` / `rejected` with `id`, `expires_at` (default 15m), `idempotency_key`, `confidence`, `requires_human`. Promoted when `confidence` ≥ `AUTOCODE_WATCHDOG_AUTO_THRESHOLD` (default 0.9) and `AUTOCODE_WATCHDOG_AUTO=on`.

### 4.2 Action vocabulary

| Action | Maps to | Auto-apply phase | Notes |
|--------|---------|------------------|-------|
| `noop` | Log only | Always | Judgment without mutation |
| `retry_with_prompt` | `metadata.remediation_prompt_prefix` + `recovery.schedule_retry(immediate=True)` | Phase 1 (guarded) | When remediation already scheduled |
| `complete_chat` | `goals.mark_goal_complete` | Phase 1 optional / Phase 2 | Double-check with `detect_overdelivery` or `impossible_handoff` |
| `archive_chat` | `store.queue_archive` | Phase 2 | Only when `done=1` verified |
| `queue_move` | `store.queue_move` | Phase 2 | Bounded position change |
| `dispatch_provider` | `metadata.provider_hint` | Phase 2–3 | Soft hint; respect `provider_in_backoff` |
| `spawn_workstream` | `squad launch` → future `workstreams` table | Phase 2–3 | Max children per parent |
| `kill_job` | `JobRunner.kill_chat_jobs` | Phase 3, rare | Only if remediation would kill anyway |
| `change_goal` | `store.set_goal` | **Human gate** | Signal YES / MCP `autocode_apply_action` |
| `pause_chat` | `pause` | Human gate | — |

Env knobs: `AUTOCODE_WATCHDOG_AUTO` (default `off`), `MAX_PER_TICK` (3), `MAX_PER_HOUR` (20), `AUTO_THRESHOLD` (0.9). All apply/skip → `state/audit.jsonl` + `events` kind `watchdog_action`.

### 4.3 Fleet Control Plane + multitask (unified phases)

Cursor **Multitask Mode** = parent coordinator + background `Task` subagents (conversation-scoped). AutoCode = **machine-scoped** queue, goals, leases, recovery. They complement when AutoCode is the fleet layer and IDE/Hermes agents are **clients**.

| Cursor multitask | AutoCode |
|------------------|----------|
| Parent spawns `Task` | Scheduler dispatches jobs |
| Background subagent | OS process per provider CLI |
| Ephemeral turn tree | Persistent SQLite queue/goals/jobs |
| No cross-session fleet | `max_active`, leases, FLEET_DONE verification |

**Unified roadmap** (watchdog + control plane + workstreams):

| Phase | Watchdog | Control plane / multitask |
|-------|----------|-----------------------------|
| **0** (days) | Enrich WATCHDOG_PROMPT; parse JSON → `watchdog-recommendations.json` only; `doctor` reads file | — |
| **1** (1–2 weeks) ⭐ | `watchdog_executor.apply_recommendations` after `remediation_pass`; auto: `retry_with_prompt` (guarded), optional `complete_chat` + overdelivery agree | POST `/v1/queue/items` + MCP `autocode_queue_add` / `autocode_status` (~200 LOC on `web.py`) |
| **2** (2–3 weeks) | `watchdog-actions.json`; `dispatch_provider`, `spawn_workstream`; shared `fleet_actions.py` | `workstreams` schema + DAG dispatch; `autocode_workstream_spawn`; per-provider slot caps from capacity weights |
| **3** (ongoing) | Hermes inbound YES for gated actions; agent-as-judge on job traces | Signal → `autocode_queue_add`; phone gateway SSE; deprecate duplicate fill loops |

**`spawn_workstream` near-term:** `store.add_priority` + `autocode squad launch` (synthetic `squad:<priority_id>:<lane>`, max 4). **Target:** `workstreams(id, parent_chat_id, provider, depends_on_json, status)` with scheduler filling ready nodes.

**MCP tools (consolidated sketch)**

| Tool | Role |
|------|------|
| `autocode_status` / `autocode_queue_*` | Fleet Control Plane Phase 1 |
| `autocode_watchdog_status` | Last Signal, recommendations, pending actions |
| `autocode_watchdog_recommend` | On-demand analysis |
| `autocode_apply_action` | Human-confirmed `{ "id": "wa-…" }` |
| `autocode_workstream_spawn` | Phase 2 parallel lanes |

HTTP mirror: `GET/POST /v1/watchdog/actions`, `POST …/apply`; auth `AUTOCODE_API_TOKEN`; `Idempotency-Key` = action `idempotency_key`.

---

## 5. Real-case flows

### 5.1 Cursor sync blocked (impossible bulk API)

**Symptoms:** `running_external_idle`, bridge errors; goal mentions cursor sync/authentication.

| Layer | Behavior |
|-------|----------|
| Deterministic | `attempt_silent_remediation` → `kickstart_my_machines_worker`; after 2 attempts → `decompose_impossible_goal` + handoff `state/remediation/cursor-handoff-*.md` + `mark_goal_complete` |
| Tier 0 | Signal: impossible API — handoff path; fleet auto-completed milestone |
| Tier 1 | Judgment `blocked_impossible`; recommend `complete_chat` if daemon hasn’t yet |
| Tier 2 | `retry_with_prompt` once with kickstart + handoff instructions; `complete_chat` only if remediation count ≥ 2 **or** handoff file exists; reject `premature_complete` |

**Complements recovery:** Grok accelerates impossibility recognition; rules enforce decomposition after cap. Never auto `dispatch_provider` to `cursor` while in backoff.

### 5.2 Simplicity over-delivery (0xbe activation re-proven)

**Symptoms:** Many `worked` jobs; `txid=`, `deployment active=true`, repeated `FLEET_DONE`; stuck pattern `OVERDELIVERY [auto-complete on tick]`.

| Layer | Behavior |
|-------|----------|
| Deterministic | `goal_evidence_keys` → `fleet_done`, `txid:*`, `deployment_active`, `simplicity_0xbe`; `detect_overdelivery` → `auto_complete_overdelivery` |
| Tier 0 | Signal: looping — daemon will archive |
| Tier 1–2 | Judgment `overdelivery` ~0.95; `complete_chat` when `detect_overdelivery` true (optional Grok double-check) |
| Optional | `spawn_workstream` (diff-reviewer) with `requires_human: true` before close |

**Complements recovery:** Heuristics fire reliably; Grok reduces false negatives on novel goal templates.

### 5.3 Activation done (deployment active, goal satisfied)

**Symptoms:** Strong `FLEET_DONE` with `active=true`; chat still queued — assessor returned `milestone` or `verify_goal_complete` failed.

| Layer | Behavior |
|-------|----------|
| Deterministic | `schedule_goal_incomplete` → another turn; eventually overdelivery or manual `autocode done` |
| Tier 0 | goal_verification echoes deployment active on signet |
| Tier 1 | Judgment `complete` — semantic “activation satisfied” |
| Tier 2 | `complete_chat` only if `verify_goal_complete` passes **or** `AUTOCODE_WATCHDOG_SEMANTIC_COMPLETE=on` with double confirmation |

**Complements recovery:** Fills regex↔semantics gap without removing `FLEET_DONE` requirement by default.

---

## 6. Phase 0–3 roadmap (recommended Phase 1)

### Phase 0 — Smarter reports only (days)

- [ ] Enrich WATCHDOG_PROMPT: remediation metadata, `task_plans`, provider backoff
- [ ] Parse `===WATCHDOG_JSON===`; write `watchdog-recommendations.json` (no executor)
- [ ] `autocode doctor` / dashboard read recommendations
- [ ] Tune goal_verification: `FLEET_MILESTONE` vs `FLEET_DONE`

### Phase 1 — Recommend + selective auto-apply (1–2 weeks) ⭐ **Recommended next**

- [ ] `watchdog_executor.apply_recommendations(store)` after `remediation_pass`
- [ ] Auto-apply **only**: guarded `retry_with_prompt`, `noop` logging; optional `complete_chat` when `detect_overdelivery` **and** Grok agree
- [ ] Extend `fleet_report.needs_luke` for pending `requires_human` recommendations
- [ ] Unit tests: schema, caps, no action when `needs_luke`
- [ ] **Parallel POC:** POST queue + MCP `autocode_queue_add` (Fleet Control Plane)

### Phase 2 — Workstreams + dispatch hints (2–3 weeks)

- [ ] `watchdog-actions.json` + promote from recommendations
- [ ] `spawn_workstream` / `dispatch_provider`; `fleet_actions` shared with MCP
- [ ] Workstreams table + scheduler DAG; per-provider concurrency caps
- [ ] `GET/POST /v1/watchdog/actions`

### Phase 3 — Closed-loop control (ongoing)

- [ ] Hermes inbound: Luke approves gated actions
- [ ] Agent-as-judge on job turn traces
- [ ] Cross-chat sibling coordination
- [ ] Single owner for kills (reconcile `autocode-watchdog` vs remediation overlap)

---

## 7. Risks + complements to deterministic recovery

### 7.1 Risks

| Risk | Mitigation |
|------|------------|
| Grok hallucinates completion | Executor re-runs `verify_goal_complete`; semantic mode off by default |
| Fights `autocode-watchdog` | Grok queue never kills; remediation owns kill timing |
| Action storm | Debounce (30s), per-tick/hour caps, idempotency keys |
| Stale Grok session | `needs_session_reset`; clear recommendations on reset |
| Luke alert fatigue | NEEDS LUKE stays deterministic; Grok adds detail not new flags |
| Cost/latency | Tier 1 JSON on `job_completed` + `fallback` only; skip bare `daemon_tick` |
| Dual writers (MCP + watchdog) | Shared `fleet_actions`; leases unchanged |
| Hermes shell control | MCP with read-only default; confirm destructive ops |

### 7.2 How Grok complements deterministic recovery

The codebase already runs a **reactive control loop**: evidence assessment → retry/backoff → remediation → overdelivery complete → provider fallback. That loop is fast, testable, and cheap.

Watchdog Brain adds **slow thinking** at event boundaries:

1. **Interpretation** — Semantic goal↔evidence when regex is brittle.
2. **Communication** — Signal explains *why* auto-fix ran.
3. **Anticipation** — Recommend decomposition before retry exhaustion.
4. **Coordination** — Workstream hints for squad-like parallelism without manual `squad launch`.

Deterministic recovery remains the **safety net** when Grok is down, rate-limited, or wrong. Grok never replaces `needs_luke`, leases, or retry caps.

### 7.3 Safety governance (synthesized; Angle A gap)

*Source: main doc §6, Angle B §10 — dedicated `GROK_WATCHDOG_INTEL_A_SAFETY.md` not received.*

1. **Separation of powers** — Grok session read-only for SQL/files; only daemon executor mutates fleet.
2. **Allowlist + caps** — `AUTOCODE_WATCHDOG_AUTO`, max 3/tick, 20/hour.
3. **Idempotency** — `hash(chat_id, action, job_id)`; skip duplicates within 1h.
4. **Audit** — Every recommendation (applied or skipped) → `audit.jsonl` + `watchdog_action` events.
5. **Human gates** — Signal “Reply YES wa-… to apply”; never auto `change_goal`.
6. **Fail closed** — Invalid JSON → Signal only; Grok timeout → existing fallback summary.
7. **Fail closed on apply** — expired / `needs_luke` / invalid schema → pending or `rejected`, no partial writes.

---

## 8. Source docs appendix

| Document | Status | Primary contributions to this combined doc |
|----------|--------|---------------------------------------------|
| [GROK_WATCHDOG_INTELLIGENCE.md](./GROK_WATCHDOG_INTELLIGENCE.md) | ✅ Present | Executive framing; current pipeline; scheduler/remediation/goals behavior; industry patterns; Tier 0–2 architecture; decision matrix; action vocabulary; safety §6; Phase 0–3; three example flows §8; integration map; MCP sketch; risks §11 |
| [GROK_WATCHDOG_INTEL_B_ACTIONS.md](./GROK_WATCHDOG_INTEL_B_ACTIONS.md) | ✅ Present | Tier 2 closed-loop; `watchdog-actions.json` schema; tick insert point; action→codebase mapping; `fleet_actions` sharing; cursor-auth + simplicity JSON sequences; mermaid tick ordering; implementation checklist |
| [GROK_WATCHDOG_INTEL_A_SAFETY.md](./GROK_WATCHDOG_INTEL_A_SAFETY.md) | ❌ **Missing** | *Expected:* dedicated governance, threat model, cap policy — **synthesized from main §6 + B §10** |
| [GROK_WATCHDOG_INTEL_C_MULTITASK.md](./GROK_WATCHDOG_INTEL_C_MULTITASK.md) | ❌ **Missing** | *Expected:* watchdog↔workstream coordination — **synthesized from [MULTITASK_RESEARCH.md](./MULTITASK_RESEARCH.md) + main Phase 2 + B `spawn_workstream`** |
| [MULTITASK_RESEARCH.md](./MULTITASK_RESEARCH.md) | ✅ Present | Fleet Control Plane Option A; Cursor Multitask mapping; gap analysis; MCP/HTTP POC; workstreams Phase 2; Hermes Phase 3; risks (dual writers, API auth) |
| `autocode/grok_watchdog.py` | ✅ Code | Debounce/coalesce (30s), triggers, `PENDING_PATH`, fallback interval 900s, `enabled()` env |
| `autocode/fleet_report.py` | ✅ Code | `needs_luke_lines` / `needs_luke_summary`; `collect_stuck_patterns_enriched` severity rules |

---

## 9. Quick file reference

| File | Role |
|------|------|
| `~/bin/autocode-grok-watchdog` | Shell orchestrator, prompts, Signal, JSON parse |
| `autocode/grok_watchdog.py` | Event debounce, spawn watchdog |
| `autocode/fleet_report.py` | Pre-classified NEEDS LUKE + stuck patterns |
| `autocode/watchdog_executor.py` | *Planned* — `process_actions`, allowlist |
| `autocode/remediation.py` | Silent remediation, impossible goal, `needs_luke` |
| `autocode/goals.py` | Completion, overdelivery, false done |
| `autocode/scheduler.py` | Tick, dispatch, capacity |
| `~/bin/autocode-watchdog` | Deterministic kill + drive (separate) |

---

*End of combined intelligence document.*
