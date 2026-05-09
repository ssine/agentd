from __future__ import annotations

import time
from typing import Any

from .base import ChannelCapabilities, ChannelEnvelope, ControlCommand

WEB_CAPABILITIES = ChannelCapabilities(
    supports_threads=False,
    supports_child_threads=False,
    supports_card_actions=False,
    supports_message_update=True,
    supports_markdown=True,
    delivery_modes=('json', 'text', 'markdown'),
)


class WebChannelAdapter:
    kind = 'web'
    capabilities = WEB_CAPABILITIES

    def envelope_from_payload(self, payload: dict[str, Any]) -> ChannelEnvelope:
        text = str(payload.get('text') or '').strip()
        conversation_ref = str(payload.get('conversation_id') or payload.get('chat_id') or 'web').strip() or 'web'
        return ChannelEnvelope(
            channel=self.kind,
            conversation_ref=conversation_ref,
            message_ref=str(payload.get('message_id') or f'web-{time.time_ns()}'),
            text=text,
            sender_ref=str(payload.get('sender_id') or 'web-user'),
            sender_name=str(payload.get('sender_name') or 'web'),
            thread_ref=str(payload.get('thread_id') or ''),
            metadata={'session_id': payload.get('session_id')},
        )

    def submit_message(self, envelope: ChannelEnvelope) -> ControlCommand:
        return ControlCommand(
            command_type='submit_message',
            channel=self.kind,
            conversation_ref=envelope.conversation_ref,
            message_ref=envelope.message_ref,
            thread_ref=envelope.thread_ref,
            sender_ref=envelope.sender_ref,
            text=envelope.text,
            metadata={**envelope.metadata, 'sender_name': envelope.sender_name, 'sender_type': 'user', 'chat_type': 'p2p'},
        )
