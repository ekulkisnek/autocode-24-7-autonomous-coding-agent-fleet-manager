"""Adaptive goal supervisor: track failure signatures and escalate repeated blockers."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ROOT

LOG_ROOT = Path("/Volumes/T705/redwallet-logs")
STATE_PATH = ROOT / "state" / "goal-supervisor-state.json"
MAX_SIGNATURE_HISTORY = 10
ESCALATION_THRESHOLD = 3

L1_RUN_SYMLINKS = (
    "current-l1-simulator-bidirectional-e2e",
    "current-l1-ios-android-e2e",
    "current-l1-android-ios-e2e",
)

SIGNATURE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("TransactionValue", r"transactionvalue|TransactionValue"),
    ("WalletsList", r"walletslist|WalletsList"),
    ("ResetWedge", r"resetToWalletsList|reset wedge|app[- ]busy|app-busy"),
    ("L1SendE2E", r"l1sende2e|L1SendE2E|CreateTransactionButton"),
    ("BalanceSync", r"0 sats|insufficient balance|scripthash|electrum|balance"),
    ("FundVerify", r"verifytxpaysaddress|fund-l1|skip[- ]seed"),
    ("DetoxTimeout", r"detox.*timeout|timed out waiting"),
)


def find_latest_l1_run_dir() -> Path | None:
    for name in L1_RUN_SYMLINKS:
        link = LOG_ROOT / name
        if link.is_symlink():
            target = link.resolve()
            if target.is_dir():
                return target
    candidates = sorted(
        LOG_ROOT.glob("l1-simulator-bidirectional-e2e-*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _log_lines(run_dir: Path | None) -> list[str]:
    lines: list[str] = []
    if run_dir:
        for rel in ("ios-to-android/detox.log", "android-to-ios/detox.log", "SUMMARY.txt"):
            path = run_dir / rel
            if path.is_file():
                try:
                    lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
                except OSError:
                    pass
    autocode_log = LOG_ROOT / "l1-e2e-until-verified-autocode.log"
    if autocode_log.is_file():
        try:
            lines.extend(autocode_log.read_text(encoding="utf-8", errors="replace").splitlines()[-30:])
        except OSError:
            pass
    return lines


def extract_failure_signature(run_dir: Path | None = None) -> str:
    """Return a compact failure signature (named pattern or first error line)."""
    run_dir = run_dir or find_latest_l1_run_dir()
    lines = _log_lines(run_dir)
    blob = "\n".join(lines)
    for name, pattern in SIGNATURE_PATTERNS:
        if re.search(pattern, blob, re.IGNORECASE):
            return name
    for line in reversed(lines):
        low = line.lower()
        if any(token in low for token in ("error", "fail", "timeout", "timed out", "exception")):
            cleaned = line.strip()
            if cleaned:
                return cleaned[:120]
    return "unknown"


def load_state() -> dict[str, Any]:
    if not STATE_PATH.is_file():
        return {
            "attempt_count": 0,
            "failure_signatures": [],
            "last_signature": "",
            "last_run_dir": "",
            "last_updated": "",
        }
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"attempt_count": 0, "failure_signatures": [], "last_signature": "", "last_run_dir": ""}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def repeated_signature_count(signatures: list[str]) -> int:
    if not signatures:
        return 0
    last = signatures[-1]
    if not last or last == "unknown":
        return 0
    count = 0
    for sig in reversed(signatures):
        if sig == last:
            count += 1
        else:
            break
    return count


def current_adaptive_context() -> dict[str, Any]:
    """Read adaptive context without incrementing attempt count."""
    state = load_state()
    run_dir = find_latest_l1_run_dir()
    signature = extract_failure_signature(run_dir)
    history = list(state.get("failure_signatures") or [])
    repeat_count = repeated_signature_count(history) if history else 0
    escalated = repeat_count >= ESCALATION_THRESHOLD
    return {
        "attempt_count": int(state.get("attempt_count") or 0),
        "last_signature": signature or state.get("last_signature") or "unknown",
        "repeat_count": repeat_count,
        "same_failure_repeated": escalated,
        "latest_run_dir": str(run_dir) if run_dir else str(state.get("last_run_dir") or ""),
        "escalated": escalated,
    }


def adaptive_context_for_dispatch(*, min_interval_sec: int = 60) -> dict[str, Any]:
    """Record a tick if stale; otherwise return current context (avoids double-count)."""
    state = load_state()
    last_updated = str(state.get("last_updated") or "")
    if last_updated:
        try:
            last = datetime.strptime(last_updated, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc).timestamp() - last.timestamp()
            if age < min_interval_sec:
                return current_adaptive_context()
        except ValueError:
            pass
    return record_tick(goals_complete=False)


def record_tick(*, goals_complete: bool = False) -> dict[str, Any]:
    """Record a supervisor tick; return adaptive context for prompts."""
    state = load_state()
    if goals_complete:
        state["attempt_count"] = 0
        state["failure_signatures"] = []
        state["last_signature"] = ""
        save_state(state)
        return {"attempt_count": 0, "goals_complete": True}

    run_dir = find_latest_l1_run_dir()
    signature = extract_failure_signature(run_dir)
    attempt = int(state.get("attempt_count") or 0) + 1
    history = list(state.get("failure_signatures") or [])
    history.append(signature)
    history = history[-MAX_SIGNATURE_HISTORY:]

    repeat_count = repeated_signature_count(history)
    escalated = repeat_count >= ESCALATION_THRESHOLD

    state.update(
        {
            "attempt_count": attempt,
            "failure_signatures": history,
            "last_signature": signature,
            "last_run_dir": str(run_dir) if run_dir else "",
            "repeat_count": repeat_count,
            "escalated": escalated,
        }
    )
    save_state(state)

    return {
        "attempt_count": attempt,
        "last_signature": signature,
        "repeat_count": repeat_count,
        "same_failure_repeated": escalated,
        "latest_run_dir": str(run_dir) if run_dir else "",
        "escalated": escalated,
    }


def format_adaptive_block(ctx: dict[str, Any]) -> str:
    if ctx.get("goals_complete"):
        return ""
    lines = [
        "## Adaptive supervisor (improving over time)",
        f"- Supervisor attempt: {ctx.get('attempt_count', 0)}",
        f"- Last failure signature: {ctx.get('last_signature') or 'unknown'}",
        f"- Same signature streak: {ctx.get('repeat_count', 0)}",
    ]
    run_dir = ctx.get("latest_run_dir") or ""
    if run_dir:
        lines.append(f"- Latest run dir: {run_dir}")
    if ctx.get("same_failure_repeated"):
        lines.append(
            "- ESCALATION: prior fix did not work — try a **different approach**, "
            "not the same patch again."
        )
    return "\n".join(lines)


def format_escalation_block(ctx: dict[str, Any]) -> str:
    if not ctx.get("escalated"):
        return ""
    sig = ctx.get("last_signature") or "unknown"
    streak = ctx.get("repeat_count") or ESCALATION_THRESHOLD
    return f"""## ESCALATION (same blocker {streak}x: {sig})

The last {streak} supervisor cycles hit the same failure signature. Prior fixes did NOT resolve it.
- Do NOT repeat the same approach (grep for what was already tried in recent commits/logs).
- Pick a different root-cause hypothesis and validate before coding.
- Consider parallel Mac fix workers and cross-check both Detox directions (ios→android AND android→ios).
- Latest run dir: {ctx.get('latest_run_dir') or '(unknown)'}"""


def maybe_bump_l1_mac_workers(store: Any, ctx: dict[str, Any]) -> dict[str, Any]:
    """Temporarily raise Mac fix worker cap when the same Detox failure repeats."""
    if not ctx.get("escalated"):
        return {"bumped": False}
    current = int(store.get_config("l1_max_mac_fix_workers", "1") or 1)
    target = max(current, 2)
    if target > current:
        store.set_config("l1_max_mac_fix_workers", str(target))
        return {"bumped": True, "from": current, "to": target}
    return {"bumped": False, "cap": current}
