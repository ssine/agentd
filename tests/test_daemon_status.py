from __future__ import annotations

import json
import unittest

from agentd.daemon import AgentDaemon, RunView
from agentd.models import AgentSession, RunRecord, SpawnRequest


class FakeFeishu:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.interactive_replies: list[dict[str, object]] = []
        self.interactive_sends: list[dict[str, object]] = []

    def send_interactive(self, chat_id: str, card: dict[str, object]) -> dict[str, object]:
        self.calls.append(('send_interactive', chat_id))
        self.interactive_sends.append(
            {
                'chat_id': chat_id,
                'card': card,
            }
        )
        return {'data': {'message_id': 'message-branch'}}

    def reply_interactive(
        self,
        message_id: str,
        card: dict[str, object],
        *,
        reply_in_thread: bool = False,
    ) -> dict[str, object]:
        self.calls.append(('reply_interactive', message_id))
        self.interactive_replies.append(
            {
                'message_id': message_id,
                'card': card,
                'reply_in_thread': reply_in_thread,
            }
        )
        if message_id == 'message-branch':
            return {'data': {'thread_id': 'message-branch', 'message_id': 'message-branch-reply'}}
        return {'data': {'thread_id': 'thread-child', 'message_id': 'message-child'}}


class DaemonStatusCardTest(unittest.TestCase):
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

    def test_child_thread_first_reply_mentions_sender(self) -> None:
        daemon = object.__new__(AgentDaemon)
        daemon.dry_send = False
        fake_feishu = FakeFeishu()
        daemon.feishu = fake_feishu

        parent = RunRecord(
            id=1,
            session_id=1,
            source_message_id='message-parent',
            prompt='delegate',
            state='running',
            status_phase='running',
            status='运行中',
            status_message_id='status-parent',
            codex_thread_id='thread-parent',
            turn_id='turn-parent',
            subject='Codex',
            display_title='Parent',
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
            finished_at=None,
            heartbeat_at=100,
            lease_until=130,
            created_at=100,
            updated_at=100,
            sender_open_id='ou_parent',
        )
        request = SpawnRequest(
            id=2,
            parent_session_id=1,
            parent_status_message_id='status-parent',
            parent_source_message_id='message-parent',
            chat_id='chat',
            cwd='/workspace',
            title='Child',
            prompt='do child work',
            context_profile='default',
            skills=(),
            state='claimed',
            sender_open_id='ou_sender',
        )

        thread_id, message_id = daemon._create_child_thread(parent, request)

        self.assertEqual(thread_id, 'thread-child')
        self.assertEqual(message_id, 'message-child')
        self.assertEqual(fake_feishu.interactive_replies[0]['message_id'], 'status-parent')
        self.assertEqual(fake_feishu.interactive_replies[0]['reply_in_thread'], True)
        card = fake_feishu.interactive_replies[0]['card']
        self.assertIsInstance(card, dict)
        assert isinstance(card, dict)
        self.assertNotEqual(card.get('schema'), '2.0')
        self.assertEqual(card['header']['template'], 'blue')
        content = card['elements'][0]['text']['content']
        self.assertIn('<at id=ou_sender></at>', content)

    def test_branch_thread_starts_from_top_level_chat_card_and_replies_intro(self) -> None:
        daemon = object.__new__(AgentDaemon)
        daemon.dry_send = False
        fake_feishu = FakeFeishu()
        daemon.feishu = fake_feishu

        parent = RunRecord(
            id=1,
            session_id=1,
            source_message_id='message-parent',
            prompt='delegate',
            state='running',
            status_phase='running',
            status='运行中',
            status_message_id='status-parent',
            codex_thread_id='thread-parent',
            turn_id='turn-parent',
            subject='Codex',
            display_title='Parent',
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
            finished_at=None,
            heartbeat_at=100,
            lease_until=130,
            created_at=100,
            updated_at=100,
            sender_open_id='ou_parent',
        )
        request = SpawnRequest(
            id=3,
            parent_session_id=1,
            parent_status_message_id='status-parent',
            parent_source_message_id='message-parent',
            chat_id='chat',
            cwd='/workspace',
            title='Branch',
            prompt='do branch work',
            context_profile='default',
            skills=(),
            state='claimed',
            sender_open_id='ou_sender',
            mode='branch',
        )

        thread_id, message_id = daemon._create_child_thread(parent, request, mode='branch')

        self.assertEqual(thread_id, 'message-branch')
        self.assertEqual(message_id, 'message-branch')
        self.assertEqual(
            fake_feishu.calls,
            [('send_interactive', 'chat'), ('reply_interactive', 'message-branch')],
        )
        self.assertEqual(fake_feishu.interactive_sends[0]['chat_id'], 'chat')
        card = fake_feishu.interactive_sends[0]['card']
        self.assertIsInstance(card, dict)
        assert isinstance(card, dict)
        self.assertEqual(card['header']['template'], 'blue')
        content = card['elements'][0]['text']['content']
        self.assertIn('<at id=ou_sender></at>', content)
        self.assertIn('并行子任务已启动', content)
        reply = fake_feishu.interactive_replies[0]
        self.assertEqual(reply['message_id'], 'message-branch')
        self.assertEqual(reply['reply_in_thread'], True)
        reply_card = reply['card']
        self.assertIsInstance(reply_card, dict)
        assert isinstance(reply_card, dict)
        reply_content = reply_card['elements'][0]['text']['content']
        self.assertIn('<at id=ou_sender></at>', reply_content)
        self.assertIn('并行子任务已启动', reply_content)


if __name__ == '__main__':
    unittest.main()
