from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .codex_app_server import CodexAppServer, CodexRunControl
from .config import AgentdConfig
from .context import ContextResolver, ResolvedContext
from .feishu import FeishuApi, FeishuListener, final_message_card_width_mode
from .models import AgentSession, CardAction, IncomingMessage, SpawnRequest, TitleRequest
from .registry import Registry
from .schedule import ScheduleJob, due_run_key
from .title import normalize_title, title_from_text


@dataclass
class RunIteration:
    message: str = ''
    phase: str = ''
    tool_counts: dict[str, int] = field(default_factory=dict)
    failed_tool_counts: dict[str, int] = field(default_factory=dict)
    running_tools: dict[str, int] = field(default_factory=dict)
    tool_details: list[str] = field(default_factory=list)


@dataclass
class ActiveRun:
    session: AgentSession
    source_message_id: str
    control: CodexRunControl
    host: str
    started_at: float
    finished_at: float | None = None
    status: str = '启动 Codex'
    last_status_sent_at: float = 0.0
    codex_thread_id: str = ''
    turn_id: str = ''
    status_message_id: str = ''
    status_phase: str = 'running'
    hide_early_iterations: bool = True
    show_tool_details: bool = False
    truncate_content: bool = True
    iterations: list[RunIteration] = field(default_factory=list)
    running_tools: dict[str, str] = field(default_factory=dict)
    tool_details: list[str] = field(default_factory=list)
    model_outputs: list[str] = field(default_factory=list)
    error_detail: str = ''
    last_status_body: str = ''
    final_message_sent: bool = False
    final_message_text: str = ''
    status_lock: threading.Lock = field(default_factory=threading.Lock)
    done: threading.Event = field(default_factory=threading.Event)
    subject: str = 'Codex'
    display_title: str = ''
    handoff_child_session_id: int | None = None
    status_reply_in_thread: bool = False
    context_profile: str = ''
    context_skills: tuple[str, ...] = ()


class AgentDaemon:
    def __init__(self, config: AgentdConfig, *, dry_send: bool = False) -> None:
        self.config = config
        self.dry_send = dry_send
        self.registry = Registry(config.db_path)
        self.feishu = FeishuApi(config.feishu)
        self.context_resolver = ContextResolver(config.context, config.workspace)
        self.log = logging.getLogger('agentd')
        self.host = socket.gethostname()
        self._active_lock = threading.Lock()
        self._active_runs: dict[int, ActiveRun] = {}
        self._status_runs: dict[str, ActiveRun] = {}
        self._spawn_watcher_started = False
        self._spawn_watcher_lock = threading.Lock()
        self._scheduler_started = False
        self._scheduler_lock = threading.Lock()

    def serve(self) -> None:
        self._ensure_spawn_watcher()
        self._ensure_scheduler()
        listener = FeishuListener(self.config.feishu)
        self.log.info('starting Feishu listener')
        listener.start(self.handle_message, self.handle_card_action)

    def _ensure_spawn_watcher(self) -> None:
        with self._spawn_watcher_lock:
            if self._spawn_watcher_started:
                return
            self._spawn_watcher_started = True
            threading.Thread(target=self._spawn_watcher, name='agentd-spawn-watcher', daemon=True).start()

    def _spawn_watcher(self) -> None:
        while True:
            try:
                self._maybe_run_deferred_service_command()
                for request in self.registry.claim_pending_title_requests():
                    self._handle_title_request(request)
                for request in self.registry.claim_pending_spawn_requests():
                    self._handle_spawn_request(request)
            except Exception:
                self.log.exception('failed while polling spawn requests')
            time.sleep(0.5)

    def _maybe_run_deferred_service_command(self) -> None:
        from .service import (
            clear_deferred_service_command,
            launch_service_command,
            read_deferred_service_command,
        )

        request = read_deferred_service_command(self.config)
        if not request:
            return
        command = str(request.get('command') or '')
        if command != 'restart':
            clear_deferred_service_command(self.config)
            self.log.warning('discarded unsupported deferred service command: %s', command)
            return
        try:
            not_before = float(request.get('not_before') or 0)
        except (TypeError, ValueError):
            not_before = 0
        if time.time() < not_before:
            return
        with self._active_lock:
            active_count = len(self._active_runs)
        if active_count:
            return

        clear_deferred_service_command(self.config)
        try:
            timeout_seconds = int(request.get('timeout_seconds') or 10)
        except (TypeError, ValueError):
            timeout_seconds = 10
        backend = str(request.get('backend') or 'auto')
        self.log.info('launching deferred service restart after daemon became idle')
        launch_service_command(self.config, backend, command, delay_seconds=0.2, timeout_seconds=timeout_seconds)

    def _ensure_scheduler(self) -> None:
        if not any(job.enabled for job in self.config.schedules.jobs):
            return
        with self._scheduler_lock:
            if self._scheduler_started:
                return
            self._scheduler_started = True
            threading.Thread(target=self._schedule_watcher, name='agentd-scheduler', daemon=True).start()

    def _schedule_watcher(self) -> None:
        while True:
            try:
                for job in self.config.schedules.jobs:
                    self._maybe_start_scheduled_job(job)
            except Exception:
                self.log.exception('failed while polling scheduled jobs')
            time.sleep(30)

    def _maybe_start_scheduled_job(self, job: ScheduleJob) -> None:
        run_key = due_run_key(job)
        if not run_key:
            return
        if not job.chat_id or not job.prompt:
            self.log.warning('scheduled job %s is missing chat_id or prompt', job.id)
            return
        context_profile = job.context_profile or self.config.context.default_profile
        session = self.registry.get_schedule_session(
            job.chat_id,
            job.id,
            str(self.config.workspace),
            context_profile=context_profile,
            skills=job.skills,
        )
        if self._active_for(session.id) is not None:
            self.log.info('scheduled job %s is due but session %s is already active', job.id, session.id)
            return
        if not self.registry.claim_schedule_run(job.id, run_key):
            return
        self._start_scheduled_job(job, run_key, session)

    def _start_scheduled_job(self, job: ScheduleJob, run_key: str, session: AgentSession) -> None:
        active = ActiveRun(
            session=session,
            source_message_id=f'schedule:{job.id}:{run_key}',
            control=CodexRunControl(),
            host=self.host,
            started_at=time.time(),
            status='启动定时任务',
            subject='定时任务',
            display_title=normalize_title(job.title or job.name, fallback='定时任务'),
            context_profile=job.context_profile or self.config.context.default_profile,
            context_skills=job.skills,
        )
        with self._active_lock:
            self._active_runs[session.id] = active

        self._publish_status(active, force=True, create=True)
        threading.Thread(
            target=self._status_ticker,
            args=(active,),
            name=f'agentd-status-{session.id}',
            daemon=True,
        ).start()
        worker = threading.Thread(
            target=self._run_turn_worker,
            args=(active, self._build_scheduled_prompt(job, session, run_key)),
            name=f'agentd-schedule-{session.id}',
        )
        worker.start()
        self.log.info('started scheduled job %s run %s session %s', job.id, run_key, session.id)

    def _handle_title_request(self, request: TitleRequest) -> None:
        active = self._active_for(request.session_id)
        if active is None:
            self.registry.finish_title_request(request.id, state='failed', error='session is not active')
            return

        title = normalize_title(request.title, fallback='任务')
        active.display_title = title
        try:
            ok, detail = active.control.set_thread_name(title)
        except Exception as exc:
            ok = False
            detail = str(exc)
        if not ok:
            self.log.info('card title updated before Codex thread name for session %s: %s', active.session.id, detail)
        self._publish_status(active, force=True, create=True)
        self.registry.finish_title_request(request.id, state='applied', error='' if ok else detail)

    def _handle_spawn_request(self, request: SpawnRequest) -> None:
        parent = self._active_for(request.parent_session_id)
        if parent is None:
            self.registry.finish_spawn_request(request.id, state='failed', error='parent session is not active')
            return
        if not parent.status_message_id:
            self.registry.finish_spawn_request(request.id, state='failed', error='parent run has no status card')
            return

        try:
            thread_id, source_message_id = self._create_child_thread(parent, request)
            child_session = self.registry.bind_child_session(
                request.chat_id,
                thread_id,
                request.cwd,
                root_message_id=parent.status_message_id,
                parent_id=parent.session.id,
                context_profile=request.context_profile or self.config.context.default_child_profile,
                skills=request.skills,
            )
            parent.handoff_child_session_id = child_session.id
            parent.control.interrupt()
            parent.done.set()
            with self._active_lock:
                self._active_runs.pop(parent.session.id, None)

            child = ActiveRun(
                session=child_session,
                source_message_id=parent.status_message_id,
                control=CodexRunControl(),
                host=self.host,
                started_at=time.time(),
                status='启动子任务',
                status_message_id=parent.status_message_id,
                subject='子任务',
                display_title=normalize_title(request.title or title_from_text(request.prompt, fallback='子任务')),
                context_profile=request.context_profile or self.config.context.default_child_profile,
                context_skills=request.skills,
            )
            with self._active_lock:
                self._active_runs[child_session.id] = child
                self._status_runs[parent.status_message_id] = child

            self._publish_status(child, force=True, create=True)
            threading.Thread(
                target=self._status_ticker,
                args=(child,),
                name=f'agentd-status-{child_session.id}',
                daemon=True,
            ).start()
            child_worker = threading.Thread(
                target=self._run_turn_worker,
                args=(child, self._build_child_prompt(request, child_session, source_message_id)),
                name=f'agentd-session-{child_session.id}',
            )
            child_worker.daemon = False
            child_worker.start()
            self.registry.finish_spawn_request(request.id, state='started')
        except Exception as exc:
            self.log.exception('failed to spawn child request %s', request.id)
            parent.status = f'子任务启动失败: {exc}'
            parent.status_phase = 'failed'
            self._mark_finished(parent)
            self._publish_status(parent, force=True, create=True)
            self.registry.finish_spawn_request(request.id, state='failed', error=str(exc))

    def _create_child_thread(self, parent: ActiveRun, request: SpawnRequest) -> tuple[str, str]:
        if self.dry_send:
            return f'dry-thread-{request.id}', f'dry-thread-message-{request.id}'
        text = '\n'.join(
            [
                f'**子任务已启动**：{request.title or request.cwd}',
                f'CWD: `{request.cwd}`',
                '',
                '可以在这个话题里继续追加指令或打断。',
            ]
        )
        result = self.feishu.reply_markdown(parent.status_message_id, text, reply_in_thread=True)
        thread_id = thread_id_from_result(result)
        message_id = message_id_from_result(result)
        if not thread_id:
            thread_id = message_id or parent.status_message_id
        return thread_id, message_id or parent.status_message_id

    def handle_message(self, message: IncomingMessage) -> str:
        self._ensure_spawn_watcher()
        if not self._message_allowed(message):
            self.log.info('ignored message %s from chat %s', message.message_id, message.chat_id)
            return ''
        if self.registry.is_duplicate(message.message_id):
            self.log.info('duplicate message ignored: %s', message.message_id)
            return ''

        session = self._resolve_session(message)
        if session is None:
            self.log.info('ignored unbound thread message %s thread %s', message.message_id, message.thread_id)
            return ''
        active = self._active_for(session.id)
        if active is not None:
            return self._handle_live_input(active, message)

        context_profile = session.context_profile or self.config.context.default_profile
        context_skills = session.skills
        prompt = self._build_prompt(message, session)
        control = CodexRunControl()
        reuse_root_card = session.kind == 'child' and message.message_id == session.root_message_id
        source_message_id = (
            session.root_message_id if reuse_root_card and session.root_message_id else message.message_id
        )
        active = ActiveRun(
            session=session,
            source_message_id=source_message_id,
            control=control,
            host=self.host,
            started_at=time.time(),
            status_message_id=session.root_message_id if reuse_root_card and session.root_message_id else '',
            subject='子任务' if session.kind == 'child' else 'Codex',
            display_title=title_from_text(message.text, fallback='子任务' if session.kind == 'child' else '主任务'),
            status_reply_in_thread=session.kind == 'child' and not reuse_root_card,
            context_profile=context_profile,
            context_skills=context_skills,
        )
        with self._active_lock:
            self._active_runs[session.id] = active
            if active.status_message_id:
                self._status_runs[active.status_message_id] = active

        self._publish_status(active, force=True, create=True)
        ticker = threading.Thread(
            target=self._status_ticker,
            args=(active,),
            name=f'agentd-status-{session.id}',
            daemon=True,
        )
        ticker.start()

        worker = threading.Thread(
            target=self._run_turn_worker,
            args=(active, prompt),
            name=f'agentd-session-{session.id}',
        )
        worker.start()
        self.log.info('started background Codex run for message %s session %s', message.message_id, session.id)
        return 'started'

    def handle_card_action(self, action: CardAction) -> str:
        active = self._run_for_card_action(action)
        if active is None:
            return '任务状态已过期'

        if action.action == 'stop':
            ok, detail = active.control.interrupt()
            if ok:
                active.status = '已请求停止'
                self._add_model_message(active, '用户在卡片上请求停止当前 turn。', phase='control')
                self._publish_status(active, force=True, create=True)
                return '已请求停止'
            return detail

        if action.action == 'toggle_early':
            active.hide_early_iterations = not active.hide_early_iterations
            self._publish_status(active, force=True, create=True)
            return '已切换早期步骤显示'

        if action.action == 'toggle_tools':
            active.show_tool_details = not active.show_tool_details
            self._publish_status(active, force=True, create=True)
            return '已切换工具详情显示'

        if action.action == 'toggle_truncate':
            active.truncate_content = not active.truncate_content
            self._publish_status(active, force=True, create=True)
            return '已切换截断策略'

        if action.action in {'live', 'history', 'tools', 'output'}:
            self._handle_legacy_view_action(active, action.action)
            self._publish_status(active, force=True, create=True)
            return '已切换视图'

        return '未知操作'

    def _run_for_card_action(self, action: CardAction) -> ActiveRun | None:
        with self._active_lock:
            if action.message_id and action.message_id in self._status_runs:
                return self._status_runs[action.message_id]
            if action.session_id is not None and action.session_id in self._active_runs:
                return self._active_runs[action.session_id]
            if action.session_id is not None:
                for run in self._status_runs.values():
                    if run.session.id == action.session_id:
                        return run
        return None

    def _run_turn_worker(self, active: ActiveRun, prompt: str) -> None:
        session = active.session
        codex = CodexAppServer(self.config.codex, self.config.log_dir)
        resolved_context = self.context_resolver.resolve(active.context_profile, active.context_skills)
        try:
            result = codex.run_turn(
                session,
                prompt,
                event_sink=lambda event: self._handle_codex_event(active, event),
                control=active.control,
                extra_env=self._codex_extra_env(active),
                config_overrides=resolved_context.codex_config_overrides(),
                developer_instructions=self._developer_instructions(active, resolved_context),
            )
        except Exception as exc:
            if active.handoff_child_session_id is not None:
                self.log.info('parent Codex run ended after handoff for session %s: %s', session.id, exc)
                active.done.set()
                return
            self.log.exception('Codex run failed for session %s', session.id)
            self._record_error(active, str(exc))
            active.status = failure_status('运行失败', active.error_detail)
            active.status_phase = 'failed'
            self._add_model_message(active, f'运行失败: {active.error_detail}', phase='error')
            self._mark_finished(active)
            self._publish_status(active, force=True, create=True)
            active.done.set()
            with self._active_lock:
                self._active_runs.pop(session.id, None)
            return

        if active.handoff_child_session_id is not None:
            if result.codex_thread_id != session.codex_thread_id:
                self.registry.update_codex_thread(session.id, result.codex_thread_id)
            active.done.set()
            return

        if result.codex_thread_id != session.codex_thread_id:
            self.registry.update_codex_thread(session.id, result.codex_thread_id)
        elif session.codex_thread_id and (
            result.status == 'systemError' or has_invalid_encrypted_content(result.final_text)
        ):
            self.registry.update_codex_thread(session.id, '')

        if result.final_text:
            final_text = result.final_text
        elif active.final_message_text:
            final_text = active.final_message_text
        elif result.status == 'interrupted':
            final_text = '已停止当前 Codex turn。'
        else:
            final_text = f'Codex turn completed with status: {result.status}'

        if final_text and (not active.model_outputs or active.model_outputs[-1] != final_text):
            self._add_model_message(active, final_text, phase='final_answer')
        if result.status == 'interrupted':
            active.status = '已停止'
            active.status_phase = 'stopped'
        elif result.status in {'completed', 'success'}:
            active.status = '完成'
            active.status_phase = 'done'
        elif result.status in {'systemError', 'failed', 'error'}:
            if result.final_text:
                self._record_error(active, result.final_text)
            active.status = codex_failure_status(result.status, active.error_detail)
            active.status_phase = 'failed'
        elif result.status not in {'', 'unknown'}:
            active.status = f'完成: {result.status}'
            active.status_phase = 'done'
        else:
            active.status = '完成'
            active.status_phase = 'done'
        self._mark_finished(active)
        self._publish_status(active, force=True, create=bool(active.status_message_id))

        try:
            self._send_final_once(active, final_text)
        finally:
            active.done.set()
            with self._active_lock:
                self._active_runs.pop(session.id, None)

    def _active_for(self, session_id: int) -> ActiveRun | None:
        with self._active_lock:
            return self._active_runs.get(session_id)

    def _status_ticker(self, active: ActiveRun) -> None:
        while not active.done.wait(5):
            self._publish_status(active, force=True, create=True)

    def _handle_live_input(self, active: ActiveRun, message: IncomingMessage) -> str:
        text = message.text.strip()
        lower = text.lower()
        if lower in {'/status', 'status', '状态', '看看状态'}:
            self._publish_status(active, force=True, create=True)
            return 'status'
        if lower in {'/stop', '/interrupt', 'stop', 'interrupt', '停', '停止', '打断', '别做了'}:
            ok, detail = active.control.interrupt()
            if ok:
                active.status = '已请求停止'
                self._add_model_message(active, '用户请求停止当前 turn。', phase='control')
                self._publish_status(active, force=True, create=True)
            else:
                active.status = f'停止失败: {detail}'
                self._publish_status(active, force=True, create=True)
            return detail

        ok, detail = active.control.steer(text)
        if ok:
            active.status = '已追加指令'
            self._add_model_message(active, f'用户追加指令：{text}', phase='control')
            self._publish_status(active, force=True, create=True)
        else:
            active.status = f'追加失败: {detail}'
            self._publish_status(active, force=True, create=True)
        return detail

    def _handle_codex_event(self, active: ActiveRun, event: dict[str, Any]) -> None:
        if active.handoff_child_session_id is not None:
            return
        event_type = str(event.get('type') or '')
        if event_type == 'thread_ready':
            active.codex_thread_id = str(event.get('codex_thread_id') or '')
            active.status = 'Codex thread ready'
            if active.display_title:
                try:
                    active.control.set_thread_name(active.display_title)
                except Exception:
                    self.log.exception('failed to set Codex thread name for session %s', active.session.id)
            self._publish_status(active, create=False)
        elif event_type == 'turn_started':
            active.turn_id = str(event.get('turn_id') or '')
            active.status = 'turn running'
            self._publish_status(active, create=False)
        elif event_type == 'thread_name_updated':
            text = str(event.get('text') or '')
            if text:
                active.display_title = normalize_title(text, fallback=active.display_title or '任务')
                self._publish_status(active, force=True, create=True)
        elif event_type == 'agent_message':
            phase = str(event.get('phase') or 'commentary')
            text = str(event.get('text') or '')
            if text:
                active.status = '生成回答' if phase == 'final_answer' else '模型输出'
                self._add_model_message(active, text, phase=phase)
                self._publish_status(active, force=True, create=True)
        elif event_type == 'final_answer_ready':
            self._send_final_once(active, str(event.get('text') or ''))
        elif event_type == 'plan_updated':
            active.status = f'计划: {event.get("text")}'
            self._publish_status(active)
        elif event_type == 'command_started':
            active.status = '执行 Bash'
            self._add_tool(
                active, 'Bash', item_id=str(event.get('item_id') or ''), detail=str(event.get('command') or '')
            )
            self._publish_status(active, create=True)
        elif event_type == 'command_completed':
            exit_code = event.get('exit_code')
            self._finish_tool(active, str(event.get('item_id') or ''), failed=exit_code not in (None, 0))
            active.status = 'Bash 完成' if exit_code in (None, 0) else f'Bash 失败: exit={exit_code}'
            self._publish_status(active)
        elif event_type == 'tool_started':
            tool = friendly_tool_name(str(event.get('tool') or 'Tool'))
            active.status = f'调用 {tool}'
            self._add_tool(active, tool, item_id=str(event.get('item_id') or ''))
            self._publish_status(active, create=True)
        elif event_type == 'file_change_started':
            active.status = '修改文件'
            self._add_tool(active, 'File edit', item_id=str(event.get('item_id') or ''))
            self._publish_status(active, create=True)
        elif event_type in {'tool_completed', 'file_change_completed'}:
            self._finish_tool(active, str(event.get('item_id') or ''))
            self._publish_status(active)
        elif event_type == 'turn_interrupted':
            active.status = '已停止'
            active.status_phase = 'stopped'
            self._mark_finished(active)
            self._publish_status(active, force=True, create=True)
        elif event_type == 'turn_completed':
            status = str(event.get('status') or '')
            final_text = str(event.get('final_text') or '').strip()
            if final_text and (not active.model_outputs or active.model_outputs[-1] != final_text):
                self._add_model_message(active, final_text, phase='final_answer')
            if final_text:
                self._send_final_once(active, final_text)
            if status in {'systemError', 'failed', 'error'}:
                self._record_error(active, final_text)
                active.status = codex_failure_status(status, active.error_detail)
                active.status_phase = 'failed'
            else:
                active.status = f'完成: {status}'
                active.status_phase = 'done'
            self._mark_finished(active)
            self._publish_status(active, force=True, create=False)
        elif event_type == 'error':
            text = str(event.get('text') or '')
            if active.session.codex_thread_id and has_invalid_encrypted_content(text):
                self.registry.update_codex_thread(active.session.id, '')
                active.codex_thread_id = ''
                self.log.info('cleared Codex thread for session %s after invalid encrypted content', active.session.id)
            if text and (not active.model_outputs or active.model_outputs[-1] != text):
                self._add_model_message(active, text, phase='error')
            self._record_error(active, text)
            active.status = failure_status('Codex error', active.error_detail)
            active.status_phase = 'failed'
            self._mark_finished(active)
            self._publish_status(active, force=True, create=True)

    @staticmethod
    def _record_error(active: ActiveRun, detail: str) -> None:
        detail = str(detail or '').strip()
        if detail:
            active.error_detail = detail

    def _add_model_message(self, active: ActiveRun, text: str, *, phase: str) -> None:
        with active.status_lock:
            active.iterations.append(RunIteration(message=text.strip(), phase=phase))
            active.model_outputs.append(text.strip())

    def _add_tool(self, active: ActiveRun, tool: str, *, item_id: str = '', detail: str = '') -> None:
        with active.status_lock:
            iteration = self._current_iteration(active)
            iteration.tool_counts[tool] = iteration.tool_counts.get(tool, 0) + 1
            if item_id:
                active.running_tools[item_id] = tool
                iteration.running_tools[tool] = iteration.running_tools.get(tool, 0) + 1
            if detail:
                tool_detail = f'{tool}: {detail}'
                iteration.tool_details.append(tool_detail)
                active.tool_details.append(compact(tool_detail, 220))
                active.tool_details = active.tool_details[-80:]

    def _finish_tool(self, active: ActiveRun, item_id: str, *, failed: bool = False) -> None:
        if not item_id:
            return
        with active.status_lock:
            tool = active.running_tools.pop(item_id, '')
            if not tool:
                return
            for iteration in reversed(active.iterations):
                if iteration.running_tools.get(tool, 0) > 0:
                    iteration.running_tools[tool] -= 1
                    if iteration.running_tools[tool] <= 0:
                        iteration.running_tools.pop(tool, None)
                    if failed:
                        iteration.failed_tool_counts[tool] = iteration.failed_tool_counts.get(tool, 0) + 1
                    break

    def _send_final_once(self, active: ActiveRun, final_text: str) -> bool:
        final_text = final_text.strip()
        if not final_text or active.handoff_child_session_id is not None:
            return False

        with active.status_lock:
            if active.final_message_sent:
                return False
            active.final_message_sent = True
            active.final_message_text = final_text

        try:
            final_width_mode = final_message_card_width_mode(final_text)
            if self.dry_send:
                print(final_text)
            elif active.session.kind in {'main', 'schedule'}:
                self.feishu.send_markdown(active.session.chat_id, final_text, width_mode=final_width_mode)
            else:
                self.feishu.reply_markdown(
                    active.source_message_id,
                    final_text,
                    reply_in_thread=self.config.feishu.child_reply_in_thread,
                    width_mode=final_width_mode,
                )
            return True
        except Exception:
            with active.status_lock:
                active.final_message_sent = False
                active.final_message_text = ''
            self.log.exception('failed to send final Feishu message for session %s', active.session.id)
            return False

    @staticmethod
    def _mark_finished(active: ActiveRun) -> None:
        if active.finished_at is None:
            active.finished_at = time.time()

    @staticmethod
    def _current_iteration(active: ActiveRun) -> RunIteration:
        if not active.iterations:
            active.iterations.append(RunIteration(message='准备中', phase='system'))
        return active.iterations[-1]

    def _publish_status(self, active: ActiveRun, *, force: bool = False, create: bool = True) -> None:
        now = time.time()
        if not force and now - active.last_status_sent_at < 2:
            return

        text = self._format_status_text(active)
        if not force and text == active.last_status_body:
            return
        if not create and not active.status_message_id:
            return

        if self.dry_send:
            print(text)
            if create and not active.status_message_id:
                active.status_message_id = 'dry-run-status'
                with self._active_lock:
                    self._status_runs[active.status_message_id] = active
            active.last_status_sent_at = now
            active.last_status_body = text
            return

        card = self._build_status_card(active)
        try:
            if active.status_message_id:
                self.feishu.update_interactive(active.status_message_id, card)
            else:
                if active.status_reply_in_thread:
                    result = self.feishu.reply_interactive(
                        active.source_message_id,
                        card,
                        reply_in_thread=self.config.feishu.child_reply_in_thread,
                    )
                else:
                    result = self.feishu.send_interactive(active.session.chat_id, card)
                message_id = message_id_from_result(result)
                if message_id:
                    active.status_message_id = message_id
                    with self._active_lock:
                        self._status_runs[message_id] = active
            active.last_status_sent_at = now
            active.last_status_body = text
        except Exception:
            self.log.exception('failed to publish Feishu status message for session %s', active.session.id)

    def _format_status_text(self, active: ActiveRun) -> str:
        elapsed = self._elapsed_seconds(active)
        lines = [
            f'Codex {self._phase_label(active.status_phase)}: {active.status}',
            self._run_line(active, elapsed),
            self._toggle_state_line(active),
        ]
        if active.status_phase == 'failed' and active.error_detail:
            lines.append(f'错误信息: {compact(active.error_detail, 1200)}')
        iterations = self._visible_iterations(active)
        offset = max(0, len(active.iterations) - len(iterations))
        for index, iteration in enumerate(iterations, start=offset + 1):
            lines.extend(self._iteration_lines(active, index, iteration))
        return '\n'.join(lines)

    def _build_status_card(self, active: ActiveRun) -> dict[str, Any]:
        elapsed = self._elapsed_seconds(active)
        template = {
            'running': 'blue',
            'done': 'green',
            'stopped': 'orange',
            'failed': 'red',
        }.get(active.status_phase, 'blue')
        title = status_title(active)

        return {
            'config': {'wide_screen_mode': True, 'update_multi': True},
            'header': {
                'template': template,
                'title': {'tag': 'plain_text', 'content': title},
            },
            'elements': [
                {'tag': 'div', 'text': {'tag': 'lark_md', 'content': self._run_line(active, elapsed)}},
                *self._error_elements(active),
                {'tag': 'hr'},
                *self._view_elements(active),
                {'tag': 'action', 'actions': self._card_actions(active)},
            ],
        }

    def _run_line(self, active: ActiveRun, elapsed: int) -> str:
        return f'{session_label(active.session)} · {active.host} · {active.session.cwd} · {format_elapsed(elapsed)}'

    def _view_elements(self, active: ActiveRun) -> list[dict[str, Any]]:
        return [{'tag': 'div', 'text': {'tag': 'lark_md', 'content': self._iterations_view(active)}}]

    def _error_elements(self, active: ActiveRun) -> list[dict[str, Any]]:
        if active.status_phase != 'failed' or not active.error_detail:
            return []
        content = '**错误信息**\n' + escape_lark_md(compact(active.error_detail, 1800))
        return [{'tag': 'div', 'text': {'tag': 'lark_md', 'content': content}}]

    def _iterations_view(self, active: ActiveRun) -> str:
        iterations = self._visible_iterations(active)
        if not iterations:
            return '等待 Codex 产生可见输出。'

        offset = max(0, len(active.iterations) - len(iterations))
        lines: list[str] = []
        for index, iteration in enumerate(iterations, start=offset + 1):
            lines.extend(self._iteration_lines(active, index, iteration))
        if len(active.iterations) > len(iterations):
            lines.append(f'已隐藏更早 {len(active.iterations) - len(iterations)} 步，点“早期：隐藏”切换。')
        return '\n'.join(lines)

    def _tools_view(self, active: ActiveRun) -> str:
        totals: dict[str, int] = {}
        failures: dict[str, int] = {}
        for iteration in active.iterations:
            for tool, count in iteration.tool_counts.items():
                totals[tool] = totals.get(tool, 0) + count
            for tool, count in iteration.failed_tool_counts.items():
                failures[tool] = failures.get(tool, 0) + count

        lines = ['**工具详情**']
        if totals:
            lines.append('总计：' + ', '.join(f'{tool} x{count}' for tool, count in totals.items()))
        if failures:
            lines.append('失败：' + ', '.join(f'{tool} x{count}' for tool, count in failures.items()))
        if active.running_tools:
            running = {}
            for tool in active.running_tools.values():
                running[tool] = running.get(tool, 0) + 1
            lines.append('进行中：' + ', '.join(f'{tool} x{count}' for tool, count in running.items()))
        if active.tool_details:
            lines.append('')
            lines.append('最近工具调用：')
            lines.extend(f'- {escape_lark_md(item)}' for item in active.tool_details[-12:])
        if len(lines) == 1:
            lines.append('还没有工具调用。')
        return '\n'.join(lines)

    def _output_view(self, active: ActiveRun) -> str:
        if not active.model_outputs:
            return '**模型输出**\n还没有模型输出。'
        parts = []
        for index, text in enumerate(active.model_outputs[-8:], start=max(1, len(active.model_outputs) - 7)):
            parts.append(f'{index}. {escape_lark_md(compact(text, 500))}')
        return '**模型输出**\n' + '\n\n'.join(parts)

    def _visible_iterations(self, active: ActiveRun) -> list[RunIteration]:
        if active.hide_early_iterations:
            return active.iterations[-6:]
        return active.iterations

    def _iteration_lines(self, active: ActiveRun, index: int, iteration: RunIteration) -> list[str]:
        message = self._display_text(active, iteration.message, truncated_limit=150, expanded_limit=4000)
        lines = [f'{index}. 💬 {escape_lark_md(message)}']
        tools = format_tool_counts(iteration)
        if tools:
            lines.append(f'   🛠 {escape_lark_md(tools)}')
        if active.show_tool_details and iteration.tool_details:
            for detail in iteration.tool_details:
                text = self._display_text(active, detail, truncated_limit=180, expanded_limit=4000)
                lines.append(f'   🔧 {escape_lark_md(text)}')
        return lines

    @staticmethod
    def _display_text(active: ActiveRun, text: object, *, truncated_limit: int, expanded_limit: int) -> str:
        return compact(text, truncated_limit if active.truncate_content else expanded_limit)

    @staticmethod
    def _elapsed_seconds(active: ActiveRun) -> int:
        end = active.finished_at if active.finished_at is not None else time.time()
        return max(0, int(end - active.started_at))

    @staticmethod
    def _toggle_state_line(active: ActiveRun) -> str:
        early = '隐藏' if active.hide_early_iterations else '显示'
        tools = '展开' if active.show_tool_details else '摘要'
        truncate = '开' if active.truncate_content else '关'
        return f'早期：{early} · 工具：{tools} · 截断：{truncate}'

    def _card_actions(self, active: ActiveRun) -> list[dict[str, Any]]:
        actions: list[tuple[str, str, str]] = []
        if active.status_phase == 'running':
            actions.append(('停止', 'danger', 'stop'))
        actions.extend(
            [
                (f'早期：{"隐藏" if active.hide_early_iterations else "显示"}', 'default', 'toggle_early'),
                (f'工具：{"展开" if active.show_tool_details else "摘要"}', 'default', 'toggle_tools'),
                (f'截断：{"开" if active.truncate_content else "关"}', 'default', 'toggle_truncate'),
            ]
        )
        return [
            {
                'tag': 'button',
                'text': {'tag': 'plain_text', 'content': label},
                'type': style,
                'value': {
                    'action': action,
                    'session_id': active.session.id,
                    'message_id': active.status_message_id,
                    'chat_id': active.session.chat_id,
                },
            }
            for label, style, action in actions
        ]

    @staticmethod
    def _phase_label(phase: str) -> str:
        return {
            'running': '工作中',
            'done': '已完成',
            'stopped': '已停止',
            'failed': '失败',
        }.get(phase, '工作中')

    @staticmethod
    def _view_label(view: str) -> str:
        return {
            'live': '实时视图',
            'history': '完整过程',
            'tools': '工具详情',
            'output': '模型输出',
        }.get(view, view)

    @staticmethod
    def _handle_legacy_view_action(active: ActiveRun, view: str) -> None:
        if view == 'live':
            active.hide_early_iterations = True
            active.show_tool_details = False
            active.truncate_content = True
        elif view == 'history':
            active.hide_early_iterations = False
        elif view == 'tools':
            active.show_tool_details = True
        elif view == 'output':
            active.truncate_content = False

    def _message_allowed(self, message: IncomingMessage) -> bool:
        if self.config.feishu.ignore_bot_messages and message.sender_type == 'app':
            return False
        return bool(message.chat_id and message.message_id and message.text)

    def _resolve_session(self, message: IncomingMessage) -> AgentSession | None:
        if message.thread_id:
            session = self.registry.get_thread_session(message.chat_id, message.thread_id)
            if session is not None:
                return session
            return None
        return self.registry.get_main_session(message.chat_id, str(self.config.workspace))

    def _build_prompt(self, message: IncomingMessage, session: AgentSession) -> str:
        sender = message.sender_name or message.sender_open_id or 'unknown'
        thread_id = message.thread_id or 'none'
        return '\n'.join(
            [
                '[Feishu Message]',
                f'- sender: {sender} (open_id: {message.sender_open_id or "unknown"})',
                f'- chat_id: {message.chat_id}',
                f'- message_id: {message.message_id}',
                f'- thread_id: {thread_id}',
                f'- agentd_session_id: {session.id}',
                '',
                f'{sender}: {message.text}',
            ]
        )

    def _build_child_prompt(self, request: SpawnRequest, session: AgentSession, source_message_id: str) -> str:
        return '\n'.join(
            [
                '[Feishu Child Task]',
                f'- parent_session_id: {request.parent_session_id}',
                f'- agentd_session_id: {session.id}',
                f'- chat_id: {session.chat_id}',
                f'- root_message_id: {session.root_message_id or request.parent_status_message_id}',
                f'- thread_id: {session.thread_id or "unknown"}',
                f'- source_message_id: {source_message_id}',
                f'- cwd: {session.cwd}',
                f'- initial_title: {request.title or ""}',
                '',
                request.prompt,
            ]
        )

    def _build_scheduled_prompt(self, job: ScheduleJob, session: AgentSession, run_key: str) -> str:
        return '\n'.join(
            [
                '[Scheduled Task]',
                f'- job_id: {job.id}',
                f'- job_name: {job.name}',
                f'- run_key: {run_key}',
                f'- agentd_session_id: {session.id}',
                f'- chat_id: {session.chat_id}',
                f'- cwd: {session.cwd}',
                '',
                job.prompt,
            ]
        )

    def _developer_instructions(self, active: ActiveRun, resolved: ResolvedContext) -> str:
        injected_skills = ', '.join(skill.name for skill in resolved.skills) or 'none'
        lines = [
            'You are running inside agentd, a Feishu-to-Codex control plane.',
            '',
            'Runtime context:',
            f'- session_kind: {active.session.kind}',
            f'- agentd_session_id: {active.session.id}',
            f'- cwd: {active.session.cwd}',
            f'- context_profile: {resolved.profile.name}',
            f'- context_config_path: {self.config.context.path}',
            f'- profiles_available: {", ".join(sorted(self.config.context.profiles))}',
            f'- memory_dir: {resolved.memory_dir}',
            f'- injected_skills: {injected_skills}',
            '',
            'Agentd contract:',
            '- Agentd sends your final answer back to Feishu. Do not call Feishu send/reply commands yourself unless the user explicitly asks you to send an additional proactive message.',
            '- For agentd service status, logs, health checks, start, stop, or restart, use `"$AGENTD_CLI" service ...`.',
            '- If restarting agentd from inside this Feishu-managed turn, use `"$AGENTD_CLI" service restart --defer 10`; the daemon records the request and applies it after active runs finish.',
            '- If you will handle substantial work in this session, set a concise task title once early with `"$AGENTD_CLI" set-title "<title>"`.',
            '- If you delegate, do not call `set-title` in the parent session. Run `"$AGENTD_CLI" spawn-child --cwd <dir> --title <short title> [--profile <profile>] [--skills a,b]` with the full child task piped on stdin, then stop without sending a final answer.',
            '- Example delegation command: `printf %s "$child_task" | "$AGENTD_CLI" spawn-child --cwd /path/to/work --title "short title" --skills bookkeeping,calendar`.',
            '',
            'Context policy:',
            '- Follow repo guidance from AGENTS.md.',
            '- For prior work, preferences, decisions, dates, people, or todos, search memory files with `rg` first and load only relevant snippets. Do not read all memory by default.',
            '- Only the injected skills are enabled in this Codex run. Read a SKILL.md only when its description clearly matches the task.',
            '- To use another profile or skill set, delegate with `--profile <profile>` or `--skills a,b`.',
        ]
        if resolved.missing_skills:
            lines.append(f'- missing_skills: {", ".join(resolved.missing_skills)}')
        return '\n'.join(lines)

    def _codex_extra_env(self, active: ActiveRun) -> dict[str, str]:
        return {
            'AGENTD_CLI': str(self.config.executable),
            'AGENTD_CONFIG': str(self.config.config_path),
            'AGENTD_SESSION_ID': str(active.session.id),
            'AGENTD_CHAT_ID': active.session.chat_id,
            'AGENTD_SOURCE_MESSAGE_ID': active.source_message_id,
            'AGENTD_STATUS_MESSAGE_ID': active.status_message_id,
            'AGENTD_SESSION_KIND': active.session.kind,
            'AGENTD_CWD': active.session.cwd,
            'AGENTD_CONTEXT_PROFILE': active.context_profile,
            'AGENTD_CONTEXT_SKILLS': ','.join(active.context_skills),
            'AGENTD_MEMORY_DIR': str(self.context_resolver.memory_dir),
        }


def message_id_from_result(result: dict[str, Any]) -> str:
    data = result.get('data') if isinstance(result.get('data'), dict) else result
    value = data.get('message_id') if isinstance(data, dict) else None
    return value if isinstance(value, str) else ''


def thread_id_from_result(result: dict[str, Any]) -> str:
    data = result.get('data') if isinstance(result.get('data'), dict) else result
    value = data.get('thread_id') if isinstance(data, dict) else None
    return value if isinstance(value, str) else ''


def session_label(session: AgentSession) -> str:
    if session.kind == 'main':
        return '主会话'
    if session.kind == 'child':
        return '话题会话'
    if session.kind == 'schedule':
        return '定时会话'
    return session.kind or '会话'


def status_title(active: ActiveRun) -> str:
    icon = '🌿' if active.session.kind == 'child' else '🧵'
    title = active.display_title or ('子任务' if active.session.kind == 'child' else '主任务')
    return f'{icon} {normalize_title(title, fallback="任务")}'


def compact(text: object, limit: int) -> str:
    value = ' '.join(str(text or '').split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 6)] + ' ...(截断)'


def format_tool_counts(iteration: RunIteration) -> str:
    parts = [f'{tool} x{count}' for tool, count in iteration.tool_counts.items()]
    if iteration.failed_tool_counts:
        failed = ', '.join(f'{tool} x{count}' for tool, count in iteration.failed_tool_counts.items())
        parts.append(f'失败：{failed}')
    if iteration.running_tools:
        running = ', '.join(f'{tool} x{count}' for tool, count in iteration.running_tools.items())
        parts.append(f'进行中：{running}')
    return ', '.join(parts)


def friendly_tool_name(tool: str) -> str:
    if not tool:
        return 'Tool'
    mapping = {
        'functions.exec_command': 'Shell',
        'functions.apply_patch': 'File edit',
        'web.run': 'Web',
        'image_gen.imagegen': 'Image',
    }
    return mapping.get(tool, tool)


def has_invalid_encrypted_content(text: object) -> bool:
    return 'invalid_encrypted_content' in str(text or '')


def failure_status(prefix: str, detail: str) -> str:
    short = compact(detail, 120).strip()
    return f'{prefix}: {short}' if short else prefix


def codex_failure_status(status: str, detail: str) -> str:
    return failure_status(f'失败: {status}', detail)


def format_elapsed(seconds: int) -> str:
    if seconds < 60:
        return f'{seconds}s'
    minutes, rem = divmod(seconds, 60)
    if minutes < 60:
        return f'{minutes}m{rem:02d}s'
    hours, minutes = divmod(minutes, 60)
    return f'{hours}h{minutes:02d}m'


def escape_lark_md(text: str) -> str:
    return text.replace('\\', '\\\\').replace('`', '\\`')
