from __future__ import annotations

from ..models import CardAction, IncomingMessage
from .base import ChannelCapabilities, ChannelEnvelope, ControlCommand

FEISHU_CAPABILITIES = ChannelCapabilities(
    supports_threads=True,
    supports_child_threads=True,
    supports_card_actions=True,
    supports_message_update=True,
    supports_markdown=True,
    delivery_modes=('text', 'markdown', 'interactive_card'),
)


class FeishuChannelAdapter:
    kind = 'feishu'
    capabilities = FEISHU_CAPABILITIES

    def envelope_from_message(self, message: IncomingMessage) -> ChannelEnvelope:
        return ChannelEnvelope(
            channel=self.kind,
            conversation_ref=message.chat_id,
            message_ref=message.message_id,
            text=message.text,
            sender_ref=message.sender_open_id,
            sender_name=message.sender_name,
            thread_ref=message.thread_id,
            metadata={
                'sender_type': message.sender_type,
                'chat_type': message.chat_type,
            },
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
            metadata={**envelope.metadata, 'sender_name': envelope.sender_name},
        )

    def card_action(self, action: CardAction) -> ControlCommand:
        return ControlCommand(
            command_type='channel_action',
            channel=self.kind,
            conversation_ref=action.chat_id,
            message_ref=action.message_id,
            metadata={'action': action.action, 'session_id': action.session_id},
        )
