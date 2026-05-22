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

- Source: `/Users/lukekensik/autocode`
- DB: `/Users/lukekensik/autocode/state/autocode.sqlite`
- Jobs: `/Users/lukekensik/autocode/state/jobs`
- Logs: `/Users/lukekensik/autocode/logs/autocode.log`

