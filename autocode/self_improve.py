"""
AutoCode self-improvement engine.

Tracks IPC (Jobs Per Verified Completion) and generates fix tasks for
systematic failure patterns. Called from scheduler.tick() on a slow cadence.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .store import Store
from .util import iso_from_ts, json_dumps, now_iso, now_ts

AUTOCODE_DIR = Path("/Users/lukekensik/autocode")
IPC_LOG = AUTOCODE_DIR / "state" / "ipc_log.jsonl"
SCAN_INTERVAL_SECONDS = 1800          # run at most every 30 min
MIN_PATTERN_COUNT = 5                 # occurrences in 24h to trigger a fix task
MAX_FIX_TASKS_PER_SCAN = 2
FIX_COOLDOWN_SECONDS = 3600 * 6      # don't re-generate same fix within 6h


# ── data classes ─────────────────────────────────────────────────────────────

@dataclass
class IPCStats:
    total_completions: int
    total_jobs_on_completed: int
    avg_jobs_per_completion: float
    median_jobs: float
    p90_jobs: float
    recent_completions: int           # last 7 days
    recent_avg_jobs: float
    worst_chat_alias: str
    worst_chat_jobs: int


@dataclass
class FailurePattern:
    kind: str
    count: int
    example_reason: str
    fix_description: str


# ── failure pattern → fix description ────────────────────────────────────────

_PATTERN_FIXES: dict[str, str] = {
    "completion_rejected": (
        "verify_goal_complete() is rejecting valid completions — "
        "review completion detection in autocode/goals.py and autocode/policy.py. "
        "Look for cases where real work is dismissed as 'too minimal' or 'missing FLEET_DONE'."
    ),
    "silent_failed": (
        "jobs are silently failing without meaningful output — "
        "review stall detection thresholds in autocode/runner.py. "
        "Consider increasing stall_seconds for long-running tasks."
    ),
    "provider_error": (
        "provider API errors are causing excessive retries — "
        "review backoff logic in autocode/recovery.py. "
        "Ensure provider_in_backoff() is respected before re-dispatch."
    ),
    "goal_incomplete": (
        "goals are repeatedly judged incomplete despite real work in output — "
        "review assess_output_state() in autocode/policy.py. "
        "Check if the LLM prompt for completion assessment is too strict."
    ),
    "killed": (
        "jobs are being killed frequently, looping without progress — "
        "review stall timeout thresholds and kill conditions in autocode/runner.py. "
        "Check if recent_kill_count detection is catching kill loops."
    ),
    "false_complete": (
        "jobs are being marked complete but goals aren't verified — "
        "review verify_goal_complete() in autocode/goals.py. "
        "Tighten the evidence check so false positives don't close chats prematurely."
    ),
}


# ── IPC metric ────────────────────────────────────────────────────────────────

def ipc_stats(store: Store) -> IPCStats:
    """Compute jobs-per-verified-completion stats from the DB."""
    rows = store.rows("""
        select c.id, c.alias, count(j.id) as job_count
        from chats c
        join jobs j on j.chat_id = c.id
        where c.done = 1
          and c.last_evidence_at != ''
        group by c.id
        having count(j.id) > 0
    """)
    if not rows:
        return IPCStats(0, 0, 0.0, 0.0, 0.0, 0, 0.0, "", 0)

    counts = sorted((int(r["job_count"]), str(r["alias"] or r["id"])) for r in rows)
    nums = [c for c, _ in counts]
    total = len(nums)
    total_jobs = sum(nums)
    avg = total_jobs / total
    median = float(nums[total // 2])
    p90 = float(nums[int(total * 0.9)])
    worst_jobs, worst_alias = counts[-1]

    recent = store.rows("""
        select count(j.id) as job_count
        from chats c
        join jobs j on j.chat_id = c.id
        where c.done = 1
          and c.last_evidence_at > datetime('now', '-7 days')
        group by c.id
        having count(j.id) > 0
    """)
    recent_nums = [int(r["job_count"]) for r in recent]
    recent_avg = sum(recent_nums) / len(recent_nums) if recent_nums else 0.0

    return IPCStats(
        total_completions=total,
        total_jobs_on_completed=total_jobs,
        avg_jobs_per_completion=round(avg, 1),
        median_jobs=median,
        p90_jobs=p90,
        recent_completions=len(recent_nums),
        recent_avg_jobs=round(recent_avg, 1),
        worst_chat_alias=worst_alias,
        worst_chat_jobs=worst_jobs,
    )


def format_ipc_report(store: Store) -> str:
    s = ipc_stats(store)
    lines = [
        "AutoCode IPC (Jobs Per Verified Completion)",
        "=" * 45,
        f"All time: {s.total_completions} completions, {s.total_jobs_on_completed} total jobs",
        f"  avg:    {s.avg_jobs_per_completion:.1f} jobs/completion",
        f"  median: {s.median_jobs:.0f}  |  p90: {s.p90_jobs:.0f}",
        f"Last 7d: {s.recent_completions} completions, avg {s.recent_avg_jobs:.1f} jobs",
        f"Worst:   {s.worst_chat_alias!r} ({s.worst_chat_jobs} jobs)",
        "",
        "Target: IPC avg < 5 (currently: " + _rating(s.avg_jobs_per_completion) + ")",
        "Meta-goal: /Users/lukekensik/autocode/METAGOAL.md",
    ]
    patterns = analyze_failure_patterns(store)
    if patterns:
        lines.append("\nTop failure patterns (24h):")
        for p in patterns[:5]:
            lines.append(f"  {p.kind:<22} {p.count:>4}x")
    return "\n".join(lines)


def _rating(avg: float) -> str:
    if avg <= 1.5:
        return "EXCELLENT"
    if avg <= 5:
        return "good"
    if avg <= 15:
        return "needs work"
    return "BROKEN"


# ── failure pattern analysis ──────────────────────────────────────────────────

def analyze_failure_patterns(store: Store) -> list[FailurePattern]:
    patterns = []
    for kind, fix_desc in _PATTERN_FIXES.items():
        row = store.row("""
            select count(*) as n
            from events
            where kind = 'recovery_scheduled'
              and ts > datetime('now', '-24 hours')
              and json_extract(details_json, '$.failure_kind') = ?
        """, (kind,))
        count = int(row["n"] if row else 0)
        if count < MIN_PATTERN_COUNT:
            continue
        example = store.row("""
            select json_extract(details_json, '$.evidence_status') as reason
            from events
            where kind = 'recovery_scheduled'
              and ts > datetime('now', '-24 hours')
              and json_extract(details_json, '$.failure_kind') = ?
            limit 1
        """, (kind,))
        reason = str(example["reason"] if example else "")
        patterns.append(FailurePattern(kind, count, reason, fix_desc))
    return sorted(patterns, key=lambda p: p.count, reverse=True)


# ── fix task generation ───────────────────────────────────────────────────────

def _recently_generated_fix(store: Store, kind: str) -> bool:
    cutoff = iso_from_ts(now_ts() - FIX_COOLDOWN_SECONDS)
    row = store.row("""
        select 1 from events
        where kind = 'self_improve_fix_generated'
          and ts > ?
          and details_json like ?
        limit 1
    """, (cutoff, f'%"{kind}"%'))
    return bool(row)


def _create_fix_task(store: Store, pattern: FailurePattern) -> str | None:
    from .models import Chat

    chat_id = f"self-improve-{pattern.kind}-{uuid.uuid4().hex[:8]}"
    objective = (
        f"[SELF-IMPROVEMENT] Fix systematic '{pattern.kind}' failures in autocode.\n"
        f"Occurrences (last 24h): {pattern.count}\n\n"
        f"Task: {pattern.fix_description}\n\n"
        f"Steps:\n"
        f"1. Identify the root cause in the relevant source file(s)\n"
        f"2. Implement the fix\n"
        f"3. Run: cd /Users/lukekensik/autocode && python3 -m pytest tests/ -x -q\n"
        f"4. Confirm tests pass\n"
        f"5. Summarize what was changed and why\n\n"
        f"The goal is complete when tests pass and the fix is in place."
    )
    chat = Chat(
        id=chat_id,
        provider="grok",
        source="grok.self_improve",
        provider_chat_id=chat_id,
        title=f"self-improve: {pattern.kind}",
        cwd=str(AUTOCODE_DIR),
        updated_at=now_iso(),
        latest_text="",
        transcript_hash="",
        alias=f"si-{pattern.kind[:20]}",
        continuation="",
        metadata={
            "self_improve": True,
            "failure_kind": pattern.kind,
            "gw_briefing_notes": (
                f"SELF-IMPROVEMENT TASK\n"
                f"You are fixing the autocode daemon itself at /Users/lukekensik/autocode/\n"
                f"Problem: '{pattern.kind}' failures happening {pattern.count}x in 24h\n"
                f"Fix focus: {pattern.fix_description}\n"
                f"After fixing, run tests: python3 -m pytest tests/ -x -q\n"
                f"This is high-priority — fixing it directly improves IPC."
            ),
        },
    )
    store.upsert_chat(chat, coding_score=95, state="active", objective=objective)
    store.queue_add(chat_id, position=2.0)
    return chat_id


def generate_fix_tasks(store: Store) -> list[str]:
    """Generate self-improvement fix tasks for top failure patterns. Returns chat_ids."""
    patterns = analyze_failure_patterns(store)
    created = []
    for pattern in patterns[:MAX_FIX_TASKS_PER_SCAN]:
        if _recently_generated_fix(store, pattern.kind):
            continue
        chat_id = _create_fix_task(store, pattern)
        if chat_id:
            created.append(chat_id)
            store.event(
                "self_improve_fix_generated",
                chat_id,
                failure_kind=pattern.kind,
                count=pattern.count,
            )
    return created


# ── IPC log ───────────────────────────────────────────────────────────────────

def _append_ipc_log(entry: dict) -> None:
    IPC_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(IPC_LOG, "a") as f:
        f.write(json_dumps(entry) + "\n")


# ── main scan entry point ─────────────────────────────────────────────────────

def _last_scan_ts(store: Store) -> float:
    row = store.row("""
        select ts from events where kind = 'self_improve_scan'
        order by ts desc limit 1
    """)
    if not row:
        return 0.0
    from .util import parse_ts
    return parse_ts(str(row["ts"]))


def scan(store: Store, force: bool = False) -> dict | None:
    """
    Run the full self-improvement scan. Skipped if called too recently.
    Returns scan result dict or None if skipped.
    """
    if not force and (now_ts() - _last_scan_ts(store)) < SCAN_INTERVAL_SECONDS:
        return None

    stats = ipc_stats(store)
    patterns = analyze_failure_patterns(store)
    created = generate_fix_tasks(store)

    result: dict[str, Any] = {
        "ts": now_iso(),
        "ipc_avg": stats.avg_jobs_per_completion,
        "ipc_recent": stats.recent_avg_jobs,
        "ipc_median": stats.median_jobs,
        "ipc_p90": stats.p90_jobs,
        "completions": stats.total_completions,
        "recent_completions": stats.recent_completions,
        "patterns_found": len(patterns),
        "patterns": [{"kind": p.kind, "count": p.count} for p in patterns],
        "fix_tasks_created": len(created),
        "fix_task_ids": created,
    }

    _append_ipc_log(result)
    store.event(
        "self_improve_scan",
        avg_ipc=stats.avg_jobs_per_completion,
        recent_ipc=stats.recent_avg_jobs,
        patterns=len(patterns),
        fix_tasks=len(created),
    )
    return result
