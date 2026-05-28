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


def test_runner_resets_failure_count_after_worked_turn(tmp_path: Path):
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
    store.upsert_chat(chat, 5, "active", "Keep driving until production ready.")
    with store.connect() as con:
        con.execute("update chats set failure_count=6 where id=?", (chat.id,))

    job_dir = tmp_path / "job-worked"
    job_dir.mkdir()
    stdout = job_dir / "stdout.txt"
    stderr = job_dir / "stderr.txt"
    stdout.write_text("FLEET_MILESTONE: {\"status\":\"active\",\"summary\":\"still working\",\"next_action\":\"continue\"}\n", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    with store.connect() as con:
        con.execute(
            """
            insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at)
            values(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job-worked",
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
    assert row["failure_count"] == 0
    assert row["state"] == "active"
    assert row["done"] == 0


def test_runner_reconciles_idle_running_chat_without_job(tmp_path: Path):
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
    store.upsert_chat(chat, 5, "running", "Keep driving.")
    JobRunner(store).refresh()
    row = store.row("select * from chats where id=?", (chat.id,))
    assert row["state"] == "active"


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


def test_runner_records_marker_usage_and_plan(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="codex:codex.rollout:plan",
        provider="codex",
        source="codex.rollout",
        provider_chat_id="plan",
        title="Plan task",
        cwd=str(tmp_path),
        updated_at="2026-05-21T00:00:00-05:00",
        latest_text="work",
        transcript_hash="h1",
        alias="plan",
        continuation="codex exec resume",
    )
    store.upsert_chat(chat, 5, "active", "finish plan")
    job_dir = tmp_path / "job-plan"
    job_dir.mkdir()
    stdout = job_dir / "stdout.txt"
    stderr = job_dir / "stderr.txt"
    stdout.write_text(
        'FLEET_PLAN: {"goal":"finish plan","subtasks":[{"id":"a","title":"A","status":"pending"}],"usage":{"input_tokens":10,"output_tokens":5,"cost_usd":0.01}}\n',
        encoding="utf-8",
    )
    stderr.write_text("", encoding="utf-8")
    with store.connect() as con:
        con.execute(
            """
            insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at)
            values(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("job-plan", chat.id, "codex", "running", 0, str(tmp_path), "[]", "prompt", str(stdout), str(stderr), now_iso(), now_iso()),
        )

    JobRunner(store).refresh()

    job = store.row("select * from jobs where id='job-plan'")
    assert job["marker_kind"] == "FLEET_PLAN"
    assert job["token_input"] == 10
    assert job["token_output"] == 5
    assert job["cost_estimate"] == 0.01
    assert "A" in store.task_plan_summary(chat.id)


def test_runner_detach_all_leaves_processes_running(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "autocode.sqlite")
    runner = JobRunner(store)
    job_id = insert_running_job(store, tmp_path, pid=4242)
    terminated: list[int] = []
    monkeypatch.setattr(runner, "_terminate", lambda pid: terminated.append(pid))

    count = runner.detach_all("daemon_shutdown")
    row = store.row("select * from jobs where id=?", (job_id,))

    assert count == 1
    assert terminated == []
    assert row["status"] == "detached"
    assert row["evidence_reason"] == "daemon_shutdown"


def test_runner_reattach_detached_running_job(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "autocode.sqlite")
    runner = JobRunner(store)
    job_id = insert_running_job(store, tmp_path, pid=5151)
    runner.detach_all("daemon_shutdown")
    monkeypatch.setattr(runner, "_pid_running", lambda pid: pid == 5151)

    count = runner.reattach_detached()
    row = store.row("select * from jobs where id=?", (job_id,))

    assert count == 1
    assert row["status"] == "running"
    assert "reattached_after_daemon_restart" in row["evidence_reason"]


def test_runner_prepares_worktree_when_enabled(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "autocode.sqlite")
    store.set_config("use_worktrees", "on")
    runner = JobRunner(store)
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = str(tmp_path) + "\n"
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Result()

    monkeypatch.setattr("autocode.runner.subprocess.run", fake_run)

    wt = runner._prepare_worktree(str(tmp_path), "job-wt")

    assert wt is not None
    assert wt.name == "job-wt"
    assert any(cmd[:4] == ["git", "-C", str(tmp_path), "worktree"] for cmd in calls)
