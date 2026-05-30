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
        con.execute("pragma busy_timeout=15000")
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

                create table if not exists queue (
                  chat_id text primary key,
                  position real not null,
                  created_at text not null
                );
                create index if not exists idx_queue_position on queue(position asc);

                create table if not exists queue_finished (
                  chat_id text primary key,
                  position real not null default 0,
                  provider text not null default '',
                  alias text not null default '',
                  objective text not null default '',
                  archived_at text not null,
                  reason text not null default ''
                );
                create index if not exists idx_queue_finished_archived on queue_finished(archived_at desc);

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

                create table if not exists spawn_intents (
                  id text primary key,
                  provider text not null,
                  cwd text not null,
                  objective text not null,
                  priority integer not null default 50,
                  parent_chat_id text not null default '',
                  status text not null default 'pending',
                  chat_id text not null default '',
                  created_at text not null,
                  updated_at text not null
                );
                create index if not exists idx_spawn_intents_status on spawn_intents(status, created_at);

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

                create table if not exists chat_dependencies (
                  chat_id text not null references chats(id) on delete cascade,
                  depends_on text not null references chats(id) on delete cascade,
                  created_at text not null,
                  primary key (chat_id, depends_on)
                );
                create index if not exists idx_chat_deps_chat on chat_dependencies(chat_id);
                create index if not exists idx_chat_deps_on on chat_dependencies(depends_on);

                create table if not exists remote_workers (
                  id text primary key,
                  host text not null,
                  ssh_user text not null,
                  provider_types text not null default 'grok',
                  weight_capacity real not null default 4.0,
                  default_cwd text not null default '~',
                  ssh_key_path text not null default '',
                  enabled integer not null default 1,
                  last_seen_at text not null default '',
                  notes text not null default ''
                );
                """
            )
            con.execute("insert or ignore into config(key,value) values('yolo','on')")
            con.execute("insert or ignore into config(key,value) values('autoadopt','manual')")
            con.execute("insert or ignore into config(key,value) values('max_active','5')")
            con.execute("insert or ignore into config(key,value) values('preserve_jobs_on_shutdown','on')")
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
                "alter table jobs add column worker_id text not null default ''",
                "alter table remote_workers add column remote_shell text not null default ''",
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
            in_queue = con.execute("select 1 from queue where chat_id=?", (chat.id,)).fetchone()
            adopted = int(old["adopted"]) if old else (1 if in_queue else 0)
            paused = int(old["paused"]) if old else 0
            done = int(old["done"]) if old else 0
            if in_queue:
                adopted = 1
                paused = 0
                if state == "done":
                    state = "active"
            if state == "done":
                done = 1
            old_obj = str(old["objective"]) if old else ""
            objective = old_obj or objective
            old_state = str(old["state"]) if old else "new"
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

    def rows(self, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.connect() as con:
            return list(con.execute(sql, args).fetchall())

    def row(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self.connect() as con:
            return con.execute(sql, args).fetchone()

    def active_chats(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.rows(
            """
            select c.*, q.position as queue_position
            from queue q join chats c on c.id=q.chat_id
            where c.paused=0 and c.done=0
            order by q.position asc
            limit ?
            """,
            (limit,),
        )

    def queue_add(self, chat_id: str, position: float | None = None) -> None:
        if position is None:
            row = self.row("select max(position) m from queue")
            position = float((row["m"] or 0) + 1) if row else 1.0
        with self.connect() as con:
            con.execute(
                "insert into queue(chat_id, position, created_at) values(?,?,?) on conflict(chat_id) do update set position=excluded.position",
                (chat_id, position, now_iso()),
            )
            con.execute(
                "update chats set adopted=1, paused=0, state='active' where id=? and done=0",
                (chat_id,),
            )
        self.event("queue_add", chat_id, position=position)

    def record_job_turn_context(
        self,
        chat_id: str,
        *,
        job_id: str,
        evidence_status: str,
        summary: str,
        reason: str = "",
    ) -> None:
        """Persist the last finished job summary for the next autonomous drive turn."""
        from .recovery import chat_metadata

        row = self.row("select metadata_json from chats where id=?", (chat_id,))
        meta = chat_metadata(row)
        meta["last_job_id"] = job_id
        meta["last_job_evidence"] = evidence_status
        meta["last_job_reason"] = (reason or "")[:500]
        meta["last_job_summary"] = summary[:6000]
        meta["last_job_at"] = now_iso()
        with self.connect() as con:
            con.execute("update chats set metadata_json=? where id=?", (json_dumps(meta), chat_id))

    def last_job_turn_context(self, chat_id: str) -> str:
        from .recovery import chat_metadata

        row = self.row("select metadata_json from chats where id=?", (chat_id,))
        meta = chat_metadata(row)
        summary = str(meta.get("last_job_summary") or "").strip()
        if not summary:
            return ""
        job_id = str(meta.get("last_job_id") or "")
        evidence = str(meta.get("last_job_evidence") or "")
        reason = str(meta.get("last_job_reason") or "")
        when = str(meta.get("last_job_at") or "")
        header = f"Prior AutoCode job {job_id} ({evidence or 'finished'}"
        if when:
            header += f" at {when}"
        header += ")"
        if reason:
            header += f": {reason}"
        return f"{header}:\n{summary}"

    def queue_remove(self, chat_id: str) -> bool:
        with self.connect() as con:
            cur = con.execute("delete from queue where chat_id=?", (chat_id,))
            deleted = cur.rowcount > 0
        if deleted:
            self.event("queue_remove", chat_id)
        return deleted

    def queue_list(self, *, include_done: bool = False) -> list[sqlite3.Row]:
        done_filter = "" if include_done else " and c.done=0"
        return self.rows(
            f"""
            select q.position, q.chat_id, q.created_at,
                   c.title, c.alias, c.provider, c.state, c.updated_at, c.objective, c.cwd, c.done
            from queue q join chats c on c.id=q.chat_id
            where 1=1{done_filter}
            order by q.position asc
            """
        )

    def queue_finished_list(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.rows(
            """
            select chat_id, position, provider, alias, objective, archived_at, reason
            from queue_finished
            order by archived_at desc
            limit ?
            """,
            (limit,),
        )

    def queue_archive(self, chat_id: str, *, reason: str = "done") -> bool:
        row = self.row(
            """
            select q.position, q.chat_id, c.provider, c.alias, c.objective
            from queue q join chats c on c.id=q.chat_id
            where q.chat_id=?
            """,
            (chat_id,),
        )
        if not row:
            return False
        with self.connect() as con:
            con.execute(
                """
                insert into queue_finished(chat_id,position,provider,alias,objective,archived_at,reason)
                values(?,?,?,?,?,?,?)
                on conflict(chat_id) do update set
                  position=excluded.position,
                  provider=excluded.provider,
                  alias=excluded.alias,
                  objective=excluded.objective,
                  archived_at=excluded.archived_at,
                  reason=excluded.reason
                """,
                (
                    chat_id,
                    float(row["position"]),
                    str(row["provider"] or ""),
                    str(row["alias"] or ""),
                    str(row["objective"] or ""),
                    now_iso(),
                    reason,
                ),
            )
            con.execute("delete from queue where chat_id=?", (chat_id,))
        self.event("queue_archive", chat_id, reason=reason)
        return True

    def queue_archive_done(self) -> list[str]:
        rows = self.rows(
            """
            select q.chat_id
            from queue q join chats c on c.id=q.chat_id
            where c.done=1
              and not exists (
                select 1 from goals g where g.chat_id=c.id and g.status='active'
              )
              and not exists (
                select 1 from project_priorities p
                where p.target_chat_id=c.id and p.status='active'
              )
            """
        )
        archived: list[str] = []
        for row in rows:
            if self.queue_archive(row["chat_id"], reason="done"):
                archived.append(str(row["chat_id"]))
        return archived

    def queue_reopen(self, chat_id: str) -> bool:
        """Move a chat from queue_finished back onto the active queue."""
        row = self.row("select position from queue_finished where chat_id=?", (chat_id,))
        if not row:
            if self.row("select 1 from queue where chat_id=?", (chat_id,)):
                return False
            self.queue_add(chat_id)
            return True
        position = float(row["position"] or 0) or None
        with self.connect() as con:
            con.execute("delete from queue_finished where chat_id=?", (chat_id,))
        self.queue_add(chat_id, position)
        self.event("queue_reopen", chat_id)
        return True

    def queue_move(self, chat_id: str, new_position: float) -> bool:
        with self.connect() as con:
            cur = con.execute("update queue set position=? where chat_id=?", (new_position, chat_id))
            updated = cur.rowcount > 0
        if updated:
            self.event("queue_move", chat_id, position=new_position)
        return updated

    def queue_bump_front(self, chat_id: str) -> bool:
        row = self.row("select min(position) m from queue")
        if not row:
            return False
        front = float(row["m"] or 1) - 1.0
        return self.queue_move(chat_id, front)

    def add_dependency(self, chat_id: str, depends_on: str) -> bool:
        """Block chat_id from dispatching until depends_on is done."""
        if chat_id == depends_on:
            return False
        with self.connect() as con:
            con.execute(
                "insert or ignore into chat_dependencies(chat_id,depends_on,created_at) values(?,?,?)",
                (chat_id, depends_on, now_iso()),
            )
        self.event("dependency_added", chat_id, depends_on=depends_on)
        return True

    def remove_dependency(self, chat_id: str, depends_on: str) -> bool:
        with self.connect() as con:
            con.execute(
                "delete from chat_dependencies where chat_id=? and depends_on=?",
                (chat_id, depends_on),
            )
        return True

    def get_dependencies(self, chat_id: str) -> list[sqlite3.Row]:
        return self.rows(
            """
            select d.depends_on, c.title, c.done, c.state
            from chat_dependencies d join chats c on c.id=d.depends_on
            where d.chat_id=?
            """,
            (chat_id,),
        )

    def record_provider_failure(self, provider: str, error: str) -> None:
        if not provider:
            return
        from .recovery import backoff_seconds

        row = self.row("select * from provider_health where provider=?", (provider,))
        count = int(row["failure_count"] or 0) + 1 if row else 1
        delay = backoff_seconds("provider_error", count)
        until = iso_from_ts(now_ts() + delay)
        with self.connect() as con:
            con.execute(
                """
                insert into provider_health(provider,failure_count,backoff_until,last_error)
                values(?,?,?,?)
                on conflict(provider) do update set
                  failure_count=excluded.failure_count,
                  backoff_until=excluded.backoff_until,
                  last_error=excluded.last_error
                """,
                (provider, count, until, error[:500]),
            )
        self.event("provider_backoff", provider=provider, failure_count=count, backoff_until=until)

    def clear_provider_health(self, provider: str) -> None:
        with self.connect() as con:
            con.execute(
                "update provider_health set failure_count=0,backoff_until='',last_error='' where provider=?",
                (provider,),
            )

    def find_chat(self, query: str) -> sqlite3.Row | None:
        q = query.strip()
        if not q:
            return None
        exact = self.row(
            """
            select * from chats
            where lower(id)=lower(?)
               or lower(provider_chat_id)=lower(?)
               or lower(alias)=lower(?)
            limit 1
            """,
            (q, q, q),
        )
        if exact:
            return exact
        like = f"%{q.lower()}%"
        return self.row(
            """
            select * from chats
            where lower(title) like ? or lower(alias) like ? or lower(cwd) like ?
            order by
              case
                when lower(alias) like ? then 0
                when lower(title) like ? then 1
                else 2
              end,
              updated_at desc
            limit 1
            """,
            (like, like, like, f"{q.lower()}%", f"{q.lower()}%"),
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
        self.queue_archive(chat_id, reason="done")
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

    def add_spawn_intent(
        self,
        provider: str,
        cwd: str,
        objective: str,
        *,
        priority: int = 50,
        parent_chat_id: str = "",
    ) -> str:
        sid = "spawn-" + sha(f"{provider}:{cwd}:{objective}")[:16]
        with self.connect() as con:
            con.execute(
                """
                insert into spawn_intents(id,provider,cwd,objective,priority,parent_chat_id,status,created_at,updated_at)
                values(?,?,?,?,?,?,'pending',?,?)
                on conflict(id) do update set status='pending',updated_at=excluded.updated_at
                """,
                (sid, provider, cwd, objective, priority, parent_chat_id, now_iso(), now_iso()),
            )
        self.event("spawn_intent_added", parent_chat_id or None, intent_id=sid, provider=provider, cwd=cwd)
        return sid

    def pop_spawn_intents(self, limit: int = 3) -> list[sqlite3.Row]:
        rows = self.rows(
            "select * from spawn_intents where status='pending' order by priority asc, created_at asc limit ?",
            (limit,),
        )
        if rows:
            ids = [r["id"] for r in rows]
            with self.connect() as con:
                con.executemany("update spawn_intents set status='spawning',updated_at=? where id=?",
                                [(now_iso(), i) for i in ids])
        return rows

    def finish_spawn_intent(self, intent_id: str, chat_id: str, *, failed: bool = False) -> None:
        status = "failed" if failed else "done"
        with self.connect() as con:
            con.execute(
                "update spawn_intents set status=?,chat_id=?,updated_at=? where id=?",
                (status, chat_id, now_iso(), intent_id),
            )

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
