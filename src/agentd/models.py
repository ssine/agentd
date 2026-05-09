from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class IncomingMessage:
    chat_id: str
    message_id: str
    text: str
    sender_open_id: str = ''
    sender_name: str = ''
    sender_type: str = ''
    thread_id: str = ''
    chat_type: str = ''


@dataclass(frozen=True)
class CardAction:
    action: str
    message_id: str = ''
    chat_id: str = ''
    session_id: int | None = None


@dataclass(frozen=True)
class AgentSession:
    id: int
    kind: str
    chat_id: str
    thread_id: str | None
    root_message_id: str | None
    codex_thread_id: str | None
    cwd: str
    context_profile: str = ''
    skills: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunRecord:
    id: int
    session_id: int
    source_message_id: str
    prompt: str
    state: str
    status_phase: str
    status: str
    status_message_id: str
    codex_thread_id: str
    turn_id: str
    subject: str
    display_title: str
    host: str
    status_reply_in_thread: bool
    context_profile: str
    skills: tuple[str, ...]
    hide_early_iterations: bool
    show_tool_details: bool
    truncate_content: bool
    final_message_text: str
    final_message_sent_at: int | None
    error: str
    handoff_child_session_id: int | None
    started_at: int
    finished_at: int | None
    heartbeat_at: int
    lease_until: int
    created_at: int
    updated_at: int
    sender_open_id: str = ''


@dataclass(frozen=True)
class RunEvent:
    id: int
    run_id: int
    event_type: str
    payload: dict[str, Any]
    created_at: int


@dataclass(frozen=True)
class FeishuOutboxItem:
    id: int
    run_id: int | None
    kind: str
    dedupe_key: str
    payload: dict[str, Any]
    state: str
    attempts: int
    last_error: str
    created_at: int
    updated_at: int
    sent_at: int | None


@dataclass(frozen=True)
class CodexTurnResult:
    codex_thread_id: str
    turn_id: str
    final_text: str
    status: str


@dataclass(frozen=True)
class SpawnRequest:
    id: int
    parent_session_id: int
    parent_status_message_id: str
    parent_source_message_id: str
    chat_id: str
    cwd: str
    title: str
    prompt: str
    context_profile: str
    skills: tuple[str, ...]
    state: str
    sender_open_id: str = ''
    mode: str = 'handoff'


@dataclass(frozen=True)
class TitleRequest:
    id: int
    session_id: int
    title: str
    state: str
