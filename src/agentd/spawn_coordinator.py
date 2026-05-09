from __future__ import annotations

import threading
import time
from typing import Any

from .active_run import ActiveRun
from .feishu import message_id_from_result, thread_id_from_result
from .models import RunRecord, SpawnRequest
from .status_rendering import escape_lark_md
from .title import normalize_title, title_from_text


class SpawnCoordinator:
    def __init__(self, owner: Any) -> None:
        self.owner = owner

    def handle_spawn_request(self, request: SpawnRequest) -> None:
        owner = self.owner
        parent = owner._active_for(request.parent_session_id)
        if parent is None:
            owner.registry.finish_spawn_request(request.id, state='failed', error='parent session is not active')
            return
        mode = spawn_request_mode(request.mode)
        if parent.session.kind == 'child':
            self.reject_spawn_request(parent, request, '子任务内不支持再创建子任务')
            return
        parent_run = owner.registry.get_run(parent.run_id)
        if parent_run is None:
            owner.registry.finish_spawn_request(request.id, state='failed', error='parent run is missing')
            return
        if not parent_run.status_message_id:
            owner._publish_status(parent, force=True, create=True)
            owner._drain_feishu_outbox()
            parent_run = owner.registry.get_run(parent.run_id)
        if parent_run is None or not parent_run.status_message_id:
            owner.registry.finish_spawn_request(request.id, state='failed', error='parent run has no status card')
            return

        sender_open_id = request.sender_open_id or parent_run.sender_open_id
        try:
            thread_id, source_message_id = self.create_child_thread(parent_run, request, mode=mode)
            child_session = owner.registry.bind_child_session(
                request.chat_id,
                thread_id,
                request.cwd,
                root_message_id=source_message_id,
                parent_id=parent.session.id,
                context_profile=request.context_profile or owner.config.context.default_child_profile,
                skills=request.skills,
            )
            if mode == 'thread':
                owner.registry.update_run(parent.run_id, status='已创建新话题')
                owner._add_model_message(parent, f'已创建新话题：{request.title or request.cwd}', phase='control')
                owner._publish_status(parent, force=True, create=True)
                owner.registry.finish_spawn_request(request.id, state='started')
                return

            if mode == 'handoff':
                parent.handoff_child_session_id = child_session.id
                owner.registry.update_run_and_mark_card_dirty(
                    parent.run_id,
                    handoff_child_session_id=child_session.id,
                    state='interrupted',
                    status='已移交子任务',
                    status_phase='stopped',
                    finished_at=int(time.time()),
                )
                owner._add_model_message(parent, f'已移交子任务：{request.title or request.cwd}', phase='control')
                owner._publish_status(parent, force=True, create=True)
                parent.control.interrupt()
                parent.done.set()
                with owner._active_lock:
                    owner._active_runs.pop(parent.session.id, None)
            else:
                owner.registry.update_run(parent.run_id, status='已创建并行子任务')
                owner._add_model_message(parent, f'已创建并行子任务：{request.title or request.cwd}', phase='control')
                owner._publish_status(parent, force=True, create=True)

            child_run = owner.registry.create_run(
                session_id=child_session.id,
                source_message_id=source_message_id,
                prompt=owner._build_child_prompt(request, child_session, source_message_id),
                host=owner.host,
                status='启动子任务',
                status_message_id=source_message_id,
                subject='子任务',
                display_title=normalize_title(request.title or title_from_text(request.prompt, fallback='子任务')),
                runner_kind=owner.runner.kind,
                context_profile=request.context_profile or owner.config.context.default_child_profile,
                skills=request.skills,
                sender_open_id=sender_open_id,
            )
            child = ActiveRun(run_id=child_run.id, session=child_session, control=owner.runner.new_control())
            with owner._active_lock:
                owner._active_runs[child_session.id] = child

            owner._publish_status(child, force=True, create=True)
            threading.Thread(
                target=owner._status_ticker,
                args=(child,),
                name=f'agentd-status-{child_session.id}',
                daemon=True,
            ).start()
            child_worker = threading.Thread(
                target=owner._run_turn_worker,
                args=(child,),
                name=f'agentd-session-{child_session.id}',
            )
            child_worker.daemon = False
            child_worker.start()
            owner.registry.finish_spawn_request(request.id, state='started')
        except Exception as exc:
            owner.log.exception('failed to spawn child request %s', request.id)
            if mode == 'handoff':
                owner.registry.update_run_and_mark_card_dirty(
                    parent.run_id,
                    status=f'子任务启动失败: {exc}',
                    status_phase='failed',
                    state='failed',
                    finished_at=int(time.time()),
                )
            else:
                owner.registry.update_run(parent.run_id, status=f'子任务启动失败: {exc}')
            owner._add_model_message(parent, f'子任务启动失败: {exc}', phase='error')
            owner._publish_status(parent, force=True, create=True)
            owner.registry.finish_spawn_request(request.id, state='failed', error=str(exc))

    def reject_spawn_request(self, active: ActiveRun, request: SpawnRequest, reason: str) -> None:
        owner = self.owner
        owner.registry.update_run(active.run_id, status=reason)
        owner._add_model_message(active, reason, phase='control')
        owner._publish_status(active, force=True, create=True)
        owner.registry.finish_spawn_request(request.id, state='failed', error=reason)

    def create_child_thread(self, parent: RunRecord, request: SpawnRequest, *, mode: str = 'handoff') -> tuple[str, str]:
        owner = self.owner
        if owner.dry_send:
            return f'dry-thread-{request.id}', f'dry-thread-message-{request.id}'
        sender_open_id = request.sender_open_id or parent.sender_open_id
        if mode == 'handoff':
            return self.reply_child_intro_in_thread(
                parent.status_message_id, request, sender_open_id=sender_open_id, mode=mode
            )

        card = self.build_child_intro_card(request, sender_open_id=sender_open_id, mode=mode)
        result = owner.feishu.send_interactive(request.chat_id, card)
        thread_id = thread_id_from_result(result)
        message_id = message_id_from_result(result)
        source_message_id = message_id or thread_id or parent.status_message_id
        if not thread_id:
            thread_id = source_message_id
        if mode == 'branch':
            reply_thread_id, _ = self.reply_child_intro_in_thread(
                source_message_id,
                request,
                sender_open_id=sender_open_id,
                mode=mode,
                fallback_thread_id=thread_id,
            )
            thread_id = reply_thread_id or thread_id
        return thread_id, source_message_id

    def reply_child_intro_in_thread(
        self,
        message_id: str,
        request: SpawnRequest,
        *,
        sender_open_id: str = '',
        mode: str = 'handoff',
        fallback_thread_id: str = '',
    ) -> tuple[str, str]:
        result = self.owner.feishu.reply_interactive(
            message_id,
            self.build_child_intro_card(request, sender_open_id=sender_open_id, mode=mode),
            reply_in_thread=True,
        )
        thread_id = thread_id_from_result(result)
        reply_message_id = message_id_from_result(result)
        if not thread_id:
            thread_id = fallback_thread_id or reply_message_id or message_id
        return thread_id, reply_message_id or message_id

    def build_child_intro_card(
        self, request: SpawnRequest, *, sender_open_id: str = '', mode: str = 'handoff'
    ) -> dict[str, Any]:
        title = normalize_title(request.title or request.cwd, fallback='子任务')
        mention = f'<at id={sender_open_id}></at> ' if sender_open_id else ''
        if mode == 'thread':
            leading = f'{mention}**新话题已创建**：{escape_lark_md(request.title or request.cwd)}'
            tail = f'在这个话题里回复第一条消息后，我会启动 {self.owner.runner.label}。'
        elif mode == 'branch':
            leading = f'{mention}**并行子任务已启动**：{escape_lark_md(request.title or request.cwd)}'
            tail = '可以在这个话题里继续追加指令或打断，不会影响原任务。'
        else:
            leading = f'{mention}**子任务已启动**：{escape_lark_md(request.title or request.cwd)}'
            tail = '可以在这个话题里继续追加指令或打断。'
        content = '\n'.join(
            [
                leading,
                f'CWD: `{escape_lark_md(request.cwd)}`',
                '',
                tail,
            ]
        )
        return {
            'config': {'wide_screen_mode': True, 'update_multi': True},
            'header': {
                'template': 'blue',
                'title': {'tag': 'plain_text', 'content': f'🧵 {title}'},
            },
            'elements': [
                {'tag': 'div', 'text': {'tag': 'lark_md', 'content': content}},
            ],
        }


def spawn_request_mode(value: str) -> str:
    mode = value.strip().lower()
    return mode if mode in {'handoff', 'branch', 'thread'} else 'handoff'
