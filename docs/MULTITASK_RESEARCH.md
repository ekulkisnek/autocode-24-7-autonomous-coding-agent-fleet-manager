# AutoCode × Cursor Multitask — Research (2026-05-28)

**Goal:** One mental model — Luke tells any agent (`queue X`, `status`, `drive Y`) and **AutoCode orchestrates** across Claude, Codex, Cursor, Grok/Hermes, and Antigravity — with parallelism comparable to Cursor IDE **Multitask Mode**, but durable and provider-agnostic.

**Status:** Research only. No large implementation in this pass.

---

## 1. Current AutoCode architecture

### 1.1 Mental model

AutoCode is a **local fleet scheduler**: it discovers provider chat sessions, persists fleet state in SQLite, maintains an ordered **queue** of chats with **goals**, and on each **daemon tick** dispatches CLI workers until **capacity** is full. Hermes/Grok are **not** the brain — README states *"Hermes is only a thin client."*

```
Luke / any CLI          autocode CLI / (future) API
        │                        │
        ▼                        ▼
   queue / drive / pause    Store (SQLite)
        │                        │
        │              ┌─────────┴─────────┐
        │              │  Scheduler.tick   │◄── launchd every 2s
        │              │  discover / goals │
        │              │  recovery / lease │
        │              └─────────┬─────────┘
        │                        │
        ▼                        ▼
              JobRunner → subprocess per provider
              (grok / codex / claude / cursor-agent / antigravity)
```

### 1.2 Entry points

| Surface | Role |
|---------|------|
| `~/bin/autocode` | Wrapper → `python3 -m autocode.cli` |
| `autocode daemon start\|install\|run` | Long-running tick loop (`Daemon` → `Scheduler.tick`) |
| `autocode tick` | One scheduler pass (used by capacity helper) |
| `autocode web` | Read-only dashboard HTTP on `127.0.0.1:8765` (GET + SSE) |
| `~/bin/autocode-watchdog` | Kills silent/stalled jobs, `autocode drive` restart |
| `~/bin/autocode-capacity` | Extra fill loop; provider **weights** for load math |
| `~/bin/autocode-grok-watchdog` | Read-only fleet reporter → Hermes `signal` |

**Home:** `$AUTOCODE_HOME` → `~/autocode`  
**DB:** `~/autocode/state/autocode.sqlite`  
**Jobs:** `~/autocode/state/jobs/<job-id>/` (stdout, stderr, prompt)

### 1.3 Core CLI (orchestration-relevant)

```bash
autocode status | now | dashboard
autocode queue list|add|remove|move|archive|finished
autocode drive <query> --goal "<goal>" [--model ...]
autocode pause|done <query>
autocode goals
autocode priority list|add|remove
autocode squad plan|launch|collect <query>
autocode cursor status|chats|new|model ...
autocode daemon install|start|stop|restart|status
autocode tick [--dry-run]
autocode web [--port 8765]
```

### 1.4 Daemon & scheduler

- **Tick interval:** `AUTOCODE_TICK_INTERVAL` (default **2s**); launchd plist uses `--interval 2`.
- **Each tick:** refresh running jobs → reconcile false `done` → archive finished queue rows → recovery → optional discovery → compute **capacity** → dispatch up to `min(capacity - active, candidates)`.
- **Dispatch:** build prompt (prior job context, task plan) → `provider.continue_plan()` → `JobRunner.start()` (subprocess + lease on **resource** = repo path).
- **Leases:** one active job per `resource` (cwd/repo); stale leases cleaned when job not `running`.

### 1.5 Capacity & limits

| Knob | Default | Effect |
|------|---------|--------|
| `AUTOCODE_MAX_ACTIVE` / config `max_active` | 5 | Max concurrent jobs |
| `yolo` config | on | If cap would be 0 but queue has work, allow **1** slot under memory pressure |
| load1 / mem / disk | dynamic | Reduces cap (0–3) before hard stop |
| `AUTOCODE_JOB_TIMEOUT` | 1800s (600s in launchd plist) | Wall clock non-Cursor |
| `AUTOCODE_CURSOR_JOB_TIMEOUT` | 14400s | Cursor jobs |
| `AUTOCODE_STALL_SECONDS` | 600s (180s launchd) | No stdout/stderr progress |
| Provider backoff | `provider_health` table | `recovery.provider_in_backoff()` skips dispatch |

**Per-provider concurrency:** Scheduler does **not** enforce per-provider slot caps. `autocode-capacity` uses informal weights (`cursor:3, codex:2, claude:1, grok:1`) only for **reporting** load, not dispatch gating. Quota visibility: `quota_probes.py` (codex/claude/grok/cursor/antigravity) — dashboard shows `ok` / `not exposed`.

### 1.6 Queue, goals, drive

- **`queue`:** `chat_id` + `position` (float ordering). Finished chats can move to `queue_finished`.
- **`goals`:** active objective per chat; incomplete work schedules **next turn** on same chat (bumps queue, injects prior job summary).
- **`drive`:** resolve chat by query → set goal → `queue_add` → immediate or next-tick dispatch.
- **Completion:** `FLEET_DONE` JSON marker (default required), min output chars, assessor heuristics in `policy.py`. False completes re-opened on tick.

### 1.7 Recovery stack

| Layer | Behavior |
|-------|----------|
| In-tick `goals.reconcile_false_done_chats` | Re-queue chats marked done without evidence |
| `recovery.schedule_retry` | Exponential backoff, `next_retry_at` in chat metadata |
| `autocode-watchdog` | Kill `running_silent` / long `external_idle`; `drive` with continue prompt |
| Provider fallback | Codex stall → Grok takeover; others → Codex takeover (`scheduler.fallback_plan`) |
| `autocode-grok-watchdog` | **Advisory only** — SELECT/logs, Signal summary; explicitly must not `drive` |

### 1.8 Provider differences (dispatch)

| Provider | Discovery | Same-chat continue? | Notes |
|----------|-----------|---------------------|-------|
| **cursor** | cli `store.db`, cloud API `bc-*`, IDE `state.vscdb`, transcript jsonl | **Yes** for `cursor.cli`, `cursor.cloud` | IDE/transcript → new `cursor-agent` worker with context paste |
| **grok** | `~/.grok/.../session_search.sqlite` | **Yes** `--resume` | |
| **codex** | `~/.codex/state_5.sqlite` + rollout | **Yes** `codex exec` resume | |
| **claude** | `~/.claude/projects/**/*.jsonl` | **Yes** `--resume` | |
| **antigravity** | `~/.gemini/antigravity` brain/logs | agentapi if ready else fork | |

Cursor model default: config `cursor_model` (`auto`). API key from env / `~/.hermes/.env` / `~/grok-cursor-bridge/.env`.

### 1.9 Squad (proto–multitask)

`autocode squad launch` starts up to **4 helper lanes** (ios-build-fixer/grok, diff-reviewer/codex, e2e-planner/claude, worktree-experiment/grok) under capacity, with synthetic `chat_id`s `squad:<priority_id>:<lane>`. `collect` aggregates `SQUAD_FINDINGS` into the **primary writer** chat. This is the closest existing pattern to Cursor subagents but **manual**, **priority-scoped**, and **not** integrated with the main queue lifecycle.

### 1.10 Hermes / Signal integration today

| Path | Direction | Control? |
|------|-----------|----------|
| `autocode-grok-watchdog` | AutoCode → Grok analysis → `hermes send --to signal` | **Read-only** fleet reports |
| Hermes agent | Could run shell | **No first-class API**; would need `autocode queue add` via bash |
| `cursor-phone-gateway` | HTTP → Cursor SDK agent | **Separate** queue (async jobs, concurrency 2); not AutoCode |
| `remote-agent-chat-spike` | Relay + proxy per IDE session | Per-session **message** queue when agent busy; not fleet-wide |

**AGENTS.md (Hermes):** AX-first computer use, browser preference — no AutoCode hooks.

### 1.11 Existing HTTP (read-only)

`autocode web` exposes:

- `GET /api/status`, `/api/queue`, `/api/dashboard`
- `GET /events` (SSE, 1 Hz)

**No POST** for queue/drive/status mutations today.

---

## 2. Cursor Multitask Mode (product behavior)

Derived from Cursor Agent tooling (IDE Multitask Mode + `Task` tool contract):

| Concept | Behavior |
|---------|----------|
| **Parent coordinator** | User-facing agent stays the coordinator; does not duplicate work the subagent owns |
| **`Task` tool** | Spawns typed subagents (`explore`, `shell`, `generalPurpose`, …) with isolated context |
| **`run_in_background: true`** | Required in Multitask Mode — subagent runs async; parent continues other work |
| **Completion** | Background subagent notifies parent (user-visible summary); parent synthesizes only when needed |
| **Scope** | Ephemeral — tied to **one chat turn tree**, not a global SQLite fleet |
| **Parallelism** | Multiple subagents in one message; parent avoids blocking on all of them |
| **Constraints** | Subagent cannot spawn subagents (no nested Task from subagent in standard rules) |

### 2.1 Mapping to AutoCode daemon tick

| Cursor multitask | AutoCode daemon |
|------------------|-----------------|
| Parent decides when to spawn | Scheduler decides when to dispatch |
| Subagent = separate context window | Job = separate OS process + provider CLI |
| Background = non-blocking for parent | Tick already non-blocking; jobs run in parallel up to `max_active` |
| Result merged in parent chat | Result assessed → next queue turn on **same** `chat_id` |
| No persistent queue across IDE sessions | **Persistent** `queue` + `goals` + `jobs` history |
| User stops subagent via UI | `pause`, watchdog kill, retry limits |

**Gap:** Cursor multitask is **conversation-scoped orchestration**; AutoCode is **machine-scoped job orchestration**. They complement each other if AutoCode is the fleet layer and Cursor agents (IDE or SDK) are **clients** of that layer.

---

## 3. Gap analysis

| Capability | Cursor Multitask | AutoCode today |
|------------|------------------|----------------|
| Parallel investigation branches | ✅ Task + background | ⚠️ Squad only; no generic `workstream` entity |
| Parent/child task hierarchy | ✅ Implicit in chat | ⚠️ `task_plans` table exists but not Task-tool-shaped |
| Cross-session persistence | ❌ | ✅ SQLite queue/goals/jobs |
| Cross-provider single queue | ❌ | ✅ One queue, many providers |
| Provider-aware backoff/quota | ❌ | ✅ probes + `provider_health` |
| Goal verification / FLEET_DONE | ❌ | ✅ |
| Control from Hermes/Signal | ❌ | ❌ (shell only) |
| Control from Codex/Claude CLI | ❌ | ❌ (shell only) |
| Control from Cursor IDE agent | ❌ | ❌ (no MCP; web read-only) |
| Unified status for any client | Partial (chat UI) | ✅ `status` / `dashboard` CLI; partial HTTP |
| IDE composer = same as fleet job | N/A | ⚠️ cursor.ide is read-only continue |
| Phone / remote control | N/A | ⚠️ separate gateways |

### 3.1 Provider-specific control gaps

| Client | Can `autocode queue add` today? | Blocker |
|--------|----------------------------------|---------|
| **Terminal / Luke** | ✅ | — |
| **Hermes (Grok)** | ⚠️ via `run_terminal_cmd` if allowed | No stable tool contract; watchdog is read-only by design |
| **Cursor IDE Agent** | ⚠️ shell | No MCP server; rules don't mention AutoCode |
| **Cursor SDK** (`cursor-phone-gateway`) | ❌ | Separate agentId + queue |
| **Codex / Claude CLI** | ⚠️ shell in session | No shared discovery of fleet state |
| **remote-agent-chat-spike** | ❌ | Own `send_queue` per browser session |

---

## 4. Proposed architectures (options)

### Option A — **Fleet Control Plane** (recommended)

AutoCode becomes the **single orchestration API**; all providers are **workers**.

```
┌─────────────┐  ┌─────────────┐  ┌──────────────┐  ┌─────────────┐
│ Cursor IDE  │  │ Hermes/     │  │ cursor-phone │  │ Codex/      │
│ Agent+MCP   │  │ Signal      │  │ gateway      │  │ Claude CLI  │
└──────┬──────┘  └──────┬──────┘  └──────┬───────┘  └──────┬──────┘
       │                │                 │                  │
       └────────────────┴────────┬────────┴──────────────────┘
                                 ▼
                    ┌────────────────────────┐
                    │  AutoCode Control API   │
                    │  MCP + HTTP POST        │
                    │  (auth token, idempotent)│
                    └───────────┬────────────┘
                                ▼
                    ┌────────────────────────┐
                    │  Existing Scheduler     │
                    │  queue · goals · jobs   │
                    └────────────────────────┘
```

**Workstreams:** New table or generalization of `task_plans` + squad:

- `workstream_id`, `parent_chat_id`, `provider_affinity`, `depends_on[]`, `status`
- Scheduler fills slots respecting dependencies (DAG edges), not only FIFO queue position

**Cursor alignment:** Cursor Multitask `Task` maps to `workstream_create` + `run_in_background` → API returns `workstream_id`; parent polls `workstream_status` or receives webhook/SSE event (extend `/events`).

### Option B — **Dual orchestrators** (not recommended)

Cursor Multitask handles in-chat parallelism; AutoCode handles overnight fleet. Risk: duplicate work, conflicting edits, two queues (IDE subagents vs SQLite), confused mental model.

### Option C — **Relay-centric** (remote-agent-chat-spike as hub)

Extend relay `send_queue` to fleet operations. Pros: phone UI already exists. Cons: reimplements AutoCode goals/recovery; Mac must run relay + proxy; weaker for headless Codex/Grok.

**Recommendation:** **Option A — Fleet Control Plane.** Reuse AutoCode's scheduler, recovery, and SQLite; add a thin **control plane** and **workstream** model; treat Cursor multitask as a **client pattern**, not a second scheduler.

---

## 5. Control API sketch (Phase 1 POC)

### 5.1 MCP tools (for Cursor Agent + Hermes)

| Tool | Purpose |
|------|---------|
| `autocode_status` | Running jobs, queue depth, capacity, provider backoff |
| `autocode_queue_list` | Ordered queue with goals |
| `autocode_queue_add` | `{ query, goal, position?, provider_hint? }` |
| `autocode_drive` | Queue + optional immediate dispatch |
| `autocode_pause` / `autocode_done` | Lifecycle |
| `autocode_workstream_spawn` | Parallel child task (Phase 2) |
| `autocode_workstream_status` | Poll children |

### 5.2 HTTP (mirror MCP, for phone gateway / scripts)

```http
POST /v1/queue/items
GET  /v1/status
GET  /v1/events          # SSE (existing pattern)
POST /v1/workstreams     # Phase 2
```

Auth: `AUTOCODE_API_TOKEN` (localhost + Tailscale bind). Idempotency: `Idempotency-Key` header → dedupe in `events` table.

### 5.3 OpenAPI fragment (illustrative)

```yaml
paths:
  /v1/status:
    get: { summary: Fleet snapshot }
  /v1/queue/items:
    post:
      requestBody:
        content:
          application/json:
            schema:
              type: object
              required: [query, goal]
              properties:
                query: { type: string }
                goal: { type: string }
                position: { type: number }
                provider_hint: { type: string }
```

Implement by extending `autocode/web.py` `Handler` with POST routes calling existing `Store` / `Scheduler` methods (small, ~200 LOC).

---

## 6. Integration map (related repos)

| Repo | Relationship |
|------|----------------|
| **`~/autocode`** | Source of truth for fleet queue |
| **`~/coding/cursor-phone-gateway`** | Cursor SDK single-agent HTTP; candidate **client** posting to AutoCode instead of owning goals |
| **`~/coding/remote-agent-chat-spike`** | Per-session busy queue + multi-agent UI; use for **status display** or forward `queue_snapshot` events, not replace AutoCode |
| **Cursor SDK** (`@cursor/sdk`) | Worker for `cursor.cli` / cloud lanes; align `agentId` with discovered `provider_chat_id` where possible |
| **Hermes** | MCP client + Signal notify; bidirectional status = Phase 3 |

---

## 7. Phased next steps

### Phase 1 — Control plane (1–2 weeks)

1. Add `POST` handlers to `web` (or separate `autocode serve --control`) for queue add, pause, status.
2. Ship **`autocode-mcp`** package (stdio MCP) wrapping same functions.
3. Document env: `AUTOCODE_API_TOKEN`, bind `127.0.0.1` + optional Tailscale.
4. Cursor user rule: *"For fleet work use autocode MCP, not ad-hoc parallel drives."*

### Phase 2 — Workstreams (2–3 weeks)

1. Schema: `workstreams(id, parent_chat_id, objective, provider, status, depends_on_json)`.
2. Scheduler: allocate slots to ready workstreams; cap children per parent.
3. CLI: `autocode workstream spawn|list|cancel`.
4. Map Cursor `Task` semantics: spawn = workstream, background = non-blocking API.

### Phase 3 — Hermes / Signal bidirectional (1–2 weeks)

1. Extend grok-watchdog or separate `autocode notify` with structured events (job done, blocked, quota).
2. Inbound: Signal → Hermes → `autocode_queue_add` MCP tool (with confirmation for destructive ops).
3. Optional: subscribe phone gateway `/events` to AutoCode SSE for single pane.

### Phase 4 — Convergence (ongoing)

- `cursor-phone-gateway`: optional mode `ORCHESTRATOR=autocode` delegates goals to fleet API.
- Deprecate duplicate fill loops where `autocode-capacity` and daemon overlap (document single owner).

---

## 8. Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| **Dual writers** on same repo | High | Keep leases; worktree lane for experiments (squad pattern) |
| **IDE vs fleet chat mismatch** | Medium | Document `cursor.ide` fork behavior; prefer `cursor.cli` for same-thread goals |
| **Hermes shell control** | Medium | MCP with read-only default role + confirm for `drive` |
| **API auth on LAN** | Medium | Long token, localhost default, Tailscale ACLs |
| **Over-parallelism** | Medium | Per-provider caps in scheduler (use capacity weights for real) |
| **Watchdog vs orchestration confusion** | Low | Keep grok-watchdog read-only; fleet writes only via API |

---

## 9. Summary recommendation

**Name:** **Fleet Control Plane** (Option A)

**One-liner:** Cursor Multitask orchestrates *within a conversation*; AutoCode orchestrates *across the machine*. Expose AutoCode via **MCP + HTTP POST** so every provider agent is a client; add **workstreams** for Task-shaped parallelism; push **status** back to Hermes/Signal in Phase 3.

**Smallest useful POC:** POST `/v1/queue/items` + MCP `autocode_queue_add` / `autocode_status` calling existing `Store.queue_add` and `Scheduler` — no scheduler rewrite required.
