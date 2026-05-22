from __future__ import annotations

import re
from sqlite3 import Row

from .util import compact, parse_ts

CODING_WORDS = re.compile(
    r"\b(code|coding|repo|repository|git|build|test|tests|lint|compile|app|site|extension|wallet|"
    r"frontend|backend|api|database|sqlite|typescript|python|swift|rust|go|react|node|package|"
    r"implementation|bug|fix|refactor|deploy|e2e|detox|chrome|cursor|codex|grok build)\b",
    re.I,
)
DONE_WORDS = re.compile(r"\b(FLEET_DONE|fully complete|complete and verified|done\.?$|nothing left|all tests pass(?:ed)?)\b", re.I)
MILESTONE_WORDS = re.compile(r"\b(FLEET_MILESTONE_COMPLETE|milestone complete|next step|remaining|still needs|todo|blocked|needs input|shall i proceed|continue)\b", re.I)
ASK_WORDS = re.compile(r"\b(shall i|should i|do you want|would you like|please confirm|need permission|waiting for|blocked)\b", re.I)
BLOCKED_NONCODING = re.compile(
    r"\b(format|erase|partition|wipe)\b.{0,80}\b(drive|disk|ssd|volume)\b|"
    r"\b(ocmma|hardware plugged|fleet captain|captain brain|smoke test|hermes autonomous coding fleet)\b",
    re.I,
)


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
    if DONE_WORDS.search(latest or "") and not MILESTONE_WORDS.search(latest or ""):
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
    if "FLEET_DONE" in text:
        return False
    if "FLEET_MILESTONE_COMPLETE" in text:
        return True
    return bool(MILESTONE_WORDS.search(text) or ASK_WORDS.search(text))


def build_prompt(row: Row, recovery: bool = False) -> str:
    priority_objective = ""
    try:
        if "priority_objective" in row.keys():
            priority_objective = row["priority_objective"] or ""
    except Exception:
        priority_objective = ""
    objective = priority_objective or row["objective"] or infer_objective(row["title"], row["latest_text"], row["cwd"])
    latest = compact(row["latest_text"], 1200)
    prefix = "RECOVERY: previous work stalled or lacked evidence.\n\n" if recovery else ""
    return (
        f"{prefix}"
        "AutoCode is driving this project in Maximum YOLO mode.\n\n"
        f"Goal:\n{objective}\n\n"
        "Rules:\n"
        "- Take the fastest safe path to complete the goal.\n"
        "- Do not wait for Luke/Hermes if a safe next action exists.\n"
        "- Edit files, run tests, commit, and push when appropriate.\n"
        "- If this is only a milestone, output FLEET_MILESTONE_COMPLETE and continueable next steps.\n"
        "- Output FLEET_DONE only when the whole goal is complete and verified.\n"
        "- If blocked, state the exact blocker and the best automatic fallback.\n\n"
        f"Latest known context:\n{latest}\n"
    )
