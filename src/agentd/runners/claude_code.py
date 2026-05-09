from __future__ import annotations

import json
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from ..config import ClaudeCodeConfig
from .base import AgentCapabilities, AgentEventSink, AgentRunControl, AgentRunner, AgentTurnRequest, AgentTurnResult


class ClaudeCodeRunControl(AgentRunControl):
    pass


class ClaudeCodeRunner(AgentRunner):
    kind = 'claude_code'
    label = 'Claude Code'
    capabilities = AgentCapabilities(
        supports_resume=True,
        supports_live_append=False,
        supports_interrupt=False,
        supports_title_update=False,
        supports_tool_events=False,
        supports_final_streaming=False,
        supports_structured_run_events=True,
    )

    def __init__(self, config: ClaudeCodeConfig, log_dir: Path) -> None:
        self.config = config
        self.log_dir = log_dir

    def new_control(self) -> ClaudeCodeRunControl:
        return ClaudeCodeRunControl()

    def start_turn(
        self,
        request: AgentTurnRequest,
        *,
        event_sink: AgentEventSink | None = None,
        control: AgentRunControl | None = None,
    ) -> AgentTurnResult:
        started_at = int(time.time())
        log_path = self.log_dir / f'claude-code-{started_at}-{request.session.id}.log'
        self.log_dir.mkdir(parents=True, exist_ok=True)
        system_prompt_path = self._write_developer_instructions(request, started_at)
        argv = self._argv(request, system_prompt_path=system_prompt_path)
        if self.config.use_login_shell:
            argv = ['zsh', '-lic', shlex.join(argv)]

        _emit(
            event_sink,
            'thread_ready',
            session_id=request.session.id,
            session_ref=request.session.agent_session_ref,
            cwd=request.session.cwd,
        )
        _emit(event_sink, 'turn_started', session_id=request.session.id, session_ref=request.session.agent_session_ref, turn_ref='')

        env = None
        if request.extra_env:
            import os

            env = os.environ.copy()
            env.update(request.extra_env)

        with log_path.open('a', encoding='utf-8') as log:
            log.write(f'{time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())} > {shlex.join(argv)}\n')
            proc = subprocess.run(
                argv,
                input=request.prompt,
                cwd=request.session.cwd,
                env=env,
                text=True,
                capture_output=True,
                timeout=self.config.turn_timeout_seconds,
                check=False,
            )
            if proc.stderr:
                log.write(proc.stderr)
                if not proc.stderr.endswith('\n'):
                    log.write('\n')
            if proc.stdout:
                log.write(proc.stdout)
                if not proc.stdout.endswith('\n'):
                    log.write('\n')

        payload = _parse_output(proc.stdout)
        final_text = _result_text(payload, proc.stdout)
        session_ref = _payload_string(payload, 'session_id') or _payload_string(payload, 'sessionId')
        turn_ref = _payload_string(payload, 'turn_id') or _payload_string(payload, 'turnId')
        status = 'completed' if proc.returncode == 0 and not _payload_bool(payload, 'is_error') else 'failed'
        if status == 'failed' and not final_text:
            final_text = (proc.stderr or f'Claude Code exited with code {proc.returncode}').strip()

        if final_text:
            _emit(event_sink, 'agent_message', text=final_text, phase='final_answer')
            _emit(event_sink, 'final_answer_ready', text=final_text, turn_id=turn_ref)
        _emit(event_sink, 'turn_completed', status=status, turn_id=turn_ref, final_text=final_text)
        return AgentTurnResult(
            session_ref=session_ref or request.session.agent_session_ref,
            turn_ref=turn_ref or f'claude:{started_at}',
            final_text=final_text,
            status=status,
        )

    def _write_developer_instructions(self, request: AgentTurnRequest, started_at: int) -> Path | None:
        if not request.developer_instructions:
            return None
        path = self.log_dir / f'claude-code-system-{started_at}-{request.session.id}.md'
        path.write_text(request.developer_instructions, encoding='utf-8')
        return path

    def _argv(self, request: AgentTurnRequest, *, system_prompt_path: Path | None = None) -> list[str]:
        argv = [*shlex.split(self.config.command), '--print', '--output-format', 'json']
        if self.config.permission_mode:
            argv.extend(['--permission-mode', self.config.permission_mode])
        if self.config.model:
            argv.extend(['--model', self.config.model])
        if request.session.agent_session_ref:
            argv.extend(['--resume', request.session.agent_session_ref])
        if system_prompt_path is not None:
            argv.extend(['--append-system-prompt-file', str(system_prompt_path)])
        argv.extend(self.config.extra_args)
        return argv


def _emit(event_sink: AgentEventSink | None, event_type: str, **data: Any) -> None:
    if event_sink is not None:
        event_sink({'type': event_type, **data})


def _parse_output(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        for line in reversed(text.splitlines()):
            try:
                value = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        else:
            return {}
    return value if isinstance(value, dict) else {}


def _result_text(payload: dict[str, Any], stdout: str) -> str:
    for key in ('result', 'text', 'message'):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    content = payload.get('content')
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get('text'), str):
                parts.append(item['text'])
        if parts:
            return ''.join(parts).strip()
    return stdout.strip()


def _payload_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ''


def _payload_bool(payload: dict[str, Any], key: str) -> bool:
    return payload.get(key) is True
