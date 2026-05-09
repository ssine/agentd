from __future__ import annotations

import unittest
from types import SimpleNamespace

from agentd.web_gateway import handle_web_message


class WebGatewayChannelTest(unittest.TestCase):
    def test_handle_web_message_uses_web_channel_envelope(self) -> None:
        seen = {}

        def handle_control_command(command):  # type: ignore[no-untyped-def]
            seen['command'] = command
            return 'started'

        daemon = SimpleNamespace(
            registry=SimpleNamespace(get_session=lambda _session_id: None),
            handle_control_command=handle_control_command,
        )

        result = handle_web_message(
            daemon,
            {
                'conversation_id': 'browser',
                'message_id': 'web-msg-1',
                'sender_id': 'user-1',
                'sender_name': 'Sine',
                'text': 'hello',
            },
        )

        self.assertTrue(result['ok'])
        self.assertEqual(result['chat_id'], 'browser')
        self.assertEqual(seen['command'].channel, 'web')
        self.assertEqual(seen['command'].conversation_ref, 'browser')
        self.assertEqual(seen['command'].message_ref, 'web-msg-1')
        self.assertEqual(seen['command'].sender_ref, 'user-1')
        self.assertEqual(seen['command'].text, 'hello')


if __name__ == '__main__':
    unittest.main()
