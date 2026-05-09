from __future__ import annotations

import json
import time
from typing import Any

from .active_run import ActiveRun
from .run_projection import compact
from .runners import AgentTurnRequest
from .title import normalize_title


class RunExecutor:
    def __init__(self, owner: Any) -> None:
        self.owner = owner

    def run_turn_worker(self, active: ActiveRun) -> None:
        owner = self.owner
        session = active.session
        run = owner.registry.get_run(active.run_id)
        if run is None:
            active.done.set()
            return
        owner.registry.update_run(active.run_id, state='running', host=owner.host)
        resolved_context, extra_env, developer_instructions = owner.runner_context_builder.build(
            active=active,
            run=run,
        )
        try:
            result = owner.runner.start_turn(
                AgentTurnRequest(
                    session=session,
                    prompt=run.prompt,
                    extra_env=extra_env,
                    config_overrides=resolved_context.codex_config_overrides(),
                    developer_instructions=developer_instructions,
                ),
                event_sink=lambda event: self.handle_runner_event(active, event),
                control=active.control,
            )
        except Exception as exc:
            if active.handoff_child_session_id is not None:
                owner.log.info('parent %s run ended after handoff for session %s: %s', owner.runner.label, session.id, exc)
                active.done.set()
                return
            owner.log.exception('%s run failed for session %s', owner.runner.label, session.id)
            error_detail = str(exc)
            owner.registry.update_run_and_mark_card_dirty(
                active.run_id,
                state='failed',
                status=failure_status('运行失败', error_detail),
                status_phase='failed',
                error=error_detail,
                finished_at=int(time.time()),
            )
            owner._add_model_message(active, f'运行失败: {error_detail}', phase='error')
            owner._publish_status(active, force=True, create=True)
            owner._schedule_terminal_status_replay(active.run_id)
            active.done.set()
            with owner._active_lock:
                owner._active_runs.pop(session.id, None)
            return

        result_session_ref = result.session_ref or ''
        previous_session_ref = session.agent_session_ref
        if active.handoff_child_session_id is not None:
            if result_session_ref != previous_session_ref:
                owner.registry.update_runner_session(
                    session.id,
                    result_session_ref,
                    runner_kind=owner.runner.kind,
                )
            active.done.set()
            return

        if result_session_ref != previous_session_ref:
            owner.registry.update_runner_session(
                session.id,
                result_session_ref,
                runner_kind=owner.runner.kind,
            )
            fields: dict[str, object] = {
                'runner_kind': owner.runner.kind,
                'runner_session_ref': result_session_ref,
                'codex_thread_id': result_session_ref,
            }
            if result.turn_ref:
                fields['runner_turn_ref'] = result.turn_ref
                fields['turn_id'] = result.turn_ref
            owner.registry.update_run(active.run_id, **fields)
        elif previous_session_ref and (
            result.status == 'systemError' or has_invalid_encrypted_content(result.final_text)
        ):
            owner.registry.update_runner_session(session.id, '', runner_kind=owner.runner.kind)
            owner.registry.update_run(active.run_id, codex_thread_id='', runner_session_ref='')
        elif result.turn_ref:
            owner.registry.update_run(
                active.run_id,
                runner_kind=owner.runner.kind,
                turn_id=result.turn_ref,
                runner_turn_ref=result.turn_ref,
            )

        run = owner.registry.get_run(active.run_id)
        if result.final_text:
            final_text = result.final_text
        elif run and run.final_message_text:
            final_text = run.final_message_text
        elif result.status == 'interrupted':
            final_text = f'已停止当前 {owner.runner.label} turn。'
        else:
            final_text = f'{owner.runner.label} turn completed with status: {result.status}'

        if final_text and owner._last_model_output(active.run_id) != final_text:
            owner._add_model_message(active, final_text, phase='final_answer')
        if result.status == 'interrupted':
            owner.registry.update_run_and_mark_card_dirty(
                active.run_id,
                state='interrupted',
                status='已停止',
                status_phase='stopped',
                finished_at=int(time.time()),
            )
        elif result.status in {'completed', 'success'}:
            owner.registry.update_run_and_mark_card_dirty(
                active.run_id,
                state='succeeded',
                status='完成',
                status_phase='done',
                finished_at=int(time.time()),
            )
        elif result.status in {'systemError', 'failed', 'error'}:
            error_detail = result.final_text
            owner.registry.update_run_and_mark_card_dirty(
                active.run_id,
                state='failed',
                status=runner_failure_status(result.status, error_detail),
                status_phase='failed',
                error=error_detail,
                finished_at=int(time.time()),
            )
        elif result.status not in {'', 'unknown'}:
            owner.registry.update_run_and_mark_card_dirty(
                active.run_id,
                state='succeeded',
                status=f'完成: {result.status}',
                status_phase='done',
                finished_at=int(time.time()),
            )
        else:
            owner.registry.update_run_and_mark_card_dirty(
                active.run_id,
                state='succeeded',
                status='完成',
                status_phase='done',
                finished_at=int(time.time()),
            )
        run = owner.registry.get_run(active.run_id)
        owner._publish_status(active, force=True, create=bool(run and run.status_message_id))
        owner._schedule_terminal_status_replay(active.run_id)

        try:
            owner._queue_final_once(active, final_text)
        finally:
            active.done.set()
            with owner._active_lock:
                owner._active_runs.pop(session.id, None)

    def status_ticker(self, active: ActiveRun, *, tick_seconds: float) -> None:
        owner = self.owner
        while not active.done.wait(tick_seconds):
            owner.registry.touch_run_lease(active.run_id)
            owner._publish_status(active, force=True, create=True)

    def handle_runner_event(self, active: ActiveRun, event: dict[str, Any]) -> None:
        owner = self.owner
        if active.handoff_child_session_id is not None:
            return
        owner.registry.touch_run_lease(active.run_id)
        event_type = str(event.get('type') or '')
        if event_type == 'thread_ready':
            session_ref = str(event.get('session_ref') or event.get('codex_thread_id') or '')
            owner.registry.update_run(
                active.run_id,
                runner_kind=owner.runner.kind,
                runner_session_ref=session_ref,
                codex_thread_id=session_ref,
                status=f'{owner.runner.label} session ready',
            )
            run = owner.registry.get_run(active.run_id)
            if run and run.display_title:
                try:
                    active.control.set_thread_name(run.display_title)
                except Exception:
                    owner.log.exception('failed to set %s title for session %s', owner.runner.label, active.session.id)
            owner._publish_status(active, create=False)
        elif event_type == 'turn_started':
            turn_ref = str(event.get('turn_ref') or event.get('turn_id') or '')
            owner.registry.update_run(
                active.run_id,
                runner_kind=owner.runner.kind,
                runner_turn_ref=turn_ref,
                turn_id=turn_ref,
                status='turn running',
            )
            owner._publish_status(active, create=False)
        elif event_type == 'thread_name_updated':
            text = str(event.get('text') or '')
            if text:
                run = owner.registry.get_run(active.run_id)
                owner.registry.update_run(
                    active.run_id,
                    display_title=normalize_title(text, fallback=(run.display_title if run else '') or '任务'),
                )
                owner._publish_status(active, force=True, create=True)
        elif event_type == 'agent_message':
            phase = str(event.get('phase') or 'commentary')
            text = str(event.get('text') or '')
            if text:
                owner.registry.update_run(active.run_id, status='生成回答' if phase == 'final_answer' else '模型输出')
                owner._add_model_message(active, text, phase=phase)
                owner._publish_status(active, force=True, create=True)
        elif event_type == 'final_answer_ready':
            owner._queue_final_once(active, str(event.get('text') or ''))
        elif event_type == 'plan_updated':
            owner.registry.update_run(active.run_id, status=f'计划: {event.get("text")}')
            owner._publish_status(active)
        elif event_type == 'command_started':
            owner.registry.update_run(active.run_id, status='执行 Bash')
            owner._add_tool(
                active, 'Bash', item_id=str(event.get('item_id') or ''), detail=str(event.get('command') or '')
            )
            owner._publish_status(active, create=True)
        elif event_type == 'command_completed':
            exit_code = event.get('exit_code')
            owner._finish_tool(active, str(event.get('item_id') or ''), failed=exit_code not in (None, 0))
            owner.registry.update_run(
                active.run_id,
                status='Bash 完成' if exit_code in (None, 0) else f'Bash 失败: exit={exit_code}',
            )
            owner._publish_status(active)
        elif event_type == 'tool_started':
            tool = friendly_tool_name(str(event.get('tool') or 'Tool'))
            owner.registry.update_run(active.run_id, status=f'调用 {tool}')
            owner._add_tool(active, tool, item_id=str(event.get('item_id') or ''))
            owner._publish_status(active, create=True)
        elif event_type == 'file_change_started':
            owner.registry.update_run(active.run_id, status='修改文件')
            owner._add_tool(active, 'File edit', item_id=str(event.get('item_id') or ''))
            owner._publish_status(active, create=True)
        elif event_type in {'tool_completed', 'file_change_completed'}:
            owner._finish_tool(active, str(event.get('item_id') or ''))
            owner._publish_status(active)
        elif event_type == 'turn_interrupted':
            owner.registry.update_run_and_mark_card_dirty(
                active.run_id,
                state='interrupted',
                status='已停止',
                status_phase='stopped',
                finished_at=int(time.time()),
            )
            owner._publish_status(active, force=True, create=True)
            owner._schedule_terminal_status_replay(active.run_id)
        elif event_type == 'turn_completed':
            status = str(event.get('status') or '')
            final_text = str(event.get('final_text') or '').strip()
            if final_text and owner._last_model_output(active.run_id) != final_text:
                owner._add_model_message(active, final_text, phase='final_answer')
            if final_text:
                owner._queue_final_once(active, final_text)
            if status in {'systemError', 'failed', 'error'}:
                owner.registry.update_run_and_mark_card_dirty(
                    active.run_id,
                    state='failed',
                    status=runner_failure_status(status, final_text),
                    status_phase='failed',
                    error=final_text,
                    finished_at=int(time.time()),
                )
            else:
                owner.registry.update_run_and_mark_card_dirty(
                    active.run_id,
                    state='succeeded',
                    status=f'完成: {status}',
                    status_phase='done',
                    finished_at=int(time.time()),
                )
            owner._publish_status(active, force=True, create=False)
            owner._schedule_terminal_status_replay(active.run_id)
        elif event_type == 'error':
            text = str(event.get('text') or '')
            if active.session.agent_session_ref and has_invalid_encrypted_content(text):
                owner.registry.update_runner_session(active.session.id, '', runner_kind=owner.runner.kind)
                owner.registry.update_run(active.run_id, codex_thread_id='', runner_session_ref='')
                owner.log.info('cleared runner session ref for session %s after invalid encrypted content', active.session.id)
            if is_retryable_runner_error_event(event):
                owner.log.info(
                    '%s reported retryable error for session %s: %s',
                    owner.runner.label,
                    active.session.id,
                    compact(text, 300),
                )
                owner.registry.update_run_and_mark_card_dirty(
                    active.run_id,
                    state='running',
                    status=f'{owner.runner.label} 正在重连',
                    status_phase='running',
                    error='',
                    finished_at=None,
                )
                owner._publish_status(active, force=True, create=True)
                return
            if text and owner._last_model_output(active.run_id) != text:
                owner._add_model_message(active, text, phase='error')
            owner.registry.update_run_and_mark_card_dirty(
                active.run_id,
                state='failed',
                status=failure_status(f'{owner.runner.label} error', text),
                status_phase='failed',
                error=text,
                finished_at=int(time.time()),
            )
            owner._publish_status(active, force=True, create=True)
            owner._schedule_terminal_status_replay(active.run_id)


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


def is_retryable_runner_error_event(event: dict[str, Any]) -> bool:
    if event.get('will_retry') is True or event.get('willRetry') is True:
        return True
    text = str(event.get('text') or '')
    if not text:
        return False
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and (payload.get('willRetry') is True or payload.get('will_retry') is True)


def failure_status(prefix: str, detail: str) -> str:
    short = compact(detail, 120).strip()
    return f'{prefix}: {short}' if short else prefix


def runner_failure_status(status: str, detail: str) -> str:
    return failure_status(f'失败: {status}', detail)
