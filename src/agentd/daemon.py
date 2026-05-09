from __future__ import annotations

import hashlib
import json
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
from .models import (
    AgentSession,
    CardAction,
    FeishuOutboxItem,
    IncomingMessage,
    RunEvent,
    RunRecord,
    SpawnRequest,
    TitleRequest,
)
from .registry import Registry
from .schedule import ScheduleJob, due_run_key
from .title import normalize_title, title_from_text


STATUS_TICK_SECONDS = 5
STATUS_UPDATE_MIN_INTERVAL_SECONDS = 1
FEISHU_SEND_MIN_INTERVAL_SECONDS = 1


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
    run_id: int
    session: AgentSession
    control: CodexRunControl
    done: threading.Event = field(default_factory=threading.Event)
    handoff_child_session_id: int | None = None


@dataclass
class RunView:
    run: RunRecord
    session: AgentSession
    iterations: list[RunIteration]
    running_tools: dict[str, str]
    tool_details: list[str]
    model_outputs: list[str]


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
        self._outbox_lock = threading.Lock()
        self._last_feishu_send_at = 0.0
        self._spawn_watcher_started = False
        self._spawn_watcher_lock = threading.Lock()
        self._scheduler_started = False
        self._scheduler_lock = threading.Lock()
        self._web_gateway: Any | None = None
        self._web_gateway_lock = threading.Lock()

    def serve(self) -> None:
        self._ensure_spawn_watcher()
        self._ensure_scheduler()
        self._ensure_web_gateway()
        self._recover_stale_runs()
        self._mark_changed_status_cards_dirty()
        self._reconcile_dirty_cards()
        self._drain_feishu_outbox()
        self._send_startup_notice_if_needed()
        listener = FeishuListener(self.config.feishu)
        self.log.info('starting Feishu listener')
        listener.start(self.handle_message, self.handle_card_action)

    def _ensure_web_gateway(self) -> None:
        if not self.config.web.enabled:
            return
        with self._web_gateway_lock:
            if self._web_gateway is not None:
                return
            try:
                from .web_gateway import WebGateway

                gateway = WebGateway(
                    self.config,
                    host=self.config.web.host,
                    port=self.config.web.port,
                    daemon=self,
                )
                host, port = gateway.start_background()
                self._web_gateway = gateway
                self.log.info('started web gateway at http://%s:%s', host, port)
            except Exception:
                self.log.exception('failed to start web gateway')

    def _ensure_spawn_watcher(self) -> None:
        with self._spawn_watcher_lock:
            if self._spawn_watcher_started:
                return
            self._spawn_watcher_started = True
            threading.Thread(target=self._spawn_watcher, name='agentd-spawn-watcher', daemon=True).start()

    def _spawn_watcher(self) -> None:
        while True:
            try:
                self._recover_stale_runs()
                self.registry.reset_stuck_outbox()
                self._reconcile_dirty_cards()
                self._drain_feishu_outbox()
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
            prepare_restart_notice,
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
        if active_count or self.registry.idle_work_count():
            return

        clear_deferred_service_command(self.config)
        try:
            timeout_seconds = int(request.get('timeout_seconds') or 10)
        except (TypeError, ValueError):
            timeout_seconds = 10
        backend = str(request.get('backend') or 'auto')
        notify_chat_id = str(request.get('notify_chat_id') or '')
        if notify_chat_id:
            self._wait_for_feishu_send_slot()
            prepare_restart_notice(self.config, notify_chat_id)
            self._last_feishu_send_at = time.monotonic()
        self.log.info('launching deferred service restart after daemon became idle')
        launch_service_command(self.config, backend, command, delay_seconds=0.2, timeout_seconds=timeout_seconds)

    def _send_startup_notice_if_needed(self) -> None:
        from .service import clear_startup_notice, read_startup_notice, send_service_notice

        notice = read_startup_notice(self.config)
        if not notice:
            return
        chat_id = str(notice.get('chat_id') or '').strip()
        text = str(notice.get('text') or '').strip()
        if not chat_id or not text:
            clear_startup_notice(self.config)
            return
        if self.dry_send:
            clear_startup_notice(self.config)
            return
        self._wait_for_feishu_send_slot()
        if send_service_notice(self.config, chat_id, text):
            self._last_feishu_send_at = time.monotonic()
            clear_startup_notice(self.config)

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
        if self.registry.get_active_run_for_session(session.id) is not None:
            self.log.info('scheduled job %s is due but session %s has a persisted active run', job.id, session.id)
            return
        if not self.registry.claim_schedule_run(job.id, run_key):
            return
        self._start_scheduled_job(job, run_key, session)

    def _start_scheduled_job(self, job: ScheduleJob, run_key: str, session: AgentSession) -> None:
        run = self.registry.create_run(
            session_id=session.id,
            source_message_id=f'schedule:{job.id}:{run_key}',
            prompt=self._build_scheduled_prompt(job, session, run_key),
            host=self.host,
            status='启动定时任务',
            subject='定时任务',
            display_title=normalize_title(job.title or job.name, fallback='定时任务'),
            context_profile=job.context_profile or self.config.context.default_profile,
            skills=job.skills,
        )
        active = ActiveRun(run_id=run.id, session=session, control=CodexRunControl())
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
            args=(active,),
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
        self.registry.update_run(active.run_id, display_title=title)
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
        parent_run = self.registry.get_run(parent.run_id)
        if parent_run is None:
            self.registry.finish_spawn_request(request.id, state='failed', error='parent run is missing')
            return
        if not parent_run.status_message_id:
            self._publish_status(parent, force=True, create=True)
            self._drain_feishu_outbox()
            parent_run = self.registry.get_run(parent.run_id)
        if parent_run is None or not parent_run.status_message_id:
            self.registry.finish_spawn_request(request.id, state='failed', error='parent run has no status card')
            return

        try:
            thread_id, source_message_id = self._create_child_thread(parent_run, request)
            child_session = self.registry.bind_child_session(
                request.chat_id,
                thread_id,
                request.cwd,
                root_message_id=parent_run.status_message_id,
                parent_id=parent.session.id,
                context_profile=request.context_profile or self.config.context.default_child_profile,
                skills=request.skills,
            )
            parent.handoff_child_session_id = child_session.id
            self.registry.update_run(parent.run_id, handoff_child_session_id=child_session.id)
            parent.control.interrupt()
            parent.done.set()
            with self._active_lock:
                self._active_runs.pop(parent.session.id, None)

            child_run = self.registry.create_run(
                session_id=child_session.id,
                source_message_id=parent_run.status_message_id,
                prompt=self._build_child_prompt(request, child_session, source_message_id),
                host=self.host,
                status='启动子任务',
                status_message_id=parent_run.status_message_id,
                subject='子任务',
                display_title=normalize_title(request.title or title_from_text(request.prompt, fallback='子任务')),
                context_profile=request.context_profile or self.config.context.default_child_profile,
                skills=request.skills,
            )
            child = ActiveRun(run_id=child_run.id, session=child_session, control=CodexRunControl())
            with self._active_lock:
                self._active_runs[child_session.id] = child

            self._publish_status(child, force=True, create=True)
            threading.Thread(
                target=self._status_ticker,
                args=(child,),
                name=f'agentd-status-{child_session.id}',
                daemon=True,
            ).start()
            child_worker = threading.Thread(
                target=self._run_turn_worker,
                args=(child,),
                name=f'agentd-session-{child_session.id}',
            )
            child_worker.daemon = False
            child_worker.start()
            self.registry.finish_spawn_request(request.id, state='started')
        except Exception as exc:
            self.log.exception('failed to spawn child request %s', request.id)
            self.registry.update_run_and_mark_card_dirty(
                parent.run_id,
                status=f'子任务启动失败: {exc}',
                status_phase='failed',
                state='failed',
                finished_at=int(time.time()),
            )
            self._add_model_message(parent, f'子任务启动失败: {exc}', phase='error')
            self._publish_status(parent, force=True, create=True)
            self.registry.finish_spawn_request(request.id, state='failed', error=str(exc))

    def _create_child_thread(self, parent: RunRecord, request: SpawnRequest) -> tuple[str, str]:
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
        self._recover_stale_runs()
        persisted_active = self.registry.get_active_run_for_session(session.id)
        if persisted_active is not None:
            self._publish_status_for_run(persisted_active.id, force=True)
            return 'session already has an active run'

        context_profile = session.context_profile or self.config.context.default_profile
        context_skills = session.skills
        prompt = self._build_prompt(message, session)
        reuse_root_card = session.kind == 'child' and message.message_id == session.root_message_id
        source_message_id = (
            session.root_message_id if reuse_root_card and session.root_message_id else message.message_id
        )
        run = self.registry.create_run(
            session_id=session.id,
            source_message_id=source_message_id,
            prompt=prompt,
            host=self.host,
            status_message_id=session.root_message_id if reuse_root_card and session.root_message_id else '',
            subject='子任务' if session.kind == 'child' else 'Codex',
            display_title=title_from_text(message.text, fallback='子任务' if session.kind == 'child' else '主任务'),
            status_reply_in_thread=session.kind == 'child' and not reuse_root_card,
            context_profile=context_profile,
            skills=context_skills,
        )
        active = ActiveRun(run_id=run.id, session=session, control=CodexRunControl())
        with self._active_lock:
            self._active_runs[session.id] = active

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
            args=(active,),
            name=f'agentd-session-{session.id}',
        )
        worker.start()
        self.log.info('started background Codex run for message %s session %s', message.message_id, session.id)
        return 'started'

    def handle_card_action(self, action: CardAction) -> str:
        run, active = self._run_for_card_action(action)
        if run is None:
            return '任务状态已过期'

        if action.action == 'stop':
            if active is not None:
                ok, detail = active.control.interrupt()
                if ok:
                    self.registry.update_run(active.run_id, state='cancel_requested', status='已请求停止')
                    self._add_model_message(active, '用户在卡片上请求停止当前 turn。', phase='control')
                    self._publish_status(active, force=True, create=True)
                    return '已请求停止'
                return detail
            self.registry.update_run(run.id, state='cancel_requested', status='已记录停止请求')
            self.registry.append_run_event(
                run.id,
                'agent_message',
                {'text': '用户在卡片上请求停止当前 turn。', 'phase': 'control'},
            )
            self._publish_status_for_run(run.id, force=True)
            return '已记录停止请求'

        if action.action == 'toggle_early':
            self.registry.update_run(run.id, hide_early_iterations=not run.hide_early_iterations)
            self._publish_status_for_run(run.id, force=True)
            return '已切换早期步骤显示'

        if action.action == 'toggle_tools':
            self.registry.update_run(run.id, show_tool_details=not run.show_tool_details)
            self._publish_status_for_run(run.id, force=True)
            return '已切换工具详情显示'

        if action.action == 'toggle_truncate':
            self.registry.update_run(run.id, truncate_content=not run.truncate_content)
            self._publish_status_for_run(run.id, force=True)
            return '已切换截断策略'

        if action.action in {'live', 'history', 'tools', 'output'}:
            self._handle_legacy_view_action(run.id, action.action)
            self._publish_status_for_run(run.id, force=True)
            return '已切换视图'

        return '未知操作'

    def _run_for_card_action(self, action: CardAction) -> tuple[RunRecord | None, ActiveRun | None]:
        run = self.registry.get_run_for_status_card(action.message_id) if action.message_id else None
        with self._active_lock:
            active = self._active_runs.get(run.session_id) if run is not None else None
            if run is None and action.session_id is not None:
                active = self._active_runs.get(action.session_id)
                if active is not None:
                    run = self.registry.get_run(active.run_id)
        if run is None and action.session_id is not None:
            run = self.registry.get_active_run_for_session(action.session_id)
        return run, active

    def _run_turn_worker(self, active: ActiveRun) -> None:
        session = active.session
        run = self.registry.get_run(active.run_id)
        if run is None:
            active.done.set()
            return
        self.registry.update_run(active.run_id, state='running', host=self.host)
        codex = CodexAppServer(self.config.codex, self.config.log_dir)
        resolved_context = self.context_resolver.resolve(run.context_profile, run.skills)
        try:
            result = codex.run_turn(
                session,
                run.prompt,
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
            error_detail = str(exc)
            self.registry.update_run_and_mark_card_dirty(
                active.run_id,
                state='failed',
                status=failure_status('运行失败', error_detail),
                status_phase='failed',
                error=error_detail,
                finished_at=int(time.time()),
            )
            self._add_model_message(active, f'运行失败: {error_detail}', phase='error')
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
            self.registry.update_run(active.run_id, codex_thread_id=result.codex_thread_id)
        elif session.codex_thread_id and (
            result.status == 'systemError' or has_invalid_encrypted_content(result.final_text)
        ):
            self.registry.update_codex_thread(session.id, '')
            self.registry.update_run(active.run_id, codex_thread_id='')

        run = self.registry.get_run(active.run_id)
        if result.final_text:
            final_text = result.final_text
        elif run and run.final_message_text:
            final_text = run.final_message_text
        elif result.status == 'interrupted':
            final_text = '已停止当前 Codex turn。'
        else:
            final_text = f'Codex turn completed with status: {result.status}'

        if final_text and self._last_model_output(active.run_id) != final_text:
            self._add_model_message(active, final_text, phase='final_answer')
        if result.status == 'interrupted':
            self.registry.update_run_and_mark_card_dirty(
                active.run_id,
                state='interrupted',
                status='已停止',
                status_phase='stopped',
                finished_at=int(time.time()),
            )
        elif result.status in {'completed', 'success'}:
            self.registry.update_run_and_mark_card_dirty(
                active.run_id,
                state='succeeded',
                status='完成',
                status_phase='done',
                finished_at=int(time.time()),
            )
        elif result.status in {'systemError', 'failed', 'error'}:
            error_detail = result.final_text
            self.registry.update_run_and_mark_card_dirty(
                active.run_id,
                state='failed',
                status=codex_failure_status(result.status, error_detail),
                status_phase='failed',
                error=error_detail,
                finished_at=int(time.time()),
            )
        elif result.status not in {'', 'unknown'}:
            self.registry.update_run_and_mark_card_dirty(
                active.run_id,
                state='succeeded',
                status=f'完成: {result.status}',
                status_phase='done',
                finished_at=int(time.time()),
            )
        else:
            self.registry.update_run_and_mark_card_dirty(
                active.run_id,
                state='succeeded',
                status='完成',
                status_phase='done',
                finished_at=int(time.time()),
            )
        run = self.registry.get_run(active.run_id)
        self._publish_status(active, force=True, create=bool(run and run.status_message_id))

        try:
            self._queue_final_once(active, final_text)
        finally:
            active.done.set()
            with self._active_lock:
                self._active_runs.pop(session.id, None)

    def _active_for(self, session_id: int) -> ActiveRun | None:
        with self._active_lock:
            return self._active_runs.get(session_id)

    def _status_ticker(self, active: ActiveRun) -> None:
        while not active.done.wait(STATUS_TICK_SECONDS):
            self.registry.touch_run_lease(active.run_id)
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
                self.registry.update_run(active.run_id, state='cancel_requested', status='已请求停止')
                self._add_model_message(active, '用户请求停止当前 turn。', phase='control')
                self._publish_status(active, force=True, create=True)
            else:
                self.registry.update_run(active.run_id, status=f'停止失败: {detail}')
                self._publish_status(active, force=True, create=True)
            return detail

        ok, detail = active.control.steer(text)
        if ok:
            self.registry.update_run(active.run_id, status='已追加指令')
            self._add_model_message(active, f'用户追加指令：{text}', phase='control')
            self._publish_status(active, force=True, create=True)
        else:
            self.registry.update_run(active.run_id, status=f'追加失败: {detail}')
            self._publish_status(active, force=True, create=True)
        return detail

    def _handle_codex_event(self, active: ActiveRun, event: dict[str, Any]) -> None:
        if active.handoff_child_session_id is not None:
            return
        self.registry.touch_run_lease(active.run_id)
        event_type = str(event.get('type') or '')
        if event_type == 'thread_ready':
            codex_thread_id = str(event.get('codex_thread_id') or '')
            self.registry.update_run(active.run_id, codex_thread_id=codex_thread_id, status='Codex thread ready')
            run = self.registry.get_run(active.run_id)
            if run and run.display_title:
                try:
                    active.control.set_thread_name(run.display_title)
                except Exception:
                    self.log.exception('failed to set Codex thread name for session %s', active.session.id)
            self._publish_status(active, create=False)
        elif event_type == 'turn_started':
            self.registry.update_run(active.run_id, turn_id=str(event.get('turn_id') or ''), status='turn running')
            self._publish_status(active, create=False)
        elif event_type == 'thread_name_updated':
            text = str(event.get('text') or '')
            if text:
                run = self.registry.get_run(active.run_id)
                self.registry.update_run(
                    active.run_id,
                    display_title=normalize_title(text, fallback=(run.display_title if run else '') or '任务'),
                )
                self._publish_status(active, force=True, create=True)
        elif event_type == 'agent_message':
            phase = str(event.get('phase') or 'commentary')
            text = str(event.get('text') or '')
            if text:
                self.registry.update_run(active.run_id, status='生成回答' if phase == 'final_answer' else '模型输出')
                self._add_model_message(active, text, phase=phase)
                self._publish_status(active, force=True, create=True)
        elif event_type == 'final_answer_ready':
            self._queue_final_once(active, str(event.get('text') or ''))
        elif event_type == 'plan_updated':
            self.registry.update_run(active.run_id, status=f'计划: {event.get("text")}')
            self._publish_status(active)
        elif event_type == 'command_started':
            self.registry.update_run(active.run_id, status='执行 Bash')
            self._add_tool(
                active, 'Bash', item_id=str(event.get('item_id') or ''), detail=str(event.get('command') or '')
            )
            self._publish_status(active, create=True)
        elif event_type == 'command_completed':
            exit_code = event.get('exit_code')
            self._finish_tool(active, str(event.get('item_id') or ''), failed=exit_code not in (None, 0))
            self.registry.update_run(
                active.run_id,
                status='Bash 完成' if exit_code in (None, 0) else f'Bash 失败: exit={exit_code}',
            )
            self._publish_status(active)
        elif event_type == 'tool_started':
            tool = friendly_tool_name(str(event.get('tool') or 'Tool'))
            self.registry.update_run(active.run_id, status=f'调用 {tool}')
            self._add_tool(active, tool, item_id=str(event.get('item_id') or ''))
            self._publish_status(active, create=True)
        elif event_type == 'file_change_started':
            self.registry.update_run(active.run_id, status='修改文件')
            self._add_tool(active, 'File edit', item_id=str(event.get('item_id') or ''))
            self._publish_status(active, create=True)
        elif event_type in {'tool_completed', 'file_change_completed'}:
            self._finish_tool(active, str(event.get('item_id') or ''))
            self._publish_status(active)
        elif event_type == 'turn_interrupted':
            self.registry.update_run_and_mark_card_dirty(
                active.run_id,
                state='interrupted',
                status='已停止',
                status_phase='stopped',
                finished_at=int(time.time()),
            )
            self._publish_status(active, force=True, create=True)
        elif event_type == 'turn_completed':
            status = str(event.get('status') or '')
            final_text = str(event.get('final_text') or '').strip()
            if final_text and self._last_model_output(active.run_id) != final_text:
                self._add_model_message(active, final_text, phase='final_answer')
            if final_text:
                self._queue_final_once(active, final_text)
            if status in {'systemError', 'failed', 'error'}:
                self.registry.update_run_and_mark_card_dirty(
                    active.run_id,
                    state='failed',
                    status=codex_failure_status(status, final_text),
                    status_phase='failed',
                    error=final_text,
                    finished_at=int(time.time()),
                )
            else:
                self.registry.update_run_and_mark_card_dirty(
                    active.run_id,
                    state='succeeded',
                    status=f'完成: {status}',
                    status_phase='done',
                    finished_at=int(time.time()),
                )
            self._publish_status(active, force=True, create=False)
        elif event_type == 'error':
            text = str(event.get('text') or '')
            if active.session.codex_thread_id and has_invalid_encrypted_content(text):
                self.registry.update_codex_thread(active.session.id, '')
                self.registry.update_run(active.run_id, codex_thread_id='')
                self.log.info('cleared Codex thread for session %s after invalid encrypted content', active.session.id)
            if text and self._last_model_output(active.run_id) != text:
                self._add_model_message(active, text, phase='error')
            self.registry.update_run_and_mark_card_dirty(
                active.run_id,
                state='failed',
                status=failure_status('Codex error', text),
                status_phase='failed',
                error=text,
                finished_at=int(time.time()),
            )
            self._publish_status(active, force=True, create=True)

    def _add_model_message(self, active: ActiveRun, text: str, *, phase: str) -> None:
        text = text.strip()
        if not text:
            return
        self.registry.append_run_event(active.run_id, 'agent_message', {'text': text, 'phase': phase})
        if phase == 'final_answer':
            self.registry.update_run(active.run_id, final_message_text=text)
        self.registry.mark_card_dirty(active.run_id)

    def _add_tool(self, active: ActiveRun, tool: str, *, item_id: str = '', detail: str = '') -> None:
        self.registry.append_run_event(
            active.run_id,
            'tool_started',
            {'tool': tool, 'item_id': item_id, 'detail': detail},
        )
        self.registry.mark_card_dirty(active.run_id)

    def _finish_tool(self, active: ActiveRun, item_id: str, *, failed: bool = False) -> None:
        if not item_id:
            return
        self.registry.append_run_event(
            active.run_id,
            'tool_completed',
            {'item_id': item_id, 'failed': failed},
        )
        self.registry.mark_card_dirty(active.run_id)

    def _queue_final_once(self, active: ActiveRun, final_text: str) -> bool:
        final_text = final_text.strip()
        if not final_text or active.handoff_child_session_id is not None:
            return False
        run = self.registry.get_run(active.run_id)
        if run is None:
            return False
        self.registry.update_run(active.run_id, final_message_text=final_text)
        if is_web_run(run):
            return True
        self.registry.upsert_outbox(
            kind='final_reply',
            dedupe_key=f'run:{active.run_id}:final',
            run_id=active.run_id,
            replace_sent=False,
            payload={
                'chat_id': active.session.chat_id,
                'session_kind': active.session.kind,
                'source_message_id': run.source_message_id,
                'text': final_text,
                'reply_in_thread': self.config.feishu.child_reply_in_thread,
            },
        )
        self._drain_feishu_outbox()
        return True

    def _publish_status(self, active: ActiveRun, *, force: bool = False, create: bool = True) -> None:
        run = self.registry.get_run(active.run_id)
        if run is None:
            return
        if not create and not run.status_message_id:
            return
        self.registry.mark_card_dirty(active.run_id)
        if not force and self._should_defer_status_publish(run):
            return
        self._reconcile_dirty_cards(run_id=active.run_id)
        self._drain_feishu_outbox()

    def _publish_status_for_run(self, run_id: int, *, force: bool = False) -> None:
        self.registry.mark_card_dirty(run_id)
        run = self.registry.get_run(run_id)
        if run is not None and not force and self._should_defer_status_publish(run):
            return
        self._reconcile_dirty_cards(run_id=run_id)
        self._drain_feishu_outbox()

    def _should_defer_status_publish(self, run: RunRecord) -> bool:
        if run.status_phase != 'running':
            return False
        projection = self.registry.get_card_projection(run.id)
        remote_message_id = run.status_message_id
        last_rendered_at = 0
        if projection is not None:
            remote_message_id = remote_message_id or str(projection['remote_message_id'] or '')
            last_rendered_at = int(projection['last_rendered_at'] or 0)
        if not remote_message_id or not last_rendered_at:
            return False
        return int(time.time()) - last_rendered_at < STATUS_UPDATE_MIN_INTERVAL_SECONDS

    def _recover_stale_runs(self) -> None:
        now = int(time.time())
        with self._active_lock:
            active_session_ids = set(self._active_runs)
        for run in self.registry.list_stale_active_runs(now=now):
            if run.session_id in active_session_ids:
                continue
            if run.final_message_text and run.final_message_sent_at is not None:
                self.registry.update_run_and_mark_card_dirty(
                    run.id,
                    state='succeeded',
                    status='完成',
                    status_phase='done',
                    error='',
                    finished_at=now,
                )
                continue
            self.registry.update_run_and_mark_card_dirty(
                run.id,
                state='interrupted',
                status='agentd 重启后检测到运行中 turn 已失去控制',
                status_phase='stopped',
                error='run lease expired after daemon restart',
                finished_at=now,
            )
            self.registry.append_run_event(
                run.id,
                'agent_message',
                {
                    'phase': 'error',
                    'text': 'agentd 重启后无法重新附着到正在执行的 Codex turn，已将本次运行标记为中断。',
                },
            )
            self.registry.mark_card_dirty(run.id)

    def _reconcile_dirty_cards(self, *, run_id: int | None = None, limit: int = 20) -> None:
        runs = [self.registry.get_run(run_id)] if run_id is not None else self.registry.list_dirty_card_runs(limit)
        for run in runs:
            if run is None:
                continue
            view = self._load_run_view(run.id)
            if view is None:
                continue
            text = self._format_status_text(view)
            card = self._build_status_card(view)
            render_hash = self._card_render_hash(card)
            if is_web_run(run):
                self.registry.mark_card_enqueued(run.id, render_hash=render_hash)
                continue
            projection = self.registry.get_card_projection(run.id)
            remote_message_id = run.status_message_id
            if projection is not None and not remote_message_id:
                remote_message_id = str(projection['remote_message_id'] or '')
            if (
                projection is not None
                and remote_message_id
                and str(projection['last_render_hash'] or '') == render_hash
            ):
                self.registry.mark_card_enqueued(run.id, render_hash=render_hash)
                continue
            action = 'update' if remote_message_id else ('reply' if run.status_reply_in_thread else 'create')
            self.registry.upsert_outbox(
                kind='status_card',
                dedupe_key=f'run:{run.id}:status_card',
                run_id=run.id,
                replace_sent=True,
                payload={
                    'action': action,
                    'chat_id': view.session.chat_id,
                    'source_message_id': run.source_message_id,
                    'message_id': remote_message_id,
                    'reply_in_thread': self.config.feishu.child_reply_in_thread,
                    'card': card,
                    'text': text,
                    'render_hash': render_hash,
                },
            )
            self.registry.mark_card_enqueued(run.id, render_hash=render_hash)

    def _mark_changed_status_cards_dirty(self, *, limit: int = 40) -> None:
        for run in self.registry.list_runs(limit=limit):
            if is_web_run(run):
                continue
            projection = self.registry.get_card_projection(run.id)
            remote_message_id = run.status_message_id
            if projection is not None and not remote_message_id:
                remote_message_id = str(projection['remote_message_id'] or '')
            if not remote_message_id:
                continue
            view = self._load_run_view(run.id)
            if view is None:
                continue
            render_hash = self._card_render_hash(self._build_status_card(view))
            if projection is None or str(projection['last_render_hash'] or '') != render_hash:
                self.registry.mark_card_dirty(run.id)

    @staticmethod
    def _card_render_hash(card: dict[str, Any]) -> str:
        raw = json.dumps(card, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(raw).hexdigest()

    def _drain_feishu_outbox(self, *, limit: int = 20) -> None:
        if not self._outbox_lock.acquire(blocking=False):
            return
        try:
            for item in self.registry.claim_pending_outbox(limit):
                self._wait_for_feishu_send_slot()
                try:
                    remote_message_id = self._send_feishu_outbox_item(item)
                    if item.kind == 'status_card' and item.run_id is not None:
                        self.registry.mark_card_sent(
                            item.run_id,
                            remote_message_id=remote_message_id,
                            render_hash=str(item.payload.get('render_hash') or ''),
                        )
                    elif item.kind == 'final_reply' and item.run_id is not None:
                        self.registry.update_run(item.run_id, final_message_sent_at=int(time.time()))
                    self.registry.finish_outbox(item.id, sent=True)
                except Exception as exc:
                    self.log.exception('failed to send Feishu outbox item %s', item.id)
                    if item.kind == 'status_card' and item.run_id is not None:
                        self.registry.mark_card_error(item.run_id, str(exc))
                    self.registry.finish_outbox(item.id, sent=False, error=str(exc))
                finally:
                    self._last_feishu_send_at = time.monotonic()
        finally:
            self._outbox_lock.release()

    def _wait_for_feishu_send_slot(self) -> None:
        if self.dry_send:
            return
        wait_seconds = self._last_feishu_send_at + FEISHU_SEND_MIN_INTERVAL_SECONDS - time.monotonic()
        if wait_seconds > 0:
            time.sleep(wait_seconds)

    def _send_feishu_outbox_item(self, item: FeishuOutboxItem) -> str:
        payload = item.payload
        if item.kind == 'status_card':
            card = payload.get('card') if isinstance(payload.get('card'), dict) else {}
            action = str(payload.get('action') or 'create')
            message_id = str(payload.get('message_id') or '')
            if self.dry_send:
                print(str(payload.get('text') or ''))
                return message_id or 'dry-run-status'
            if action == 'update' and message_id:
                result = self.feishu.update_interactive(message_id, card)
                return message_id_from_result(result) or message_id
            if action == 'reply':
                result = self.feishu.reply_interactive(
                    str(payload.get('source_message_id') or ''),
                    card,
                    reply_in_thread=bool(payload.get('reply_in_thread')),
                )
            else:
                result = self.feishu.send_interactive(str(payload.get('chat_id') or ''), card)
            return message_id_from_result(result) or message_id

        if item.kind == 'final_reply':
            text = str(payload.get('text') or '')
            if self.dry_send:
                print(text)
                return ''
            width_mode = final_message_card_width_mode(text)
            session_kind = str(payload.get('session_kind') or '')
            if session_kind in {'main', 'schedule'}:
                result = self.feishu.send_markdown(str(payload.get('chat_id') or ''), text, width_mode=width_mode)
            else:
                result = self.feishu.reply_markdown(
                    str(payload.get('source_message_id') or ''),
                    text,
                    reply_in_thread=bool(payload.get('reply_in_thread')),
                    width_mode=width_mode,
                )
            return message_id_from_result(result)

        raise ValueError(f'unknown Feishu outbox kind: {item.kind}')

    def _load_run_view(self, run_id: int) -> RunView | None:
        run = self.registry.get_run(run_id)
        if run is None:
            return None
        session = self.registry.get_session(run.session_id)
        if session is None:
            return None
        iterations, running_tools, tool_details, model_outputs = self._project_run_events(
            self.registry.list_run_events(run_id)
        )
        return RunView(
            run=run,
            session=session,
            iterations=iterations,
            running_tools=running_tools,
            tool_details=tool_details,
            model_outputs=model_outputs,
        )

    @staticmethod
    def _project_run_events(
        events: list[RunEvent],
    ) -> tuple[list[RunIteration], dict[str, str], list[str], list[str]]:
        iterations: list[RunIteration] = []
        running_tools: dict[str, str] = {}
        tool_details: list[str] = []
        model_outputs: list[str] = []

        def current_iteration() -> RunIteration:
            if not iterations:
                iterations.append(RunIteration(message='准备中', phase='system'))
            return iterations[-1]

        for event in events:
            payload = event.payload
            if event.event_type == 'agent_message':
                text = str(payload.get('text') or '').strip()
                if text:
                    iterations.append(RunIteration(message=text, phase=str(payload.get('phase') or 'commentary')))
                    model_outputs.append(text)
            elif event.event_type == 'tool_started':
                tool = str(payload.get('tool') or 'Tool')
                item_id = str(payload.get('item_id') or '')
                detail = str(payload.get('detail') or '')
                iteration = current_iteration()
                iteration.tool_counts[tool] = iteration.tool_counts.get(tool, 0) + 1
                if item_id:
                    running_tools[item_id] = tool
                    iteration.running_tools[tool] = iteration.running_tools.get(tool, 0) + 1
                if detail:
                    tool_detail = f'{tool}: {detail}'
                    iteration.tool_details.append(tool_detail)
                    tool_details.append(compact(tool_detail, 220))
                    tool_details = tool_details[-80:]
            elif event.event_type == 'tool_completed':
                item_id = str(payload.get('item_id') or '')
                tool = running_tools.pop(item_id, '') if item_id else ''
                if not tool:
                    continue
                for iteration in reversed(iterations):
                    if iteration.running_tools.get(tool, 0) > 0:
                        iteration.running_tools[tool] -= 1
                        if iteration.running_tools[tool] <= 0:
                            iteration.running_tools.pop(tool, None)
                        if payload.get('failed'):
                            iteration.failed_tool_counts[tool] = iteration.failed_tool_counts.get(tool, 0) + 1
                        break
        return iterations, running_tools, tool_details, model_outputs

    def _last_model_output(self, run_id: int) -> str:
        for event in reversed(self.registry.list_run_events(run_id)):
            if event.event_type == 'agent_message':
                return str(event.payload.get('text') or '').strip()
        return ''

    def _format_status_text(self, active: RunView) -> str:
        elapsed = self._elapsed_seconds(active)
        lines = [
            f'Codex {self._phase_label(active.run.status_phase)}: {active.run.status}',
            self._run_line(active, elapsed),
            self._toggle_state_line(active),
        ]
        if active.run.status_phase == 'failed' and active.run.error:
            lines.append(f'错误信息: {compact(active.run.error, 1200)}')
        iterations = self._visible_iterations(active)
        offset = max(0, len(active.iterations) - len(iterations))
        for index, iteration in enumerate(iterations, start=offset + 1):
            lines.extend(self._iteration_lines(active, index, iteration))
        return '\n'.join(lines)

    def _build_status_card(self, active: RunView) -> dict[str, Any]:
        elapsed = self._elapsed_seconds(active)
        template = {
            'running': 'blue',
            'done': 'green',
            'stopped': 'orange',
            'failed': 'red',
        }.get(active.run.status_phase, 'blue')
        title = status_title(active)

        return {
            'config': {'wide_screen_mode': True, 'update_multi': True},
            'header': {
                'template': template,
                'title': {'tag': 'plain_text', 'content': title},
            },
            'elements': [
                {'tag': 'div', 'text': {'tag': 'lark_md', 'content': self._status_line(active, elapsed)}},
                *self._error_elements(active),
                {'tag': 'hr'},
                *self._view_elements(active),
                {'tag': 'action', 'actions': self._card_actions(active)},
            ],
        }

    def _status_line(self, active: RunView, elapsed: int) -> str:
        status = escape_lark_md(active.run.status)
        return f'**状态**：{self._phase_label(active.run.status_phase)} · {status}\n{self._run_line(active, elapsed)}'

    def _run_line(self, active: RunView, elapsed: int) -> str:
        return f'{session_label(active.session)} · {active.run.host} · {active.session.cwd} · {format_elapsed(elapsed)}'

    def _view_elements(self, active: RunView) -> list[dict[str, Any]]:
        return [{'tag': 'div', 'text': {'tag': 'lark_md', 'content': self._iterations_view(active)}}]

    def _error_elements(self, active: RunView) -> list[dict[str, Any]]:
        if active.run.status_phase != 'failed' or not active.run.error:
            return []
        content = '**错误信息**\n' + escape_lark_md(compact(active.run.error, 1800))
        return [{'tag': 'div', 'text': {'tag': 'lark_md', 'content': content}}]

    def _iterations_view(self, active: RunView) -> str:
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

    def _tools_view(self, active: RunView) -> str:
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

    def _output_view(self, active: RunView) -> str:
        if not active.model_outputs:
            return '**模型输出**\n还没有模型输出。'
        parts = []
        for index, text in enumerate(active.model_outputs[-8:], start=max(1, len(active.model_outputs) - 7)):
            parts.append(f'{index}. {escape_lark_md(compact(text, 500))}')
        return '**模型输出**\n' + '\n\n'.join(parts)

    def _visible_iterations(self, active: RunView) -> list[RunIteration]:
        if active.run.hide_early_iterations:
            return active.iterations[-6:]
        return active.iterations

    def _iteration_lines(self, active: RunView, index: int, iteration: RunIteration) -> list[str]:
        message = self._display_text(active, iteration.message, truncated_limit=150, expanded_limit=4000)
        lines = [f'{index}. 💬 {escape_lark_md(message)}']
        tools = format_tool_counts(iteration)
        if tools:
            lines.append(f'   🛠 {escape_lark_md(tools)}')
        if active.run.show_tool_details and iteration.tool_details:
            for detail in iteration.tool_details:
                text = self._display_text(active, detail, truncated_limit=180, expanded_limit=4000)
                lines.append(f'   🔧 {escape_lark_md(text)}')
        return lines

    @staticmethod
    def _display_text(active: RunView, text: object, *, truncated_limit: int, expanded_limit: int) -> str:
        return compact(text, truncated_limit if active.run.truncate_content else expanded_limit)

    @staticmethod
    def _elapsed_seconds(active: RunView) -> int:
        end = active.run.finished_at if active.run.finished_at is not None else int(time.time())
        return max(0, int(end - active.run.started_at))

    @staticmethod
    def _toggle_state_line(active: RunView) -> str:
        early = '隐藏' if active.run.hide_early_iterations else '显示'
        tools = '展开' if active.run.show_tool_details else '摘要'
        truncate = '开' if active.run.truncate_content else '关'
        return f'早期：{early} · 工具：{tools} · 截断：{truncate}'

    def _card_actions(self, active: RunView) -> list[dict[str, Any]]:
        actions: list[tuple[str, str, str]] = []
        if active.run.status_phase == 'running':
            actions.append(('停止', 'danger', 'stop'))
        actions.extend(
            [
                (f'早期：{"隐藏" if active.run.hide_early_iterations else "显示"}', 'default', 'toggle_early'),
                (f'工具：{"展开" if active.run.show_tool_details else "摘要"}', 'default', 'toggle_tools'),
                (f'截断：{"开" if active.run.truncate_content else "关"}', 'default', 'toggle_truncate'),
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
                    'message_id': active.run.status_message_id,
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

    def _handle_legacy_view_action(self, run_id: int, view: str) -> None:
        if view == 'live':
            self.registry.update_run(
                run_id,
                hide_early_iterations=True,
                show_tool_details=False,
                truncate_content=True,
            )
        elif view == 'history':
            self.registry.update_run(run_id, hide_early_iterations=False)
        elif view == 'tools':
            self.registry.update_run(run_id, show_tool_details=True)
        elif view == 'output':
            self.registry.update_run(run_id, truncate_content=False)

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
        prompt_files = ', '.join(file.label for file in resolved.prompt_files) or 'none'
        lines = [
            'You are running inside agentd, a Feishu-to-Codex control plane.',
            '',
            'Runtime context:',
            f'- session_kind: {active.session.kind}',
            f'- agentd_session_id: {active.session.id}',
            f'- config_path: {self.config.config_path}',
            f'- home_dir: {self.config.home_dir}',
            f'- source_dir: {self.config.source_dir}',
            f'- state_dir: {self.config.state_dir}',
            f'- workspace: {self.config.workspace}',
            f'- cwd: {active.session.cwd}',
            f'- context_dir: {self.config.context.context_dir}',
            f'- context_profile: {resolved.profile.name}',
            f'- context_config_path: {self.config.context.path}',
            f'- profiles_available: {", ".join(sorted(self.config.context.profiles))}',
            f'- memory_dir: {resolved.memory_dir}',
            f'- prompt_files: {prompt_files}',
            f'- injected_skills: {injected_skills}',
            '',
            'Agentd contract:',
            '- Agentd sends your final answer back to Feishu. Do not call Feishu send/reply commands yourself unless the user explicitly asks you to send an additional proactive message.',
            '- For agentd service status, logs, health checks, start, stop, or restart, use `"$AGENTD_CLI" service ...`.',
            '- Agentd persists run, card, and final-reply state across restarts. Use `"$AGENTD_CLI" service restart --defer` when you want to avoid interrupting the current turn.',
            '- If you will handle substantial work in this session, set a concise task title once early with `"$AGENTD_CLI" set-title "<title>"`.',
            '- If you delegate, do not call `set-title` in the parent session. Run `"$AGENTD_CLI" spawn-child --cwd <dir> --title <short title> [--profile <profile>] [--skills a,b]` with the full child task piped on stdin, then stop without sending a final answer.',
            '- Example delegation command: `printf %s "$child_task" | "$AGENTD_CLI" spawn-child --cwd /path/to/work --title "short title" --skills bookkeeping,calendar`.',
            '',
            'Context policy:',
            '- Treat the injected agentd context files below as persistent user-managed context.',
            '- The current user request takes precedence over older context or memory if they conflict.',
            '- MEMORY.md is injected as an index. For deeper prior work, preferences, decisions, dates, people, or todos, search memory files with `rg` first and load only relevant snippets.',
            '- Only the injected skills are enabled in this Codex run. Read a SKILL.md only when its description clearly matches the task.',
            '- To use another profile or skill set, delegate with `--profile <profile>` or `--skills a,b`.',
        ]
        if resolved.missing_skills:
            lines.append(f'- missing_skills: {", ".join(resolved.missing_skills)}')
        if resolved.prompt_files:
            lines.extend(
                [
                    '',
                    'Agentd context files:',
                    'The following files are injected by agentd from the configured context directory.',
                ]
            )
            for file in resolved.prompt_files:
                lines.extend(
                    [
                        '',
                        f'## {file.label}',
                        f'Path: {file.path}',
                        '<agentd_context_file>',
                        file.text,
                        '</agentd_context_file>',
                    ]
                )
        return '\n'.join(lines)

    def _codex_extra_env(self, active: ActiveRun) -> dict[str, str]:
        run = self.registry.get_run(active.run_id)
        source_message_id = run.source_message_id if run else ''
        status_message_id = run.status_message_id if run else ''
        context_profile = run.context_profile if run else ''
        context_skills = run.skills if run else ()
        return {
            'AGENTD_CLI': str(self.config.executable),
            'AGENTD_CONFIG': str(self.config.config_path),
            'AGENTD_SESSION_ID': str(active.session.id),
            'AGENTD_CHAT_ID': active.session.chat_id,
            'AGENTD_SOURCE_MESSAGE_ID': source_message_id,
            'AGENTD_STATUS_MESSAGE_ID': status_message_id,
            'AGENTD_SESSION_KIND': active.session.kind,
            'AGENTD_CWD': active.session.cwd,
            'AGENTD_CONTEXT_PROFILE': context_profile,
            'AGENTD_CONTEXT_SKILLS': ','.join(context_skills),
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


def is_web_run(run: RunRecord) -> bool:
    return str(run.source_message_id).startswith('web-')


def session_label(session: AgentSession) -> str:
    if session.kind == 'main':
        return '主会话'
    if session.kind == 'child':
        return '话题会话'
    if session.kind == 'schedule':
        return '定时会话'
    return session.kind or '会话'


def status_title(active: RunView) -> str:
    icon = '🌿' if active.session.kind == 'child' else '🧵'
    title = active.run.display_title or ('子任务' if active.session.kind == 'child' else '主任务')
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
