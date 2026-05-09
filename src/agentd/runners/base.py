from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..models import AgentSession

AgentEventSink = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class AgentCapabilities:
    supports_resume: bool = False
    supports_live_append: bool = False
    supports_interrupt: bool = False
    supports_title_update: bool = False
    supports_tool_events: bool = False
    supports_final_streaming: bool = False
    supports_structured_run_events: bool = False


@dataclass(frozen=True)
class AgentTurnRequest:
    session: AgentSession
    prompt: str
    extra_env: dict[str, str] = field(default_factory=dict)
    config_overrides: list[str] = field(default_factory=list)
    developer_instructions: str = ''


@dataclass(frozen=True)
class AgentTurnResult:
    session_ref: str
    turn_ref: str
    final_text: str
    status: str

    @property
    def codex_thread_id(self) -> str:
        return self.session_ref

    @property
    def turn_id(self) -> str:
        return self.turn_ref


class AgentRunControl:
    def append_input(self, text: str) -> tuple[bool, str]:
        return False, 'agent runner does not support live input.'

    def steer(self, text: str) -> tuple[bool, str]:
        return self.append_input(text)

    def interrupt(self) -> tuple[bool, str]:
        return False, 'agent runner does not support interrupt.'

    def set_title(self, title: str) -> tuple[bool, str]:
        return False, 'agent runner does not support title updates.'

    def set_thread_name(self, name: str) -> tuple[bool, str]:
        return self.set_title(name)


class AgentRunner(Protocol):
    kind: str
    label: str
    capabilities: AgentCapabilities

    def new_control(self) -> AgentRunControl:
        ...

    def start_turn(
        self,
        request: AgentTurnRequest,
        *,
        event_sink: AgentEventSink | None = None,
        control: AgentRunControl | None = None,
    ) -> AgentTurnResult:
        ...
