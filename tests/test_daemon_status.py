from __future__ import annotations

import json
import unittest

from agentd.daemon import AgentDaemon, RunView
from agentd.models import AgentSession, RunRecord


class DaemonStatusCardTest(unittest.TestCase):
    def test_status_card_includes_visible_status_line(self) -> None:
        daemon = object.__new__(AgentDaemon)
        session = AgentSession(
            id=1,
            kind='main',
            chat_id='chat',
            thread_id=None,
            root_message_id=None,
            codex_thread_id='thread',
            cwd='/workspace',
        )
        run = RunRecord(
            id=1,
            session_id=session.id,
            source_message_id='message',
            prompt='hello',
            state='succeeded',
            status_phase='done',
            status='完成',
            status_message_id='status-message',
            codex_thread_id='thread',
            turn_id='turn',
            subject='Codex',
            display_title='Status test',
            host='host',
            status_reply_in_thread=False,
            context_profile='default',
            skills=(),
            hide_early_iterations=True,
            show_tool_details=False,
            truncate_content=True,
            final_message_text='',
            final_message_sent_at=None,
            error='',
            handoff_child_session_id=None,
            started_at=100,
            finished_at=110,
            heartbeat_at=100,
            lease_until=130,
            created_at=100,
            updated_at=110,
        )
        active = RunView(
            run=run,
            session=session,
            iterations=[],
            running_tools={},
            tool_details=[],
            model_outputs=[],
        )

        card = daemon._build_status_card(active)

        self.assertIn('**状态**：已完成 · 完成', card['elements'][0]['text']['content'])

    def test_failed_status_card_includes_error_detail(self) -> None:
        daemon = object.__new__(AgentDaemon)
        session = AgentSession(
            id=1,
            kind='main',
            chat_id='chat',
            thread_id=None,
            root_message_id=None,
            codex_thread_id='thread',
            cwd='/workspace',
        )
        run = RunRecord(
            id=1,
            session_id=session.id,
            source_message_id='message',
            prompt='hello',
            state='failed',
            status_phase='failed',
            status='失败: systemError',
            status_message_id='status-message',
            codex_thread_id='thread',
            turn_id='turn',
            subject='Codex',
            display_title='Status test',
            host='host',
            status_reply_in_thread=False,
            context_profile='default',
            skills=(),
            hide_early_iterations=True,
            show_tool_details=False,
            truncate_content=True,
            final_message_text='',
            final_message_sent_at=None,
            error='upstream returned 500: model overloaded',
            handoff_child_session_id=None,
            started_at=100,
            finished_at=110,
            heartbeat_at=100,
            lease_until=130,
            created_at=100,
            updated_at=110,
        )
        active = RunView(
            run=run,
            session=session,
            iterations=[],
            running_tools={},
            tool_details=[],
            model_outputs=[],
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
