from __future__ import annotations

import threading
import time
from typing import Any

from .active_run import ActiveRun
from .channels.base import ControlCommand
from .models import CardAction, IncomingMessage, RunRecord
from .title import normalize_title, title_from_text


class AgentCore:
    def __init__(self, owner: Any) -> None:
        self.owner = owner

    def handle_control_command(self, command: ControlCommand) -> str:
        if command.command_type == 'submit_message':
            return self.handle_submit_message(message_from_control_command(command))
        return f'unsupported control command: {command.command_type}'

    def handle_submit_message(self, message: IncomingMessage) -> str:
        owner = self.owner
        owner._ensure_spawn_watcher()
        if not owner._message_allowed(message):
            owner.log.info('ignored message %s from chat %s', message.message_id, message.chat_id)
            return ''
        if owner.registry.is_duplicate(message.message_id):
            owner.log.info('duplicate message ignored: %s', message.message_id)
            return ''

        session = owner._resolve_session(message)
        if session is None:
            owner.log.info('ignored unbound thread message %s thread %s', message.message_id, message.thread_id)
            return ''
        active = owner._active_for(session.id)
        if active is not None:
            return self.handle_live_input(active, message)
        owner._recover_stale_runs()
        persisted_active = owner.registry.get_active_run_for_session(session.id)
        if persisted_active is not None:
            owner._publish_status_for_run(persisted_active.id, force=True)
            return 'session already has an active run'

        context_profile = session.context_profile or owner.config.context.default_profile
        context_skills = session.skills
        prompt = owner.run_context_builder.message_prompt(message, session)
        reuse_root_card = session.kind == 'child' and message.message_id == session.root_message_id
        if session.kind == 'child' and session.root_message_id and not reuse_root_card:
            reuse_root_card = not owner.registry.list_runs(session_id=session.id, limit=1)
        source_message_id = session.root_message_id if reuse_root_card and session.root_message_id else message.message_id
        run = owner.registry.create_run(
            session_id=session.id,
            source_message_id=source_message_id,
            prompt=prompt,
            host=owner.host,
            status=f'启动 {owner.runner.label}',
            status_message_id=session.root_message_id if reuse_root_card and session.root_message_id else '',
            subject='子任务' if session.kind == 'child' else owner.runner.label,
            display_title=title_from_text(message.text, fallback='子任务' if session.kind == 'child' else '主任务'),
            runner_kind=owner.runner.kind,
            status_reply_in_thread=session.kind == 'child' and not reuse_root_card,
            context_profile=context_profile,
            skills=context_skills,
            sender_open_id=message.sender_open_id,
        )
        active = ActiveRun(run_id=run.id, session=session, control=owner.runner.new_control())
        with owner._active_lock:
            owner._active_runs[session.id] = active

        owner._publish_status(active, force=True, create=True)
        threading.Thread(
            target=owner._status_ticker,
            args=(active,),
            name=f'agentd-status-{session.id}',
            daemon=True,
        ).start()
        threading.Thread(
            target=owner._run_turn_worker,
            args=(active,),
            name=f'agentd-session-{session.id}',
        ).start()
        owner.log.info(
            'started background %s run for message %s session %s',
            owner.runner.label,
            message.message_id,
            session.id,
        )
        return 'started'

    def handle_card_action(self, action: CardAction) -> str:
        owner = self.owner
        run, active = self.run_for_card_action(action)
        if run is None:
            return '任务状态已过期'

        if action.action == 'stop':
            if active is not None:
                ok, detail = active.control.interrupt()
                if ok:
                    owner.registry.update_run(active.run_id, state='cancel_requested', status='已请求停止')
                    owner._add_model_message(active, '用户在卡片上请求停止当前 turn。', phase='control')
                    owner._publish_status(active, force=True, create=True)
                    return '已请求停止'
                return detail
            owner.registry.update_run(run.id, state='cancel_requested', status='已记录停止请求')
            owner.registry.append_run_event(
                run.id,
                'agent_message',
                {'text': '用户在卡片上请求停止当前 turn。', 'phase': 'control'},
            )
            owner._publish_status_for_run(run.id, force=True)
            return '已记录停止请求'

        if action.action == 'toggle_early':
            owner.registry.update_run(run.id, hide_early_iterations=not run.hide_early_iterations)
            owner._publish_status_for_run(run.id, force=True)
            return '已切换早期步骤显示'

        if action.action == 'toggle_tools':
            owner.registry.update_run(run.id, show_tool_details=not run.show_tool_details)
            owner._publish_status_for_run(run.id, force=True)
            return '已切换工具详情显示'

        if action.action == 'toggle_truncate':
            owner.registry.update_run(run.id, truncate_content=not run.truncate_content)
            owner._publish_status_for_run(run.id, force=True)
            return '已切换截断策略'

        if action.action in {'live', 'history', 'tools', 'output'}:
            owner._handle_legacy_view_action(run.id, action.action)
            owner._publish_status_for_run(run.id, force=True)
            return '已切换视图'

        return '未知操作'

    def run_for_card_action(self, action: CardAction) -> tuple[RunRecord | None, ActiveRun | None]:
        owner = self.owner
        run = owner.registry.get_run_for_status_card(action.message_id) if action.message_id else None
        with owner._active_lock:
            active = owner._active_runs.get(run.session_id) if run is not None else None
            if run is None and action.session_id is not None:
                active = owner._active_runs.get(action.session_id)
                if active is not None:
                    run = owner.registry.get_run(active.run_id)
        if run is None and action.session_id is not None:
            run = owner.registry.get_active_run_for_session(action.session_id)
        return run, active

    def handle_live_input(self, active: ActiveRun, message: IncomingMessage) -> str:
        owner = self.owner
        text = message.text.strip()
        lower = text.lower()
        if lower in {'/status', 'status', '状态', '看看状态'}:
            owner._publish_status(active, force=True, create=True)
            return 'status'
        if lower in {'/stop', '/interrupt', 'stop', 'interrupt', '停', '停止', '打断', '别做了'}:
            ok, detail = active.control.interrupt()
            if ok:
                owner.registry.update_run(active.run_id, state='cancel_requested', status='已请求停止')
                owner._add_model_message(active, '用户请求停止当前 turn。', phase='control')
                owner._publish_status(active, force=True, create=True)
            else:
                owner.registry.update_run(active.run_id, status=f'停止失败: {detail}')
                owner._publish_status(active, force=True, create=True)
            return detail

        branch_command = parse_live_branch_command(text)
        if branch_command is not None:
            return self.handle_live_branch_command(active, message, branch_command)

        ok, detail = active.control.steer(text)
        if ok:
            owner.registry.update_run(active.run_id, status='已追加指令')
            owner._add_model_message(active, f'用户追加指令：{text}', phase='control')
            owner._publish_status(active, force=True, create=True)
        else:
            owner.registry.update_run(active.run_id, status=f'追加失败: {detail}')
            owner._publish_status(active, force=True, create=True)
        return detail

    def handle_live_branch_command(
        self,
        active: ActiveRun,
        message: IncomingMessage,
        command: dict[str, str],
    ) -> str:
        owner = self.owner
        if active.session.kind == 'child':
            detail = '子任务内不支持再创建子任务；请回到主会话开启新的任务。'
            owner.registry.update_run(active.run_id, status=detail)
            owner._add_model_message(active, detail, phase='control')
            owner._publish_status(active, force=True, create=True)
            return detail
        run = owner.registry.get_run(active.run_id)
        if run is None:
            return 'active run is missing'
        if not run.status_message_id:
            owner._publish_status(active, force=True, create=True)
            owner._drain_feishu_outbox()
            run = owner.registry.get_run(active.run_id)
        if run is None or not run.status_message_id:
            return '当前任务还没有状态卡，无法创建新话题。'

        mode = command['mode']
        prompt = command['prompt']
        if mode == 'branch' and not prompt:
            return '请在 /branch 后面写新任务内容。'
        title = normalize_title(command['title'] or title_from_text(prompt, fallback='新话题'), fallback='新话题')
        request_id = owner.registry.enqueue_spawn_request(
            parent_session_id=active.session.id,
            parent_status_message_id=run.status_message_id,
            parent_source_message_id=run.source_message_id,
            chat_id=message.chat_id,
            cwd=active.session.cwd,
            title=title,
            prompt=prompt,
            context_profile=run.context_profile,
            skills=run.skills,
            sender_open_id=message.sender_open_id or run.sender_open_id,
            mode=mode,
        )
        status = '已请求创建新话题' if mode == 'thread' else '已请求创建并行子任务'
        owner.registry.update_run(active.run_id, status=status)
        owner._add_model_message(active, f'{status}：{title}', phase='control')
        owner._publish_status(active, force=True, create=True)
        return f'{status}: {request_id}'


def parse_live_branch_command(text: str) -> dict[str, str] | None:
    stripped = text.strip()
    lowered = stripped.lower()
    for command, mode in (('/branch', 'branch'), ('/thread', 'thread')):
        if lowered == command or lowered.startswith(command + ' ') or lowered.startswith(command + '\n'):
            body = stripped[len(command) :].strip()
            if mode == 'thread':
                return {'mode': mode, 'title': body, 'prompt': ''}
            return {'mode': mode, 'title': '', 'prompt': body}
    return None


def message_from_control_command(command: ControlCommand) -> IncomingMessage:
    chat_id = command.conversation_ref
    message_id = command.message_ref
    if command.channel not in {'feishu', 'web'}:
        chat_id = f'{command.channel}:{chat_id}' if chat_id else command.channel
        message_id = f'{command.channel}-{message_id}' if message_id else f'{command.channel}-{int(time.time() * 1000)}'
    return IncomingMessage(
        chat_id=chat_id,
        message_id=message_id,
        text=command.text,
        sender_open_id=command.sender_ref,
        sender_name=str(command.metadata.get('sender_name') or command.sender_ref),
        sender_type=str(command.metadata.get('sender_type') or 'user'),
        thread_id=command.thread_ref,
        chat_type=str(command.metadata.get('chat_type') or command.channel),
        channel=command.channel,
    )
