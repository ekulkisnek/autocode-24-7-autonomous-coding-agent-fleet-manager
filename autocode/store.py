from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import DB, ensure_dirs
from .audit import append_audit
from .models import Chat
from .util import iso_from_ts, json_dumps, json_loads, now_iso, now_ts, parse_ts, sha


class Store:
    def __init__(self, path: Path = DB):
        ensure_dirs()
        self.path = path
        self.audit_path = self.path.parent / "audit.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("pragma journal_mode=wal")
        con.execute("pragma busy_timeout=5000")
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def init(self) -> None:
        with self.connect() as con:
            con.executescript(
                """
                create table if not exists config (
                  key text primary key,
                  value text not null
                );
                create table if not exists chats (
                  id text primary key,
                  provider text not null,
                  source text not null,
                  provider_chat_id text not null,
                  alias text not null,
                  title text not null,
                  cwd text not null,
                  updated_at text not null,
                  latest_text text not null,
                  transcript_hash text not null,
                  continuation text not null,
                  coding_score integer not null default 0,
                  state text not null default 'new',
                  adopted integer not null default 0,
                  paused integer not null default 0,
                  done integer not null default 0,
                  objective text not null default '',
                  last_seen_at text not null,
                  last_drive_at text not null default '',
                  last_evidence_at text not null default '',
                  failure_count integer not null default 0,
                  metadata_json text not null default '{}'
                );
                create unique index if not exists idx_chats_provider_sid on chats(provider, source, provider_chat_id);
                create index if not exists idx_chats_updated on chats(updated_at desc);
                create index if not exists idx_chats_active on chats(done, paused, adopted, coding_score, updated_at desc);

                create table if not exists goals (
                  id text primary key,
                  chat_id text not null references chats(id) on delete cascade,
                  objective text not null,
                  status text not null,
                  source text not null,
                  created_at text not null,
                  updated_at text not null
                );
                create index if not exists idx_goals_status on goals(status, updated_at desc);

                create table if not exists project_priorities (
                  id text primary key,
                  query text not null,
                  objective text not null,
                  resource_path text not null default '',
                  target_chat_id text not null default '',
                  worker_lanes integer not null default 1,
                  priority integer not null default 100,
                  status text not null default 'active',
                  created_at text not null,
                  updated_at text not null
                );
                create index if not exists idx_project_priorities_status on project_priorities(status, priority desc, updated_at desc);

                create table if not exists jobs (
                  id text primary key,
                  chat_id text not null,
                  provider text not null,
                  status text not null,
                  pid integer,
                  cwd text not null,
                  cmd_json text not null,
                  prompt text not null,
                  stdout_path text not null,
                  stderr_path text not null,
                  created_at text not null,
                  updated_at text not null,
                  completed_at text not null default '',
                  evidence_status text not null default 'started',
                  evidence_reason text not null default '',
                  stdout_size integer not null default 0,
                  stderr_size integer not null default 0,
                  attempt integer not null default 1,
                  marker_kind text not null default '',
                  marker_json text not null default '',
                  worktree_path text not null default '',
                  queue_snapshot_id text not null default '',
                  token_input integer not null default 0,
                  token_output integer not null default 0,
                  cost_estimate real not null default 0
                );
                create index if not exists idx_jobs_status on jobs(status, updated_at desc);
                create index if not exists idx_jobs_chat on jobs(chat_id, created_at desc);

                create table if not exists leases (
                  resource text primary key,
                  chat_id text not null,
                  job_id text not null,
                  expires_at text not null
                );

                create table if not exists queue_snapshots (
                  id text primary key,
                  created_at text not null,
                  reason text not null,
                  capacity integer not null default 0,
                  active_jobs integer not null default 0,
                  items_json text not null
                );
                create index if not exists idx_queue_snapshots_created on queue_snapshots(created_at desc);

                create table if not exists queue_items (
                  snapshot_id text not null,
                  position integer not null,
                  chat_id text not null,
                  provider text not null,
                  priority_id text not null default '',
                  resource text not null default '',
                  state text not null,
                  objective text not null,
                  created_at text not null,
                  primary key(snapshot_id, position)
                );
                create index if not exists idx_queue_items_chat on queue_items(chat_id, created_at desc);

                create table if not exists task_plans (
                  id text primary key,
                  chat_id text not null,
                  goal text not null,
                  subtasks_json text not null,
                  status text not null default 'active',
                  created_at text not null,
                  updated_at text not null
                );
                create index if not exists idx_task_plans_chat on task_plans(chat_id, status, updated_at desc);

                create table if not exists provider_health (
                  provider text primary key,
                  failure_count integer not null default 0,
                  backoff_until text not null default '',
                  last_error text not null default ''
                );

                create table if not exists events (
                  id integer primary key autoincrement,
                  ts text not null,
                  kind text not null,
                  chat_id text,
                  job_id text,
                  details_json text not null default '{}'
                );
                """
            )
            con.execute("insert or ignore into config(key,value) values('yolo','on')")
            con.execute("insert or ignore into config(key,value) values('autoadopt','all_coding_chats')")
            con.execute("insert or ignore into config(key,value) values('max_active','5')")
            try:
                con.execute("alter table project_priorities add column resource_path text not null default ''")
            except sqlite3.OperationalError:
                pass
            try:
                con.execute("alter table project_priorities add column target_chat_id text not null default ''")
            except sqlite3.OperationalError:
                pass
            try:
                con.execute("alter table project_priorities add column worker_lanes integer not null default 1")
            except sqlite3.OperationalError:
                pass
            for ddl in (
                "alter table jobs add column marker_kind text not null default ''",
                "alter table jobs add column marker_json text not null default ''",
                "alter table jobs add column worktree_path text not null default ''",
                "alter table jobs add column queue_snapshot_id text not null default ''",
                "alter table jobs add column token_input integer not null default 0",
                "alter table jobs add column token_output integer not null default 0",
                "alter table jobs add column cost_estimate real not null default 0",
            ):
                try:
                    con.execute(ddl)
                except sqlite3.OperationalError:
                    pass

    def set_default(self, key: str, value: str) -> None:
        with self.connect() as con:
            con.execute("insert or ignore into config(key,value) values(?,?)", (key, value))

    def set_config(self, key: str, value: str) -> None:
        with self.connect() as con:
            con.execute("insert into config(key,value) values(?,?) on conflict(key) do update set value=excluded.value", (key, value))

    def get_config(self, key: str, default: str = "") -> str:
        with self.connect() as con:
            row = con.execute("select value from config where key=?", (key,)).fetchone()
            return str(row["value"]) if row else default

    def event(self, kind: str, chat_id: str | None = None, job_id: str | None = None, **details: Any) -> None:
        append_audit(kind, chat_id=chat_id, job_id=job_id, path=self.audit_path, **details)
        with self.connect() as con:
            con.execute(
                "insert into events(ts,kind,chat_id,job_id,details_json) values(?,?,?,?,?)",
                (now_iso(), kind, chat_id, job_id, json_dumps(details)),
            )

    def upsert_chat(self, chat: Chat, coding_score: int, state: str, objective: str) -> None:
        metadata = json_dumps(chat.metadata)
        alias = chat.alias or chat.id.split(":", 1)[-1]
        with self.connect() as con:
            old = con.execute("select * from chats where id=?", (chat.id,)).fetchone()
            priority = con.execute(
                """
                select * from project_priorities
                where status='active' and target_chat_id=?
                order by priority desc, updated_at desc
                limit 1
                """,
                (chat.id,),
            ).fetchone()
            adopted = int(old["adopted"]) if old else int(coding_score > 0)
            paused = int(old["paused"]) if old else 0
            done = int(old["done"]) if old else 0
            if priority:
                adopted = 1
                paused = 0
                done = 0
                coding_score = max(coding_score, 1)
                objective = str(priority["objective"] or objective)
                if state == "done":
                    state = "active"
            if state == "done":
                done = 1
            old_obj = str(old["objective"]) if old else ""
            objective = old_obj or objective
            old_state = str(old["state"]) if old else "new"
            if state in {"blocked", "reference"}:
                adopted = 0
                coding_score = 0
            if done:
                state = "done"
            elif paused:
                state = "paused"
            elif old_state in {"active", "needs_input", "stalled", "running"} and state not in {"done", "paused", "blocked", "reference"}:
                state = old_state
            con.execute(
                """
                insert into chats(id,provider,source,provider_chat_id,alias,title,cwd,updated_at,latest_text,transcript_hash,continuation,coding_score,state,adopted,paused,done,objective,last_seen_at,metadata_json)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                on conflict(id) do update set
                  alias=excluded.alias,title=excluded.title,cwd=excluded.cwd,updated_at=excluded.updated_at,
                  latest_text=excluded.latest_text,transcript_hash=excluded.transcript_hash,continuation=excluded.continuation,
                  coding_score=max(chats.coding_score, excluded.coding_score),state=excluded.state,
                  adopted=max(chats.adopted, excluded.adopted),objective=case when chats.objective='' then excluded.objective else chats.objective end,
                  last_seen_at=excluded.last_seen_at,metadata_json=excluded.metadata_json,
                  paused=excluded.paused,done=excluded.done
                """,
                (
                    chat.id, chat.provider, chat.source, chat.provider_chat_id, alias, chat.title, chat.cwd,
                    chat.updated_at, chat.latest_text, chat.transcript_hash, chat.continuation, coding_score,
                    state, adopted, paused, done, objective, now_iso(), metadata,
                ),
            )
            if state in {"blocked", "reference"}:
                con.execute(
                    "update chats set coding_score=?, adopted=0, state=? where id=?",
                    (coding_score, state, chat.id),
                )

    def rows(self, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.connect() as con:
            return list(con.execute(sql, args).fetchall())

    def row(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self.connect() as con:
            return con.execute(sql, args).fetchone()

    def active_chats(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.rows(
            """
            select * from chats
            where adopted=1 and paused=0 and done=0 and coding_score>0
            order by
              case when objective!='' then 0 else 1 end,
              case state when 'needs_input' then 0 when 'stalled' then 1 when 'active' then 2 else 3 end,
              updated_at desc
            limit ?
            """,
            (limit,),
        )

    def find_chat(self, query: str) -> sqlite3.Row | None:
        q = f"%{query.lower()}%"
        return self.row(
            """
            select * from chats
            where lower(id)=lower(?) or lower(alias)=lower(?) or lower(provider_chat_id)=lower(?)
               or lower(title) like ? or lower(alias) like ? or lower(cwd) like ?
            order by updated_at desc limit 1
            """,
            (query, query, query, q, q, q),
        )

    def set_goal(self, chat_id: str, objective: str, source: str = "user") -> None:
        gid = sha(chat_id + "\n" + objective)[:20]
        with self.connect() as con:
            con.execute("update chats set objective=?, adopted=1, paused=0, done=0, state='active' where id=?", (objective, chat_id))
            con.execute(
                "update goals set status='superseded',updated_at=? where chat_id=? and status='active' and id!=?",
                (now_iso(), chat_id, gid),
            )
            con.execute(
                """
                insert into goals(id,chat_id,objective,status,source,created_at,updated_at)
                values(?,?,?,?,?,?,?)
                on conflict(id) do update set objective=excluded.objective,status='active',updated_at=excluded.updated_at
                """,
                (gid, chat_id, objective, "active", source, now_iso(), now_iso()),
            )
        self.ensure_task_plan(chat_id, objective)

    def active_priority_for_chat(self, chat_id: str) -> sqlite3.Row | None:
        return self.row(
            """
            select * from project_priorities
            where status='active' and target_chat_id=?
            order by priority desc, updated_at desc
            limit 1
            """,
            (chat_id,),
        )

    def add_priority(
        self,
        query: str,
        objective: str,
        priority: int = 100,
        resource_path: str = "",
        target_chat_id: str = "",
        worker_lanes: int = 1,
    ) -> str:
        clean_target = target_chat_id.strip()
        existing = None
        if clean_target:
            existing = self.row(
                """
                select * from project_priorities
                where target_chat_id=?
                order by case when status='active' then 0 else 1 end, priority desc, updated_at desc
                limit 1
                """,
                (clean_target,),
            )
        pid = str(existing["id"]) if existing else sha(query.lower().strip() + "\n" + objective.strip())[:20]
        with self.connect() as con:
            con.execute(
                """
                insert into project_priorities(id,query,objective,resource_path,target_chat_id,worker_lanes,priority,status,created_at,updated_at)
                values(?,?,?,?,?,?,?,?,?,?)
                on conflict(id) do update set
                  query=excluded.query,objective=excluded.objective,priority=excluded.priority,
                  resource_path=case when excluded.resource_path!='' then excluded.resource_path else project_priorities.resource_path end,
                  target_chat_id=case when excluded.target_chat_id!='' then excluded.target_chat_id else project_priorities.target_chat_id end,
                  worker_lanes=max(1, excluded.worker_lanes),
                  status='active',updated_at=excluded.updated_at
                """,
                (
                    pid, query.strip(), objective.strip(), resource_path.strip(), clean_target,
                    max(1, int(worker_lanes or 1)), int(priority), "active", now_iso(), now_iso(),
                ),
            )
            if clean_target:
                con.execute(
                    "update chats set objective=?,adopted=1,paused=0,done=0,state='active' where id=?",
                    (objective.strip(), clean_target),
                )
                priority_gid = f"priority:{pid}"
                con.execute(
                    "update goals set status='superseded',updated_at=? where chat_id=? and status='active' and id!=?",
                    (now_iso(), clean_target, priority_gid),
                )
                con.execute(
                    """
                    insert into goals(id,chat_id,objective,status,source,created_at,updated_at)
                    values(?,?,?,?,?,?,?)
                    on conflict(id) do update set objective=excluded.objective,status='active',updated_at=excluded.updated_at
                    """,
                    (priority_gid, clean_target, objective.strip(), "active", "priority", now_iso(), now_iso()),
                )
        self.event(
            "priority_added",
            **{
                "priority_id": pid,
                "query": query,
                "priority": priority,
                "resource_path": resource_path,
                "target_chat_id": target_chat_id,
                "worker_lanes": worker_lanes,
            },
        )
        if clean_target:
            self.ensure_task_plan(clean_target, objective.strip())
        return pid

    def remove_priority(self, query_or_id: str) -> int:
        q = f"%{query_or_id.lower()}%"
        with self.connect() as con:
            cur = con.execute(
                """
                update project_priorities set status='paused',updated_at=?
                where status='active' and (id=? or lower(query) like ? or lower(objective) like ?)
                """,
                (now_iso(), query_or_id, q, q),
            )
            return cur.rowcount

    def find_priority(self, query_or_id: str) -> sqlite3.Row | None:
        q = f"%{query_or_id.lower()}%"
        return self.row(
            """
            select * from project_priorities
            where status='active' and (id=? or lower(query) like ? or lower(objective) like ?)
            order by priority desc, updated_at desc
            limit 1
            """,
            (query_or_id, q, q),
        )

    def pause_chat(self, chat_id: str) -> None:
        with self.connect() as con:
            con.execute("update chats set paused=1,state='paused' where id=?", (chat_id,))
            con.execute("update goals set status='paused',updated_at=? where chat_id=? and status='active'", (now_iso(), chat_id))
            con.execute("update task_plans set status='paused',updated_at=? where chat_id=? and status in ('active','needs_decomposition')", (now_iso(), chat_id))
        self.event("chat_paused", chat_id)

    def done_chat(self, chat_id: str) -> None:
        with self.connect() as con:
            con.execute("update chats set done=1,state='done' where id=?", (chat_id,))
            con.execute("update goals set status='complete',updated_at=? where chat_id=? and status!='complete'", (now_iso(), chat_id))
            con.execute("update project_priorities set status='complete',updated_at=? where target_chat_id=? and status='active'", (now_iso(), chat_id))
            con.execute("update task_plans set status='complete',updated_at=? where chat_id=? and status in ('active','needs_decomposition')", (now_iso(), chat_id))
        self.event("chat_done", chat_id)

    def ensure_task_plan(self, chat_id: str, goal: str) -> str:
        existing = self.row(
            "select * from task_plans where chat_id=? and goal=? and status in ('active','needs_decomposition') order by updated_at desc limit 1",
            (chat_id, goal),
        )
        if existing:
            return str(existing["id"])
        pid = "plan-" + sha(chat_id + "\n" + goal)[:16]
        subtasks: list[dict[str, str]] = []
        with self.connect() as con:
            con.execute(
                """
                insert into task_plans(id,chat_id,goal,subtasks_json,status,created_at,updated_at)
                values(?,?,?,?,?,?,?)
                on conflict(id) do update set goal=excluded.goal,status='needs_decomposition',updated_at=excluded.updated_at
                """,
                (pid, chat_id, goal, json_dumps(subtasks), "needs_decomposition", now_iso(), now_iso()),
            )
        self.event("task_plan_created", chat_id, plan_id=pid, subtask_count=len(subtasks))
        return pid

    def task_plan_summary(self, chat_id: str) -> str:
        row = self.row(
            "select * from task_plans where chat_id=? and status in ('active','needs_decomposition') order by updated_at desc limit 1",
            (chat_id,),
        )
        if not row:
            return ""
        subtasks = json_loads(row["subtasks_json"], [])
        if not isinstance(subtasks, list):
            return ""
        if not subtasks:
            return "No decomposition captured yet. First response should include FLEET_PLAN JSON with 3-5 subtasks, then proceed with the first useful action."
        lines = []
        for item in subtasks[:5]:
            if isinstance(item, dict):
                lines.append(f"- {item.get('id')}: {item.get('title')} [{item.get('status','pending')}]")
        return "\n".join(lines)

    def record_queue_snapshot(self, rows: list[sqlite3.Row], *, reason: str, capacity: int, active_jobs: int, resource_for) -> str:
        sid = "queue-" + uuid.uuid4().hex[:16]
        items = []
        with self.connect() as con:
            con.execute(
                "insert into queue_snapshots(id,created_at,reason,capacity,active_jobs,items_json) values(?,?,?,?,?,?)",
                (sid, now_iso(), reason, int(capacity), int(active_jobs), json_dumps([])),
            )
            for idx, row in enumerate(rows):
                priority_id = ""
                try:
                    if "priority_id" in row.keys():
                        priority_id = str(row["priority_id"] or "")
                except Exception:
                    pass
                resource = resource_for(row)
                item = {
                    "position": idx,
                    "chat_id": row["id"],
                    "provider": row["provider"],
                    "priority_id": priority_id,
                    "resource": resource,
                    "state": row["state"],
                    "objective": row["objective"] or row["title"] or "",
                }
                items.append(item)
                con.execute(
                    """
                    insert into queue_items(snapshot_id,position,chat_id,provider,priority_id,resource,state,objective,created_at)
                    values(?,?,?,?,?,?,?,?,?)
                    """,
                    (sid, idx, item["chat_id"], item["provider"], item["priority_id"], resource, item["state"], item["objective"], now_iso()),
                )
            con.execute("update queue_snapshots set items_json=? where id=?", (json_dumps(items), sid))
        self.event("queue_snapshot", snapshot_id=sid, reason=reason, items=len(items), capacity=capacity, active_jobs=active_jobs)
        return sid
