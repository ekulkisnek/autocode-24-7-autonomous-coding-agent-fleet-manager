from __future__ import annotations

import os
import signal
import subprocess
import uuid
from pathlib import Path
from sqlite3 import Row

from .config import DEFAULT_JOB_TIMEOUT, JOBS
from .config import HOME
from .models import Chat, ContinuePlan
from .policy import should_continue_after_output
from .store import Store
from .util import json_dumps, now_iso, now_ts, read_text


class JobRunner:
    def __init__(self, store: Store):
        self.store = store

    def start(self, row: Row, plan: ContinuePlan, prompt: str, job_dir: Path | None = None) -> str:
        return self._start(
            chat_id=row["id"],
            cwd=row["cwd"] or str(HOME),
            plan=plan,
            prompt=prompt,
            job_dir=job_dir,
            lease_resource=self.resource_for(row),
        )

    def start_aux(
        self,
        chat_id: str,
        cwd: str,
        plan: ContinuePlan,
        prompt: str,
        job_dir: Path | None = None,
        lease_resource: str = "",
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
        proc = subprocess.Popen(
            plan.cmd,
            cwd=cwd or None,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            stdout=out_f,
            stderr=err_f,
            env=env,
        )
        if stdin_data is not None and proc.stdin:
            proc.stdin.write(stdin_data.encode("utf-8", errors="replace"))
            proc.stdin.close()
        out_f.close()
        err_f.close()
        with self.store.connect() as con:
            con.execute(
                """
                insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at)
                values(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (job_id, chat_id, plan.provider, "running", proc.pid, cwd, json_dumps(plan.cmd), prompt, str(stdout), str(stderr), now_iso(), now_iso()),
            )
            if lease_resource:
                con.execute("insert or replace into leases(resource,chat_id,job_id,expires_at) values(?,?,?,?)", (lease_resource, chat_id, job_id, now_iso()))
            con.execute("update chats set state='running',last_drive_at=? where id=?", (now_iso(), chat_id))
        self.store.event("job_started", chat_id, job_id, provider=plan.provider, pid=proc.pid, same_chat=plan.same_chat)
        return job_id

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
        if age > DEFAULT_JOB_TIMEOUT and running:
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
                output = read_text(out, limit=12000) + "\n" + read_text(err, limit=4000)
                if "FLEET_DONE" in output:
                    con.execute("update chats set done=1,state='done',last_evidence_at=? where id=?", (now_iso(), job["chat_id"]))
                    con.execute("update goals set status='complete',updated_at=? where chat_id=? and status='active'", (now_iso(), job["chat_id"]))
                elif evidence_status == "worked":
                    next_state = "active" if should_continue_after_output(output) else "active"
                    con.execute("update chats set state=?,last_evidence_at=? where id=?", (next_state, now_iso(), job["chat_id"]))
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
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass

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
