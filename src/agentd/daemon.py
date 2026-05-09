from __future__ import annotations

import hashlib
import json
import logging
import socket
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .active_run import ActiveRun
from .agent_core import AgentCore, message_from_control_command
from .channel_utils import channel_from_message, conversation_ref_from_message
from .channels.base import ControlCommand, DeliveryRequest
from .channels.delivery import (
    ChannelBinding,
    binding_from_run,
    channel_from_legacy_run,
    final_reply_delivery,
    status_delivery,
)
from .channels.feishu import FeishuChannelAdapter
from .config import AgentdConfig
from .context import ContextResolver, ResolvedContext
from .delivery_dispatcher import DeliveryDispatcher
from .feishu import FeishuApi, FeishuListener
from .models import (
    AgentSession,
    CardAction,
    IncomingMessage,
    RunRecord,
    SpawnRequest,
    TitleRequest,
)
from .registry import Registry
from .run_context import RunContextBuilder, RunnerContextBuilder
from .run_executor import (
    RunExecutor,
    failure_status,
    friendly_tool_name,
    has_invalid_encrypted_content,
    is_retryable_runner_error_event,
    runner_failure_status,
)
from .run_projection import RunIteration, RunView, last_model_output, load_run_view, project_run_events
from .runner_factory import create_runner
from .schedule import ScheduleConfig, ScheduleJob, due_run_key, load_schedule_config
from .spawn_coordinator import SpawnCoordinator
from .spawn_coordinator import spawn_request_mode as spawn_request_mode
from .status_rendering import (
    build_status_card,
    format_status_text,
    iteration_lines,
    output_view,
    phase_label,
    toggle_state_line,
    tools_view,
    view_label,
    visible_iterations,
)
from .title import normalize_title

STATUS_TICK_SECONDS = 5
STATUS_UPDATE_MIN_INTERVAL_SECONDS = 1
TERMINAL_STATUS_REPLAY_DELAY_SECONDS = 3
TERMINAL_STATUS_PHASES = {'done', 'failed', 'stopped'}
DEFERRED_RESTART_TERMINAL_GRACE_SECONDS = TERMINAL_STATUS_REPLAY_DELAY_SECONDS + 2
SCHEDULE_POLL_SECONDS = 30

__all__ = [
    'ActiveRun',
    'AgentDaemon',
    'RunIteration',
    'RunView',
    'codex_failure_status',
    'failure_status',
    'friendly_tool_name',
    'has_invalid_encrypted_content',
    'is_retryable_codex_error_event',
    'message_from_control_command',
]


@dataclass(frozen=True)
class ScheduleFileFingerprint:
    exists: bool
    mtime_ns: int = 0
    size: int = 0
    digest: str = ''


class AgentDaemon:
    def __init__(self, config: AgentdConfig, *, dry_send: bool = False) -> None:
        self.config = config
        self.dry_send = dry_send
        self.registry = Registry(config.db_path)
        self.log = logging.getLogger('agentd')
        self.feishu = FeishuApi(config.feishu)
        self.delivery_dispatcher = DeliveryDispatcher(
            registry=self.registry,
            feishu=self.feishu,
            log=self.log,
            dry_send=dry_send,
        )
        self.runner = create_runner(config)
        self.context_resolver = ContextResolver(config.context, config.workspace)
        self.run_context_builder = RunContextBuilder(
            config=config,
            context_resolver=self.context_resolver,
            runner_kind=self.runner.kind,
            runner_label=self.runner.label,
        )
        self.runner_context_builder = RunnerContextBuilder(self.run_context_builder)
        self.spawn_coordinator = SpawnCoordinator(self)
        self.core = AgentCore(self)
        self.run_executor = RunExecutor(self)
        self.host = socket.gethostname()
        self._active_lock = threading.Lock()
        self._active_runs: dict[int, ActiveRun] = {}
        self._spawn_watcher_started = False
        self._spawn_watcher_lock = threading.Lock()
        self._scheduler_started = False
        self._scheduler_lock = threading.Lock()
        self._schedule_config_lock = threading.Lock()
        self._schedule_fingerprint = self._schedule_file_fingerprint(config.schedules.path)
        self._web_gateway: Any | None = None
        self._web_gateway_lock = threading.Lock()

    @property
    def feishu(self) -> FeishuApi:
        return self._feishu

    @feishu.setter
    def feishu(self, value: FeishuApi) -> None:
        self._feishu = value
        if hasattr(self, 'delivery_dispatcher'):
            self.delivery_dispatcher.feishu = value

    @property
    def _last_feishu_send_at(self) -> float:
        return self.delivery_dispatcher.last_feishu_send_at

    @_last_feishu_send_at.setter
    def _last_feishu_send_at(self, value: float) -> None:
        self.delivery_dispatcher.last_feishu_send_at = value

    def serve(self) -> None:
        self._reset_abandoned_outbox_on_startup()
        self._ensure_spawn_watcher()
        self._ensure_scheduler()
        self._ensure_web_gateway()
        self._recover_stale_runs()
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
        if self.registry.has_recently_finished_run(within_seconds=DEFERRED_RESTART_TERMINAL_GRACE_SECONDS):
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

    def _reset_abandoned_outbox_on_startup(self) -> None:
        self.registry.reset_stuck_outbox(older_than_seconds=0)

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
        with self._scheduler_lock:
            if self._scheduler_started:
                return
            self._scheduler_started = True
            threading.Thread(target=self._schedule_watcher, name='agentd-scheduler', daemon=True).start()

    def _schedule_watcher(self) -> None:
        while True:
            try:
                self._reload_schedule_config_if_changed()
                for job in self._schedule_jobs_snapshot():
                    self._maybe_start_scheduled_job(job)
            except Exception:
                self.log.exception('failed while polling scheduled jobs')
            time.sleep(SCHEDULE_POLL_SECONDS)

    def _schedule_jobs_snapshot(self) -> tuple[ScheduleJob, ...]:
        with self._schedule_config_lock:
            return self.config.schedules.jobs

    def _reload_schedule_config_if_changed(self, now: datetime | None = None) -> bool:
        path = self.config.schedules.path
        fingerprint = self._schedule_file_fingerprint(path)
        with self._schedule_config_lock:
            if fingerprint == self._schedule_fingerprint:
                return False
            previous = self.config.schedules

        loaded = load_schedule_config(path)
        self._suppress_reloaded_daily_backfill(previous, loaded, now=now)

        with self._schedule_config_lock:
            self.config = replace(self.config, schedules=loaded)
            self._schedule_fingerprint = fingerprint

        enabled_count = sum(1 for job in loaded.jobs if job.enabled)
        self.log.info('reloaded schedule config %s: %s jobs, %s enabled', path, len(loaded.jobs), enabled_count)
        return True

    def _suppress_reloaded_daily_backfill(
        self,
        previous: ScheduleConfig,
        loaded: ScheduleConfig,
        *,
        now: datetime | None = None,
    ) -> None:
        previous_by_id = {job.id: job for job in previous.jobs}
        now = now or datetime.now(tz=ZoneInfo('UTC'))
        for job in loaded.jobs:
            if not self._daily_reload_backfill_guard_required(previous_by_id.get(job.id), job, now):
                continue
            run_key = due_run_key(job, now)
            if run_key and self.registry.claim_schedule_run(job.id, run_key):
                self.log.info('marked newly due daily schedule %s run %s as already handled after reload', job.id, run_key)

    def _daily_reload_backfill_guard_required(
        self,
        previous: ScheduleJob | None,
        job: ScheduleJob,
        now: datetime,
    ) -> bool:
        if not job.enabled or job.kind != 'daily' or not job.chat_id or not job.prompt:
            return False
        if not due_run_key(job, now):
            return False
        if previous is None:
            return True
        if not previous.enabled or previous.kind != 'daily' or not previous.chat_id or not previous.prompt:
            return True
        return self._daily_schedule_key(previous) != self._daily_schedule_key(job)

    @staticmethod
    def _daily_schedule_key(job: ScheduleJob) -> tuple[str, str, str]:
        return (job.kind, job.timezone, job.time)

    @staticmethod
    def _schedule_file_fingerprint(path: Path) -> ScheduleFileFingerprint:
        try:
            data = path.read_bytes()
            stat = path.stat()
        except FileNotFoundError:
            return ScheduleFileFingerprint(exists=False)
        return ScheduleFileFingerprint(
            exists=True,
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
            digest=hashlib.sha256(data).hexdigest(),
        )

    def _maybe_start_scheduled_job(self, job: ScheduleJob, now: datetime | None = None) -> None:
        if not job.enabled:
            return
        if not job.chat_id or not job.prompt:
            self.log.warning('scheduled job %s is missing chat_id or prompt', job.id)
            return
        if job.session == 'main':
            self._maybe_enqueue_main_scheduled_job(job, now=now)
            self._maybe_start_pending_main_scheduled_job(job)
            return

        run_key = due_run_key(job, now)
        if not run_key:
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

    def _maybe_enqueue_main_scheduled_job(self, job: ScheduleJob, *, now: datetime | None = None) -> None:
        run_key = due_run_key(job, now)
        if not run_key:
            return
        if not self.registry.claim_schedule_run(job.id, run_key):
            return
        if self.registry.enqueue_pending_schedule_run(job.id, run_key):
            self.log.info('queued scheduled job %s run %s for main session', job.id, run_key)

    def _maybe_start_pending_main_scheduled_job(self, job: ScheduleJob) -> None:
        run_key = self.registry.get_pending_schedule_run(job.id)
        if not run_key:
            return
        session = self.registry.get_main_session(
            job.chat_id,
            str(self.config.workspace),
            channel='feishu',
            conversation_ref=job.chat_id,
        )
        if self._active_for(session.id) is not None:
            self.log.info('scheduled job %s is pending but main session %s is already active', job.id, session.id)
            return
        if self.registry.get_active_run_for_session(session.id) is not None:
            self.log.info(
                'scheduled job %s is pending but main session %s has a persisted active run',
                job.id,
                session.id,
            )
            return
        self._start_scheduled_job(job, run_key, session)
        self.registry.finish_pending_schedule_run(job.id, run_key)

    def _start_scheduled_job(self, job: ScheduleJob, run_key: str, session: AgentSession) -> None:
        run = self.registry.create_run(
            session_id=session.id,
            source_message_id=f'schedule:{job.id}:{run_key}',
            prompt=self._build_scheduled_prompt(job, session, run_key),
            host=self.host,
            status='启动定时任务',
            subject='定时任务',
            display_title=normalize_title(job.title or job.name, fallback='定时任务'),
            runner_kind=self.runner.kind,
            context_profile=job.context_profile or self.config.context.default_profile,
            skills=job.skills,
        )
        active = ActiveRun(run_id=run.id, session=session, control=self.runner.new_control())
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
            self.log.info('card title updated before %s title for session %s: %s', self.runner.label, active.session.id, detail)
        self._publish_status(active, force=True, create=True)
        self.registry.finish_title_request(request.id, state='applied', error='' if ok else detail)

    def _handle_spawn_request(self, request: SpawnRequest) -> None:
        self._spawn_coordinator().handle_spawn_request(request)

    def _reject_spawn_request(self, active: ActiveRun, request: SpawnRequest, reason: str) -> None:
        self._spawn_coordinator().reject_spawn_request(active, request, reason)

    def _create_child_thread(
        self, parent: RunRecord, request: SpawnRequest, *, mode: str = 'handoff'
    ) -> tuple[str, str]:
        return self._spawn_coordinator().create_child_thread(parent, request, mode=mode)

    def _reply_child_intro_in_thread(
        self,
        message_id: str,
        request: SpawnRequest,
        *,
        sender_open_id: str = '',
        mode: str = 'handoff',
        fallback_thread_id: str = '',
    ) -> tuple[str, str]:
        return self._spawn_coordinator().reply_child_intro_in_thread(
            message_id,
            request,
            sender_open_id=sender_open_id,
            mode=mode,
            fallback_thread_id=fallback_thread_id,
        )

    def _build_child_intro_card(
        self, request: SpawnRequest, *, sender_open_id: str = '', mode: str = 'handoff'
    ) -> dict[str, Any]:
        return self._spawn_coordinator().build_child_intro_card(request, sender_open_id=sender_open_id, mode=mode)

    def _spawn_coordinator(self) -> SpawnCoordinator:
        coordinator = getattr(self, 'spawn_coordinator', None)
        if coordinator is None:
            coordinator = SpawnCoordinator(self)
            self.spawn_coordinator = coordinator
        return coordinator

    def handle_message(self, message: IncomingMessage) -> str:
        adapter = FeishuChannelAdapter()
        command = adapter.submit_message(adapter.envelope_from_message(message))
        return self.handle_control_command(command)

    def handle_control_command(self, command: ControlCommand) -> str:
        return self.core.handle_control_command(command)

    def _handle_submit_message(self, message: IncomingMessage) -> str:
        return self.core.handle_submit_message(message)

    def handle_card_action(self, action: CardAction) -> str:
        return self.core.handle_card_action(action)

    def _run_for_card_action(self, action: CardAction) -> tuple[RunRecord | None, ActiveRun | None]:
        return self.core.run_for_card_action(action)

    def _run_turn_worker(self, active: ActiveRun) -> None:
        self.run_executor.run_turn_worker(active)

    def _active_for(self, session_id: int) -> ActiveRun | None:
        with self._active_lock:
            return self._active_runs.get(session_id)

    def _status_ticker(self, active: ActiveRun) -> None:
        self.run_executor.status_ticker(active, tick_seconds=STATUS_TICK_SECONDS)

    def _handle_live_input(self, active: ActiveRun, message: IncomingMessage) -> str:
        return self.core.handle_live_input(active, message)

    def _handle_live_branch_command(self, active: ActiveRun, message: IncomingMessage, command: dict[str, str]) -> str:
        return self.core.handle_live_branch_command(active, message, command)

    def _handle_codex_event(self, active: ActiveRun, event: dict[str, Any]) -> None:
        self.run_executor.handle_runner_event(active, event)

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

    def _binding_from_run(self, run: RunRecord, session: AgentSession) -> ChannelBinding:
        return binding_from_run(run, session, self.registry.get_channel_binding(session.id))

    def _dispatch_delivery(
        self,
        delivery: DeliveryRequest,
        *,
        run_id: int,
        replace_sent: bool = True,
    ) -> None:
        self.delivery_dispatcher.dispatch(delivery, run_id=run_id, replace_sent=replace_sent)

    def _queue_final_once(self, active: ActiveRun, final_text: str) -> bool:
        final_text = final_text.strip()
        if not final_text or active.handoff_child_session_id is not None:
            return False
        run = self.registry.get_run(active.run_id)
        if run is None:
            return False
        self.registry.update_run(active.run_id, final_message_text=final_text)
        binding = self._binding_from_run(run, active.session)
        delivery = final_reply_delivery(
            binding,
            text=final_text,
            feishu_reply_in_thread=self.config.feishu.child_reply_in_thread,
        )
        self._dispatch_delivery(
            delivery,
            run_id=active.run_id,
            replace_sent=False,
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

    def _publish_status_for_run(self, run_id: int, *, force: bool = False, resend: bool = False) -> None:
        self.registry.mark_card_dirty(run_id)
        run = self.registry.get_run(run_id)
        if run is not None and not force and self._should_defer_status_publish(run):
            return
        self._reconcile_dirty_cards(run_id=run_id, resend=resend)
        self._drain_feishu_outbox()

    def _schedule_terminal_status_replay(self, run_id: int) -> None:
        def replay() -> None:
            time.sleep(TERMINAL_STATUS_REPLAY_DELAY_SECONDS)
            self._replay_terminal_status_card(run_id)

        threading.Thread(target=replay, name=f'agentd-terminal-card-replay-{run_id}', daemon=True).start()

    def _replay_terminal_status_card(self, run_id: int) -> None:
        run = self.registry.get_run(run_id)
        if run is None or run.status_phase not in TERMINAL_STATUS_PHASES:
            return
        self._publish_status_for_run(run_id, force=True, resend=True)

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
                    'text': 'agentd 重启后无法重新附着到正在执行的 agent turn，已将本次运行标记为中断。',
                },
            )
            self.registry.mark_card_dirty(run.id)

    def _reconcile_dirty_cards(self, *, run_id: int | None = None, limit: int = 20, resend: bool = False) -> None:
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
            projection = self.registry.get_card_projection(run.id)
            remote_message_id = run.status_message_id
            if projection is not None and not remote_message_id:
                remote_message_id = str(projection['remote_message_id'] or '')
            if (
                projection is not None
                and remote_message_id
                and str(projection['last_render_hash'] or '') == render_hash
                and not resend
            ):
                self.registry.mark_card_enqueued(run.id, render_hash=render_hash)
                continue
            delivery = status_delivery(
                self._binding_from_run(run, view.session),
                text=text,
                card=card,
                render_hash=render_hash,
                remote_message_ref=remote_message_id,
                status_reply_in_thread=run.status_reply_in_thread,
                feishu_reply_in_thread=self.config.feishu.child_reply_in_thread,
            )
            self._dispatch_delivery(
                delivery,
                run_id=run.id,
                replace_sent=True,
            )
            self.registry.mark_card_enqueued(run.id, render_hash=render_hash)

    @staticmethod
    def _card_render_hash(card: dict[str, Any]) -> str:
        raw = json.dumps(card, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(raw).hexdigest()

    def _drain_feishu_outbox(self, *, limit: int = 20) -> None:
        self.delivery_dispatcher.drain_feishu_outbox(limit=limit)

    def _wait_for_feishu_send_slot(self) -> None:
        self.delivery_dispatcher.wait_for_feishu_send_slot()

    def _load_run_view(self, run_id: int) -> RunView | None:
        return load_run_view(self.registry, run_id)

    @staticmethod
    def _project_run_events(
        events: list[Any],
    ) -> tuple[list[RunIteration], dict[str, str], list[str], list[str]]:
        return project_run_events(events)

    def _last_model_output(self, run_id: int) -> str:
        return last_model_output(self.registry, run_id)

    def _format_status_text(self, active: RunView) -> str:
        return format_status_text(active)

    def _build_status_card(self, active: RunView) -> dict[str, Any]:
        return build_status_card(active)

    def _iterations_view(self, active: RunView) -> str:
        from .status_rendering import iterations_view

        return iterations_view(active)

    def _tools_view(self, active: RunView) -> str:
        return tools_view(active)

    def _output_view(self, active: RunView) -> str:
        return output_view(active)

    def _visible_iterations(self, active: RunView) -> list[RunIteration]:
        return visible_iterations(active)

    def _iteration_lines(self, active: RunView, index: int, iteration: RunIteration) -> list[str]:
        return iteration_lines(active, index, iteration)

    @staticmethod
    def _display_text(active: RunView, text: object, *, truncated_limit: int, expanded_limit: int) -> str:
        from .status_rendering import display_text

        return display_text(active, text, truncated_limit=truncated_limit, expanded_limit=expanded_limit)

    @staticmethod
    def _elapsed_seconds(active: RunView) -> int:
        from .status_rendering import elapsed_seconds

        return elapsed_seconds(active)

    @staticmethod
    def _toggle_state_line(active: RunView) -> str:
        return toggle_state_line(active)

    def _card_actions(self, active: RunView) -> list[dict[str, Any]]:
        from .status_rendering import card_actions

        return card_actions(active)

    @staticmethod
    def _phase_label(phase: str) -> str:
        return phase_label(phase)

    @staticmethod
    def _view_label(view: str) -> str:
        return view_label(view)

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
        channel = channel_from_message(message)
        conversation_ref = conversation_ref_from_message(message)
        if message.thread_id:
            session = self.registry.get_thread_session(message.chat_id, message.thread_id, channel=channel)
            if session is not None:
                return session
            return None
        return self.registry.get_main_session(
            message.chat_id,
            str(self.config.workspace),
            channel=channel,
            conversation_ref=conversation_ref,
        )

    def _build_prompt(self, message: IncomingMessage, session: AgentSession) -> str:
        return self.run_context_builder.message_prompt(message, session)

    def _build_child_prompt(self, request: SpawnRequest, session: AgentSession, source_message_id: str) -> str:
        return self.run_context_builder.child_prompt(request, session, source_message_id)

    def _build_scheduled_prompt(self, job: ScheduleJob, session: AgentSession, run_key: str) -> str:
        return self.run_context_builder.scheduled_prompt(job, session, run_key)

    def _developer_instructions(self, active: ActiveRun, resolved: ResolvedContext) -> str:
        return self.run_context_builder.developer_instructions(active, resolved)

    def _codex_extra_env(self, active: ActiveRun) -> dict[str, str]:
        run = self.registry.get_run(active.run_id)
        return self.run_context_builder.runner_env(active, run)


def is_web_run(run: RunRecord) -> bool:
    return channel_from_legacy_run(run) == 'web'


is_retryable_codex_error_event = is_retryable_runner_error_event
codex_failure_status = runner_failure_status
