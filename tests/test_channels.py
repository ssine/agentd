from __future__ import annotations

import unittest

from agentd.channels import FeishuChannelAdapter, WebChannelAdapter, WeComChannelAdapter
from agentd.models import CardAction, IncomingMessage


class ChannelAdapterTest(unittest.TestCase):
    def test_feishu_preserves_thread_and_card_capabilities(self) -> None:
        adapter = FeishuChannelAdapter()
        message = IncomingMessage(
            chat_id='chat-1',
            message_id='msg-1',
            thread_id='thread-1',
            sender_open_id='ou_user',
            sender_name='Sine',
            sender_type='user',
            chat_type='group',
            text='hello',
        )

        command = adapter.submit_message(adapter.envelope_from_message(message))
        action = adapter.card_action(CardAction(action='stop', message_id='card-1', chat_id='chat-1', session_id=42))

        self.assertTrue(adapter.capabilities.supports_child_threads)
        self.assertTrue(adapter.capabilities.supports_card_actions)
        self.assertEqual(command.channel, 'feishu')
        self.assertEqual(command.thread_ref, 'thread-1')
        self.assertEqual(command.sender_ref, 'ou_user')
        self.assertEqual(action.metadata['action'], 'stop')
        self.assertEqual(action.metadata['session_id'], 42)

    def test_web_is_a_first_class_channel_without_feishu_thread_capabilities(self) -> None:
        adapter = WebChannelAdapter()

        envelope = adapter.envelope_from_payload(
            {'conversation_id': 'browser', 'message_id': 'web-msg-1', 'sender_id': 'user-1', 'text': 'run this'}
        )
        command = adapter.submit_message(envelope)

        self.assertFalse(adapter.capabilities.supports_child_threads)
        self.assertEqual(adapter.capabilities.delivery_modes[0], 'json')
        self.assertEqual(command.channel, 'web')
        self.assertEqual(command.conversation_ref, 'browser')
        self.assertEqual(command.message_ref, 'web-msg-1')

    def test_wecom_degrades_thread_and_update_features(self) -> None:
        adapter = WeComChannelAdapter()

        envelope = adapter.envelope_from_event(
            {'MsgType': 'text', 'ChatId': 'room-1', 'FromUserName': 'user-1', 'MsgId': 'wx-1', 'Content': 'hello'}
        )
        command = adapter.submit_message(envelope)

        self.assertFalse(adapter.capabilities.supports_threads)
        self.assertFalse(adapter.capabilities.supports_message_update)
        self.assertEqual(adapter.capabilities.delivery_modes, ('text', 'markdown'))
        self.assertEqual(command.channel, 'wecom')
        self.assertEqual(command.thread_ref, '')
        self.assertTrue(command.metadata['degraded_threading'])


if __name__ == '__main__':
    unittest.main()
