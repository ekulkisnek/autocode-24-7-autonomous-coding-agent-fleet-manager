from __future__ import annotations

import os
import signal
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Row

from .config import DEFAULT_CURSOR_JOB_TIMEOUT, DEFAULT_JOB_TIMEOUT, DEFAULT_STALL_SECONDS, JOBS
from .config import HOME, WORKTREES
from .markers import parse_fleet_marker
from .models import Chat, ContinuePlan
from .policy import assess_output_state
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
            lease_resource=self.resource_for(row),
            queue_snapshot_id=queue_snapshot_id,
        )

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
        lease_resource: str = "",
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
        if self._use_worktree(lease_resource, cwd):
            wt = self._prepare_worktree(cwd, job_id)
            if wt:
                cwd = str(wt)
                worktree_path = str(wt)
        proc = subprocess.Popen(
            plan.cmd,
            cwd=cwd or None,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            stdout=out_f,
            stderr=err_f,
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

    def kill_jobs(self, jobs: list[Row], reason: str) -> int:
        killed = 0
        with self.store.connect() as con:
            for job in jobs:
                pid = int(job["pid"] or 0)
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
        return killed

    def resource_for(self, row: Row) -> str:
        priority_resource = self._row_value(row, "priority_resource_path")
        if priority_resource:
            return self._resource_from_path(priority_resource)
        return self._resource_from_path(row["cwd"] or "") or row["id"]

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
        jobs = self.store.rows("select * from jobs where status='running' order by created_at")
        for job in jobs:
            self._refresh_one(job)

    def _refresh_one(self, job: Row) -> None:
        pid = int(job["pid"] or 0)
        running = self._pid_running(pid)
        out = Path(job["stdout_path"])
        err = Path(job["stderr_path"])
        out_size = out.stat().st_size if out.exists() else 0
        err_size = err.stat().st_size if err.exists() else 0
        age = now_ts() - self._ts(job["created_at"])
        evidence_status = "running_working" if out_size or err_size else "running"
        evidence_reason = f"stdout_bytes={out_size}; stderr_bytes={err_size}"
        status = "running" if running else "completed"
        completed = ""
        if running and not (out_size or err_size):
            activity = self._process_tree_snapshot(pid)
            if activity.has_activity():
                evidence_status = "running_external_activity" if activity.is_recent_or_busy(DEFAULT_STALL_SECONDS) else "running_external_idle"
                evidence_reason = f"{evidence_reason}; {activity.summary()}"
            elif age > DEFAULT_STALL_SECONDS:
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
            if self._fatal_stderr(err) and not out_size:
                evidence_status = "provider_error"
                status = "failed"
            elif out_size or self._meaningful_stderr(err):
                evidence_status = "worked"
            else:
                evidence_status = "silent_failed"
                status = "failed"
            evidence_reason = f"process exited; {evidence_reason}"
        with self.store.connect() as con:
            con.execute(
                """
                update jobs set status=?,updated_at=?,completed_at=case when ?!='' then ? else completed_at end,
                  evidence_status=?,evidence_reason=?,stdout_size=?,stderr_size=? where id=?
                """,
                (status, now_iso(), completed, completed, evidence_status, evidence_reason, out_size, err_size, job["id"]),
            )
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
                    con.execute(
                        "update jobs set marker_kind=?,marker_json=? where id=?",
                        (marker.kind, json_dumps(marker.payload), job["id"]),
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
                    assessment = assess_output_state(objective, assessment_text)
                    if assessment.complete:
                        con.execute("update chats set done=1,state='done',last_evidence_at=? where id=?", (now_iso(), job["chat_id"]))
                        con.execute("update goals set status='complete',updated_at=? where chat_id=? and status='active'", (now_iso(), job["chat_id"]))
                        con.execute("update project_priorities set status='complete',updated_at=? where status='active' and target_chat_id=?", (now_iso(), job["chat_id"]))
                    else:
                        con.execute("update chats set done=0,state=?,last_evidence_at=? where id=?", (assessment.state, now_iso(), job["chat_id"]))
                        con.execute(
                            "insert into events(ts,kind,chat_id,job_id,details_json) values(?,?,?,?,?)",
                            (
                                now_iso(),
                                "completion_assessed",
                                job["chat_id"],
                                job["id"],
                                json_dumps({"state": assessment.state, "complete": assessment.complete, "reason": assessment.reason, "missing": list(assessment.missing)}),
                            ),
                        )
                else:
                    con.execute("update chats set state='stalled',failure_count=failure_count+1 where id=?", (job["chat_id"],))
        if status != "running":
            self.store.event("job_finished", job["chat_id"], job["id"], status=status, evidence_status=evidence_status, reason=evidence_reason)

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

    def _fatal_stderr(self, path: Path) -> bool:
        text = read_text(path, limit=4000).lower()
        fatal = [
            "error: --resume requires",
            "traceback (most recent call last)",
            "no such file or directory",
            "command not found",
            "permission denied",
            "api error",
            "bad request",
            "http_status",
            "internal error",
            "does not support parameter",
        ]
        return any(marker in text for marker in fatal)

    def _ts(self, value: str) -> float:
        from .util import parse_ts
        return parse_ts(value)
