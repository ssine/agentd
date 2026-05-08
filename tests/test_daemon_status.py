from __future__ import annotations

import json
import unittest

from agentd.codex_app_server import CodexRunControl
from agentd.daemon import ActiveRun, AgentDaemon
from agentd.models import AgentSession


class DaemonStatusCardTest(unittest.TestCase):
    def test_failed_status_card_includes_error_detail(self) -> None:
        daemon = object.__new__(AgentDaemon)
        active = ActiveRun(
            session=AgentSession(
                id=1,
                kind='main',
                chat_id='chat',
                thread_id=None,
                root_message_id=None,
                codex_thread_id='thread',
                cwd='/workspace',
            ),
            source_message_id='message',
            control=CodexRunControl(),
            host='host',
            started_at=100,
            finished_at=110,
            status='失败: systemError',
            status_phase='failed',
            error_detail='upstream returned 500: model overloaded',
        )

        status_text = daemon._format_status_text(active)
        card = daemon._build_status_card(active)
        raw_card = json.dumps(card, ensure_ascii=False)

        self.assertIn('错误信息: upstream returned 500: model overloaded', status_text)
        self.assertIn('**错误信息**', raw_card)
        self.assertIn('upstream returned 500: model overloaded', raw_card)
        self.assertEqual(card['header']['template'], 'red')


if __name__ == '__main__':
    unittest.main()
