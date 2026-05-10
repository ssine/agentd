from __future__ import annotations

import unittest

from agentd.channels import ControlCommand
from agentd.daemon import message_from_control_command
from agentd.models import MessageAttachment


class ControlCommandTest(unittest.TestCase):
    def test_feishu_submit_command_maps_to_incoming_message_without_prefix(self) -> None:
        command = ControlCommand(
            command_type='submit_message',
            channel='feishu',
            conversation_ref='chat-1',
            message_ref='msg-1',
            thread_ref='thread-1',
            sender_ref='ou_user',
            text='hello',
            metadata={'sender_name': 'Sine', 'sender_type': 'user', 'chat_type': 'group'},
            attachments=(MessageAttachment(kind='file', key='file_1', local_path='/tmp/file.txt'),),
        )

        message = message_from_control_command(command)

        self.assertEqual(message.chat_id, 'chat-1')
        self.assertEqual(message.message_id, 'msg-1')
        self.assertEqual(message.thread_id, 'thread-1')
        self.assertEqual(message.sender_name, 'Sine')
        self.assertEqual(message.chat_type, 'group')
        self.assertEqual(message.channel, 'feishu')
        self.assertEqual(message.attachments, command.attachments)

    def test_wecom_submit_command_gets_channel_scoped_legacy_ids(self) -> None:
        command = ControlCommand(
            command_type='submit_message',
            channel='wecom',
            conversation_ref='room-1',
            message_ref='wx-1',
            sender_ref='user-1',
            text='hello',
        )

        message = message_from_control_command(command)

        self.assertEqual(message.chat_id, 'wecom:room-1')
        self.assertEqual(message.message_id, 'wecom-wx-1')
        self.assertEqual(message.chat_type, 'wecom')
        self.assertEqual(message.channel, 'wecom')


if __name__ == '__main__':
    unittest.main()
