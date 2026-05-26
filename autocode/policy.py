from __future__ import annotations

import re
from dataclasses import dataclass
from sqlite3 import Row

from .util import compact, parse_ts
from .markers import parse_fleet_marker

CODING_WORDS = re.compile(
    r"\b(code|coding|repo|repository|git|build|test|tests|lint|compile|app|site|extension|wallet|"
    r"frontend|backend|api|database|sqlite|typescript|python|swift|rust|go|react|node|package|"
    r"implementation|bug|fix|refactor|deploy|e2e|detox|chrome|cursor|codex|grok build)\b",
    re.I,
)
FLEET_DONE_MARKER = re.compile(r"(?im)^\s*FLEET_DONE(?:\s*:|\b)")
FLEET_MILESTONE_MARKER = re.compile(r"(?im)^\s*(FLEET_MILESTONE_COMPLETE|FLEET_MILESTONE\s*:)\b")
DONE_WORDS = re.compile(r"\b(fully complete|complete and verified|done\.?$|nothing left|all tests pass(?:ed)?)\b", re.I)
MILESTONE_WORDS = re.compile(r"\b(milestone complete|next step|remaining|still needs|todo|blocked|needs input|shall i proceed|continue)\b", re.I)
ASK_WORDS = re.compile(r"\b(shall i|should i|do you want|would you like|please confirm|need permission|waiting for|blocked)\b", re.I)
BLOCKED_NONCODING = re.compile(
    r"\b(format|erase|partition|wipe)\b.{0,80}\b(drive|disk|ssd|volume)\b|"
    r"\b(create|make|register|sign\s*up|signup)\b.{0,80}\b(accounts?|profiles?)\b|"
    r"\b(keys?|passwords?|passphrases?|secrets?|tokens?|credentials?)\b.{0,80}\b(leak|dump|includ(?:e|ing)|show|print|share|expose|writeup|all)\b|"
    r"\b(leak|dump|includ(?:e|ing)|show|print|share|expose)\b.{0,80}\b(keys?|passwords?|passphrases?|secrets?|tokens?|credentials?)\b|"
    r"\b(ocmma|hardware plugged|fleet captain|captain brain|smoke test|hermes autonomous coding fleet)\b",
    re.I,
)
NOT_COMPLETE_WORDS = re.compile(
    r"\b(not\s+(?:complete|done|finished|verified)|still\s+(?:not|needs?|missing|blocked)|"
    r"remaining|current blocker|blocker:|next automatic step|next step|todo|needs input|"
    r"does not complete|failed|fails|stuck|stall(?:ed|s)?)\b",
    re.I,
)
USER_GATED_WORDS = re.compile(
    r"\b(user action required|need(?:s)? (?:you|luke|hermes)|please grant|permission required|"
    r"waiting for (?:you|luke|permission)|manual action required|cannot proceed without)\b",
    re.I,
)
COMPLETION_CLAIM_WORDS = re.compile(
    r"\b(complete(?:d)?|finished|done|production ready|fully verified|all tests pass(?:ed)?|"
    r"shipped|wrapped up|no remaining work|nothing left)\b",
    re.I,
)
VERIFICATION_WORDS = re.compile(
    r"\b(verif(?:y|ied|ication)|test(?:s|ed|ing)?|pass(?:ed|ing)?|green|"
    r"lint(?:ed)?|typecheck(?:ed)?|tsc|jest|pytest|cargo test|detox|smoke)\b",
    re.I,
)
ONGOING_OBJECTIVE_WORDS = re.compile(
    r"\b(ongoing|continuous|continually|keep working|keep polishing|until (?:luke|user|i) (?:explicitly )?(?:says?|tell)|until .*stop)\b",
    re.I,
)


@dataclass(frozen=True)
class OutputAssessment:
    state: str
    complete: bool
    reason: str
    missing: tuple[str, ...] = ()


def classify_chat(title: str, cwd: str, latest: str) -> tuple[int, str, str]:
    text = " ".join([title or "", cwd or "", latest or ""])
    if BLOCKED_NONCODING.search(text):
        return 0, "blocked", infer_objective(title, latest, cwd)
    score = 0
    if CODING_WORDS.search(text):
        score += 5
    if "/coding/" in cwd or "/projects/" in cwd or "/Documents/Codex/" in cwd:
        score += 4
    if re.search(r"\b(implemented|changed files|tests|build passed|error|traceback|diff --git)\b", text, re.I):
        score += 3
    if len(text) > 500:
        score += 1
    if FLEET_DONE_MARKER.search(latest or "") or (DONE_WORDS.search(latest or "") and not (FLEET_MILESTONE_MARKER.search(latest or "") or MILESTONE_WORDS.search(latest or ""))):
        state = "done"
    elif ASK_WORDS.search(latest or ""):
        state = "needs_input"
    elif score > 0:
        state = "active"
    else:
        state = "reference"
    objective = infer_objective(title, latest, cwd)
    return score, state, objective


def infer_objective(title: str, latest: str, cwd: str) -> str:
    title = compact(title, 180)
    latest = compact(latest, 260)
    if title:
        return f"Drive this coding conversation to completion: {title}"
    if latest:
        return f"Continue the coding task until it is implemented, verified, and clearly complete: {latest}"
    return f"Continue the coding project in {cwd or 'the current workspace'} until it is implemented, tested, and complete."


def should_continue_after_output(text: str) -> bool:
    if not text:
        return True
    marker = parse_fleet_marker(text)
    if marker:
        return marker.kind != "FLEET_DONE"
    if FLEET_DONE_MARKER.search(text):
        return False
    if FLEET_MILESTONE_MARKER.search(text):
        return True
    return bool(MILESTONE_WORDS.search(text) or ASK_WORDS.search(text))


def hard_requirement_gaps(objective: str, output: str) -> list[str]:
    text = (output or "").lower()
    goal = (objective or "").lower()
    if "hard requirement" not in goal and "hard completion definition" not in goal:
        return []

    missing: list[str] = []
    if not VERIFICATION_WORDS.search(output or ""):
        missing.append("verification/tests")
    if re.search(r"\b(live|full-system|full system|smoke|e2e|detox|restart)\b", goal) and not re.search(
        r"\b(live|full-system|full system|smoke|e2e|detox|restart|txid|tx=|asset_id|pushed heads?)\b", text
    ):
        missing.append("live/e2e evidence")
    if re.search(r"\b(persist|persistence|durable|restart|storage)\b", goal) and not re.search(
        r"\b(persist|persistence|durable|restart|storage|cache|reload)\b", text
    ):
        missing.append("persistence evidence")
    for required in ("utreexo", "proof"):
        if required in goal and required not in text:
            missing.append(required)
    checks = [
        ("asset creation", ("asset", "creat")),
        ("sending", ("send", "sent", "transfer")),
        ("receiving", ("receiv",)),
    ]
    for label, needles in checks:
        if label in goal and not any(n in text for n in needles):
            missing.append(label)
    return missing


def hard_completion_has_substantial_evidence(objective: str, output: str) -> bool:
    goal = (objective or "").lower()
    if "hard requirement" not in goal and "hard completion definition" not in goal:
        return True
    text = (output or "").lower()
    if hard_requirement_gaps(objective, output):
        return False
    evidence_groups = [
        ("implementation", ("implemented", "changed", "commit", "pushed heads", "constructors", "protocol", "client")),
        ("verification", ("test", "passed", "verified", "green", "cargo", "jest", "detox", "e2e", "smoke")),
        ("live proof", ("live", "full-system", "full system", "asset_id", "tx", "txid", "sidechain", "restart")),
        ("persistence", ("persist", "persistence", "durable", "restart", "cache", "reload", "storage")),
    ]
    hits = sum(1 for _, needles in evidence_groups if any(needle in text for needle in needles))
    return hits >= 2 and len(text.split()) >= 12


def assess_output_state(objective: str, output: str) -> OutputAssessment:
    """Read job output and infer whether the goal is complete, active, or waiting."""
    text = output or ""
    if not text.strip():
        return OutputAssessment("stalled", False, "no output")

    marker = parse_fleet_marker(text)
    ongoing = bool(ONGOING_OBJECTIVE_WORDS.search(objective or ""))
    missing = tuple(hard_requirement_gaps(objective, text))
    milestone = bool((marker and marker.kind == "FLEET_MILESTONE") or FLEET_MILESTONE_MARKER.search(text))
    not_complete = bool(NOT_COMPLETE_WORDS.search(text))
    user_gated = bool(USER_GATED_WORDS.search(text) or (marker and marker.blockers))
    completion_claim = bool((marker and marker.kind == "FLEET_DONE") or FLEET_DONE_MARKER.search(text) or COMPLETION_CLAIM_WORDS.search(text))
    verified = bool(VERIFICATION_WORDS.search(text))
    hard_goal = "hard requirement" in (objective or "").lower() or "hard completion definition" in (objective or "").lower()

    if user_gated and not completion_claim:
        return OutputAssessment("needs_input", False, "output says user action is required", missing)
    if milestone:
        return OutputAssessment("active", False, "milestone output with continuing work", missing)
    if not_complete:
        return OutputAssessment("active", False, "output describes remaining work or blocker", missing)
    if missing:
        return OutputAssessment("active", False, "missing hard requirement evidence: " + ", ".join(missing), missing)
    if hard_goal and completion_claim and not hard_completion_has_substantial_evidence(objective, text):
        return OutputAssessment("active", False, "hard completion claim lacks substantial evidence")
    if ongoing and completion_claim:
        return OutputAssessment("active", False, "ongoing objective remains active until explicitly stopped")
    if marker and marker.kind == "FLEET_DONE" and not missing:
        if hard_goal and not hard_completion_has_substantial_evidence(objective, text):
            return OutputAssessment("active", False, "hard completion marker lacks substantial evidence")
        return OutputAssessment("done", True, "structured FLEET_DONE marker accepted")
    if completion_claim and verified:
        return OutputAssessment("done", True, "output claims completion with verification")
    if completion_claim and "hard requirement" not in (objective or "").lower():
        return OutputAssessment("done", True, "output claims completion")
    return OutputAssessment("active", False, "worked output but no complete verified state", missing)


def priority_completion_satisfied(objective: str, output: str) -> tuple[bool, str]:
    """Return whether output is strong enough to complete a pinned priority."""
    assessment = assess_output_state(objective, output)
    return assessment.complete, assessment.reason


def build_prompt(row: Row, recovery: bool = False) -> str:
    priority_objective = ""
    try:
        if "priority_objective" in row.keys():
            priority_objective = row["priority_objective"] or ""
    except Exception:
        priority_objective = ""
    objective = priority_objective or row["objective"] or infer_objective(row["title"], row["latest_text"], row["cwd"])
    latest = compact(row["latest_text"], 1200)
    plan = ""
    try:
        if "task_plan" in row.keys():
            plan = str(row["task_plan"] or "")
    except Exception:
        plan = ""
    prefix = "RECOVERY: previous work stalled or lacked evidence.\n\n" if recovery else ""
    return (
        f"{prefix}"
        "AutoCode is driving this project in Maximum YOLO mode.\n\n"
        f"Goal:\n{objective}\n\n"
        "Rules:\n"
        "- Take the fastest safe path to complete the goal.\n"
        "- Do not wait for Luke/Hermes if a safe next action exists.\n"
        "- Edit files, run tests, commit, and push when appropriate.\n"
        "- Do not spend a turn passively waiting on long tests or background processes. If work will take more than 30 seconds, start it in tmux/background, record the log path/status command, report current evidence, and exit so AutoCode can re-enter immediately.\n"
        "- When a background test/process is already running, inspect its current log/process state and either fix/continue from new evidence or report that it is still running; do not sleep longer than 30 seconds inside the agent turn.\n"
        "- If this is only a milestone, output FLEET_MILESTONE_COMPLETE and continueable next steps.\n"
        "- Always end your response with exactly one structured marker line.\n"
        "- Use `FLEET_MILESTONE: {\"status\":\"active|blocked|needs_input\",\"summary\":\"...\",\"evidence\":[\"...\"],\"blockers\":[\"...\"],\"next_action\":\"...\"}` for partial progress.\n"
        "- Use `FLEET_DONE: {\"status\":\"done\",\"summary\":\"...\",\"evidence\":[\"tests/logs/commits\"],\"next_action\":\"none\"}` only when the whole goal is complete and verified.\n"
        "- If blocked, state the exact blocker and the best automatic fallback.\n\n"
        f"Current decomposition:\n{plan or '(not yet decomposed; infer the next concrete subtask from the goal and context)'}\n\n"
        f"Latest known context:\n{latest}\n"
    )
