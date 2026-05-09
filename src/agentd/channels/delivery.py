from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import AgentSession, ChannelBindingRecord, RunRecord
from .base import DeliveryRequest


@dataclass(frozen=True)
class ChannelBinding:
    run_ref: str
    session_ref: str
    channel: str
    conversation_ref: str
    session_kind: str
    source_message_ref: str = ''
    status_message_ref: str = ''
    thread_ref: str = ''


def binding_from_run(
    run: RunRecord,
    session: AgentSession,
    durable_binding: ChannelBindingRecord | None = None,
) -> ChannelBinding:
    return ChannelBinding(
        run_ref=str(run.id),
        session_ref=str(session.id),
        channel=durable_binding.channel if durable_binding is not None else channel_from_legacy_run(run, session),
        conversation_ref=durable_binding.conversation_ref if durable_binding is not None else session.chat_id,
        session_kind=session.kind,
        source_message_ref=run.source_message_id,
        status_message_ref=run.status_message_id,
        thread_ref=durable_binding.thread_ref if durable_binding is not None else session.thread_id or '',
    )


def channel_from_legacy_run(run: RunRecord, session: AgentSession | None = None) -> str:
    source = str(run.source_message_id or '')
    chat_id = str(session.chat_id if session is not None else '')
    if source.startswith('web-') or chat_id == 'web' or chat_id.startswith('web:'):
        return 'web'
    if source.startswith('wecom-') or chat_id.startswith('wecom:'):
        return 'wecom'
    return 'feishu'


def delivery_needs_queue(delivery: DeliveryRequest) -> bool:
    return delivery.channel == 'feishu'


def _delivery_key(binding: ChannelBinding, suffix: str) -> str:
    identity = binding.run_ref or binding.source_message_ref
    return f'run:{identity}:{suffix}'


def status_delivery(
    binding: ChannelBinding,
    *,
    text: str,
    render_hash: str,
    card: dict[str, Any] | None = None,
    remote_message_ref: str = '',
    status_reply_in_thread: bool = False,
    feishu_reply_in_thread: bool = True,
) -> DeliveryRequest:
    if binding.channel == 'feishu':
        action = 'update' if remote_message_ref else ('reply' if status_reply_in_thread else 'create')
        return DeliveryRequest(
            channel='feishu',
            destination_ref=binding.conversation_ref,
            kind='status_card',
            dedupe_key=_delivery_key(binding, 'status_card'),
            payload={
                'action': action,
                'chat_id': binding.conversation_ref,
                'source_message_id': binding.source_message_ref,
                'message_id': remote_message_ref,
                'reply_in_thread': feishu_reply_in_thread,
                'card': card or {},
                'text': text,
                'render_hash': render_hash,
            },
        )
    if binding.channel == 'web':
        return DeliveryRequest(
            channel='web',
            destination_ref=binding.conversation_ref,
            kind='status_state',
            dedupe_key=_delivery_key(binding, 'status_state'),
            payload={'text': text, 'render_hash': render_hash},
            thread_ref=binding.thread_ref,
        )
    return DeliveryRequest(
        channel=binding.channel,
        destination_ref=binding.conversation_ref,
        kind='status_text',
        dedupe_key=_delivery_key(binding, 'status_text'),
        payload={'text': text, 'render_hash': render_hash},
        thread_ref=binding.thread_ref,
    )


def final_reply_delivery(
    binding: ChannelBinding,
    *,
    text: str,
    feishu_reply_in_thread: bool = True,
) -> DeliveryRequest:
    if binding.channel == 'feishu':
        return DeliveryRequest(
            channel='feishu',
            destination_ref=binding.conversation_ref,
            kind='final_reply',
            dedupe_key=_delivery_key(binding, 'final'),
            payload={
                'chat_id': binding.conversation_ref,
                'session_kind': binding.session_kind,
                'source_message_id': binding.source_message_ref,
                'text': text,
                'reply_in_thread': feishu_reply_in_thread,
            },
            thread_ref=binding.thread_ref,
        )
    if binding.channel == 'web':
        return DeliveryRequest(
            channel='web',
            destination_ref=binding.conversation_ref,
            kind='final_state',
            dedupe_key=_delivery_key(binding, 'final_state'),
            payload={'text': text},
            thread_ref=binding.thread_ref,
        )
    return DeliveryRequest(
        channel=binding.channel,
        destination_ref=binding.conversation_ref,
        kind='final_text',
        dedupe_key=_delivery_key(binding, 'final_text'),
        payload={'text': text},
        thread_ref=binding.thread_ref,
    )
