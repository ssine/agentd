from __future__ import annotations

from dataclasses import dataclass


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


@dataclass(frozen=True)
class TitleRequest:
    id: int
    session_id: int
    title: str
    state: str
