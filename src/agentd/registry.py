from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .capture_proxy import ensure_model_http_exchanges_schema
from .context import split_skill_names
from .models import (
    AgentSession,
    ChannelBindingRecord,
    DeliveryRecord,
    FeishuOutboxItem,
    RunEvent,
    RunRecord,
    SpawnRequest,
    TitleRequest,
)


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
                    runner_kind text not null default '',
                    runner_session_ref text,
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
                    sender_open_id text not null default '',
                    mode text not null default 'handoff',
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

                create table if not exists schedule_pending_runs (
                    id integer primary key autoincrement,
                    job_id text not null,
                    run_key text not null,
                    state text not null default 'pending',
                    created_at integer not null,
                    updated_at integer not null,
                    unique(job_id, run_key)
                );

                create index if not exists schedule_pending_runs_state_idx
                on schedule_pending_runs(state, created_at);

                create table if not exists runs (
                    id integer primary key autoincrement,
                    session_id integer not null,
                    source_message_id text not null,
                    sender_open_id text not null default '',
                    prompt text not null,
                    state text not null,
                    status_phase text not null,
                    status text not null,
                    status_message_id text not null default '',
                    codex_thread_id text not null default '',
                    turn_id text not null default '',
                    runner_kind text not null default '',
                    runner_session_ref text not null default '',
                    runner_turn_ref text not null default '',
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

                create table if not exists channel_bindings (
                    id integer primary key autoincrement,
                    session_id integer not null unique,
                    channel text not null,
                    conversation_ref text not null,
                    thread_ref text not null default '',
                    root_message_ref text not null default '',
                    metadata_json text not null default '{}',
                    created_at integer not null,
                    updated_at integer not null
                );

                create index if not exists channel_bindings_channel_conversation_idx
                on channel_bindings(channel, conversation_ref, thread_ref);

                create table if not exists deliveries (
                    id integer primary key autoincrement,
                    run_id integer,
                    channel text not null,
                    destination_ref text not null,
                    thread_ref text not null default '',
                    kind text not null,
                    dedupe_key text not null unique,
                    payload_json text not null default '{}',
                    state text not null,
                    attempts integer not null default 0,
                    external_ref text not null default '',
                    last_error text not null default '',
                    created_at integer not null,
                    updated_at integer not null,
                    sent_at integer
                );

                create index if not exists deliveries_state_idx
                on deliveries(state, updated_at);

                create index if not exists deliveries_run_idx
                on deliveries(run_id, id);

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
            self._add_column_if_missing(conn, 'sessions', 'runner_kind', "text not null default ''")
            self._add_column_if_missing(conn, 'sessions', 'runner_session_ref', 'text')
            self._add_column_if_missing(conn, 'runs', 'sender_open_id', "text not null default ''")
            self._add_column_if_missing(conn, 'runs', 'runner_kind', "text not null default ''")
            self._add_column_if_missing(conn, 'runs', 'runner_session_ref', "text not null default ''")
            self._add_column_if_missing(conn, 'runs', 'runner_turn_ref', "text not null default ''")
            self._add_column_if_missing(conn, 'spawn_requests', 'sender_open_id', "text not null default ''")
            self._add_column_if_missing(conn, 'spawn_requests', 'mode', "text not null default 'handoff'")
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

    def get_main_session(
        self,
        chat_id: str,
        cwd: str,
        *,
        channel: str = '',
        conversation_ref: str = '',
    ) -> AgentSession:
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
            self._ensure_channel_binding_for_row(
                conn,
                row,
                channel=channel,
                conversation_ref=conversation_ref,
            )
        return self._session_from_row(row)

    def get_thread_session(self, chat_id: str, thread_id: str, *, channel: str = '') -> AgentSession | None:
        with self.connect() as conn:
            row = conn.execute(
                'select * from sessions where chat_id = ? and thread_id = ?',
                (chat_id, thread_id),
            ).fetchone()
            if row is not None:
                self._ensure_channel_binding_for_row(conn, row, channel=channel)
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
        channel: str = '',
        conversation_ref: str = '',
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
            self._ensure_channel_binding_for_row(
                conn,
                row,
                channel=channel,
                conversation_ref=conversation_ref,
                root_message_ref=root_message_id,
            )
        return self._session_from_row(row)

    def get_schedule_session(
        self,
        chat_id: str,
        job_id: str,
        cwd: str,
        *,
        context_profile: str = '',
        skills: tuple[str, ...] = (),
        channel: str = '',
        conversation_ref: str = '',
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
            self._ensure_channel_binding_for_row(
                conn,
                row,
                channel=channel,
                conversation_ref=conversation_ref,
            )
        return self._session_from_row(row)

    def update_codex_thread(self, session_id: int, codex_thread_id: str) -> None:
        self.update_runner_session(session_id, codex_thread_id, runner_kind='codex')

    def update_runner_session(self, session_id: int, runner_session_ref: str, *, runner_kind: str = '') -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                update sessions
                set runner_kind = coalesce(nullif(?, ''), runner_kind),
                    runner_session_ref = ?,
                    codex_thread_id = ?,
                    updated_at = ?
                where id = ?
                """,
                (runner_kind, runner_session_ref, runner_session_ref, now, session_id),
            )

    def bind_session_channel(
        self,
        session_id: int,
        *,
        channel: str,
        conversation_ref: str,
        thread_ref: str = '',
        root_message_ref: str = '',
        metadata: dict[str, Any] | None = None,
    ) -> ChannelBindingRecord:
        now = int(time.time())
        payload_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        with self.connect() as conn:
            conn.execute(
                """
                insert into channel_bindings(
                    session_id,
                    channel,
                    conversation_ref,
                    thread_ref,
                    root_message_ref,
                    metadata_json,
                    created_at,
                    updated_at
                )
                values(?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(session_id) do update set
                    channel = excluded.channel,
                    conversation_ref = excluded.conversation_ref,
                    thread_ref = excluded.thread_ref,
                    root_message_ref = coalesce(nullif(excluded.root_message_ref, ''), root_message_ref),
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id,
                    self._normalize_channel(channel),
                    conversation_ref,
                    thread_ref,
                    root_message_ref,
                    payload_json,
                    now,
                    now,
                ),
            )
            row = conn.execute('select * from channel_bindings where session_id = ?', (session_id,)).fetchone()
        return self._channel_binding_from_row(row)

    def get_channel_binding(self, session_id: int) -> ChannelBindingRecord | None:
        with self.connect() as conn:
            row = conn.execute('select * from channel_bindings where session_id = ?', (session_id,)).fetchone()
            if row is not None:
                return self._channel_binding_from_row(row)
            session = conn.execute('select * from sessions where id = ?', (session_id,)).fetchone()
            if session is None:
                return None
            self._ensure_channel_binding_for_row(conn, session)
            row = conn.execute('select * from channel_bindings where session_id = ?', (session_id,)).fetchone()
        return self._channel_binding_from_row(row) if row else None

    def get_session(self, session_id: int) -> AgentSession | None:
        with self.connect() as conn:
            row = conn.execute('select * from sessions where id = ?', (session_id,)).fetchone()
            if row is not None:
                self._ensure_channel_binding_for_row(conn, row)
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
        sender_open_id: str = '',
        status: str = '启动 Codex',
        status_phase: str = 'running',
        state: str = 'running',
        runner_kind: str = '',
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
                    sender_open_id,
                    prompt,
                    state,
                    status_phase,
                    status,
                    status_message_id,
                    runner_kind,
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
                values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    source_message_id,
                    sender_open_id,
                    prompt,
                    state,
                    status_phase,
                    status,
                    status_message_id,
                    runner_kind,
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

    def idle_work_count(self) -> int:
        active_states = ('queued', 'starting', 'running', 'cancel_requested', 'recovering')
        placeholders = ','.join('?' for _ in active_states)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                select
                    (select count(*) from runs where state in ({placeholders})) +
                    (select count(*) from feishu_outbox where state in ('pending', 'sending')) +
                    (select count(*) from card_projections where dirty != 0) as count
                """,
                active_states,
            ).fetchone()
        return int(row['count']) if row else 0

    def has_recently_finished_run(self, *, within_seconds: int, now: int | None = None) -> bool:
        if within_seconds <= 0:
            return False
        now = int(time.time()) if now is None else now
        cutoff = now - within_seconds
        with self.connect() as conn:
            row = conn.execute(
                """
                select 1
                from runs
                where finished_at is not null and finished_at >= ?
                limit 1
                """,
                (cutoff,),
            ).fetchone()
        return row is not None

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
        self._update_run(run_id, fields, mark_card_dirty=False)

    def update_run_and_mark_card_dirty(self, run_id: int, **fields: object) -> None:
        self._update_run(run_id, fields, mark_card_dirty=True)

    def _update_run(self, run_id: int, fields: dict[str, object], *, mark_card_dirty: bool) -> None:
        allowed = {
            'state',
            'status_phase',
            'status',
            'status_message_id',
            'codex_thread_id',
            'turn_id',
            'runner_kind',
            'runner_session_ref',
            'runner_turn_ref',
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
        now = int(time.time())
        assignments: list[str] = []
        values: list[object] = []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f'unsupported runs column: {key}')
            assignments.append(f'{key} = ?')
            values.append(self._db_value(value))
        assignments.append('updated_at = ?')
        values.append(now)
        values.append(run_id)
        with self.connect() as conn:
            conn.execute(f'update runs set {", ".join(assignments)} where id = ?', values)
            if 'status_message_id' in fields:
                conn.execute(
                    """
                    insert into card_projections(run_id, remote_message_id, updated_at)
                    values(?, ?, ?)
                    on conflict(run_id) do update set
                        dirty = case
                            when remote_message_id != excluded.remote_message_id then 1
                            else dirty
                        end,
                        last_render_hash = case
                            when remote_message_id != excluded.remote_message_id then ''
                            else last_render_hash
                        end,
                        last_rendered_at = case
                            when remote_message_id != excluded.remote_message_id then null
                            else last_rendered_at
                        end,
                        remote_message_id = excluded.remote_message_id,
                        updated_at = excluded.updated_at
                    """,
                    (run_id, str(fields.get('status_message_id') or ''), now),
                )
            if mark_card_dirty:
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

    def list_sessions(self, limit: int = 100) -> list[AgentSession]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select *
                from sessions
                order by updated_at desc, id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [self._session_from_row(row) for row in rows]

    def list_runs(self, *, session_id: int | None = None, limit: int = 100) -> list[RunRecord]:
        where = ''
        params: list[object] = []
        if session_id is not None:
            where = 'where session_id = ?'
            params.append(session_id)
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select *
                from runs
                {where}
                order by started_at desc, id desc
                limit ?
                """,
                params,
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def list_model_http_exchanges(
        self,
        *,
        session_id: int | None = None,
        codex_thread_id: str = '',
        codex_turn_id: str = '',
        limit: int = 200,
    ) -> list[sqlite3.Row]:
        conditions: list[str] = []
        params: list[object] = []
        if session_id is not None:
            conditions.append('session_id = ?')
            params.append(session_id)
        if codex_thread_id:
            conditions.append('codex_thread_id = ?')
            params.append(codex_thread_id)
        if codex_turn_id:
            conditions.append('codex_turn_id = ?')
            params.append(codex_turn_id)
        where = f'where {" and ".join(conditions)}' if conditions else ''
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select *
                from (
                    select *
                    from model_http_exchanges
                    {where}
                    order by created_at desc, id desc
                    limit ?
                )
                order by created_at, id
                """,
                params,
            ).fetchall()
        return rows

    def get_model_http_exchange(self, exchange_id: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute('select * from model_http_exchanges where id = ?', (exchange_id,)).fetchone()

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

    def upsert_delivery(
        self,
        *,
        channel: str,
        destination_ref: str,
        kind: str,
        dedupe_key: str,
        payload: dict[str, Any],
        run_id: int | None = None,
        thread_ref: str = '',
        state: str = 'pending',
        replace_sent: bool = True,
    ) -> int:
        now = int(time.time())
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.connect() as conn:
            row = conn.execute('select * from deliveries where dedupe_key = ?', (dedupe_key,)).fetchone()
            if row:
                if str(row['state']) == 'sent' and not replace_sent:
                    return int(row['id'])
                conn.execute(
                    """
                    update deliveries
                    set run_id = ?,
                        channel = ?,
                        destination_ref = ?,
                        thread_ref = ?,
                        kind = ?,
                        payload_json = ?,
                        state = ?,
                        attempts = 0,
                        external_ref = '',
                        last_error = '',
                        updated_at = ?
                    where id = ?
                    """,
                    (
                        run_id,
                        self._normalize_channel(channel),
                        destination_ref,
                        thread_ref,
                        kind,
                        payload_json,
                        state,
                        now,
                        int(row['id']),
                    ),
                )
                return int(row['id'])
            cursor = conn.execute(
                """
                insert into deliveries(
                    run_id,
                    channel,
                    destination_ref,
                    thread_ref,
                    kind,
                    dedupe_key,
                    payload_json,
                    state,
                    created_at,
                    updated_at
                )
                values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    self._normalize_channel(channel),
                    destination_ref,
                    thread_ref,
                    kind,
                    dedupe_key,
                    payload_json,
                    state,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def get_delivery_by_dedupe_key(self, dedupe_key: str) -> DeliveryRecord | None:
        with self.connect() as conn:
            row = conn.execute('select * from deliveries where dedupe_key = ?', (dedupe_key,)).fetchone()
        return self._delivery_from_row(row) if row else None

    def list_deliveries(self, *, run_id: int | None = None, limit: int = 100) -> list[DeliveryRecord]:
        where = ''
        params: list[object] = []
        if run_id is not None:
            where = 'where run_id = ?'
            params.append(run_id)
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select *
                from deliveries
                {where}
                order by updated_at desc, id desc
                limit ?
                """,
                params,
            ).fetchall()
        return [self._delivery_from_row(row) for row in rows]

    def mark_delivery_sent(self, delivery_id: int, *, external_ref: str = '') -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                update deliveries
                set state = 'sent',
                    attempts = 0,
                    external_ref = coalesce(nullif(?, ''), external_ref),
                    last_error = '',
                    sent_at = ?,
                    updated_at = ?
                where id = ?
                """,
                (external_ref, now, now, delivery_id),
            )

    def mark_delivery_failed(self, delivery_id: int, error: str) -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                update deliveries
                set state = 'failed',
                    last_error = ?,
                    updated_at = ?
                where id = ?
                """,
                (error, now, delivery_id),
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
                        attempts = 0,
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
                        attempts = 0,
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
                where state = 'sending' and updated_at <= ?
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
        sender_open_id: str = '',
        mode: str = 'handoff',
    ) -> int:
        now = int(time.time())
        with self.connect() as conn:
            cursor = conn.execute(
                """
                insert into spawn_requests(
                    parent_session_id,
                    parent_status_message_id,
                    parent_source_message_id,
                    sender_open_id,
                    mode,
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
                values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    parent_session_id,
                    parent_status_message_id,
                    parent_source_message_id,
                    sender_open_id,
                    mode,
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

    def enqueue_pending_schedule_run(self, job_id: str, run_key: str) -> bool:
        now = int(time.time())
        with self.connect() as conn:
            cursor = conn.execute(
                """
                insert or ignore into schedule_pending_runs(job_id, run_key, state, created_at, updated_at)
                values(?, ?, 'pending', ?, ?)
                """,
                (job_id, run_key, now, now),
            )
            return cursor.rowcount > 0

    def get_pending_schedule_run(self, job_id: str) -> str:
        with self.connect() as conn:
            row = conn.execute(
                """
                select run_key from schedule_pending_runs
                where job_id = ? and state = 'pending'
                order by id
                limit 1
                """,
                (job_id,),
            ).fetchone()
        return str(row['run_key']) if row else ''

    def finish_pending_schedule_run(self, job_id: str, run_key: str, *, state: str = 'started') -> None:
        with self.connect() as conn:
            conn.execute(
                """
                update schedule_pending_runs
                set state = ?, updated_at = ?
                where job_id = ? and run_key = ? and state = 'pending'
                """,
                (state, int(time.time()), job_id, run_key),
            )

    def _ensure_channel_binding_for_row(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        channel: str = '',
        conversation_ref: str = '',
        root_message_ref: str = '',
    ) -> None:
        session_id = int(row['id'])
        chat_id = str(row['chat_id'])
        thread_ref = str(row['thread_id'] or '')
        resolved_channel = self._normalize_channel(channel or self._legacy_channel_for_chat(chat_id))
        resolved_conversation = conversation_ref or self._legacy_conversation_ref(resolved_channel, chat_id)
        now = int(time.time())
        conn.execute(
            """
            insert into channel_bindings(
                session_id,
                channel,
                conversation_ref,
                thread_ref,
                root_message_ref,
                metadata_json,
                created_at,
                updated_at
            )
            values(?, ?, ?, ?, ?, '{}', ?, ?)
            on conflict(session_id) do update set
                channel = case
                    when ? != '' then excluded.channel
                    else channel
                end,
                conversation_ref = case
                    when ? != '' then excluded.conversation_ref
                    else conversation_ref
                end,
                thread_ref = case
                    when excluded.thread_ref != '' then excluded.thread_ref
                    else thread_ref
                end,
                root_message_ref = case
                    when excluded.root_message_ref != '' then excluded.root_message_ref
                    else root_message_ref
                end,
                updated_at = excluded.updated_at
            """,
            (
                session_id,
                resolved_channel,
                resolved_conversation,
                thread_ref,
                root_message_ref or str(row['root_message_id'] or ''),
                now,
                now,
                channel,
                conversation_ref,
            ),
        )

    @staticmethod
    def _normalize_channel(channel: str) -> str:
        value = str(channel or '').strip().lower().replace('-', '_')
        if value in {'claude', 'codex'}:
            return 'feishu'
        return value or 'feishu'

    @staticmethod
    def _legacy_channel_for_chat(chat_id: str) -> str:
        value = str(chat_id or '')
        if value == 'web' or value.startswith('web:'):
            return 'web'
        if value == 'wecom' or value.startswith('wecom:'):
            return 'wecom'
        return 'feishu'

    @staticmethod
    def _legacy_conversation_ref(channel: str, chat_id: str) -> str:
        value = str(chat_id or '')
        prefix = f'{channel}:'
        if channel in {'web', 'wecom'} and value.startswith(prefix):
            return value[len(prefix) :]
        return value or channel

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
            runner_kind=str(row['runner_kind'] or ''),
            runner_session_ref=str(row['runner_session_ref'] or row['codex_thread_id'] or '') or None,
        )

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            id=int(row['id']),
            session_id=int(row['session_id']),
            source_message_id=str(row['source_message_id']),
            sender_open_id=str(row['sender_open_id'] or ''),
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
            runner_kind=str(row['runner_kind'] or ''),
            runner_session_ref=str(row['runner_session_ref'] or row['codex_thread_id'] or ''),
            runner_turn_ref=str(row['runner_turn_ref'] or row['turn_id'] or ''),
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
    def _channel_binding_from_row(row: sqlite3.Row) -> ChannelBindingRecord:
        try:
            metadata = json.loads(str(row['metadata_json'] or '{}'))
        except json.JSONDecodeError:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        return ChannelBindingRecord(
            id=int(row['id']),
            session_id=int(row['session_id']),
            channel=str(row['channel']),
            conversation_ref=str(row['conversation_ref']),
            thread_ref=str(row['thread_ref'] or ''),
            root_message_ref=str(row['root_message_ref'] or ''),
            metadata=metadata,
            created_at=int(row['created_at']),
            updated_at=int(row['updated_at']),
        )

    @staticmethod
    def _delivery_from_row(row: sqlite3.Row) -> DeliveryRecord:
        try:
            payload = json.loads(str(row['payload_json'] or '{}'))
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return DeliveryRecord(
            id=int(row['id']),
            run_id=int(row['run_id']) if row['run_id'] is not None else None,
            channel=str(row['channel']),
            destination_ref=str(row['destination_ref']),
            thread_ref=str(row['thread_ref'] or ''),
            kind=str(row['kind']),
            dedupe_key=str(row['dedupe_key']),
            payload=payload,
            state=str(row['state']),
            attempts=int(row['attempts']),
            external_ref=str(row['external_ref'] or ''),
            last_error=str(row['last_error'] or ''),
            created_at=int(row['created_at']),
            updated_at=int(row['updated_at']),
            sent_at=int(row['sent_at']) if row['sent_at'] is not None else None,
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
            sender_open_id=str(row['sender_open_id'] or ''),
            mode=str(row['mode'] or 'handoff'),
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
