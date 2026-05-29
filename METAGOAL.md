# AutoCode Meta-Goal: Zero-Babysit Autonomous Completion

## The Goal

**One prompt. No follow-up. Verified done.**

Luke sends a single natural-language task. AutoCode runs it until the goal is
genuinely, verifiably complete — no re-prompts, no babysitting, no checking in.
AutoCode ships the result. Luke never touches it again.

## The Metric: IPC (Inputs Per Completion)

**IPC = total human inputs / verified completions**

- IPC = 1.0 → perfect (one prompt, done)
- IPC = 2.0 → Luke had to re-prompt once per task on average
- IPC = 10.0 → broken (current baseline, some tasks took 488 jobs)

Track with: `python3 -m autocode ipc`

### What counts as an "input"
- The original task prompt: always 1
- Any re-queue, re-prompt, goal change, manual watchdog action: +1 each
- Pausing a chat and re-enabling it: +1

### What counts as "verified completion"
- `verify_goal_complete()` returned True on the last job output
- The chat is marked `done=1` with `last_evidence_at` set
- NOT: chat stuck in queue, not: false completion

## Targets

| Milestone | IPC Target | What it means                                      |
|-----------|------------|----------------------------------------------------|
| Now       | ~15-50     | Current reality — most tasks need intervention     |
| Phase 1   | < 5        | Rare intervention — good for well-defined tasks    |
| Phase 2   | < 2        | Occasional edge cases only                         |
| Phase 3   | ≤ 1.1      | Essentially autonomous — only exotic tasks trip up |

## Self-Improvement Mechanism

AutoCode fixes itself when it repeatedly fails a class of task:

1. **Grok watchdog** classifies failures into patterns (silent_failed, goal_incomplete, etc.)
2. **self_improve.py** detects when a pattern exceeds threshold (5+ in 24h)
3. A fix task is created **in the autocode queue itself**, targeting `/Users/lukekensik/autocode`
4. AutoCode runs the fix, tests it, marks done
5. IPC is re-measured — improvement tracked

The watchdog also flags "AUTOCODE_BUG" patterns (not task failures) and proposes
code changes via the WATCHDOG_JSON action block.

## Challenges (Benchmark Suite)

The 10 canonical challenges test specific IPC failure modes. Run them:

    python3 -m autocode ipc --challenges

Results tracked in `state/ipc_log.jsonl`.

## Current Baseline (2026-05-28)

- Total completed chats (done=1): see `autocode ipc`
- Worst case: 488 jobs for one task
- Target date for IPC < 5: 2026-06-30

## Anti-Patterns (What We're Eliminating)

- Completion rejected because output is "too minimal" but actually is done
- Silent failures that loop 50x before anyone notices
- Missing FLEET_DONE marker causing fake "not done" state
- Provider errors retrying instantly (should back off)
- Tasks with vague objectives never reaching a verifiable end state
- Luke having to type "keep trying" or "it didn't work" to unstick things
