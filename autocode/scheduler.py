from __future__ import annotations

import time
from pathlib import Path
from sqlite3 import Row

from .config import DEFAULT_DISCOVERY_INTERVAL, DEFAULT_MAX_ACTIVE, HOME
from .discovery import discover
from .models import Chat
from .models import ContinuePlan
from .policy import build_prompt
from .providers import provider_map
from .runner import JobRunner
from .store import Store
from .util import disk_free_gb, load1, memory_free_percent, now_iso, parse_ts


class Scheduler:
    def __init__(self, store: Store):
        self.store = store
        self.runner = JobRunner(store)

    def tick(self, dispatch: bool = True, max_projects: int | None = None) -> dict:
        self.runner.refresh()
        stale_leases = self.cleanup_stale_leases()
        self._maybe_discover()
        self.enforce_priority_invariants()
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
            "stale_leases": stale_leases,
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
        self.enforce_priority_invariants()
        return stats

    def enforce_priority_invariants(self) -> int:
        """Keep active pinned priorities schedulable in their exact target chat."""
        priorities = self.store.rows(
            """
            select * from project_priorities
            where status='active' and target_chat_id!=''
            """
        )
        repaired = 0
        with self.store.connect() as con:
            for p in priorities:
                row = con.execute("select * from chats where id=?", (p["target_chat_id"],)).fetchone()
                if not row:
                    continue
                state = "running" if row["state"] == "running" else "active"
                if row["done"] or row["paused"] or not row["adopted"] or row["state"] not in {"active", "running", "stalled", "needs_input"}:
                    repaired += 1
                con.execute(
                    """
                    update chats
                    set adopted=1,paused=0,done=0,state=?,objective=?,
                      coding_score=max(coding_score, 1)
                    where id=?
                    """,
                    (state, p["objective"], p["target_chat_id"]),
                )
                con.execute(
                    """
                    insert into goals(id,chat_id,objective,status,source,created_at,updated_at)
                    values(?,?,?,?,?,?,?)
                    on conflict(id) do update set objective=excluded.objective,status='active',updated_at=excluded.updated_at
                    """,
                    (
                        f"priority:{p['id']}",
                        p["target_chat_id"],
                        p["objective"],
                        "active",
                        "priority",
                        now_iso(),
                        now_iso(),
                    ),
                )
                con.execute(
                    "update goals set status='superseded',updated_at=? where chat_id=? and status='active' and id!=?",
                    (now_iso(), p["target_chat_id"], f"priority:{p['id']}"),
                )
        if repaired:
            self.store.event("priority_invariants_repaired", repaired=repaired)
        return repaired

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
        priority_rows = self.priority_candidates(limit)
        if self.store.get_config("priority_only", "off").lower() in {"1", "true", "yes", "on"}:
            return priority_rows[:limit]
        seen = {r["id"] for r in priority_rows}
        if len(priority_rows) >= limit:
            return priority_rows[:limit]
        general = self.store.rows(
            """
            select * from chats
            where adopted=1 and paused=0 and done=0 and coding_score>0
              and failure_count < 3
            order by
              case state when 'needs_input' then 0 when 'stalled' then 1 when 'active' then 2 when 'running' then 3 else 4 end,
              case when objective!='' then 0 else 1 end,
              failure_count asc,
              updated_at desc
            limit ?
            """,
            (max(limit * 2, 20),),
        )
        rows = list(priority_rows)
        for row in general:
            if row["id"] in seen:
                continue
            rows.append(row)
            if len(rows) >= limit:
                break
        return rows

    def priority_candidates(self, limit: int) -> list[Row]:
        priorities = self.store.rows(
            """
            select * from project_priorities
            where status='active'
            order by priority desc, updated_at desc
            limit 50
            """
        )
        if not priorities:
            return []
        rows: list[Row] = []
        seen: set[str] = set()
        for p in priorities:
            target = str(p["target_chat_id"] or "")
            if target:
                match = self.store.row(
                    """
                    select *, ? as priority_objective, ? as priority_rank, ? as priority_id,
                      ? as priority_resource_path, ? as priority_worker_lanes
                    from chats
                    where id=? and paused=0 and done=0 and coding_score>0
                    limit 1
                    """,
                    (p["objective"], p["priority"], p["id"], p["resource_path"], p["worker_lanes"], target),
                )
                if match and match["id"] not in seen:
                    seen.add(match["id"])
                    rows.append(match)
                    if len(rows) >= limit:
                        return rows
            q = f"%{str(p['query']).lower()}%"
            matches = self.store.rows(
                """
                select *, ? as priority_objective, ? as priority_rank, ? as priority_id,
                  ? as priority_resource_path, ? as priority_worker_lanes
                from chats
                where paused=0 and done=0 and coding_score>0
                  and (
                    lower(id)=lower(?) or lower(alias)=lower(?) or lower(provider_chat_id)=lower(?)
                    or lower(title) like ? or lower(alias) like ? or lower(cwd) like ?
                  )
                order by
                  case state when 'needs_input' then 0 when 'stalled' then 1 when 'active' then 2 when 'running' then 3 else 4 end,
                  failure_count asc,
                  updated_at desc
                limit 8
                """,
                (
                    p["objective"], p["priority"], p["id"], p["resource_path"], p["worker_lanes"],
                    p["query"], p["query"], p["query"], q, q, q,
                ),
            )
            for row in matches:
                if row["id"] in seen:
                    continue
                seen.add(row["id"])
                rows.append(row)
                if len(rows) >= limit:
                    return rows
        return rows

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

    def dispatch(self, row: Row) -> str | None:
        prompt = build_prompt(row, recovery=row["state"] == "stalled")
        return self.dispatch_with_prompt(row, prompt)

    def dispatch_with_prompt(self, row: Row, prompt: str) -> str | None:
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
        if int(row["failure_count"] or 0) >= 2 and not self._pinned_priority(row) and not self._direct_cursor_lane(row):
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

    def _pinned_priority(self, row: Row) -> bool:
        try:
            return bool(row["priority_id"] and row["priority_objective"])
        except Exception:
            return False

    def _direct_cursor_lane(self, row: Row) -> bool:
        try:
            return row["provider"] == "cursor" and row["source"] in {"cursor.cli", "cursor.cloud"}
        except Exception:
            return False
