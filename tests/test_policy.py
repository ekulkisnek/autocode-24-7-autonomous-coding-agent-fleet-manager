from autocode.policy import classify_chat, should_continue_after_output


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
