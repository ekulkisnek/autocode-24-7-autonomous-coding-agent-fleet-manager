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

`autocode dashboard` is the live terminal view. It updates one dashboard copy on the normal terminal screen and shows current driving jobs, watched priorities, next scheduler candidates, model/effort/speed where detectable, resource/capacity state, recent evidence, and observed provider usage windows.

```bash
autocode dashboard                  # live ASCII dashboard, one updating copy
autocode dashboard --interval 1     # faster refresh
autocode dashboard --once --limit 8 # one snapshot for logs/Hermes
autocode dashboard --append-history # keep every refresh frame in scrollback
autocode dashboard --alt-screen     # alternate-screen dashboard
```

Live mode uses the normal terminal screen by default, updating one copy without clearing scrollback so long dashboards can still be scrolled in the terminal. Use `--append-history` when you want every refresh preserved as a separate frame, or `--alt-screen` when you want a full-screen panel owned by the dashboard. Exact provider quota remaining is only shown when a provider exposes a reliable local endpoint. Otherwise the dashboard reports `not exposed` and shows observed AutoCode usage counts for `1h`, `24h`, and `7d` instead of guessing.

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
