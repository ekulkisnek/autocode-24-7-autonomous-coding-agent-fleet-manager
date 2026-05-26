import json
from pathlib import Path

from autocode.audit import append_audit, replay_summary
from autocode.plugins import scaffold_plugin, validate_plugin
from autocode.store import Store
from autocode.watchers import WatchState, latest_mtime
from autocode.web import latest_queue, sse_payload, status_payload
from autocode.workflows import apply_workflow, load_workflow


def test_audit_replay_summary_counts_events(tmp_path: Path):
    audit = tmp_path / "audit.jsonl"
    append_audit("job_started", chat_id="chat-1", job_id="job-1", path=audit)  # type: ignore[arg-type]

    summary = replay_summary(audit)

    assert summary["events"] == 1
    assert summary["kinds"]["job_started"] == 1
    assert summary["chats"] == 1
    assert summary["jobs"] == 1


def test_watch_state_detects_signature_changes(tmp_path: Path, monkeypatch):
    watched = tmp_path / "state.sqlite"
    watched.write_text("one", encoding="utf-8")
    monkeypatch.setattr("autocode.watchers.provider_watch_paths", lambda: [watched])
    state = WatchState()

    changed, first = state.poll()
    watched.write_text("two", encoding="utf-8")
    changed_again, second = state.poll()

    assert changed is True
    assert changed_again is True
    assert first != second
    assert latest_mtime(watched) > 0


def test_plugin_scaffold_and_validation(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("autocode.plugins.PLUGIN_DIR", tmp_path / "plugins")

    root = scaffold_plugin("demo-provider")
    manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))

    assert manifest["id"] == "demo-provider"
    assert validate_plugin(manifest) == []
    assert "providers must be a list" in validate_plugin({"id": "bad", "providers": {}})


def test_workflow_load_and_apply_creates_priorities(tmp_path: Path):
    workflow_path = tmp_path / "workflow.json"
    workflow_path.write_text(
        json.dumps(
            {
                "name": "demo",
                "steps": [
                    {"query": "alpha", "goal": "Finish alpha", "rank": 500, "path": str(tmp_path)},
                    {"query": "beta", "goal": "Finish beta", "priority": 400},
                ],
            }
        ),
        encoding="utf-8",
    )
    store = Store(tmp_path / "autocode.sqlite")

    workflow = load_workflow(workflow_path)
    created = apply_workflow(store, workflow)

    assert len(created) == 2
    rows = store.rows("select * from project_priorities where status='active' order by priority desc")
    assert [row["query"] for row in rows] == ["alpha", "beta"]


def test_web_payloads_include_queue_and_sse(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    sid = store.record_queue_snapshot([], reason="test", capacity=2, active_jobs=0, resource_for=lambda row: "")

    queue = latest_queue(store)
    status = status_payload(store)
    sse = sse_payload(store)

    assert queue["id"] == sid
    assert status["queue"]["id"] == sid
    assert sse.startswith("data: ")
