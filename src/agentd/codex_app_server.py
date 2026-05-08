from __future__ import annotations

import json
import os
import select
import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .capture_proxy import (
    CAPTURE_PROVIDER_ID,
    CaptureContext,
    CaptureProxy,
    capture_provider_overrides,
    turn_client_metadata,
)
from .config import CodexConfig
from .models import AgentSession, CodexTurnResult


class CodexAppServerError(RuntimeError):
    pass


CodexEventSink = Callable[[dict[str, Any]], None]


class CodexRunControl:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._codex: CodexAppServer | None = None
        self._proc: subprocess.Popen[str] | None = None
        self._log: Any | None = None
        self._interrupted_at: float | None = None
        self.thread_id = ''
        self.turn_id = ''

    def attach(self, codex: CodexAppServer, proc: subprocess.Popen[str], log: Any) -> None:
        with self._lock:
            self._codex = codex
            self._proc = proc
            self._log = log

    def set_thread_id(self, thread_id: str) -> None:
        with self._lock:
            self.thread_id = thread_id

    def set_turn_id(self, turn_id: str) -> None:
        with self._lock:
            self.turn_id = turn_id

    def steer(self, text: str) -> tuple[bool, str]:
        with self._lock:
            if not self._codex or not self._proc or not self._log or not self.thread_id or not self.turn_id:
                return False, 'Codex turn is not ready yet.'
            self._codex._send_request(
                self._proc,
                self._log,
                'turn/steer',
                {
                    'threadId': self.thread_id,
                    'expectedTurnId': self.turn_id,
                    'input': [{'type': 'text', 'text': text, 'text_elements': []}],
                },
            )
            return True, 'Steer request sent.'

    def interrupt(self) -> tuple[bool, str]:
        with self._lock:
            if not self._codex or not self._proc or not self._log or not self.thread_id or not self.turn_id:
                return False, 'Codex turn is not ready yet.'
            self._codex._send_request(
                self._proc,
                self._log,
                'turn/interrupt',
                {
                    'threadId': self.thread_id,
                    'turnId': self.turn_id,
                },
            )
            self._interrupted_at = time.time()
            return True, 'Interrupt request sent.'

    def set_thread_name(self, name: str) -> tuple[bool, str]:
        with self._lock:
            if not self._codex or not self._proc or not self._log or not self.thread_id:
                return False, 'Codex thread is not ready yet.'
            self._codex._send_request(
                self._proc,
                self._log,
                'thread/name/set',
                {
                    'threadId': self.thread_id,
                    'name': name,
                },
            )
            return True, 'Thread name update sent.'

    def interrupted_at(self) -> float | None:
        with self._lock:
            return self._interrupted_at


class CodexAppServer:
    def __init__(self, config: CodexConfig, log_dir: Path) -> None:
        self.config = config
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._next_id = 1
        self._send_lock = threading.Lock()

    def run_turn(
        self,
        session: AgentSession,
        user_text: str,
        *,
        event_sink: CodexEventSink | None = None,
        control: CodexRunControl | None = None,
        extra_env: dict[str, str] | None = None,
        config_overrides: list[str] | None = None,
        developer_instructions: str = '',
    ) -> CodexTurnResult:
        started_at = int(time.time())
        log_path = self.log_dir / f'codex-app-server-{started_at}-{session.id}.log'
        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
        capture_proxy: CaptureProxy | None = None
        active_overrides = list(config_overrides or [])
        model_provider_override = ''
        responses_metadata: dict[str, str] | None = None
        if self.config.capture.enabled:
            capture_proxy = CaptureProxy(
                capture_dir=self.config.capture.capture_dir,
                db_path=self.config.capture.db_path,
                upstream_mode=self.config.capture.upstream_mode,
                upstream_url=self.config.capture.upstream_url,
                context=CaptureContext(session_id=session.id, provider_id=CAPTURE_PROVIDER_ID, model=self.config.model),
                save_sensitive_headers=self.config.capture.save_sensitive_headers,
            )
            capture_proxy.start()
            active_overrides.extend(capture_provider_overrides(capture_proxy.base_url))
            model_provider_override = CAPTURE_PROVIDER_ID

        argv = [*shlex.split(self.config.command), 'app-server']
        for override in active_overrides:
            argv.extend(['-c', override])
        argv.extend(['--listen', 'stdio://'])
        try:
            with log_path.open('a', encoding='utf-8') as log:
                proc = subprocess.Popen(
                    argv,
                    cwd=session.cwd,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
                try:
                    if control:
                        control.attach(self, proc, log)
                    self._initialize(proc, log)
                    codex_thread_id = self._start_or_resume_thread(
                        proc,
                        log,
                        session,
                        developer_instructions=developer_instructions,
                        model_provider_override=model_provider_override,
                    )
                    if capture_proxy:
                        capture_proxy.update_context(codex_thread_id=codex_thread_id)
                        responses_metadata = turn_client_metadata(
                            session_id=session.id,
                            request_id=f'{session.id}:{started_at}',
                            codex_thread_id=codex_thread_id,
                        )
                    if control:
                        control.set_thread_id(codex_thread_id)
                    self._emit(
                        event_sink,
                        'thread_ready',
                        session_id=session.id,
                        codex_thread_id=codex_thread_id,
                        cwd=session.cwd,
                    )
                    turn_id = self._start_turn(proc, log, codex_thread_id, user_text, responses_metadata)
                    if capture_proxy:
                        capture_proxy.update_context(codex_turn_id=turn_id)
                    if control:
                        control.set_turn_id(turn_id)
                    self._emit(
                        event_sink,
                        'turn_started',
                        session_id=session.id,
                        codex_thread_id=codex_thread_id,
                        turn_id=turn_id,
                    )
                    final_text, status = self._collect_turn(proc, log, codex_thread_id, turn_id, event_sink, control)
                    return CodexTurnResult(
                        codex_thread_id=codex_thread_id,
                        turn_id=turn_id,
                        final_text=final_text.strip(),
                        status=status,
                    )
                finally:
                    self._terminate(proc)
        finally:
            if capture_proxy:
                capture_proxy.stop()

    def _initialize(self, proc: subprocess.Popen[str], log: Any) -> None:
        self._request(
            proc,
            log,
            'initialize',
            {
                'clientInfo': {'name': 'agentd', 'version': '0.1.0'},
                'capabilities': None,
            },
            timeout=self.config.startup_timeout_seconds,
        )

    def _start_or_resume_thread(
        self,
        proc: subprocess.Popen[str],
        log: Any,
        session: AgentSession,
        *,
        developer_instructions: str = '',
        model_provider_override: str = '',
    ) -> str:
        common: dict[str, Any] = {
            'cwd': session.cwd,
            'approvalPolicy': self.config.approval_policy,
            'sandbox': self.config.sandbox,
            'persistExtendedHistory': False,
        }
        if developer_instructions:
            common['developerInstructions'] = developer_instructions
        if self.config.model:
            common['model'] = self.config.model
        model_provider = model_provider_override or self.config.model_provider
        if model_provider:
            common['modelProvider'] = model_provider

        if session.codex_thread_id:
            result = self._request(
                proc,
                log,
                'thread/resume',
                {
                    'threadId': session.codex_thread_id,
                    **common,
                },
                timeout=self.config.startup_timeout_seconds,
                allow_error=True,
            )
            if 'thread' in result:
                return str(result['thread']['id'])
            self._log(log, f'resume failed, starting new thread: {result.get("error")}')

        result = self._request(
            proc,
            log,
            'thread/start',
            {
                'experimentalRawEvents': False,
                **common,
            },
            timeout=self.config.startup_timeout_seconds,
        )
        return str(result['thread']['id'])

    def _start_turn(
        self,
        proc: subprocess.Popen[str],
        log: Any,
        thread_id: str,
        user_text: str,
        responses_metadata: dict[str, str] | None = None,
    ) -> str:
        params: dict[str, Any] = {
            'threadId': thread_id,
            'input': [{'type': 'text', 'text': user_text, 'text_elements': []}],
            'approvalPolicy': self.config.approval_policy,
        }
        if responses_metadata:
            params['responsesapiClientMetadata'] = responses_metadata
        sandbox_policy = self._turn_sandbox_policy()
        if sandbox_policy:
            params['sandboxPolicy'] = sandbox_policy

        result = self._request(
            proc,
            log,
            'turn/start',
            params,
            timeout=self.config.startup_timeout_seconds,
        )
        return str(result['turn']['id'])

    def _turn_sandbox_policy(self) -> dict[str, Any] | None:
        if self.config.sandbox == 'danger-full-access':
            return {'type': 'dangerFullAccess'}
        return None

    def _collect_turn(
        self,
        proc: subprocess.Popen[str],
        log: Any,
        thread_id: str,
        turn_id: str,
        event_sink: CodexEventSink | None = None,
        control: CodexRunControl | None = None,
    ) -> tuple[str, str]:
        deadline = time.time() + self.config.turn_timeout_seconds
        item_phases: dict[str, str | None] = {}
        final_chunks: list[str] = []
        final_text_from_completed = ''
        final_answer_completed_at: float | None = None
        terminal_status = ''
        terminal_status_at: float | None = None
        last_error = ''

        while time.time() < deadline:
            if final_answer_completed_at and time.time() - final_answer_completed_at > 2:
                final_text = ''.join(final_chunks).strip() or final_text_from_completed.strip()
                self._emit(
                    event_sink,
                    'turn_completed',
                    status='completed',
                    turn_id=turn_id,
                    final_text=final_text,
                )
                return final_text, 'completed'
            if terminal_status_at and time.time() - terminal_status_at > 2:
                text = last_error or f'Codex thread entered {terminal_status}'
                self._emit(event_sink, 'turn_completed', status=terminal_status, turn_id=turn_id, final_text=text)
                return text, terminal_status

            msg = self._read_message(proc, log, timeout=0.5)
            interrupted_at = control.interrupted_at() if control else None
            if interrupted_at and time.time() - interrupted_at > 3:
                self._emit(event_sink, 'turn_interrupted', turn_id=turn_id)
                return ''.join(final_chunks).strip(), 'interrupted'
            if msg is None:
                continue
            if 'id' in msg and 'method' not in msg:
                if 'error' in msg:
                    error = json.dumps(msg.get('error'), ensure_ascii=False)
                    self._log(log, f'codex request error response: {error}')
                    self._emit(event_sink, 'error', text=error)
                continue
            if 'id' in msg and 'method' in msg:
                self._handle_server_request(proc, log, msg)
                continue
            method = msg.get('method')
            params = msg.get('params') if isinstance(msg.get('params'), dict) else {}

            if method == 'item/started':
                item = params.get('item') if isinstance(params.get('item'), dict) else {}
                if item.get('type') == 'agentMessage':
                    item_phases[str(item.get('id'))] = item.get('phase')
                elif item.get('type') == 'commandExecution':
                    self._emit(
                        event_sink,
                        'command_started',
                        item_id=str(item.get('id') or ''),
                        command=str(item.get('command') or ''),
                        cwd=str(item.get('cwd') or ''),
                    )
                elif item.get('type') == 'mcpToolCall':
                    self._emit(
                        event_sink,
                        'tool_started',
                        item_id=str(item.get('id') or ''),
                        tool=f'{item.get("server") or ""}.{item.get("tool") or ""}'.strip('.'),
                    )
                elif item.get('type') == 'fileChange':
                    self._emit(event_sink, 'file_change_started', item_id=str(item.get('id') or ''))
                elif item.get('type') == 'plan':
                    self._emit(event_sink, 'plan_started', text=str(item.get('text') or ''))
            elif method == 'item/completed':
                item = params.get('item') if isinstance(params.get('item'), dict) else {}
                if item.get('type') == 'agentMessage':
                    phase = str(item.get('phase') or '')
                    text = str(item.get('text') or '')
                    self._emit(
                        event_sink,
                        'agent_message',
                        text=text,
                        phase=phase,
                    )
                    if phase == 'final_answer':
                        final_text_from_completed = text
                        final_answer_completed_at = time.time()
                        self._emit(event_sink, 'final_answer_ready', text=text.strip(), turn_id=turn_id)
                elif item.get('type') == 'commandExecution':
                    self._emit(
                        event_sink,
                        'command_completed',
                        item_id=str(item.get('id') or ''),
                        command=str(item.get('command') or ''),
                        exit_code=item.get('exitCode'),
                        duration_ms=item.get('durationMs'),
                    )
                elif item.get('type') == 'mcpToolCall':
                    self._emit(
                        event_sink,
                        'tool_completed',
                        item_id=str(item.get('id') or ''),
                        tool=f'{item.get("server") or ""}.{item.get("tool") or ""}'.strip('.'),
                        duration_ms=item.get('durationMs'),
                    )
                elif item.get('type') == 'fileChange':
                    self._emit(event_sink, 'file_change_completed', item_id=str(item.get('id') or ''))
            elif method == 'turn/plan/updated':
                plan = params.get('plan') if isinstance(params.get('plan'), list) else []
                active = [
                    str(step.get('step'))
                    for step in plan
                    if isinstance(step, dict) and step.get('status') == 'in_progress'
                ]
                if active:
                    self._emit(event_sink, 'plan_updated', text=active[0])
            elif method == 'item/agentMessage/delta':
                if params.get('threadId') != thread_id or params.get('turnId') != turn_id:
                    continue
                item_id = str(params.get('itemId') or '')
                phase = item_phases.get(item_id)
                if phase in (None, 'final_answer'):
                    final_chunks.append(str(params.get('delta') or ''))
            elif method == 'error':
                last_error = json.dumps(params, ensure_ascii=False)
                self._log(log, f'codex error notification: {last_error}')
                self._emit(event_sink, 'error', text=last_error)
                if (
                    params.get('threadId') == thread_id
                    and params.get('turnId') == turn_id
                    and params.get('willRetry') is False
                ):
                    terminal_status = 'error'
                    terminal_status_at = terminal_status_at or time.time()
            elif method == 'thread/status/changed':
                status = params.get('status') if isinstance(params.get('status'), dict) else {}
                if params.get('threadId') != thread_id:
                    continue
                status_type = str(status.get('type') or '')
                if status_type == 'idle' and final_chunks:
                    final_text = ''.join(final_chunks).strip()
                    self._emit(
                        event_sink,
                        'turn_completed',
                        status='completed',
                        turn_id=turn_id,
                        final_text=final_text,
                    )
                    return final_text, 'completed'
                if status_type == 'systemError':
                    terminal_status = status_type
                    terminal_status_at = terminal_status_at or time.time()
            elif method == 'thread/name/updated':
                if params.get('threadId') != thread_id:
                    continue
                self._emit(event_sink, 'thread_name_updated', text=str(params.get('threadName') or ''))
            elif method == 'turn/completed':
                if params.get('threadId') != thread_id or params.get('turnId') != turn_id:
                    continue
                turn = params.get('turn') if isinstance(params.get('turn'), dict) else {}
                status = str(turn.get('status') or 'unknown')
                text = ''.join(final_chunks).strip() or final_text_from_completed.strip()
                if not text and turn.get('error'):
                    text = json.dumps(turn.get('error'), ensure_ascii=False)
                if not text and last_error:
                    text = last_error
                self._emit(event_sink, 'turn_completed', status=status, turn_id=turn_id, final_text=text)
                return text, status

        raise CodexAppServerError(f'timed out waiting for Codex turn {turn_id}')

    def _request(
        self,
        proc: subprocess.Popen[str],
        log: Any,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: int = 60,
        allow_error: bool = False,
    ) -> dict[str, Any]:
        request_id = self._send_request(proc, log, method, params)
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self._read_message(proc, log, timeout=0.5)
            if msg is None:
                continue
            if msg.get('id') == request_id:
                if 'error' in msg:
                    if allow_error:
                        return msg
                    raise CodexAppServerError(f'{method} failed: {msg["error"]}')
                return msg.get('result') if isinstance(msg.get('result'), dict) else {}
            if 'id' in msg and 'method' in msg:
                self._handle_server_request(proc, log, msg)
        raise CodexAppServerError(f'timed out waiting for {method}')

    def _send_request(
        self,
        proc: subprocess.Popen[str],
        log: Any,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> int:
        if proc.stdin is None:
            raise CodexAppServerError('Codex app-server stdin is closed')
        with self._send_lock:
            request_id = self._next_id
            self._next_id += 1
            payload: dict[str, Any] = {'jsonrpc': '2.0', 'id': request_id, 'method': method}
            if params is not None:
                payload['params'] = params
            self._write_payload_unlocked(proc, log, payload)
            return request_id

    def _send_response(self, proc: subprocess.Popen[str], log: Any, payload: dict[str, Any]) -> None:
        with self._send_lock:
            self._write_payload_unlocked(proc, log, payload)

    def _write_payload_unlocked(self, proc: subprocess.Popen[str], log: Any, payload: dict[str, Any]) -> None:
        if proc.stdin is None:
            raise CodexAppServerError('Codex app-server stdin is closed')
        line = json.dumps(payload, ensure_ascii=False)
        self._log(log, f'> {line}')
        proc.stdin.write(line + '\n')
        proc.stdin.flush()

    def _read_message(self, proc: subprocess.Popen[str], log: Any, timeout: float) -> dict[str, Any] | None:
        if proc.poll() is not None:
            raise CodexAppServerError(f'Codex app-server exited with code {proc.returncode}')
        streams = [stream for stream in (proc.stdout, proc.stderr) if stream is not None]
        ready, _, _ = select.select(streams, [], [], timeout)
        for stream in ready:
            line = stream.readline()
            if not line:
                continue
            if stream is proc.stderr:
                self._log(log, f'! {line.rstrip()}')
                continue
            self._log(log, f'< {line.rstrip()}')
            try:
                return json.loads(line)
            except json.JSONDecodeError as exc:
                self._log(log, f'json decode failed: {exc}')
        return None

    def _handle_server_request(self, proc: subprocess.Popen[str], log: Any, msg: dict[str, Any]) -> None:
        method = str(msg.get('method') or '')
        request_id = msg.get('id')
        if request_id is None or proc.stdin is None:
            return

        if method == 'item/commandExecution/requestApproval':
            result: dict[str, Any] = {'decision': 'decline'}
        elif method == 'item/fileChange/requestApproval':
            result = {'decision': 'decline'}
        elif method == 'item/tool/requestUserInput':
            result = {'answers': {}}
        elif method == 'item/tool/call':
            result = {'contentItems': [], 'success': False}
        else:
            response = {
                'jsonrpc': '2.0',
                'id': request_id,
                'error': {'code': -32601, 'message': f'agentd does not implement server request {method}'},
            }
            self._send_response(proc, log, response)
            return

        response = {'jsonrpc': '2.0', 'id': request_id, 'result': result}
        self._send_response(proc, log, response)

    def _terminate(self, proc: subprocess.Popen[str]) -> None:
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    @staticmethod
    def _log(log: Any, message: str) -> None:
        log.write(f'{time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())} {message}\n')
        log.flush()

    @staticmethod
    def _emit(event_sink: CodexEventSink | None, event_type: str, **data: Any) -> None:
        if event_sink is None:
            return
        event_sink({'type': event_type, **data})
