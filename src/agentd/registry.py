from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .capture_proxy import ensure_model_http_exchanges_schema
from .context import split_skill_names
from .models import AgentSession, FeishuOutboxItem, RunEvent, RunRecord, SpawnRequest, TitleRequest


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool | None:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class Registry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute('pragma busy_timeout = 5000')
        return conn

    def _init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                create table if not exists sessions (
                    id integer primary key autoincrement,
                    kind text not null,
                    chat_id text not null,
                    thread_id text,
                    root_message_id text,
                    codex_thread_id text,
                    cwd text not null,
                    context_profile text not null default '',
                    skills text not null default '',
                    created_at integer not null,
                    updated_at integer not null,
                    unique(chat_id, thread_id)
                );

                create unique index if not exists sessions_main_chat_idx
                on sessions(chat_id)
                where thread_id is null;

                create table if not exists dedup (
                    message_id text primary key,
                    created_at integer not null
                );

                create table if not exists spawn_requests (
                    id integer primary key autoincrement,
                    parent_session_id integer not null,
                    parent_status_message_id text not null,
                    parent_source_message_id text not null,
                    chat_id text not null,
                    cwd text not null,
                    title text not null,
                    prompt text not null,
                    context_profile text not null default '',
                    skills text not null default '',
                    state text not null,
                    error text,
                    created_at integer not null,
                    updated_at integer not null
                );

                create table if not exists title_requests (
                    id integer primary key autoincrement,
                    session_id integer not null,
                    title text not null,
                    state text not null,
                    error text,
                    created_at integer not null,
                    updated_at integer not null
                );

                create table if not exists schedule_runs (
                    job_id text primary key,
                    last_run_key text not null,
                    updated_at integer not null
                );

                create table if not exists runs (
                    id integer primary key autoincrement,
                    session_id integer not null,
                    source_message_id text not null,
                    prompt text not null,
                    state text not null,
                    status_phase text not null,
                    status text not null,
                    status_message_id text not null default '',
                    codex_thread_id text not null default '',
                    turn_id text not null default '',
                    subject text not null default '',
                    display_title text not null default '',
                    host text not null default '',
                    status_reply_in_thread integer not null default 0,
                    context_profile text not null default '',
                    skills text not null default '',
                    hide_early_iterations integer not null default 1,
                    show_tool_details integer not null default 0,
                    truncate_content integer not null default 1,
                    final_message_text text not null default '',
                    final_message_sent_at integer,
                    error text not null default '',
                    handoff_child_session_id integer,
                    started_at integer not null,
                    finished_at integer,
                    heartbeat_at integer not null,
                    lease_until integer not null default 0,
                    created_at integer not null,
                    updated_at integer not null
                );

                create index if not exists runs_session_state_idx
                on runs(session_id, state, updated_at);

                create index if not exists runs_status_message_idx
                on runs(status_message_id)
                where status_message_id != '';

                create table if not exists run_events (
                    id integer primary key autoincrement,
                    run_id integer not null,
                    event_type text not null,
                    payload_json text not null default '{}',
                    created_at integer not null
                );

                create index if not exists run_events_run_idx
                on run_events(run_id, id);

                create table if not exists card_projections (
                    run_id integer primary key,
                    dirty integer not null default 1,
                    last_render_hash text not null default '',
                    last_rendered_at integer,
                    remote_message_id text not null default '',
                    error text not null default '',
                    updated_at integer not null
                );

                create index if not exists card_projections_remote_message_idx
                on card_projections(remote_message_id)
                where remote_message_id != '';

                create table if not exists feishu_outbox (
                    id integer primary key autoincrement,
                    run_id integer,
                    kind text not null,
                    dedupe_key text not null unique,
                    payload_json text not null default '{}',
                    state text not null,
                    attempts integer not null default 0,
                    last_error text not null default '',
                    created_at integer not null,
                    updated_at integer not null,
                    sent_at integer
                );

                create index if not exists feishu_outbox_state_idx
                on feishu_outbox(state, updated_at);
                """
            )
            ensure_model_http_exchanges_schema(conn)
            self._add_column_if_missing(conn, 'sessions', 'root_message_id', 'text')
            self._add_column_if_missing(conn, 'sessions', 'context_profile', "text not null default ''")
            self._add_column_if_missing(conn, 'sessions', 'skills', "text not null default ''")
            self._add_column_if_missing(conn, 'spawn_requests', 'context_profile', "text not null default ''")
            self._add_column_if_missing(conn, 'spawn_requests', 'skills', "text not null default ''")

    @staticmethod
    def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
        columns = {str(row['name']) for row in conn.execute(f'pragma table_info({table})')}
        if column not in columns:
            conn.execute(f'alter table {table} add column {column} {column_type}')

    def is_duplicate(self, message_id: str, ttl_seconds: int = 86400) -> bool:
        if not message_id:
            return False
        now = int(time.time())
        cutoff = now - ttl_seconds
        with self.connect() as conn:
            conn.execute('delete from dedup where created_at < ?', (cutoff,))
            row = conn.execute('select 1 from dedup where message_id = ?', (message_id,)).fetchone()
            if row:
                return True
            conn.execute('insert into dedup(message_id, created_at) values(?, ?)', (message_id, now))
            return False

    def get_main_session(self, chat_id: str, cwd: str) -> AgentSession:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                insert or ignore into sessions(kind, chat_id, thread_id, cwd, created_at, updated_at)
                values('main', ?, null, ?, ?, ?)
                """,
                (chat_id, cwd, now, now),
            )
            row = conn.execute(
                'select * from sessions where chat_id = ? and thread_id is null',
                (chat_id,),
            ).fetchone()
        return self._session_from_row(row)

    def get_thread_session(self, chat_id: str, thread_id: str) -> AgentSession | None:
        with self.connect() as conn:
            row = conn.execute(
                'select * from sessions where chat_id = ? and thread_id = ?',
                (chat_id, thread_id),
            ).fetchone()
        return self._session_from_row(row) if row else None

    def bind_child_session(
        self,
        chat_id: str,
        thread_id: str,
        cwd: str,
        *,
        root_message_id: str = '',
        parent_id: int | None = None,
        context_profile: str = '',
        skills: tuple[str, ...] = (),
    ) -> AgentSession:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                insert or ignore into sessions(kind, chat_id, thread_id, root_message_id, cwd, context_profile, skills, created_at, updated_at)
                values('child', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (chat_id, thread_id, root_message_id, cwd, context_profile, ','.join(skills), now, now),
            )
            conn.execute(
                """
                update sessions
                set root_message_id = coalesce(nullif(?, ''), root_message_id),
                    cwd = ?,
                    context_profile = coalesce(nullif(?, ''), context_profile),
                    skills = coalesce(nullif(?, ''), skills),
                    updated_at = ?
                where chat_id = ? and thread_id = ?
                """,
                (root_message_id, cwd, context_profile, ','.join(skills), now, chat_id, thread_id),
            )
            row = conn.execute(
                'select * from sessions where chat_id = ? and thread_id = ?',
                (chat_id, thread_id),
            ).fetchone()
        return self._session_from_row(row)

    def get_schedule_session(
        self, chat_id: str, job_id: str, cwd: str, *, context_profile: str = '', skills: tuple[str, ...] = ()
    ) -> AgentSession:
        now = int(time.time())
        thread_id = f'schedule:{job_id}'
        with self.connect() as conn:
            conn.execute(
                """
                insert or ignore into sessions(kind, chat_id, thread_id, cwd, context_profile, skills, created_at, updated_at)
                values('schedule', ?, ?, ?, ?, ?, ?, ?)
                """,
                (chat_id, thread_id, cwd, context_profile, ','.join(skills), now, now),
            )
            conn.execute(
                """
                update sessions
                set cwd = ?,
                    context_profile = ?,
                    skills = ?,
                    updated_at = ?
                where chat_id = ? and thread_id = ?
                """,
                (cwd, context_profile, ','.join(skills), now, chat_id, thread_id),
            )
            row = conn.execute(
                'select * from sessions where chat_id = ? and thread_id = ?',
                (chat_id, thread_id),
            ).fetchone()
        return self._session_from_row(row)

    def update_codex_thread(self, session_id: int, codex_thread_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                'update sessions set codex_thread_id = ?, updated_at = ? where id = ?',
                (codex_thread_id, int(time.time()), session_id),
            )

    def get_session(self, session_id: int) -> AgentSession | None:
        with self.connect() as conn:
            row = conn.execute('select * from sessions where id = ?', (session_id,)).fetchone()
        return self._session_from_row(row) if row else None

    def create_run(
        self,
        *,
        session_id: int,
        source_message_id: str,
        prompt: str,
        host: str,
        subject: str,
        display_title: str,
        context_profile: str = '',
        skills: tuple[str, ...] = (),
        status: str = '启动 Codex',
        status_phase: str = 'running',
        state: str = 'running',
        status_message_id: str = '',
        status_reply_in_thread: bool = False,
        lease_seconds: int = 30,
    ) -> RunRecord:
        now = int(time.time())
        with self.connect() as conn:
            cursor = conn.execute(
                """
                insert into runs(
                    session_id,
                    source_message_id,
                    prompt,
                    state,
                    status_phase,
                    status,
                    status_message_id,
                    subject,
                    display_title,
                    host,
                    status_reply_in_thread,
                    context_profile,
                    skills,
                    started_at,
                    heartbeat_at,
                    lease_until,
                    created_at,
                    updated_at
                )
                values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    source_message_id,
                    prompt,
                    state,
                    status_phase,
                    status,
                    status_message_id,
                    subject,
                    display_title,
                    host,
                    int(status_reply_in_thread),
                    context_profile,
                    ','.join(skills),
                    now,
                    now,
                    now + lease_seconds,
                    now,
                    now,
                ),
            )
            run_id = int(cursor.lastrowid)
            conn.execute(
                """
                insert or ignore into card_projections(run_id, dirty, remote_message_id, updated_at)
                values(?, 1, ?, ?)
                """,
                (run_id, status_message_id, now),
            )
            row = conn.execute('select * from runs where id = ?', (run_id,)).fetchone()
        return self._run_from_row(row)

    def get_run(self, run_id: int) -> RunRecord | None:
        with self.connect() as conn:
            row = conn.execute('select * from runs where id = ?', (run_id,)).fetchone()
        return self._run_from_row(row) if row else None

    def get_active_run_for_session(self, session_id: int) -> RunRecord | None:
        active_states = ('queued', 'starting', 'running', 'cancel_requested', 'recovering')
        placeholders = ','.join('?' for _ in active_states)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                select * from runs
                where session_id = ? and state in ({placeholders})
                order by id desc
                limit 1
                """,
                (session_id, *active_states),
            ).fetchone()
        return self._run_from_row(row) if row else None

    def active_run_count(self) -> int:
        active_states = ('queued', 'starting', 'running', 'cancel_requested', 'recovering')
        placeholders = ','.join('?' for _ in active_states)
        with self.connect() as conn:
            row = conn.execute(
                f'select count(*) as count from runs where state in ({placeholders})',
                active_states,
            ).fetchone()
        return int(row['count']) if row else 0

    def get_run_for_status_card(self, message_id: str) -> RunRecord | None:
        if not message_id:
            return None
        with self.connect() as conn:
            row = conn.execute(
                """
                select r.*
                from runs r
                left join card_projections c on c.run_id = r.id
                where r.status_message_id = ? or c.remote_message_id = ?
                order by r.id desc
                limit 1
                """,
                (message_id, message_id),
            ).fetchone()
        return self._run_from_row(row) if row else None

    def list_stale_active_runs(self, *, now: int | None = None, limit: int = 20) -> list[RunRecord]:
        active_states = ('starting', 'running', 'cancel_requested', 'recovering')
        placeholders = ','.join('?' for _ in active_states)
        cutoff = int(time.time()) if now is None else now
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select * from runs
                where state in ({placeholders}) and lease_until < ?
                order by lease_until
                limit ?
                """,
                (*active_states, cutoff, limit),
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def update_run(self, run_id: int, **fields: object) -> None:
        allowed = {
            'state',
            'status_phase',
            'status',
            'status_message_id',
            'codex_thread_id',
            'turn_id',
            'subject',
            'display_title',
            'host',
            'status_reply_in_thread',
            'context_profile',
            'skills',
            'hide_early_iterations',
            'show_tool_details',
            'truncate_content',
            'final_message_text',
            'final_message_sent_at',
            'error',
            'handoff_child_session_id',
            'finished_at',
            'heartbeat_at',
            'lease_until',
        }
        if not fields:
            return
        assignments: list[str] = []
        values: list[object] = []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f'unsupported runs column: {key}')
            assignments.append(f'{key} = ?')
            values.append(self._db_value(value))
        assignments.append('updated_at = ?')
        values.append(int(time.time()))
        values.append(run_id)
        with self.connect() as conn:
            conn.execute(f'update runs set {", ".join(assignments)} where id = ?', values)
            if 'status_message_id' in fields:
                conn.execute(
                    """
                    insert into card_projections(run_id, remote_message_id, updated_at)
                    values(?, ?, ?)
                    on conflict(run_id) do update set
                        remote_message_id = excluded.remote_message_id,
                        updated_at = excluded.updated_at
                    """,
                    (run_id, str(fields.get('status_message_id') or ''), int(time.time())),
                )

    def touch_run_lease(self, run_id: int, *, lease_seconds: int = 30) -> None:
        now = int(time.time())
        self.update_run(run_id, heartbeat_at=now, lease_until=now + lease_seconds)

    def append_run_event(self, run_id: int, event_type: str, payload: dict[str, Any] | None = None) -> int:
        now = int(time.time())
        payload_json = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                insert into run_events(run_id, event_type, payload_json, created_at)
                values(?, ?, ?, ?)
                """,
                (run_id, event_type, payload_json, now),
            )
            return int(cursor.lastrowid)

    def list_run_events(self, run_id: int) -> list[RunEvent]:
        with self.connect() as conn:
            rows = conn.execute(
                'select * from run_events where run_id = ? order by id',
                (run_id,),
            ).fetchall()
        return [self._run_event_from_row(row) for row in rows]

    def mark_card_dirty(self, run_id: int) -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                insert into card_projections(run_id, dirty, updated_at)
                values(?, 1, ?)
                on conflict(run_id) do update set
                    dirty = 1,
                    updated_at = excluded.updated_at
                """,
                (run_id, now),
            )

    def list_dirty_card_runs(self, limit: int = 20) -> list[RunRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select r.*
                from card_projections c
                join runs r on r.id = c.run_id
                where c.dirty = 1
                order by c.updated_at, c.run_id
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def get_card_projection(self, run_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute('select * from card_projections where run_id = ?', (run_id,)).fetchone()

    def mark_card_enqueued(self, run_id: int, *, render_hash: str) -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                insert into card_projections(run_id, dirty, last_render_hash, last_rendered_at, updated_at)
                values(?, 0, ?, ?, ?)
                on conflict(run_id) do update set
                    dirty = 0,
                    last_render_hash = excluded.last_render_hash,
                    last_rendered_at = excluded.last_rendered_at,
                    updated_at = excluded.updated_at
                """,
                (run_id, render_hash, now, now),
            )

    def mark_card_sent(self, run_id: int, *, remote_message_id: str, render_hash: str) -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                insert into card_projections(
                    run_id,
                    dirty,
                    last_render_hash,
                    last_rendered_at,
                    remote_message_id,
                    error,
                    updated_at
                )
                values(?, 0, ?, ?, ?, '', ?)
                on conflict(run_id) do update set
                    dirty = 0,
                    last_render_hash = excluded.last_render_hash,
                    last_rendered_at = excluded.last_rendered_at,
                    remote_message_id = coalesce(nullif(excluded.remote_message_id, ''), remote_message_id),
                    error = '',
                    updated_at = excluded.updated_at
                """,
                (run_id, render_hash, now, remote_message_id, now),
            )
            if remote_message_id:
                conn.execute(
                    'update runs set status_message_id = ?, updated_at = ? where id = ?',
                    (remote_message_id, now, run_id),
                )

    def mark_card_error(self, run_id: int, error: str) -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                insert into card_projections(run_id, dirty, error, updated_at)
                values(?, 1, ?, ?)
                on conflict(run_id) do update set
                    dirty = 1,
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                (run_id, error, now),
            )

    def upsert_outbox(
        self,
        *,
        kind: str,
        dedupe_key: str,
        payload: dict[str, Any],
        run_id: int | None = None,
        replace_sent: bool = True,
    ) -> int:
        now = int(time.time())
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.connect() as conn:
            row = conn.execute('select * from feishu_outbox where dedupe_key = ?', (dedupe_key,)).fetchone()
            if row:
                if str(row['state']) in {'sending', 'sent'} and not replace_sent:
                    return int(row['id'])
                conn.execute(
                    """
                    update feishu_outbox
                    set run_id = ?,
                        kind = ?,
                        payload_json = ?,
                        state = 'pending',
                        last_error = '',
                        updated_at = ?
                    where id = ?
                    """,
                    (run_id, kind, payload_json, now, int(row['id'])),
                )
                return int(row['id'])
            cursor = conn.execute(
                """
                insert into feishu_outbox(run_id, kind, dedupe_key, payload_json, state, created_at, updated_at)
                values(?, ?, ?, ?, 'pending', ?, ?)
                """,
                (run_id, kind, dedupe_key, payload_json, now, now),
            )
            return int(cursor.lastrowid)

    def claim_pending_outbox(self, limit: int = 20, *, max_attempts: int = 10) -> list[FeishuOutboxItem]:
        now = int(time.time())
        with self.connect() as conn:
            rows = conn.execute(
                """
                select * from feishu_outbox
                where state in ('pending', 'failed_retryable') and attempts < ?
                order by updated_at, id
                limit ?
                """,
                (max_attempts, limit),
            ).fetchall()
            ids = [int(row['id']) for row in rows]
            if ids:
                placeholders = ','.join('?' for _ in ids)
                conn.execute(
                    f"""
                    update feishu_outbox
                    set state = 'sending',
                        attempts = attempts + 1,
                        updated_at = ?
                    where id in ({placeholders})
                    """,
                    (now, *ids),
                )
                rows = conn.execute(
                    f'select * from feishu_outbox where id in ({placeholders}) order by id',
                    ids,
                ).fetchall()
        return [self._outbox_from_row(row) for row in rows]

    def finish_outbox(self, outbox_id: int, *, sent: bool, error: str = '', max_attempts: int = 10) -> None:
        now = int(time.time())
        with self.connect() as conn:
            if sent:
                conn.execute(
                    """
                    update feishu_outbox
                    set state = 'sent',
                        last_error = '',
                        sent_at = ?,
                        updated_at = ?
                    where id = ?
                    """,
                    (now, now, outbox_id),
                )
                return
            row = conn.execute('select attempts from feishu_outbox where id = ?', (outbox_id,)).fetchone()
            attempts = int(row['attempts']) if row else max_attempts
            state = 'dead' if attempts >= max_attempts else 'failed_retryable'
            conn.execute(
                """
                update feishu_outbox
                set state = ?,
                    last_error = ?,
                    updated_at = ?
                where id = ?
                """,
                (state, error, now, outbox_id),
            )

    def reset_stuck_outbox(self, *, older_than_seconds: int = 120) -> None:
        cutoff = int(time.time()) - older_than_seconds
        with self.connect() as conn:
            conn.execute(
                """
                update feishu_outbox
                set state = 'failed_retryable',
                    updated_at = ?
                where state = 'sending' and updated_at < ?
                """,
                (int(time.time()), cutoff),
            )

    def enqueue_spawn_request(
        self,
        *,
        parent_session_id: int,
        parent_status_message_id: str,
        parent_source_message_id: str,
        chat_id: str,
        cwd: str,
        title: str,
        prompt: str,
        context_profile: str = '',
        skills: tuple[str, ...] = (),
    ) -> int:
        now = int(time.time())
        with self.connect() as conn:
            cursor = conn.execute(
                """
                insert into spawn_requests(
                    parent_session_id,
                    parent_status_message_id,
                    parent_source_message_id,
                    chat_id,
                    cwd,
                    title,
                    prompt,
                    context_profile,
                    skills,
                    state,
                    created_at,
                    updated_at
                )
                values(?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    parent_session_id,
                    parent_status_message_id,
                    parent_source_message_id,
                    chat_id,
                    cwd,
                    title,
                    prompt,
                    context_profile,
                    ','.join(skills),
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def claim_pending_spawn_requests(self, limit: int = 5) -> list[SpawnRequest]:
        now = int(time.time())
        with self.connect() as conn:
            rows = conn.execute(
                """
                select * from spawn_requests
                where state = 'pending'
                order by id
                limit ?
                """,
                (limit,),
            ).fetchall()
            ids = [int(row['id']) for row in rows]
            if ids:
                placeholders = ','.join('?' for _ in ids)
                conn.execute(
                    f"update spawn_requests set state = 'claimed', updated_at = ? where id in ({placeholders})",
                    (now, *ids),
                )
        return [self._spawn_request_from_row(row) for row in rows]

    def finish_spawn_request(self, request_id: int, *, state: str, error: str = '') -> None:
        with self.connect() as conn:
            conn.execute(
                'update spawn_requests set state = ?, error = ?, updated_at = ? where id = ?',
                (state, error, int(time.time()), request_id),
            )

    def enqueue_title_request(self, *, session_id: int, title: str) -> int:
        now = int(time.time())
        with self.connect() as conn:
            cursor = conn.execute(
                """
                insert into title_requests(session_id, title, state, created_at, updated_at)
                values(?, ?, 'pending', ?, ?)
                """,
                (session_id, title, now, now),
            )
            return int(cursor.lastrowid)

    def claim_pending_title_requests(self, limit: int = 10) -> list[TitleRequest]:
        now = int(time.time())
        with self.connect() as conn:
            rows = conn.execute(
                """
                select * from title_requests
                where state = 'pending'
                order by id
                limit ?
                """,
                (limit,),
            ).fetchall()
            ids = [int(row['id']) for row in rows]
            if ids:
                placeholders = ','.join('?' for _ in ids)
                conn.execute(
                    f"update title_requests set state = 'claimed', updated_at = ? where id in ({placeholders})",
                    (now, *ids),
                )
        return [self._title_request_from_row(row) for row in rows]

    def finish_title_request(self, request_id: int, *, state: str, error: str = '') -> None:
        with self.connect() as conn:
            conn.execute(
                'update title_requests set state = ?, error = ?, updated_at = ? where id = ?',
                (state, error, int(time.time()), request_id),
            )

    def claim_schedule_run(self, job_id: str, run_key: str) -> bool:
        now = int(time.time())
        with self.connect() as conn:
            row = conn.execute('select last_run_key from schedule_runs where job_id = ?', (job_id,)).fetchone()
            if row and str(row['last_run_key']) == run_key:
                return False
            conn.execute(
                """
                insert into schedule_runs(job_id, last_run_key, updated_at)
                values(?, ?, ?)
                on conflict(job_id) do update set
                    last_run_key = excluded.last_run_key,
                    updated_at = excluded.updated_at
                """,
                (job_id, run_key, now),
            )
            return True

    @staticmethod
    def _db_value(value: object) -> object:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, tuple):
            return ','.join(str(item) for item in value)
        return value

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> AgentSession:
        return AgentSession(
            id=int(row['id']),
            kind=str(row['kind']),
            chat_id=str(row['chat_id']),
            thread_id=str(row['thread_id']) if row['thread_id'] is not None else None,
            root_message_id=str(row['root_message_id']) if row['root_message_id'] else None,
            codex_thread_id=str(row['codex_thread_id']) if row['codex_thread_id'] else None,
            cwd=str(row['cwd']),
            context_profile=str(row['context_profile'] or ''),
            skills=split_skill_names(row['skills']),
        )

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            id=int(row['id']),
            session_id=int(row['session_id']),
            source_message_id=str(row['source_message_id']),
            prompt=str(row['prompt']),
            state=str(row['state']),
            status_phase=str(row['status_phase']),
            status=str(row['status']),
            status_message_id=str(row['status_message_id'] or ''),
            codex_thread_id=str(row['codex_thread_id'] or ''),
            turn_id=str(row['turn_id'] or ''),
            subject=str(row['subject'] or ''),
            display_title=str(row['display_title'] or ''),
            host=str(row['host'] or ''),
            status_reply_in_thread=bool(row['status_reply_in_thread']),
            context_profile=str(row['context_profile'] or ''),
            skills=split_skill_names(row['skills']),
            hide_early_iterations=bool(row['hide_early_iterations']),
            show_tool_details=bool(row['show_tool_details']),
            truncate_content=bool(row['truncate_content']),
            final_message_text=str(row['final_message_text'] or ''),
            final_message_sent_at=int(row['final_message_sent_at'])
            if row['final_message_sent_at'] is not None
            else None,
            error=str(row['error'] or ''),
            handoff_child_session_id=(
                int(row['handoff_child_session_id']) if row['handoff_child_session_id'] is not None else None
            ),
            started_at=int(row['started_at']),
            finished_at=int(row['finished_at']) if row['finished_at'] is not None else None,
            heartbeat_at=int(row['heartbeat_at']),
            lease_until=int(row['lease_until']),
            created_at=int(row['created_at']),
            updated_at=int(row['updated_at']),
        )

    @staticmethod
    def _run_event_from_row(row: sqlite3.Row) -> RunEvent:
        try:
            payload = json.loads(str(row['payload_json'] or '{}'))
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return RunEvent(
            id=int(row['id']),
            run_id=int(row['run_id']),
            event_type=str(row['event_type']),
            payload=payload,
            created_at=int(row['created_at']),
        )

    @staticmethod
    def _outbox_from_row(row: sqlite3.Row) -> FeishuOutboxItem:
        try:
            payload = json.loads(str(row['payload_json'] or '{}'))
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return FeishuOutboxItem(
            id=int(row['id']),
            run_id=int(row['run_id']) if row['run_id'] is not None else None,
            kind=str(row['kind']),
            dedupe_key=str(row['dedupe_key']),
            payload=payload,
            state=str(row['state']),
            attempts=int(row['attempts']),
            last_error=str(row['last_error'] or ''),
            created_at=int(row['created_at']),
            updated_at=int(row['updated_at']),
            sent_at=int(row['sent_at']) if row['sent_at'] is not None else None,
        )

    @staticmethod
    def _spawn_request_from_row(row: sqlite3.Row) -> SpawnRequest:
        return SpawnRequest(
            id=int(row['id']),
            parent_session_id=int(row['parent_session_id']),
            parent_status_message_id=str(row['parent_status_message_id']),
            parent_source_message_id=str(row['parent_source_message_id']),
            chat_id=str(row['chat_id']),
            cwd=str(row['cwd']),
            title=str(row['title']),
            prompt=str(row['prompt']),
            context_profile=str(row['context_profile'] or ''),
            skills=split_skill_names(row['skills']),
            state=str(row['state']),
        )

    @staticmethod
    def _title_request_from_row(row: sqlite3.Row) -> TitleRequest:
        return TitleRequest(
            id=int(row['id']),
            session_id=int(row['session_id']),
            title=str(row['title']),
            state=str(row['state']),
        )
