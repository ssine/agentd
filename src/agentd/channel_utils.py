from __future__ import annotations

from .models import IncomingMessage


def channel_from_message(message: IncomingMessage) -> str:
    channel = str(message.channel or '').strip().lower()
    if channel:
        return channel
    chat_id = str(message.chat_id or '')
    if chat_id == 'web' or chat_id.startswith('web:'):
        return 'web'
    if chat_id == 'wecom' or chat_id.startswith('wecom:'):
        return 'wecom'
    return 'feishu'


def conversation_ref_from_message(message: IncomingMessage) -> str:
    channel = channel_from_message(message)
    chat_id = str(message.chat_id or '')
    prefix = f'{channel}:'
    if channel in {'web', 'wecom'} and chat_id.startswith(prefix):
        return chat_id[len(prefix) :]
    return chat_id or channel


def channel_label(channel: str) -> str:
    return {'feishu': 'Feishu', 'web': 'Web', 'wecom': 'WeCom'}.get(channel, channel or 'Channel')
