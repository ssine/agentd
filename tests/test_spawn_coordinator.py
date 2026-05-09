from __future__ import annotations

import unittest
from types import SimpleNamespace

from agentd.models import SpawnRequest
from agentd.spawn_coordinator import SpawnCoordinator, spawn_request_mode


class SpawnCoordinatorTest(unittest.TestCase):
    def test_spawn_request_mode_defaults_to_handoff(self) -> None:
        self.assertEqual(spawn_request_mode('branch'), 'branch')
        self.assertEqual(spawn_request_mode('thread'), 'thread')
        self.assertEqual(spawn_request_mode('unknown'), 'handoff')

    def test_thread_intro_card_mentions_runner_label(self) -> None:
        coordinator = SpawnCoordinator(SimpleNamespace(runner=SimpleNamespace(label='Claude Code')))
        card = coordinator.build_child_intro_card(
            SpawnRequest(
                id=1,
                parent_session_id=1,
                parent_status_message_id='status-1',
                parent_source_message_id='source-1',
                chat_id='chat-1',
                cwd='/workspace',
                title='Investigate',
                prompt='',
                context_profile='default',
                skills=(),
                state='claimed',
                mode='thread',
            ),
            sender_open_id='ou_1',
            mode='thread',
        )

        content = card['elements'][0]['text']['content']
        self.assertIn('<at id=ou_1></at>', content)
        self.assertIn('新话题已创建', content)
        self.assertIn('Claude Code', content)


if __name__ == '__main__':
    unittest.main()
