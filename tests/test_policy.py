from autocode.policy import classify_chat, priority_completion_satisfied, should_continue_after_output


def test_classifies_coding_chat():
    score, state, objective = classify_chat("Fix React build", "/Users/lukekensik/coding/app", "tests fail")
    assert score > 0
    assert state == "active"
    assert "Fix React build" in objective


def test_done_detection():
    score, state, objective = classify_chat("Task", "/tmp/repo", "FLEET_DONE: complete and verified")
    assert state == "done"


def test_milestone_continues():
    assert should_continue_after_output("FLEET_MILESTONE_COMPLETE\nNext step: run e2e")
    assert not should_continue_after_output("FLEET_DONE: all complete")


def test_destructive_disk_chat_is_not_auto_adopted():
    score, state, objective = classify_chat("Format 1TB connected drive", "/Users/lukekensik", "please confirm")
    assert score == 0
    assert state == "blocked"


def test_hard_priority_completion_requires_named_evidence():
    goal = (
        "Make RedWallet production ready. HARD REQUIREMENT: do not call this done until tests prove "
        "full Utreexo/proof-backed storage and validation for BitAssets asset creation, sending, and receiving."
    )
    ok, reason = priority_completion_satisfied(goal, "FLEET_DONE: all complete")
    assert not ok
    assert "utreexo" in reason.lower()

    ok, reason = priority_completion_satisfied(
        goal,
        "FLEET_DONE: verified tests passed for Utreexo proof-backed storage, asset creation, sending, and receiving.",
    )
    assert ok
