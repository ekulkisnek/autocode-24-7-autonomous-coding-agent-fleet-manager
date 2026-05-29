from pathlib import Path
from unittest.mock import MagicMock, patch

from autocode.models import ContinuePlan
from autocode.runner import JobRunner
from autocode.scheduler import Scheduler
from autocode.store import Store
from autocode.util import now_iso


def _insert_remote_worker(store: Store, worker_id: str = "win") -> None:
    with store.connect() as con:
        con.execute(
            """insert into remote_workers(id,host,ssh_user,provider_types,weight_capacity,default_cwd,ssh_key_path,enabled,notes,remote_shell)
               values(?,?,?,?,?,?,?,1,?,?)""",
            (worker_id, "100.0.0.1", "Luke", "grok", 4.0, "C:/Users/Luke", "", "", "powershell"),
        )


def _insert_running_remote_job(store: Store, tmp_path: Path, *, job_id: str = "job-remote", pid: int = 99999) -> None:
    with store.connect() as con:
        con.execute(
            """
            insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at,worker_id)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                job_id,
                "grok:grok.sqlite:test",
                "grok",
                "running",
                pid,
                "C:/Users/Luke",
                "[]",
                "prompt",
                str(tmp_path / f"{job_id}-out.txt"),
                str(tmp_path / f"{job_id}-err.txt"),
                now_iso(),
                now_iso(),
                "win",
            ),
        )


def test_reap_stale_remote_jobs_finalizes_dead_ssh_wrapper(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    _insert_running_remote_job(store, tmp_path, pid=0)
    runner = JobRunner(store)
    reaped = runner.reap_stale_remote_jobs()
    assert reaped == ["job-remote"]
    row = store.row("select status from jobs where id='job-remote'")
    assert row["status"] != "running"


def test_refresh_one_remote_marks_completed_when_ssh_exits(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    out = tmp_path / "job-remote-out.txt"
    err = tmp_path / "job-remote-err.txt"
    out.write_text("FLEET_DONE\nworked\n", encoding="utf-8")
    err.write_text("", encoding="utf-8")
    _insert_running_remote_job(store, tmp_path, pid=0)
    JobRunner(store).refresh()
    row = store.row("select status,evidence_status from jobs where id='job-remote'")
    assert row["status"] == "completed"
    assert row["evidence_status"] == "worked"


def test_tick_continues_when_dispatch_remote_raises(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "autocode.sqlite")
    _insert_remote_worker(store)
    from autocode.models import Chat

    chat = Chat(
        id="grok:grok.sqlite:alpha",
        provider="grok",
        source="grok.sqlite",
        provider_chat_id="alpha",
        title="alpha",
        cwd=str(tmp_path),
        updated_at="2026-05-21T00:00:00-05:00",
        latest_text="work",
        transcript_hash="h1",
        alias="alpha",
        continuation="grok",
    )
    store.upsert_chat(chat, 5, "active", "goal")
    store.queue_add(chat.id, 1.0)

    scheduler = Scheduler(store)
    monkeypatch.setattr(scheduler.runner, "refresh", lambda: None)
    monkeypatch.setattr(scheduler, "_maybe_discover", lambda: "test")
    monkeypatch.setattr(scheduler, "capacity", lambda: 0)
    monkeypatch.setattr(scheduler, "_running_dispatch_weight", lambda: 0.0)
    monkeypatch.setattr(scheduler, "dispatch", lambda *a, **k: None)

    def boom(row, worker, queue_snapshot_id=""):
        raise RuntimeError("ssh failed")

    monkeypatch.setattr(scheduler, "dispatch_remote", boom)

    result = scheduler.tick(dispatch=True)
    assert result["remote_sent"] == 0
    events = store.rows("select kind from events where kind='dispatch_remote_failed'")
    assert len(events) >= 1


def test_remote_dispatch_budget_caps_spill(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "autocode.sqlite")
    _insert_remote_worker(store)
    for alias, pos in (("a", 1.0), ("b", 2.0), ("c", 3.0)):
        from autocode.models import Chat

        chat = Chat(
            id=f"grok:grok.sqlite:{alias}",
            provider="grok",
            source="grok.sqlite",
            provider_chat_id=alias,
            title=alias,
            cwd=str(tmp_path / alias),
            updated_at="2026-05-21T00:00:00-05:00",
            latest_text="work",
            transcript_hash=f"h-{alias}",
            alias=alias,
            continuation="grok",
        )
        (tmp_path / alias).mkdir(parents=True, exist_ok=True)
        store.upsert_chat(chat, 5, "active", "goal")
        store.queue_add(chat.id, pos)

    scheduler = Scheduler(store)
    monkeypatch.setattr(scheduler.runner, "refresh", lambda: None)
    monkeypatch.setattr(scheduler, "_maybe_discover", lambda: "test")
    monkeypatch.setattr(scheduler, "capacity", lambda: 0)
    monkeypatch.setattr(scheduler, "_running_dispatch_weight", lambda: 0.0)
    with store.connect() as con:
        con.execute("update remote_workers set weight_capacity=1.0")

    remote_calls: list[str] = []

    def fake_remote(row, worker, queue_snapshot_id=""):
        remote_calls.append(str(row["id"]))
        return f"job-{len(remote_calls)}"

    monkeypatch.setattr(scheduler, "dispatch_remote", fake_remote)
    monkeypatch.setattr(scheduler, "dispatch", lambda *a, **k: None)

    result = scheduler.tick(dispatch=True)
    assert result["remote_sent"] == 1
    assert len(remote_calls) == 1


def test_kill_jobs_invokes_remote_kill(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "autocode.sqlite")
    _insert_remote_worker(store)
    _insert_running_remote_job(store, tmp_path, pid=4242)
    runner = JobRunner(store)
    killed_remote: list[str] = []
    monkeypatch.setattr(runner, "_terminate", lambda pid: None)
    monkeypatch.setattr(
        "autocode.runner.subprocess.run",
        lambda *a, **k: MagicMock(returncode=0),
    )

    def capture_kill(job):
        killed_remote.append(str(job["id"]))

    monkeypatch.setattr(runner, "_kill_remote_job", capture_kill)
    count = runner.kill_chat_jobs("grok:grok.sqlite:test", "test")
    assert count == 1
    assert killed_remote == ["job-remote"]


def test_bench_remote_worker_mocked():
    from autocode import remote_ssh

    worker = {"host": "1.2.3.4", "ssh_user": "Luke", "ssh_key_path": "", "default_cwd": "C:/Users/Luke", "remote_shell": "powershell"}
    ok = MagicMock(returncode=0, stdout="ok", stderr="")
    with patch("autocode.remote_ssh.subprocess.run", return_value=ok):
        with patch("autocode.remote_ssh.ensure_remote_job_dir", return_value=ok):
            with patch("autocode.remote_ssh.scp_prompt_file", return_value=ok):
                result = remote_ssh.bench_remote_worker(worker)
    assert result["ping_ok"] == 1
    assert result["mkdir_ok"] == 1
    assert result["scp_ok"] == 1
    assert "total_s" in result


def test_pick_remote_worker_cursor_requires_provider_in_set(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    with store.connect() as con:
        con.execute(
            """insert into remote_workers(id,host,ssh_user,provider_types,weight_capacity,default_cwd,ssh_key_path,enabled,notes,remote_shell)
               values(?,?,?,?,?,?,?,1,?,?)""",
            ("win", "100.0.0.1", "Luke", "grok", 8.0, "C:/Users/Luke", "", "", "powershell"),
        )
    sched = Scheduler(store)
    assert sched._pick_remote_worker("grok", 1.0) is not None
    assert sched._pick_remote_worker("cursor", 1.0) is None
