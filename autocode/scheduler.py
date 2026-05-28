from __future__ import annotations

import time
from pathlib import Path
from sqlite3 import Row

from .config import (
    DEFAULT_ACTIVE_DISCOVERY_INTERVAL,
    DEFAULT_IDLE_DISCOVERY_INTERVAL,
    DEFAULT_MAX_ACTIVE,
    DEFAULT_MAX_FAILURE_COUNT,
    HOME,
)
from . import recovery
from .discovery import discover
from .models import Chat
from .models import ContinuePlan
from .policy import build_prompt
from .providers import provider_map
from .runner import JobRunner
from .store import Store
from .util import disk_free_gb, load1, memory_free_percent
from .watchers import watch_signature


class Scheduler:
    def __init__(self, store: Store):
        self.store = store
        self.runner = JobRunner(store)

    def tick(self, dispatch: bool = True, max_projects: int | None = None) -> dict:
        self.runner.refresh()
        self.store.queue_archive_done()
        unstuck = recovery.reconcile_killed_chats(self.store)
        stale_leases = self.cleanup_stale_leases()
        discovery_reason = self._maybe_discover()
        active = self.active_job_count()
        cap = self.capacity()
        limit = max(0, min(max_projects or cap, cap) - active)
        sent: list[str] = []
        queued = self.candidates(limit * 3 if limit else 50)
        snapshot_id = self.store.record_queue_snapshot(
            queued,
            reason=discovery_reason or "tick",
            capacity=cap,
            active_jobs=active,
            resource_for=self.runner.resource_for,
        )
        if dispatch and limit > 0:
            for row in queued:
                if len(sent) >= limit:
                    break
                if self.has_active_job(row["id"]) or self.has_active_lease(row):
                    continue
                job_id = self.dispatch(row, queue_snapshot_id=snapshot_id)
                if job_id:
                    sent.append(job_id)
        return {
            "sent": len(sent),
            "jobs": sent,
            "active_jobs": self.active_job_count(),
            "capacity": cap,
            "candidates": len(queued),
            "queue_snapshot": snapshot_id,
            "stale_leases": stale_leases,
            "recovery_unstuck": unstuck,
        }

    def _maybe_discover(self) -> str:
        last = self.store.get_config("last_discovery_ts", "0")
        sig = watch_signature()
        old_sig = self.store.get_config("last_watch_signature", "")
        active = self.active_job_count()
        interval = DEFAULT_ACTIVE_DISCOVERY_INTERVAL if active else DEFAULT_IDLE_DISCOVERY_INTERVAL
        if sig and sig != old_sig:
            stats = discover(self.store)
            self.store.set_config("last_discovery_ts", str(time.time()))
            self.store.set_config("last_watch_signature", sig)
            self.store.event("discovery_refresh", reason="watch", **stats)
            return "watch"
        if time.time() - float(last or 0) > interval:
            stats = discover(self.store)
            self.store.set_config("last_discovery_ts", str(time.time()))
            self.store.set_config("last_watch_signature", sig)
            self.store.event("discovery_refresh", reason="poll", interval=interval, **stats)
            return "poll"
        return "none"

    def force_discover(self) -> dict[str, int]:
        stats = discover(self.store)
        self.store.set_config("last_discovery_ts", str(time.time()))
        return stats

    def capacity(self) -> int:
        configured = int(self.store.get_config("max_active", str(DEFAULT_MAX_ACTIVE)) or DEFAULT_MAX_ACTIVE)
        disk_free = disk_free_gb(self.store.path.parent)
        if disk_free is not None and disk_free < 0.75:
            return 0
        l1 = load1()
        mem_free = memory_free_percent()
        if mem_free is not None and mem_free < 12:
            return 0
        if mem_free is not None and mem_free < 20:
            configured = min(configured, 1)
        elif mem_free is not None and mem_free < 30:
            configured = min(configured, 2)
        if l1 >= 10:
            return min(configured, 1)
        if l1 >= 7:
            return min(configured, 2)
        if l1 >= 5:
            return min(configured, 3)
        return configured

    def active_job_count(self) -> int:
        row = self.store.row("select count(*) c from jobs where status='running'")
        return int(row["c"] if row else 0)

    def candidates(self, limit: int) -> list[Row]:
        rows = self.store.rows(
            """
            select c.*, q.position as queue_position
            from queue q join chats c on c.id=q.chat_id
            where c.paused=0 and c.done=0 and c.failure_count < ?
              and (
                c.last_drive_at is not null
                or c.last_seen_at < datetime('now', '-60 seconds')
              )
            order by q.position asc
            limit ?
            """,
            (DEFAULT_MAX_FAILURE_COUNT + 4, limit * 4),
        )
        ready: list[Row] = []
        for row in rows:
            cap = recovery.max_failure_count(self.store, row["id"])
            if int(row["failure_count"] or 0) >= cap:
                continue
            if not recovery.retry_ready(row):
                continue
            if recovery.provider_in_backoff(self.store, str(row["provider"] or "")):
                continue
            ready.append(row)
            if len(ready) >= limit:
                break
        return ready

    def has_active_job(self, chat_id: str) -> bool:
        row = self.store.row("select count(*) c from jobs where chat_id=? and status='running'", (chat_id,))
        return int(row["c"] if row else 0) > 0

    def has_active_lease(self, row: Row) -> bool:
        resource = self.runner.resource_for(row)
        lease = self.store.row(
            """
            select l.*,j.status job_status,j.pid job_pid
            from leases l left join jobs j on j.id=l.job_id
            where l.resource=?
            """,
            (resource,),
        )
        if not lease:
            return False
        if lease["job_status"] == "running":
            return True
        with self.store.connect() as con:
            con.execute("delete from leases where resource=?", (resource,))
        self.store.event("stale_lease_removed", lease["chat_id"], lease["job_id"], resource=resource, job_status=lease["job_status"] or "missing")
        return False

    def cleanup_stale_leases(self) -> int:
        stale = self.store.rows(
            """
            select l.*,j.status job_status
            from leases l left join jobs j on j.id=l.job_id
            where j.id is null or j.status!='running'
            """
        )
        if not stale:
            return 0
        with self.store.connect() as con:
            for lease in stale:
                con.execute("delete from leases where resource=? and job_id=?", (lease["resource"], lease["job_id"]))
        self.store.event("stale_leases_removed", count=len(stale))
        return len(stale)

    def dispatch(self, row: Row, queue_snapshot_id: str = "") -> str | None:
        prompt = build_prompt(self._row_with_plan(row), recovery=row["state"] == "stalled")
        return self.dispatch_with_prompt(row, prompt, queue_snapshot_id=queue_snapshot_id)

    def dispatch_with_prompt(self, row: Row, prompt: str, queue_snapshot_id: str = "") -> str | None:
        chat = Chat(
            id=row["id"],
            provider=row["provider"],
            source=row["source"],
            provider_chat_id=row["provider_chat_id"],
            title=row["title"],
            cwd=row["cwd"],
            updated_at=row["updated_at"],
            latest_text=row["latest_text"],
            transcript_hash=row["transcript_hash"],
            alias=row["alias"],
            continuation=row["continuation"],
        )
        job_dir = self._planned_job_dir()
        if recovery.should_use_fallback(row) and not self._direct_cursor_lane(row):
            plan = self.fallback_plan(row, prompt, job_dir)
        else:
            providers = provider_map()
            provider = providers.get(row["provider"])
            if not provider or recovery.provider_in_backoff(self.store, str(row["provider"] or "")):
                plan = self.fallback_plan(row, prompt, job_dir)
            else:
                plan = provider.continue_plan(chat, prompt, job_dir)
        if not plan.supported:
            self.store.event("dispatch_unsupported", row["id"], provider=row["provider"], reason=plan.reason)
            return None
        job_id = self.runner.start(row, plan, prompt, job_dir, queue_snapshot_id=queue_snapshot_id)
        self.store.event("dispatch", row["id"], job_id, provider=plan.provider)
        return job_id

    def _row_with_plan(self, row: Row):
        plan = self.store.task_plan_summary(row["id"])
        if not plan:
            return row
        data = {key: row[key] for key in row.keys()}
        data["task_plan"] = plan

        class PromptRow(dict):
            def keys(self):
                return super().keys()

        return PromptRow(data)

    def fallback_plan(self, row: Row, prompt: str, job_dir) -> ContinuePlan:
        raw_cwd = row["cwd"] or str(HOME)
        cwd = raw_cwd if Path(raw_cwd).exists() else str(HOME)
        takeover = (
            "AutoCode provider recovery takeover.\n"
            f"Original provider: {row['provider']} / {row['source']}\n"
            f"Original chat id: {row['provider_chat_id']}\n\n"
            f"{prompt}\n"
        )
        if row["provider"] == "codex":
            prompt_path = job_dir / "prompt.txt"
            return ContinuePlan(
                True,
                "grok",
                cwd,
                cmd=[
                    "grok",
                    "--cwd",
                    cwd,
                    "--prompt-file",
                    str(prompt_path),
                    "--no-alt-screen",
                    "--permission-mode",
                    "bypassPermissions",
                ],
                prompt_file=True,
                same_chat=False,
                reason="Codex stalled twice; switching to Grok takeover.",
            )
        return ContinuePlan(
            True,
            "codex",
            cwd,
            cmd=["codex", "exec", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "-C", cwd, "-"],
            stdin=takeover,
            same_chat=False,
            reason=f"{row['provider']} stalled/unsupported; switching to Codex takeover.",
        )

    def _planned_job_dir(self):
        from .config import JOBS
        import uuid
        p = JOBS / ("job-" + uuid.uuid4().hex[:12])
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _direct_cursor_lane(self, row: Row) -> bool:
        try:
            return row["provider"] == "cursor" and row["source"] in {"cursor.cli", "cursor.cloud"}
        except Exception:
            return False
