from __future__ import annotations

import os
import re
import signal
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Row

from .config import DEFAULT_CURSOR_JOB_TIMEOUT, DEFAULT_JOB_TIMEOUT, JOBS
from . import grok_watchdog
from . import recovery
from .config import HOME, WORKTREES
from .markers import parse_fleet_marker
from .models import Chat, ContinuePlan
from . import goals
from .store import Store
from .util import json_dumps, now_iso, now_ts, read_text


@dataclass(frozen=True)
class ProcessActivity:
    child_count: int = 0
    busy_children: int = 0
    child_sample: str = ""
    newest_terminal_age: float | None = None
    terminal_sample: str = ""

    def summary(self) -> str:
        parts = [
            f"child_processes={self.child_count}",
            f"busy_children={self.busy_children}",
        ]
        if self.child_sample:
            parts.append(f"child_sample={self.child_sample}")
        if self.newest_terminal_age is not None:
            parts.append(f"terminal_idle={int(self.newest_terminal_age)}s")
        if self.terminal_sample:
            parts.append(f"terminal_sample={self.terminal_sample}")
        return "; ".join(parts)

    def has_activity(self) -> bool:
        return self.child_count > 0 or self.newest_terminal_age is not None

    def is_recent_or_busy(self, idle_seconds: int) -> bool:
        if self.busy_children > 0:
            return True
        return self.newest_terminal_age is not None and self.newest_terminal_age <= idle_seconds


class JobRunner:
    def __init__(self, store: Store):
        self.store = store

    def start(self, row: Row, plan: ContinuePlan, prompt: str, job_dir: Path | None = None, queue_snapshot_id: str = "") -> str:
        return self._start(
            chat_id=row["id"],
            cwd=row["cwd"] or str(HOME),
            plan=plan,
            prompt=prompt,
            job_dir=job_dir,
            repo_resource=self.resource_for(row),
            lease_resource=self.lease_resource_for(row),
            queue_snapshot_id=queue_snapshot_id,
        )

    def start_remote(self, row: Row, plan: ContinuePlan, prompt: str, worker: dict, job_dir: Path | None = None, queue_snapshot_id: str = "") -> str:
        """Dispatch a job to a remote worker over SSH, streaming output back locally."""
        from . import remote_ssh

        job_id = job_dir.name if job_dir else "job-" + uuid.uuid4().hex[:12]
        job_dir = job_dir or JOBS / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = job_dir / "stdout.txt"
        stderr_path = job_dir / "stderr.txt"

        host = worker["host"]
        worker_id = worker["id"] if "id" in worker.keys() else host
        remote_cwd = worker["default_cwd"] or "~"

        mkdir = remote_ssh.ensure_remote_job_dir(worker, job_id)
        if mkdir.returncode != 0:
            raise RuntimeError(
                f"remote mkdir failed for {worker_id}: {(mkdir.stderr or mkdir.stdout).strip()}"
            )
        if plan.prompt_file or str(plan.provider or "") == "cursor":
            (job_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
            copied = remote_ssh.scp_prompt_file(worker, str(job_dir / "prompt.txt"), job_id)
            if copied.returncode != 0:
                raise RuntimeError(
                    f"remote scp failed for {worker_id}: {(copied.stderr or copied.stdout).strip()}"
                )

        full_cmd = remote_ssh.build_remote_exec_command(worker, remote_cwd, plan.cmd, job_id, env=plan.env)

        out_f = stdout_path.open("wb")
        err_f = stderr_path.open("wb")
        proc = subprocess.Popen(full_cmd, stdout=out_f, stderr=err_f, stdin=subprocess.DEVNULL, start_new_session=True)
        out_f.close()
        err_f.close()

        chat_id = str(row["id"])
        with self.store.connect() as con:
            con.execute(
                """
                insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at,queue_snapshot_id,worker_id)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (job_id, chat_id, plan.provider, "running", proc.pid, remote_cwd,
                 json_dumps(full_cmd), prompt, str(stdout_path), str(stderr_path),
                 now_iso(), now_iso(), queue_snapshot_id, worker["id"]),
            )
            con.execute("update chats set state='running',last_drive_at=? where id=?", (now_iso(), chat_id))
        self.store.event("job_started_remote", chat_id, job_id, provider=plan.provider, pid=proc.pid, worker=worker["id"], host=host)
        return job_id

    def start_aux(
        self,
        chat_id: str,
        cwd: str,
        plan: ContinuePlan,
        prompt: str,
        job_dir: Path | None = None,
        lease_resource: str = "",
        queue_snapshot_id: str = "",
    ) -> str:
        return self._start(
            chat_id=chat_id,
            cwd=cwd,
            plan=plan,
            prompt=prompt,
            job_dir=job_dir,
            lease_resource=lease_resource,
        )

    def _start(
        self,
        chat_id: str,
        cwd: str,
        plan: ContinuePlan,
        prompt: str,
        job_dir: Path | None = None,
        repo_resource: str = "",
        lease_resource: str = "",
        queue_snapshot_id: str = "",
    ) -> str:
        job_id = job_dir.name if job_dir else "job-" + uuid.uuid4().hex[:12]
        job_dir = job_dir or JOBS / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        stdout = job_dir / "stdout.txt"
        stderr = job_dir / "stderr.txt"
        if plan.prompt_file:
            (job_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        stdin_data = plan.stdin
        out_f = stdout.open("wb")
        err_f = stderr.open("wb")
        env = os.environ.copy()
        env.update(plan.env or {})
        cwd = plan.cwd if plan.cwd and Path(plan.cwd).exists() else (cwd if cwd and Path(cwd).exists() else str(HOME))
        worktree_path = ""
        if self._use_worktree(repo_resource or lease_resource, cwd):
            wt = self._prepare_worktree(cwd, job_id)
            if wt:
                cwd = str(wt)
                worktree_path = str(wt)
        # Merge stderr→stdout for cursor-agent so auth/rate-limit errors surface
        # as "worked" output rather than silent_failed (0 bytes on both streams).
        first_cmd = plan.cmd[0] if plan.cmd else ""
        merge_stderr = "cursor-agent" in str(first_cmd)
        proc = subprocess.Popen(
            plan.cmd,
            cwd=cwd or None,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            stdout=out_f,
            stderr=out_f if merge_stderr else err_f,
            env=env,
            start_new_session=True,
        )
        if stdin_data is not None and proc.stdin:
            proc.stdin.write(stdin_data.encode("utf-8", errors="replace"))
            proc.stdin.close()
        out_f.close()
        err_f.close()
        with self.store.connect() as con:
            con.execute(
                """
                insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at,worktree_path,queue_snapshot_id)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (job_id, chat_id, plan.provider, "running", proc.pid, cwd, json_dumps(plan.cmd), prompt, str(stdout), str(stderr), now_iso(), now_iso(), worktree_path, queue_snapshot_id),
            )
            if lease_resource:
                con.execute("insert or replace into leases(resource,chat_id,job_id,expires_at) values(?,?,?,?)", (lease_resource, chat_id, job_id, now_iso()))
            con.execute("update chats set state='running',last_drive_at=? where id=?", (now_iso(), chat_id))
        self.store.event("job_started", chat_id, job_id, provider=plan.provider, pid=proc.pid, same_chat=plan.same_chat)
        return job_id

    def kill_chat_jobs(self, chat_id: str, reason: str = "stopped") -> int:
        jobs = self.store.rows("select * from jobs where chat_id=? and status='running'", (chat_id,))
        return self.kill_jobs(jobs, reason)

    def kill_priority_jobs(self, priority_query_or_id: str, reason: str = "priority_paused") -> int:
        q = f"%{priority_query_or_id.lower()}%"
        jobs = self.store.rows(
            """
            select j.* from jobs j
            left join project_priorities p on p.target_chat_id=j.chat_id
            where j.status='running'
              and (p.id=? or lower(p.query) like ? or lower(p.objective) like ?)
            """,
            (priority_query_or_id, q, q),
        )
        return self.kill_jobs(jobs, reason)

    def kill_all(self, reason: str = "daemon_stop") -> int:
        return self.kill_jobs(self.store.rows("select * from jobs where status='running'"), reason)

    def detach_all(self, reason: str = "daemon_shutdown") -> int:
        """Leave provider processes running across daemon restarts."""
        jobs = self.store.rows("select * from jobs where status='running'")
        detached = 0
        with self.store.connect() as con:
            for job in jobs:
                con.execute(
                    """
                    update jobs set status='detached',updated_at=?,evidence_status='detached',evidence_reason=?
                    where id=?
                    """,
                    (now_iso(), reason, job["id"]),
                )
                detached += 1
        for job in jobs:
            self.store.event("job_detached", job["chat_id"], job["id"], reason=reason)
        return detached

    def reattach_detached(self) -> int:
        jobs = self.store.rows("select * from jobs where status='detached' order by updated_at desc")
        reattached = 0
        with self.store.connect() as con:
            for job in jobs:
                pid = int(job["pid"] or 0)
                if self._pid_running(pid):
                    con.execute(
                        """
                        update jobs set status='running',updated_at=?,evidence_status='running',evidence_reason='reattached_after_daemon_restart'
                        where id=?
                        """,
                        (now_iso(), job["id"]),
                    )
                    con.execute("update chats set state='running' where id=? and state!='done'", (job["chat_id"],))
                    reattached += 1
                else:
                    con.execute(
                        """
                        update jobs set status='completed',updated_at=?,completed_at=?,evidence_status='worked',evidence_reason='detached_process_exited'
                        where id=?
                        """,
                        (now_iso(), now_iso(), job["id"]),
                    )
                    con.execute("delete from leases where job_id=?", (job["id"],))
        if reattached:
            self.store.event("jobs_reattached", count=reattached)
        return reattached

    def kill_jobs(self, jobs: list[Row], reason: str) -> int:
        killed = 0
        with self.store.connect() as con:
            for job in jobs:
                pid = int(job["pid"] or 0)
                if str(job["worker_id"] or "").strip():
                    self._kill_remote_job(job)
                self._terminate(pid)
                con.execute(
                    """
                    update jobs set status='killed',updated_at=?,completed_at=?,
                      evidence_status='killed',evidence_reason=? where id=?
                    """,
                    (now_iso(), now_iso(), reason, job["id"]),
                )
                con.execute("delete from leases where job_id=?", (job["id"],))
                con.execute("update chats set state='paused' where id=? and state='running'", (job["chat_id"],))
                killed += 1
        for job in jobs:
            self.store.event("job_killed", job["chat_id"], job["id"], reason=reason)
            grok_watchdog.request("job_killed")
            if reason not in {"daemon_shutdown", "daemon_stop", "daemon_restart", "chat_paused"}:
                recovery.handle_job_failure(
                    self.store,
                    job,
                    evidence_status="killed",
                    evidence_reason=reason,
                )
            if reason in {"daemon_shutdown", "daemon_stop", "daemon_restart"}:
                chat = self.store.row("select * from chats where id=?", (job["chat_id"],))
                if chat and not chat["done"]:
                    with self.store.connect() as con:
                        con.execute(
                            "update chats set paused=0,done=0,state='stalled' where id=? and done=0",
                            (job["chat_id"],),
                        )
        return killed

    def resource_for(self, row: Row) -> str:
        priority_resource = self._row_value(row, "priority_resource_path")
        if priority_resource:
            return self._resource_from_path(priority_resource)
        return self._resource_from_path(row["cwd"] or "") or row["id"]

    def lease_resource_for(self, row: Row) -> str:
        """Per-chat lease key so distinct queued chats on one repo can run in parallel."""
        base = self.resource_for(row)
        chat_id = str(row["id"] or "")
        if base and chat_id:
            return f"{base}::{chat_id}"
        return chat_id or base

    def _resource_from_path(self, value: str) -> str:
        if not value:
            return ""
        path = Path(value).expanduser()
        if not path.exists():
            return str(path)
        if path.is_file():
            path = path.parent
        try:
            found = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                text=True,
                capture_output=True,
                timeout=3,
            )
            if found.returncode == 0 and found.stdout.strip():
                return str(Path(found.stdout.strip()).resolve())
        except Exception:
            pass
        return str(path.resolve())

    def _row_value(self, row: Row, key: str) -> str:
        try:
            if key in row.keys():
                return str(row[key] or "")
        except Exception:
            return ""
        return ""

    def refresh(self) -> None:
        self.reap_stale_remote_jobs()
        self.reattach_detached()
        jobs = self.store.rows("select * from jobs where status='running' order by created_at")
        for job in jobs:
            self._refresh_one(job)
        self._reconcile_idle_running_chats()

    def reap_stale_remote_jobs(self, *, dry_run: bool = False) -> list[str]:
        """Finalize remote jobs whose local SSH wrapper has already exited."""
        reaped: list[str] = []
        jobs = self.store.rows(
            "select * from jobs where status='running' and coalesce(worker_id, '') != ''"
        )
        for job in jobs:
            pid = int(job["pid"] or 0)
            if self._pid_running(pid):
                continue
            reaped.append(str(job["id"]))
            if not dry_run:
                self._refresh_one(job)
        if reaped and not dry_run:
            self.store.event("remote_jobs_reaped", count=len(reaped), job_ids=reaped[:20])
        return reaped

    def _reconcile_idle_running_chats(self) -> None:
        """Chats stuck in running with no live job cannot receive a new drive turn."""
        rows = self.store.rows(
            """
            select c.id from chats c
            where c.state='running' and c.done=0 and c.paused=0
              and not exists (
                select 1 from jobs j where j.chat_id=c.id and j.status='running'
              )
            """
        )
        if not rows:
            return
        with self.store.connect() as con:
            for row in rows:
                con.execute(
                    "update chats set state='active',updated_at=? where id=? and state='running'",
                    (now_iso(), row["id"]),
                )
        self.store.event("idle_running_chats_reconciled", count=len(rows))

    def _refresh_one(self, job: Row) -> None:
        if str(job["worker_id"] or "").strip():
            self._refresh_one_remote(job)
            return
        pid = int(job["pid"] or 0)
        running = self._pid_running(pid)
        out = Path(job["stdout_path"])
        err = Path(job["stderr_path"])
        out_size = out.stat().st_size if out.exists() else 0
        err_size = err.stat().st_size if err.exists() else 0
        age = self._job_activity_age(job, out, err)
        stall_seconds = self._stall_seconds_for(job)
        # Increase stall threshold for long-running tasks (esp. cursor agent jobs which frequently
        # have quiet periods >10-20min with no stdout writes, low-cpu children, or stale terminal
        # mtime updates while reasoning / executing multi-step plans). Base min 600s overrides
        # launchd's low 300s default; cursor gets 1800s. This prevents premature lease release
        # (running_external_idle) → duplicate dispatches → "Couldn't create session" / silent_failed
        # cascades. Directly addresses systematic silent_failed (24x/24h).
        stall_seconds = max(stall_seconds, 600)
        if str(job["provider"] or "") == "cursor":
            stall_seconds = max(stall_seconds, 1800)
        evidence_status = "running_working" if out_size or err_size else "running"
        evidence_reason = f"stdout_bytes={out_size}; stderr_bytes={err_size}"
        status = "running" if running else "completed"
        completed = ""
        archive_chat_id: str | None = None
        pending_goal_retry: tuple[str, str, str] | None = None
        release_lease = False
        if running and not (out_size or err_size):
            activity = self._process_tree_snapshot(pid)
            if activity.has_activity():
                evidence_status = "running_external_activity" if activity.is_recent_or_busy(stall_seconds) else "running_external_idle"
                evidence_reason = f"{evidence_reason}; {activity.summary()}"
                if evidence_status == "running_external_idle":
                    release_lease = True
            elif age > stall_seconds:
                evidence_status = "running_silent"
                evidence_reason = f"{evidence_reason}; no output or child process activity for {int(age)}s"
        job_timeout = DEFAULT_CURSOR_JOB_TIMEOUT if job["provider"] == "cursor" else DEFAULT_JOB_TIMEOUT
        if age > job_timeout and running:
            self._terminate(pid)
            status = "failed"
            completed = now_iso()
            evidence_status = "timed_out_with_work" if out_size or err_size else "silent_failed"
            evidence_reason = f"timed out after {int(age)}s; {evidence_reason}"
        elif not running:
            completed = now_iso()
            stdout_preview = read_text(out, limit=12000) if out_size else ""
            if self._fatal_stderr(err) and not out_size:
                if self._goal_fleet_max_turns(str(job["chat_id"]), err):
                    evidence_status = "goal_incomplete"
                    status = "failed"
                    evidence_reason = f"max_turns; {evidence_reason}"
                else:
                    evidence_status = "provider_error"
                    status = "failed"
            elif out_size or self._meaningful_stderr(err):
                # Use stderr as fallback when stdout is empty — stderr often carries real output
                preview_for_minimal = stdout_preview
                if not preview_for_minimal.strip() and err_size:
                    preview_for_minimal = read_text(err, limit=4000)
                if goals.output_too_minimal(self.store, preview_for_minimal):
                    evidence_status = "goal_incomplete"
                    status = "failed"
                    evidence_reason = f"process exited; minimal output; {evidence_reason}"
                else:
                    evidence_status = "worked"
            else:
                evidence_status = "silent_failed"
                status = "failed"
            if evidence_status != "goal_incomplete":
                evidence_reason = f"process exited; {evidence_reason}"
        with self.store.connect() as con:
            con.execute(
                """
                update jobs set status=?,updated_at=?,completed_at=case when ?!='' then ? else completed_at end,
                  evidence_status=?,evidence_reason=?,stdout_size=?,stderr_size=? where id=?
                """,
                (status, now_iso(), completed, completed, evidence_status, evidence_reason, out_size, err_size, job["id"]),
            )
            if release_lease:
                con.execute("delete from leases where job_id=?", (job["id"],))
            if status != "running":
                con.execute("delete from leases where job_id=?", (job["id"],))
                chat = con.execute("select * from chats where id=?", (job["chat_id"],)).fetchone()
                stdout_text = read_text(out, limit=12000)
                stderr_text = read_text(err, limit=4000)
                # Codex writes prompt/session framing to stderr. Completion state must be
                # judged from assistant output first, otherwise our own prompt text can
                # keep a finished priority artificially active.
                assessment_text = stdout_text if stdout_text.strip() else stderr_text
                marker = parse_fleet_marker(assessment_text)
                if marker:
                    usage = marker.payload.get("usage") if isinstance(marker.payload, dict) else {}
                    if not isinstance(usage, dict):
                        usage = {}
                    token_input = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
                    token_output = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
                    cost_estimate = float(usage.get("cost_usd") or usage.get("cost") or 0)
                    con.execute(
                        """
                        update jobs set marker_kind=?,marker_json=?,
                          token_input=?,token_output=?,cost_estimate=?
                        where id=?
                        """,
                        (marker.kind, json_dumps(marker.payload), token_input, token_output, cost_estimate, job["id"]),
                    )
                    if marker.kind == "FLEET_PLAN":
                        subtasks = marker.payload.get("subtasks") or marker.payload.get("tasks") or []
                        if isinstance(subtasks, list) and subtasks:
                            con.execute(
                                """
                                insert into task_plans(id,chat_id,goal,subtasks_json,status,created_at,updated_at)
                                values(?,?,?,?,?,?,?)
                                on conflict(id) do update set subtasks_json=excluded.subtasks_json,status='active',updated_at=excluded.updated_at
                                """,
                                (
                                    "plan-" + job["chat_id"].replace(":", "-")[:48],
                                    job["chat_id"],
                                    str(marker.payload.get("goal") or ""),
                                    json_dumps(subtasks),
                                    "active",
                                    now_iso(),
                                    now_iso(),
                                ),
                            )
                if evidence_status == "worked":
                    priority = con.execute(
                        """
                        select * from project_priorities
                        where status='active' and target_chat_id=?
                        order by priority desc, updated_at desc
                        limit 1
                        """,
                        (job["chat_id"],),
                    ).fetchone()
                    objective = str(priority["objective"] if priority else (chat["objective"] if chat else ""))
                    assessment = goals.assess_for_completion(self.store, objective, assessment_text)
                    verified, verify_reason = goals.verify_goal_complete(self.store, objective, assessment_text)
                    # If stdout explicitly contains FLEET_DONE, always accept as verified
                    from .policy import FLEET_DONE_MARKER
                    if not verified and (marker and marker.kind == "FLEET_DONE" or FLEET_DONE_MARKER.search(stdout_text)):
                        verified = True
                        verify_reason = "fleet_done_in_stdout"
                    if verified:
                        from . import goal_fleets

                        ext_ok, ext_gid, ext_reason = goal_fleets.external_goal_complete_for_chat(
                            self.store, str(job["chat_id"])
                        )
                        if not ext_ok:
                            verified = False
                            verify_reason = ext_reason or f"external goal {ext_gid} incomplete"
                    if verified:
                        con.execute("update chats set done=1,state='done',last_evidence_at=? where id=?", (now_iso(), job["chat_id"]))
                        con.execute("update goals set status='complete',updated_at=? where chat_id=? and status='active'", (now_iso(), job["chat_id"]))
                        con.execute("update project_priorities set status='complete',updated_at=? where status='active' and target_chat_id=?", (now_iso(), job["chat_id"]))
                        archive_chat_id = str(job["chat_id"])
                    else:
                        con.execute(
                            "update chats set done=0,state=?,failure_count=0,last_evidence_at=? where id=?",
                            (assessment.state, now_iso(), job["chat_id"]),
                        )
                        con.execute(
                            "insert into events(ts,kind,chat_id,job_id,details_json) values(?,?,?,?,?)",
                            (
                                now_iso(),
                                "completion_assessed",
                                job["chat_id"],
                                job["id"],
                                json_dumps(
                                    {
                                        "state": assessment.state,
                                        "complete": False,
                                        "reason": verify_reason or assessment.reason,
                                        "missing": list(assessment.missing),
                                    }
                                ),
                            ),
                        )
                        pending_goal_retry = (verify_reason or assessment.reason, str(job["chat_id"]), str(job["id"]))
                else:
                    con.execute("update chats set state='stalled',failure_count=failure_count+1 where id=?", (job["chat_id"],))
        if status != "running":
            out_path = Path(job["stdout_path"])
            err_path = Path(job["stderr_path"])
            turn_text = read_text(out_path, limit=12000) if out_path.exists() else ""
            if not turn_text.strip() and err_path.exists():
                turn_text = read_text(err_path, limit=4000)
            if turn_text.strip():
                self.store.record_job_turn_context(
                    str(job["chat_id"]),
                    job_id=str(job["id"]),
                    evidence_status=evidence_status,
                    summary=turn_text,
                    reason=evidence_reason,
                )
            self.store.event("job_finished", job["chat_id"], job["id"], status=status, evidence_status=evidence_status, reason=evidence_reason)
            grok_watchdog.request(f"job_{status}")
            if evidence_status == "goal_incomplete":
                recovery.schedule_goal_incomplete(
                    self.store,
                    str(job["chat_id"]),
                    reason=evidence_reason,
                    job_id=str(job["id"]),
                )
            elif evidence_status == "worked":
                recovery.clear_retry_state(self.store, str(job["chat_id"]))
                self.store.clear_provider_health(str(job["provider"] or ""))
            else:
                recovery.handle_job_failure(
                    self.store,
                    job,
                    evidence_status=evidence_status,
                    evidence_reason=evidence_reason,
                )
        if pending_goal_retry:
            reason, chat_id, job_id = pending_goal_retry
            recovery.schedule_goal_incomplete(
                self.store,
                chat_id,
                reason=reason,
                job_id=job_id,
                immediate=True,
            )
        if archive_chat_id:
            self.store.queue_archive(archive_chat_id, reason="completed")

    def _refresh_one_remote(self, job: Row) -> None:
        """Refresh a job dispatched via SSH to a remote worker (tracks local ssh pid only)."""
        pid = int(job["pid"] or 0)
        running = self._pid_running(pid)
        out = Path(job["stdout_path"])
        err = Path(job["stderr_path"])
        out_size = out.stat().st_size if out.exists() else 0
        err_size = err.stat().st_size if err.exists() else 0
        age = self._job_activity_age(job, out, err)
        stall_seconds = max(self._stall_seconds_for(job), 600)
        if str(job["provider"] or "") == "cursor":
            stall_seconds = max(stall_seconds, 1800)
        worker_id = str(job["worker_id"] or "")
        evidence_status = "running_working" if out_size or err_size else "running"
        evidence_reason = f"remote worker={worker_id}; ssh_pid={pid}; stdout_bytes={out_size}; stderr_bytes={err_size}"
        status = "running" if running else "completed"
        completed = ""
        release_lease = False
        if running and err_size and self._fatal_stderr(err) and not out_size:
            evidence_status = "provider_error"
            evidence_reason = f"{evidence_reason}; ssh/provider error in stderr"
        elif running and not (out_size or err_size) and age > stall_seconds:
            evidence_status = "running_silent"
            evidence_reason = f"{evidence_reason}; no remote output for {int(age)}s"
        job_timeout = DEFAULT_CURSOR_JOB_TIMEOUT if job["provider"] == "cursor" else DEFAULT_JOB_TIMEOUT
        if age > job_timeout and running:
            self._kill_remote_job(job)
            self._terminate(pid)
            status = "failed"
            completed = now_iso()
            evidence_status = "timed_out_with_work" if out_size or err_size else "silent_failed"
            evidence_reason = f"remote timed out after {int(age)}s; {evidence_reason}"
        elif not running:
            completed = now_iso()
            stdout_preview = read_text(out, limit=12000) if out_size else ""
            stderr_preview = read_text(err, limit=4000) if err_size else ""
            if self._fatal_stderr(err) and not out_size:
                evidence_status = "provider_error"
                status = "failed"
            elif out_size or self._meaningful_stderr(err):
                preview_for_minimal = stdout_preview or stderr_preview
                if goals.output_too_minimal(self.store, preview_for_minimal):
                    evidence_status = "goal_incomplete"
                    status = "failed"
                elif self._fatal_stderr(err) and not stdout_preview.strip():
                    evidence_status = "provider_error"
                    status = "failed"
                else:
                    evidence_status = "worked"
            else:
                evidence_status = "silent_failed"
                status = "failed"
            evidence_reason = f"remote ssh exited; {evidence_reason}"
        with self.store.connect() as con:
            con.execute(
                """
                update jobs set status=?,updated_at=?,completed_at=case when ?!='' then ? else completed_at end,
                  evidence_status=?,evidence_reason=?,stdout_size=?,stderr_size=? where id=?
                """,
                (status, now_iso(), completed, completed, evidence_status, evidence_reason, out_size, err_size, job["id"]),
            )
            if release_lease or status != "running":
                con.execute("delete from leases where job_id=?", (job["id"],))
            if status != "running":
                chat = con.execute("select * from chats where id=?", (job["chat_id"],)).fetchone()
                if chat and not chat["done"]:
                    new_state = "active" if status == "completed" and evidence_status == "worked" else "stalled"
                    con.execute(
                        "update chats set state=?,updated_at=? where id=? and state='running'",
                        (new_state, now_iso(), job["chat_id"]),
                    )
        if status == "failed":
            recovery.handle_job_failure(
                self.store,
                job,
                evidence_status=evidence_status,
                evidence_reason=evidence_reason,
            )

    def _kill_remote_job(self, job: Row) -> None:
        from . import remote_ssh

        worker_id = str(job["worker_id"] or "").strip()
        if not worker_id:
            return
        worker = self.store.row("select * from remote_workers where id=?", (worker_id,))
        if not worker:
            return
        try:
            subprocess.run(
                remote_ssh.build_remote_kill_command(worker, str(job["id"])),
                capture_output=True,
                text=True,
                timeout=20,
            )
        except Exception:
            pass

    def _stall_seconds_for(self, job: Row) -> int:
        chat = self.store.row("select objective from chats where id=?", (job["chat_id"],))
        objective = str(chat["objective"] or "") if chat else ""
        return recovery.stall_seconds_for_chat(self.store, str(job["chat_id"]), str(job["prompt"] or ""), objective)

    def _pid_running(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            waited, _ = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                return False
        except ChildProcessError:
            pass
        try:
            stat = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], text=True, capture_output=True, timeout=2).stdout.strip()
            if not stat or "Z" in stat:
                return False
        except Exception:
            pass
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _terminate(self, pid: int) -> None:
        if pid <= 0:
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass

    def _use_worktree(self, lease_resource: str, cwd: str) -> bool:
        value = self.store.get_config("use_worktrees", "off").lower()
        return value in {"1", "true", "yes", "on"} and bool(lease_resource) and Path(cwd).exists()

    def _prepare_worktree(self, cwd: str, job_id: str) -> Path | None:
        try:
            root = subprocess.run(
                ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                text=True,
                capture_output=True,
                timeout=5,
            )
            if root.returncode != 0 or not root.stdout.strip():
                return None
            repo = Path(root.stdout.strip()).resolve()
            target = WORKTREES / job_id
            if target.exists():
                return target
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", "--detach", str(target), "HEAD"],
                text=True,
                capture_output=True,
                timeout=60,
                check=True,
            )
            self.store.event("worktree_created", job_id=job_id, repo=str(repo), worktree=str(target))
            return target
        except Exception as exc:
            self.store.event("worktree_create_failed", job_id=job_id, cwd=cwd, error=str(exc))
            return None

    def _process_tree_activity(self, pid: int) -> str:
        return self._process_tree_snapshot(pid).summary()

    def _process_tree_snapshot(self, pid: int) -> ProcessActivity:
        if pid <= 0:
            return ProcessActivity()
        try:
            proc = subprocess.run(
                ["ps", "-axo", "pid=,ppid=,stat=,%cpu=,command="],
                text=True,
                capture_output=True,
                timeout=3,
            )
        except Exception:
            return ProcessActivity()
        children: dict[int, list[tuple[int, str, float, str]]] = {}
        for line in proc.stdout.splitlines():
            parts = line.strip().split(None, 4)
            if len(parts) < 5:
                continue
            try:
                child_pid = int(parts[0])
                parent_pid = int(parts[1])
                cpu = float(parts[3])
            except Exception:
                continue
            children.setdefault(parent_pid, []).append((child_pid, parts[2], cpu, parts[4]))
        stack = list(children.get(pid, []))
        seen: set[int] = set()
        active: list[tuple[int, str, float, str]] = []
        while stack:
            item = stack.pop()
            child_pid, stat, cpu, command = item
            if child_pid in seen:
                continue
            seen.add(child_pid)
            active.append(item)
            stack.extend(children.get(child_pid, []))
        busy = sum(1 for _, _, cpu, _ in active if cpu >= 1.0)
        sample = "; ".join(f"{p}:{cmd[:70]}" for p, _, _, cmd in active[:3])
        newest_terminal_age, terminal_sample = self._cursor_terminal_activity(pid)
        return ProcessActivity(
            child_count=len(active),
            busy_children=busy,
            child_sample=sample,
            newest_terminal_age=newest_terminal_age,
            terminal_sample=terminal_sample,
        )

    def _cursor_terminal_activity(self, pid: int) -> tuple[float | None, str]:
        projects = HOME / ".cursor" / "projects"
        if not projects.exists():
            return None, ""
        newest_age: float | None = None
        sample = ""
        for path in projects.glob("**/terminals/*.txt"):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if f"pid: {pid}" not in text and f"ppid: {pid}" not in text:
                continue
            try:
                age = max(0.0, now_ts() - path.stat().st_mtime)
            except Exception:
                continue
            if newest_age is None or age < newest_age:
                newest_age = age
                sample = f"{path.name}:{read_text(path, limit=160).replace(chr(10), ' ')[:120]}"
        return newest_age, sample

    def _meaningful_stderr(self, path: Path) -> bool:
        text = read_text(path, limit=4000).lower()
        if not text.strip():
            return False
        noisy = ["could not find a git repository", "new version", "warning"]
        return any(line.strip() and not any(n in line.lower() for n in noisy) for line in text.splitlines())

    def _goal_fleet_max_turns(self, chat_id: str, err: Path) -> bool:
        if "goal-fleet" not in str(chat_id or ""):
            return False
        text = read_text(err, limit=4000).lower()
        return "max_turns exceeded" in text

    def _fatal_stderr(self, path: Path) -> bool:
        text = read_text(path, limit=4000).lower()
        fatal = [
            "error: --resume requires",
            "couldn't create session",
            "session does not exist",
            "traceback (most recent call last)",
            "no such file or directory",
            "command not found",
            "permission denied",
            "api error",
            "bad request",
            "http_status",
            "internal error",
            "does not support parameter",
            "exec request failed on channel",
            "failed to read",
            "cannot find the path specified",
            "max_turns exceeded",
            "workspace path does not exist",
            "workspace path does not exist:",
            "signing in with grok",
            "open this url to sign in",
            "out of usage",
            "increase your limit",
        ]
        return any(marker in text for marker in fatal)

    def _ts(self, value: str) -> float:
        from .util import parse_ts
        return parse_ts(value)

    def _job_activity_age(self, job: Row, stdout: Path, stderr: Path) -> float:
        """Seconds since last observable job activity (start or stream output)."""
        now = now_ts()
        last = self._ts(job["created_at"])
        for path in (stdout, stderr):
            try:
                if path.exists():
                    st = path.stat()
                    if st.st_size > 0:
                        last = max(last, min(st.st_mtime, now))
            except OSError:
                pass
        return max(0.0, now - last)
