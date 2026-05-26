from autocode.markers import parse_fleet_marker
from autocode.policy import assess_output_state, should_continue_after_output


def test_parse_structured_done_marker():
    marker = parse_fleet_marker(
        'Work complete.\nFLEET_DONE: {"status":"done","summary":"tests passed","evidence":["pytest"]}\n'
    )

    assert marker is not None
    assert marker.kind == "FLEET_DONE"
    assert marker.complete
    assert marker.summary == "tests passed"


def test_structured_milestone_keeps_work_active():
    text = 'FLEET_MILESTONE: {"status":"blocked","summary":"signing missing","blockers":["no profile"]}'

    assert should_continue_after_output(text)
    assessment = assess_output_state("Install app on phones", text)
    assert not assessment.complete
    assert assessment.state in {"active", "needs_input"}


def test_structured_done_marker_completes_normal_goal():
    assessment = assess_output_state(
        "Fix dashboard readability",
        'Implemented and verified.\nFLEET_DONE: {"status":"done","summary":"pytest passed","evidence":["pytest -q"]}',
    )

    assert assessment.complete
    assert assessment.state == "done"
