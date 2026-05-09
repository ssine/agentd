from __future__ import annotations

from pathlib import Path

from ..codex_app_server import CodexAppServer, CodexRunControl
from ..config import CodexConfig
from .base import AgentCapabilities, AgentEventSink, AgentRunControl, AgentRunner, AgentTurnRequest, AgentTurnResult


class CodexRunner(AgentRunner):
    kind = 'codex'
    label = 'Codex'
    capabilities = AgentCapabilities(
        supports_resume=True,
        supports_live_append=True,
        supports_interrupt=True,
        supports_title_update=True,
        supports_tool_events=True,
        supports_final_streaming=True,
        supports_structured_run_events=True,
    )

    def __init__(self, config: CodexConfig, log_dir: Path) -> None:
        self.config = config
        self.log_dir = log_dir

    def new_control(self) -> CodexRunControl:
        return CodexRunControl()

    def start_turn(
        self,
        request: AgentTurnRequest,
        *,
        event_sink: AgentEventSink | None = None,
        control: AgentRunControl | None = None,
    ) -> AgentTurnResult:
        codex_control = control if isinstance(control, CodexRunControl) else CodexRunControl()
        result = CodexAppServer(self.config, self.log_dir).run_turn(
            request.session,
            request.prompt,
            event_sink=event_sink,
            control=codex_control,
            extra_env=request.extra_env,
            config_overrides=request.config_overrides,
            developer_instructions=request.developer_instructions,
        )
        return AgentTurnResult(
            session_ref=result.codex_thread_id,
            turn_ref=result.turn_id,
            final_text=result.final_text,
            status=result.status,
        )
