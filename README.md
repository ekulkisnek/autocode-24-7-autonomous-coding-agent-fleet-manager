# AutoCode

Standalone 24/7 autonomous coding driver for Grok, Codex, Claude, Cursor, and Antigravity.

Hermes is only a thin client. AutoCode owns state in SQLite, discovers provider chats, adopts coding work, drives incomplete goals, monitors job evidence, and recovers silent/stalled jobs.

## Commands

```bash
autocode status
autocode now
autocode dashboard
autocode goals
autocode chats --recent 24h
autocode drive <query> --goal "<goal>"
autocode yolo on
autocode pause <query>
autocode done <query>
autocode logs
autocode doctor
autocode daemon start|stop|restart|install
```

`autocode dashboard` is the live terminal view. It opens a single scrollable terminal viewport and shows current driving jobs, watched priorities, next scheduler candidates, model/effort/speed where detectable, resource/capacity state, recent evidence, and observed provider usage windows.

```bash
autocode dashboard                  # live scrollable terminal dashboard
autocode dashboard --interval 1     # faster refresh
autocode dashboard --once --limit 8 # one snapshot for logs/Hermes
autocode dashboard --append-history # keep every refresh frame in scrollback
autocode dashboard --alt-screen     # alternate-screen dashboard
```

Live mode uses a terminal UI by default so there is one copy of the dashboard; scroll with arrow keys, Page Up/Page Down, Home/End, and press `q` to quit. Use `--append-history` when you want every refresh preserved as a separate frame, or `--alt-screen` when you want the older full-screen print renderer. Exact provider quota remaining is only shown when a provider exposes a reliable local endpoint. Otherwise the dashboard reports `not exposed` and shows observed AutoCode usage counts for `1h`, `24h`, and `7d` instead of guessing.

## Cursor

AutoCode indexes Cursor from four sources:

- `cursor.cli`: local Cursor Agent CLI chat stores in `~/.cursor/chats/**/store.db`; same-chat sends use `cursor-agent --resume`.
- `cursor.cloud`: Cursor Cloud Agents from `api.cursor.com` plus local Cursor IDE cloud composer references; `bc-*` agents get same-agent follow-ups through the Cursor Cloud API.
- `cursor.ide`: local Cursor IDE composer metadata from `~/Library/Application Support/Cursor/User/**/state.vscdb`; readable and searchable, with continuation through a new Cursor Agent worker unless Cursor exposes a stable same-chat IDE API.
- `cursor.transcript`: Cursor project transcript JSONL files in `~/.cursor/projects/**/agent-transcripts`.

```bash
autocode cursor status
autocode cursor chats --source cursor.cli --limit 20
autocode cursor chats --source cursor.cloud --limit 20
autocode cursor history <query>
autocode cursor models
autocode cursor model                 # show default model
autocode cursor model auto            # set default model
autocode cursor model composer-2.5    # set default model
autocode cursor new --workspace <path> --goal "<goal>" --model composer-2.5
autocode drive <cursor-query-or-id> --goal "<goal>" --model composer-2.5
```

Headless Cursor sends load `CURSOR_API_KEY` from the environment, `~/.hermes/.env`, or `~/grok-cursor-bridge/.env` without storing the key in AutoCode state.
The default Cursor model is stored as AutoCode config key `cursor_model` and defaults to `auto`; per-send `--model` overrides do not change that default.

## State

- Source: `$AUTOCODE_HOME` or `~/autocode`
- DB: `$AUTOCODE_HOME/state/autocode.sqlite`
- Jobs: `$AUTOCODE_HOME/state/jobs`
- Logs: `$AUTOCODE_HOME/logs/autocode.log`

## Drive until goal

AutoCode keeps assigned chats on the queue and dispatches new turns until a goal is **verified** complete—not when a provider merely exits or claims success.

Completion requires a structured `FLEET_DONE` marker (default) or assessor-verified criteria with substantive output. Milestones (`FLEET_MILESTONE`), minimal/empty stdout, and premature `done=1` flags are rejected; the daemon re-opens false completes on the next tick and schedules `goal_incomplete` retries. Stall detection uses stdout/stderr heartbeats (file mtimes reset the clock); e2e-style goals get longer stall windows.

Terminal job outcomes (`silent_failed`, `provider_error`, `killed`, `chat_paused` without `paused=1`, false `completed`) schedule exponential backoff retries up to the goal retry cap. `autocode pause` is intentional: `paused=1` blocks recovery and dispatch.

## Reliability / recovery

Environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `AUTOCODE_REQUIRE_FLEET_DONE` | `1` | Require `FLEET_DONE` before marking goals complete |
| `AUTOCODE_MIN_OUTPUT_CHARS` | `48` | Reject “worked” completion below this output size |
| `AUTOCODE_MAX_GOAL_RETRIES` | `20` | Max retries per queued chat with an active goal |
| `AUTOCODE_STALL_SECONDS` | `600` | No-output stall window (e2e goals use at least `1800`) |
| `AUTOCODE_MAX_FAILURE_RETRIES` | `8` | Max failure streak for non-goal chats |
| `AUTOCODE_PRIORITY_MAX_FAILURE_RETRIES` | `12` | Higher limit for chats with an active priority pin |
| `AUTOCODE_RETRY_BACKOFF_BASE` | `30` | Initial retry delay (seconds) |
| `AUTOCODE_RETRY_BACKOFF_MAX` | `900` | Cap on retry backoff (seconds) |
| `AUTOCODE_MAX_GOAL_RETRIES` | `20` | Max retries for queued chats with an active goal |
| `AUTOCODE_MIN_OUTPUT_CHARS` | `48` | Minimum output size before a turn counts as progress |
| `AUTOCODE_REQUIRE_FLEET_DONE` | `1` | Require `FLEET_DONE` before goal completion |
| `AUTOCODE_JOB_TIMEOUT` | `1800` | Non-Cursor job wall clock |
| `AUTOCODE_CURSOR_JOB_TIMEOUT` | `14400` | Cursor job wall clock |
| `AUTOCODE_TICK_INTERVAL` | `2` | Daemon tick interval (seconds) |
| `AUTOCODE_MAX_ACTIVE` | `5` | Max concurrent jobs |

Explicit `autocode pause` sets `paused=1` and stops auto-recovery for that chat until you resume it.

## Autonomous multi-turn operation

AutoCode is designed to keep driving queued chats across many agent turns without manual `autocode drive` or opening chats in the IDE.

**What Luke needs**

1. Daemon running: `autocode daemon install` (launchd) or `autocode daemon start`
2. YOLO on (default in fresh DB): `autocode yolo on`
3. Queue a chat with a goal: `autocode drive <query> --goal "<goal>"` (or `autocode queue add <query>` after setting a goal)

After that, each finished job is assessed. If the goal is incomplete (including partial `FLEET_MILESTONE` progress), the scheduler schedules the next turn on the same chat, bumps it toward the front of the queue, and injects the prior job summary into the next prompt. No resume click in Cursor/Grok/Codex is required.

**Daemon / launchd**

```bash
autocode daemon install   # writes ~/Library/LaunchAgents/com.lukekensik.autocode.plist
autocode daemon restart   # after code changes
autocode daemon status
tail -f ~/autocode/logs/autocode.log
```

The launchd plist sets `AUTOCODE_HOME`, tick interval, and stall/timeouts. Keep the daemon alive (`KeepAlive`); AutoCode re-dispatches when capacity frees up.

**Environment**

| Variable | Default | Purpose |
|----------|---------|---------|
| `AUTOCODE_YOLO` | (config `yolo=on`) | When on, keeps at least one dispatch slot while the queue has work even under memory pressure |
| `AUTOCODE_MAX_GOAL_RETRIES` | `20` | Max failure streak for queued chats with an active goal |
| `AUTOCODE_REQUIRE_FLEET_DONE` | `1` | Require `FLEET_DONE` marker before marking a goal complete |
| `AUTOCODE_MIN_OUTPUT_CHARS` | `48` | Treat shorter output as incomplete (next turn scheduled) |
| `AUTOCODE_TICK_INTERVAL` | `2` | Daemon scheduler period (seconds) |
| `AUTOCODE_MAX_ACTIVE` | `5` | Concurrent jobs |

**Human-only blockers**

- Explicit `autocode pause` on a chat
- Provider auth / quota errors until credentials recover
- `needs_input` when the agent reports user-gated work
- Retry limits exhausted (`AUTOCODE_MAX_GOAL_RETRIES` or priority limits)

## Newer Control Surfaces

```bash
autocode priority list
autocode priority add <query> --goal "<goal>" --rank 100 --path <repo> --exact --lanes 3
autocode last <query>
autocode squad plan <query>
autocode squad launch <query>
autocode squad collect <query> --send-writer
```

Priority projects pin important work to exact provider chats and repository resources. Squad lanes add bounded helper agents for read-only investigation or isolated worktree experiments while the main writer remains authoritative.
