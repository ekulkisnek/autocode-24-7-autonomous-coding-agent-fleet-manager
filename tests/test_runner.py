from pathlib import Path

from autocode.models import Chat
from autocode.runner import JobRunner, ProcessActivity
from autocode.store import Store
from autocode.util import now_iso, parse_ts


def insert_running_job(
    store: Store,
    tmp_path: Path,
    chat_id: str = "cursor:cursor.cli:quota",
    job_id: str = "job-running",
    provider: str = "cursor",
    pid: int = 12345,
    created_at: str | None = None,
):
    job_dir = tmp_path / job_id
    job_dir.mkdir()
    stdout = job_dir / "stdout.txt"
    stderr = job_dir / "stderr.txt"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    created = created_at or now_iso()
    with store.connect() as con:
        con.execute(
            """
            insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at)
            values(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                job_id,
                chat_id,
                provider,
                "running",
                pid,
                str(tmp_path),
                "[]",
                "prompt",
                str(stdout),
                str(stderr),
                created,
                created,
            ),
        )
    return job_id


def test_runner_assesses_stdout_completion_without_prompt_stderr_noise(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="codex:codex.rollout:redwallet",
        provider="codex",
        source="codex.rollout",
        provider_chat_id="redwallet",
        title="Implement wallet persistence",
        cwd="/tmp/redwallet",
        updated_at="2026-05-21T00:00:00-05:00",
        latest_text="work",
        transcript_hash="h1",
        alias="redwallet",
        continuation="codex exec resume",
    )
    goal = (
        "Make RedWallet production ready. HARD REQUIREMENT: do not call this done until tests prove "
        "full Utreexo/proof-backed storage and validation for BitAssets asset creation, sending, and receiving."
    )
    store.upsert_chat(chat, 5, "active", goal)
    store.add_priority("redwallet", goal, 1001, "/tmp/redwallet", chat.id, 1)

    job_dir = tmp_path / "job-complete"
    job_dir.mkdir()
    stdout = job_dir / "stdout.txt"
    stderr = job_dir / "stderr.txt"
    stdout.write_text(
        "FLEET_DONE\n\n"
        "Complete and verified. Tests passed for Utreexo proof-backed storage and validation, "
        "BitAssets asset creation, sending/transfer, and receiving/change.\n",
        encoding="utf-8",
    )
    stderr.write_text(
        "user prompt: Current known next step: continue. "
        "Rules: do not call this done until tests prove the hard requirement.\n",
        encoding="utf-8",
    )
    with store.connect() as con:
        con.execute(
            """
            insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at)
            values(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job-complete",
                chat.id,
                "codex",
                "running",
                0,
                "/tmp/redwallet",
                "[]",
                "prompt",
                str(stdout),
                str(stderr),
                now_iso(),
                now_iso(),
            ),
        )

    JobRunner(store).refresh()

    row = store.row("select * from chats where id=?", (chat.id,))
    priority = store.row("select * from project_priorities where target_chat_id=?", (chat.id,))
    assert row["done"] == 1
    assert row["state"] == "done"
    assert priority["status"] == "complete"


def test_runner_marks_quiet_cursor_job_with_child_processes_as_external_activity(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "autocode.sqlite")
    job_id = insert_running_job(store, tmp_path)
    runner = JobRunner(store)
    monkeypatch.setattr(runner, "_pid_running", lambda pid: True)
    monkeypatch.setattr(
        runner,
        "_process_tree_snapshot",
        lambda pid: ProcessActivity(child_count=2, busy_children=1, child_sample="12346:cursor-agent; 12347:codex exec"),
    )

    runner.refresh()

    row = store.row("select * from jobs where id=?", (job_id,))
    assert row["status"] == "running"
    assert row["evidence_status"] == "running_external_activity"
    assert "child_processes=2" in row["evidence_reason"]


def test_runner_marks_quiet_job_as_silent_without_killing_before_timeout(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "autocode.sqlite")
    job_id = insert_running_job(
        store,
        tmp_path,
        job_id="job-silent",
        provider="codex",
        created_at="2026-05-24T00:00:00-05:00",
    )
    runner = JobRunner(store)
    monkeypatch.setattr(runner, "_pid_running", lambda pid: True)
    monkeypatch.setattr(runner, "_process_tree_snapshot", lambda pid: ProcessActivity())
    monkeypatch.setattr("autocode.runner.now_ts", lambda: parse_ts("2026-05-24T00:20:00-05:00"))

    runner.refresh()

    row = store.row("select * from jobs where id=?", (job_id,))
    assert row["status"] == "running"
    assert row["evidence_status"] == "running_silent"
    assert "no output or child process activity" in row["evidence_reason"]


def test_runner_keeps_quiet_cursor_job_running_until_normal_timeout(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "autocode.sqlite")
    job_id = insert_running_job(
        store,
        tmp_path,
        job_id="job-cursor-idle",
        provider="cursor",
        created_at="2026-05-24T00:00:00-05:00",
    )
    runner = JobRunner(store)
    terminated: list[int] = []
    monkeypatch.setattr(runner, "_pid_running", lambda pid: True)
    monkeypatch.setattr(
        runner,
        "_process_tree_snapshot",
        lambda pid: ProcessActivity(
            child_count=2,
            busy_children=0,
            child_sample="12346:zsh claude doctor; 12347:zsh codex help",
            newest_terminal_age=1200,
            terminal_sample="318544.txt:claude doctor",
        ),
    )
    monkeypatch.setattr(runner, "_terminate", lambda pid: terminated.append(pid))
    monkeypatch.setattr("autocode.runner.now_ts", lambda: parse_ts("2026-05-24T00:20:00-05:00"))

    runner.refresh()

    row = store.row("select * from jobs where id=?", (job_id,))
    assert row["status"] == "running"
    assert row["evidence_status"] == "running_external_idle"
    assert terminated == []
    assert "terminal_idle=1200s" in row["evidence_reason"]


def test_runner_uses_longer_cursor_timeout(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "autocode.sqlite")
    job_id = insert_running_job(
        store,
        tmp_path,
        job_id="job-cursor-long",
        provider="cursor",
        created_at="2026-05-24T00:00:00-05:00",
    )
    runner = JobRunner(store)
    monkeypatch.setattr(runner, "_pid_running", lambda pid: True)
    monkeypatch.setattr(runner, "_process_tree_snapshot", lambda pid: ProcessActivity())
    monkeypatch.setattr("autocode.runner.DEFAULT_JOB_TIMEOUT", 1800)
    monkeypatch.setattr("autocode.runner.DEFAULT_CURSOR_JOB_TIMEOUT", 14400)
    monkeypatch.setattr("autocode.runner.now_ts", lambda: parse_ts("2026-05-24T01:00:00-05:00"))

    runner.refresh()

    row = store.row("select * from jobs where id=?", (job_id,))
    assert row["status"] == "running"
    assert row["evidence_status"] == "running_silent"


def test_runner_kill_chat_jobs_releases_lease(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "autocode.sqlite")
    job_id = insert_running_job(store, tmp_path, chat_id="codex:codex.rollout:kill", pid=2222)
    with store.connect() as con:
        con.execute(
            "insert into leases(resource,chat_id,job_id,expires_at) values(?,?,?,?)",
            (str(tmp_path), "codex:codex.rollout:kill", job_id, now_iso()),
        )
    runner = JobRunner(store)
    killed: list[int] = []
    monkeypatch.setattr(runner, "_terminate", lambda pid: killed.append(pid))

    count = runner.kill_chat_jobs("codex:codex.rollout:kill", "test")

    row = store.row("select * from jobs where id=?", (job_id,))
    assert count == 1
    assert killed == [2222]
    assert row["status"] == "killed"
    assert store.rows("select * from leases") == []
