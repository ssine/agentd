from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .context import split_skill_names
from .models import AgentSession, SpawnRequest, TitleRequest


class Registry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
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
                """
            )
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
