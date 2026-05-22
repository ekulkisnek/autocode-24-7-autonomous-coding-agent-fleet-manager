# AutoCode

Standalone 24/7 autonomous coding driver for Grok, Codex, Claude, Cursor, and Antigravity.

Hermes is only a thin client. AutoCode owns state in SQLite, discovers provider chats, adopts coding work, drives incomplete goals, monitors job evidence, and recovers silent/stalled jobs.

## Commands

```bash
autocode status
autocode now
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
