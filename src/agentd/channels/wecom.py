from __future__ import annotations

from typing import Any

from .base import ChannelCapabilities, ChannelEnvelope, ControlCommand

WECOM_CAPABILITIES = ChannelCapabilities(
    supports_threads=False,
    supports_child_threads=False,
    supports_card_actions=False,
    supports_message_update=False,
    supports_markdown=True,
    delivery_modes=('text', 'markdown'),
)


class WeComChannelAdapter:
    kind = 'wecom'
    capabilities = WECOM_CAPABILITIES

    def envelope_from_event(self, event: dict[str, Any]) -> ChannelEnvelope:
        text = _text_from_event(event)
        conversation_ref = str(event.get('ChatId') or event.get('FromUserName') or event.get('conversation_id') or '')
        return ChannelEnvelope(
            channel=self.kind,
            conversation_ref=conversation_ref,
            message_ref=str(event.get('MsgId') or event.get('msgid') or ''),
            text=text,
            sender_ref=str(event.get('FromUserName') or event.get('sender') or ''),
            sender_name=str(event.get('FromUserName') or event.get('sender_name') or ''),
            thread_ref='',
            metadata={'msg_type': event.get('MsgType') or event.get('msgtype') or ''},
        )

    def submit_message(self, envelope: ChannelEnvelope) -> ControlCommand:
        return ControlCommand(
            command_type='submit_message',
            channel=self.kind,
            conversation_ref=envelope.conversation_ref,
            message_ref=envelope.message_ref,
            sender_ref=envelope.sender_ref,
            text=envelope.text,
            metadata={**envelope.metadata, 'sender_name': envelope.sender_name, 'degraded_threading': True},
        )


def _text_from_event(event: dict[str, Any]) -> str:
    msg_type = str(event.get('MsgType') or event.get('msgtype') or 'text')
    if msg_type == 'text':
        return str(event.get('Content') or event.get('content') or '').strip()
    if msg_type == 'image':
        return '[image]'
    if msg_type == 'voice':
        return '[voice]'
    if msg_type == 'file':
        return '[file]'
    return f'[{msg_type}]'
