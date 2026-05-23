from autocode.policy import assess_output_state, build_prompt, classify_chat, priority_completion_satisfied, should_continue_after_output


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
    assert should_continue_after_output("This is not `FLEET_DONE`; continue with next step.")
    score, state, objective = classify_chat("Task", "/tmp/repo", "FLEET_MILESTONE_COMPLETE\nnot `FLEET_DONE`")
    assert state == "active"


def test_destructive_disk_chat_is_not_auto_adopted():
    score, state, objective = classify_chat("Format 1TB connected drive", "/Users/lukekensik", "please confirm")
    assert score == 0
    assert state == "blocked"


def test_account_creation_chat_is_not_auto_adopted_from_coding_folder():
    score, state, objective = classify_chat("Can you make 10 accounts on Kith for me", "/Users/lukekensik/coding/app", "")
    assert score == 0
    assert state == "blocked"


def test_secret_dump_chat_is_not_auto_adopted():
    score, state, objective = classify_chat(
        "Deployment writeup",
        "/Users/lukekensik/coding/site",
        "create a complete writeup including all my keys and passwords and stuff",
    )
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


def test_negated_completion_marker_is_not_completion():
    goal = (
        "Make RedWallet production ready. HARD REQUIREMENT: do not call this done until tests prove "
        "full Utreexo/proof-backed storage and validation for BitAssets asset creation, sending, and receiving."
    )
    ok, reason = priority_completion_satisfied(
        goal,
        "FLEET_MILESTONE_COMPLETE\nThis is not `FLEET_DONE`; transfer/send/receive is still not complete.",
    )
    assert not ok
    assert "milestone" in reason


def test_completion_is_inferred_without_magic_marker():
    goal = (
        "Make RedWallet production ready. HARD REQUIREMENT: do not call this done until tests prove "
        "full Utreexo/proof-backed storage and validation for BitAssets asset creation, sending, and receiving."
    )
    assessment = assess_output_state(
        goal,
        "Completed and verified. Tests passed for Utreexo proof-backed storage, asset creation, sending, and receiving.",
    )
    assert assessment.complete
    assert assessment.state == "done"


def test_remaining_work_overrides_completion_language():
    goal = (
        "Make RedWallet production ready. HARD REQUIREMENT: do not call this done until tests prove "
        "full Utreexo/proof-backed storage and validation for BitAssets asset creation, sending, and receiving."
    )
    assessment = assess_output_state(
        goal,
        "Completed and pushed a clean checkpoint. Current blocker: full transfer/send/receive plus restart persistence proof is still not complete. Next automatic step: fix mining.",
    )
    assert not assessment.complete
    assert assessment.state == "active"
    assert "remaining work" in assessment.reason


def test_prompt_forbids_long_passive_waits():
    class Row(dict):
        def keys(self):
            return super().keys()

    row = Row(
        id="codex:codex.rollout:redwallet",
        provider="codex",
        source="codex.rollout",
        provider_chat_id="redwallet",
        title="RedWallet",
        cwd="/tmp/redwallet",
        updated_at="2026-05-21T00:00:00-05:00",
        latest_text="Android proof running in tmux",
        transcript_hash="h",
        alias="redwallet",
        continuation="codex exec resume",
        objective="Finish RedWallet",
    )
    prompt = build_prompt(row)
    assert "Do not spend a turn passively waiting" in prompt
    assert "do not sleep longer than 30 seconds" in prompt
