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
from . import goals
from . import recovery
from . import remediation
from .discovery import discover
from .models import Chat
from .models import ContinuePlan
from .policy import build_prompt
from .providers import provider_map
from . import grok_watchdog
from .runner import JobRunner
from .store import Store
from .util import disk_free_gb, load1, memory_free_percent
from .watchers import watch_signature

# Jobs waiting on an external desktop agent should not consume dispatch slots or repo leases.
DISPATCH_SLOT_EXCLUDED_EVIDENCE = frozenset({"running_external_idle"})


class Scheduler:
    def __init__(self, store: Store):
        self.store = store
        self.runner = JobRunner(store)

    def tick(self, dispatch: bool = True, max_projects: int | None = None) -> dict:
        self.runner.refresh()
        from . import goals

        queue_archived = goals.reconcile_done_still_in_queue(self.store)
        reopened = goals.reconcile_false_done_chats(self.store)
        auto_fix = remediation.remediation_pass(self.store)
        from . import watchdog_executor, self_improve
        watchdog_executor.process_actions(self.store, self)
        self_improve.scan(self.store)
        queue_archived.extend(self.store.queue_archive_done())
        auto_fix["queue_archived"] = queue_archived
        unstuck = recovery.reconcile_killed_chats(self.store)
        stale_leases = self.cleanup_stale_leases()
        discovery_reason = self._maybe_discover()
        active = self.dispatch_active_job_count()
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
                grok_watchdog.request("prompt_due")
                job_id = self.dispatch(row, queue_snapshot_id=snapshot_id)
                if job_id:
                    sent.append(job_id)
                    grok_watchdog.request("dispatch")
        return {
            "sent": len(sent),
            "jobs": sent,
            "active_jobs": self.active_job_count(),
            "capacity": cap,
            "candidates": len(queued),
            "queue_snapshot": snapshot_id,
            "stale_leases": stale_leases,
            "recovery_unstuck": unstuck,
            "goal_reopened": reopened,
            "auto_fix": auto_fix,
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
        yolo = self.store.get_config("yolo", "off").lower() in {"1", "true", "yes", "on"}
        disk_free = disk_free_gb(self.store.path.parent)
        if disk_free is not None and disk_free < 0.75:
            cap = 0
        else:
            cap = configured
            l1 = load1()
            mem_free = memory_free_percent()
            if mem_free is not None and mem_free < 12:
                cap = 0
            elif mem_free is not None and mem_free < 20:
                cap = min(cap, 1)
            elif mem_free is not None and mem_free < 30:
                cap = min(cap, 2)
            if l1 >= 10:
                cap = min(cap, 1)
            elif l1 >= 7:
                cap = min(cap, 2)
            elif l1 >= 5:
                cap = min(cap, 3)
        if yolo and cap == 0 and configured > 0:
            queued = self.store.row(
                """
                select count(*) c from queue q
                join chats c on c.id=q.chat_id
                where c.paused=0 and c.done=0
                """
            )
            if queued and int(queued["c"] or 0) > 0:
                cap = min(configured, 1)
        return cap

    def active_job_count(self) -> int:
        row = self.store.row("select count(*) c from jobs where status='running'")
        return int(row["c"] if row else 0)

    def dispatch_active_job_count(self) -> int:
        """Running jobs that occupy a worker slot for new dispatches."""
        placeholders = ",".join("?" * len(DISPATCH_SLOT_EXCLUDED_EVIDENCE))
        row = self.store.row(
            f"""
            select count(*) c from jobs
            where status='running'
              and coalesce(evidence_status, '') not in ({placeholders})
            """,
            tuple(DISPATCH_SLOT_EXCLUDED_EVIDENCE),
        )
        return int(row["c"] if row else 0)

    def candidates(self, limit: int) -> list[Row]:
        cap = DEFAULT_MAX_FAILURE_COUNT + 4
        rows = self.store.rows(
            """
            select c.*, q.position as queue_position
            from queue q join chats c on c.id=q.chat_id
            where c.paused=0 and c.done=0 and c.failure_count < ?
              and not exists (
                select 1 from chat_dependencies d
                join chats dep on dep.id=d.depends_on
                where d.chat_id=c.id and dep.done=0
              )
            order by q.position asc
            limit ?
            """,
            (cap, limit * 4),
        )
        if len(rows) < limit:
            healed = self.store.rows(
                """
                select c.*, q.position as queue_position
                from queue q join chats c on c.id=q.chat_id
                where c.paused=0 and c.done=1 and c.failure_count < ?
                  and (
                    exists (select 1 from goals g where g.chat_id=c.id and g.status='active')
                    or exists (
                      select 1 from project_priorities p
                      where p.target_chat_id=c.id and p.status='active'
                    )
                  )
                order by q.position asc
                limit ?
                """,
                (cap, limit),
            )
            seen = {str(row["id"]) for row in rows}
            for row in healed:
                if str(row["id"]) not in seen:
                    rows.append(row)
                    seen.add(str(row["id"]))
        ready: list[Row] = []
        for row in rows:
            if int(row["done"] or 0) and goals.should_reopen_done_chat(self.store, str(row["id"])):
                goals.reopen_chat_for_goal(self.store, str(row["id"]), reason="candidate_self_heal")
                refreshed = self.store.row(
                    """
                    select c.*, q.position as queue_position
                    from queue q join chats c on c.id=q.chat_id
                    where c.id=?
                    """,
                    (row["id"],),
                )
                if refreshed:
                    row = refreshed
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

        # Apply gw_candidate_priority_boost: in-memory re-sort by effective position.
        # boost subtracts from queue_position so higher boost = dispatched sooner.
        if any(recovery.chat_metadata(r).get("gw_candidate_priority_boost") for r in ready):
            def _eff_pos(r: Row) -> float:
                boost = float(recovery.chat_metadata(r).get("gw_candidate_priority_boost") or 0)
                return float(r["queue_position"]) - boost
            ready.sort(key=_eff_pos)

        return ready

    def has_active_job(self, chat_id: str) -> bool:
        row = self.store.row("select count(*) c from jobs where chat_id=? and status='running'", (chat_id,))
        return int(row["c"] if row else 0) > 0

    def has_active_lease(self, row: Row) -> bool:
        resource = self.runner.lease_resource_for(row)
        lease = self.store.row(
            """
            select l.*,j.status job_status,j.pid job_pid,j.evidence_status
            from leases l left join jobs j on j.id=l.job_id
            where l.resource=?
            """,
            (resource,),
        )
        if not lease:
            return False
        if lease["job_status"] == "running":
            evidence = str(lease["evidence_status"] or "")
            if evidence in DISPATCH_SLOT_EXCLUDED_EVIDENCE:
                return False
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
        from .util import json_loads, json_dumps
        _meta = json_loads(str(row["metadata_json"] or ""), {})
        gw_hint = str(_meta.get("gw_provider_hint") or "") if isinstance(_meta, dict) else ""
        blocked_raw = self.store.get_config("blocked_providers", "")
        blocked = {p.strip() for p in blocked_raw.split(",") if p.strip()}
        native_provider = str(row["provider"] or "")
        if recovery.should_use_fallback(row) and not self._direct_cursor_lane(row):
            plan = self.fallback_plan(row, prompt, job_dir)
        else:
            providers = provider_map()
            hint_provider = providers.get(gw_hint) if gw_hint else None
            if hint_provider and not recovery.provider_in_backoff(self.store, gw_hint):
                plan = hint_provider.continue_plan(chat, prompt, job_dir)
                if plan.supported:
                    _meta.pop("gw_provider_hint", None)
                    with self.store.connect() as con:
                        con.execute("update chats set metadata_json=? where id=?", (json_dumps(_meta), row["id"]))
                    self.store.event("dispatch_provider_hint_used", row["id"], provider=gw_hint)
                else:
                    plan = self.fallback_plan(row, prompt, job_dir)
            elif native_provider in blocked:
                self.store.event("dispatch_provider_blocked", row["id"], provider=native_provider)
                plan = self.fallback_plan(row, prompt, job_dir)
            else:
                provider = providers.get(native_provider)
                if not provider or recovery.provider_in_backoff(self.store, native_provider):
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
        data = {key: row[key] for key in row.keys()}
        plan = self.store.task_plan_summary(row["id"])
        if plan:
            data["task_plan"] = plan
        prior = self.store.last_job_turn_context(str(row["id"]))
        if prior:
            data["prior_job_context"] = prior
        from .util import json_loads

        meta = json_loads(str(data.get("metadata_json") or ""), {})
        if isinstance(meta, dict):
            prefix = meta.get("remediation_prompt_prefix")
            if prefix:
                data["prior_job_context"] = str(prefix) + (data.get("prior_job_context") or "")
                meta.pop("remediation_prompt_prefix", None)
                from .util import json_dumps

                with self.store.connect() as con:
                    con.execute(
                        "update chats set metadata_json=? where id=?",
                        (json_dumps(meta), row["id"]),
                    )

            briefing = meta.get("gw_briefing_notes")
            if briefing and isinstance(briefing, str):
                existing = str(data.get("prior_job_context") or "")
                data["prior_job_context"] = (
                    "[WATCHDOG BRIEFING]\n" + briefing + ("\n\n" + existing if existing else "")
                )

        # Inject workspace context: dirty git state + parallel session awareness
        cwd = str(data.get("cwd") or "")
        if cwd and Path(cwd).exists():
            workspace_ctx = self._workspace_context(cwd, str(data.get("id", "")))
            if workspace_ctx:
                existing = str(data.get("prior_job_context") or "")
                data["prior_job_context"] = workspace_ctx + ("\n\n" + existing if existing else "")

        class PromptRow(dict):
            def keys(self):
                return super().keys()

        has_enrichment = plan or prior or data.get("prior_job_context")
        return PromptRow(data) if has_enrichment else row

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
            reason=f"{row['provider']} stalled/unsupported; switching to Grok takeover.",
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

    def _workspace_context(self, cwd: str, current_chat_id: str) -> str:
        import subprocess
        parts = []
        # Git repo root
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=cwd, capture_output=True, text=True, timeout=5
            )
            repo_root = result.stdout.strip() if result.returncode == 0 else cwd
        except Exception:
            repo_root = cwd
        # Dirty working tree
        try:
            diff_stat = subprocess.check_output(
                ["git", "diff", "--stat", "HEAD"],
                cwd=repo_root, text=True, timeout=10, stderr=subprocess.DEVNULL
            ).strip()
            status = subprocess.check_output(
                ["git", "status", "--short"],
                cwd=repo_root, text=True, timeout=5, stderr=subprocess.DEVNULL
            ).strip()
            if status:
                parts.append(f"Workspace git status ({repo_root}):\n{status}")
            if diff_stat:
                parts.append(f"Uncommitted changes:\n{diff_stat}")
        except Exception:
            pass
        # Parallel sessions in same repo
        try:
            repo_prefix = (repo_root or "").rstrip("/") + "/"
            running = self.store.rows(
                """
                select c.id, c.title, t.subtasks_json, j.evidence_status
                from jobs j join chats c on c.id=j.chat_id
                left join task_plans t on t.chat_id=c.id and t.status='active'
                where j.status='running' and c.id != ?
                  and (c.cwd=? or c.cwd=? or c.cwd like ?)
                """,
                (current_chat_id, cwd, repo_root, repo_prefix + "%"),
            )
            if running:
                peer_lines = []
                for r in running:
                    title = str(r["title"] or "")[:60]
                    ev = str(r["evidence_status"] or "")
                    subtasks = ""
                    if r["subtasks_json"]:
                        try:
                            from .util import json_loads
                            st = json_loads(str(r["subtasks_json"]), [])
                            active = [s.get("title", "") for s in st if isinstance(s, dict) and s.get("status") not in ("completed", "done")]
                            if active:
                                subtasks = f" | working on: {'; '.join(active[:2])}"
                        except Exception:
                            pass
                    peer_lines.append(f"  - {title} [{ev}]{subtasks}")
                parts.append("PARALLEL SESSIONS in this workspace:\n" + "\n".join(peer_lines))
        except Exception:
            pass
        return "\n\n".join(parts) if parts else ""
