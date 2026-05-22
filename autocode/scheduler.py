from __future__ import annotations

import time
from sqlite3 import Row

from .config import DEFAULT_DISCOVERY_INTERVAL, DEFAULT_MAX_ACTIVE
from .discovery import discover
from .models import Chat
from .models import ContinuePlan
from .policy import build_prompt
from .providers import provider_map
from .runner import JobRunner
from .store import Store
from .util import load1, now_iso, parse_ts


class Scheduler:
    def __init__(self, store: Store):
        self.store = store
        self.runner = JobRunner(store)

    def tick(self, dispatch: bool = True, max_projects: int | None = None) -> dict:
        self.runner.refresh()
        self._maybe_discover()
        active = self.active_job_count()
        cap = self.capacity()
        limit = max(0, min(max_projects or cap, cap) - active)
        sent: list[str] = []
        if dispatch and limit > 0:
            for row in self.candidates(limit * 3):
                if len(sent) >= limit:
                    break
                if self.has_active_job(row["id"]) or self.has_active_lease(row):
                    continue
                job_id = self.dispatch(row)
                if job_id:
                    sent.append(job_id)
        return {
            "sent": len(sent),
            "jobs": sent,
            "active_jobs": self.active_job_count(),
            "capacity": cap,
            "candidates": len(self.candidates(50)),
        }

    def _maybe_discover(self) -> None:
        last = self.store.get_config("last_discovery_ts", "0")
        if time.time() - float(last or 0) > DEFAULT_DISCOVERY_INTERVAL:
            stats = discover(self.store)
            self.store.set_config("last_discovery_ts", str(time.time()))
            self.store.event("discovery_refresh", **stats)

    def force_discover(self) -> dict[str, int]:
        stats = discover(self.store)
        self.store.set_config("last_discovery_ts", str(time.time()))
        return stats

    def capacity(self) -> int:
        configured = int(self.store.get_config("max_active", str(DEFAULT_MAX_ACTIVE)) or DEFAULT_MAX_ACTIVE)
        l1 = load1()
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
        return self.store.rows(
            """
            select * from chats
            where adopted=1 and paused=0 and done=0 and coding_score>0
            order by
              case state when 'needs_input' then 0 when 'stalled' then 1 when 'active' then 2 when 'running' then 3 else 4 end,
              case when objective!='' then 0 else 1 end,
              failure_count asc,
              updated_at desc
            limit ?
            """,
            (limit,),
        )

    def has_active_job(self, chat_id: str) -> bool:
        row = self.store.row("select count(*) c from jobs where chat_id=? and status='running'", (chat_id,))
        return int(row["c"] if row else 0) > 0

    def has_active_lease(self, row: Row) -> bool:
        resource = row["cwd"] or row["id"]
        lease = self.store.row("select * from leases where resource=?", (resource,))
        return bool(lease)

    def dispatch(self, row: Row) -> str | None:
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
        prompt = build_prompt(row, recovery=row["state"] == "stalled")
        job_dir = self._planned_job_dir()
        if int(row["failure_count"] or 0) >= 2:
            plan = self.fallback_plan(row, prompt, job_dir)
        else:
            providers = provider_map()
            provider = providers.get(row["provider"])
            if not provider:
                plan = self.fallback_plan(row, prompt, job_dir)
            else:
                plan = provider.continue_plan(chat, prompt, job_dir)
        if not plan.supported:
            self.store.event("dispatch_unsupported", row["id"], provider=row["provider"], reason=plan.reason)
            return None
        job_id = self.runner.start(row, plan, prompt, job_dir)
        self.store.event("dispatch", row["id"], job_id, provider=plan.provider)
        return job_id

    def fallback_plan(self, row: Row, prompt: str, job_dir) -> ContinuePlan:
        cwd = row["cwd"] or "/Users/lukekensik"
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
                    "--effort",
                    "high",
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
