from __future__ import annotations

import unittest

from agentd.channels.delivery import (
    binding_from_run,
    delivery_needs_queue,
    final_reply_delivery,
    status_delivery,
)
from agentd.models import AgentSession, ChannelBindingRecord, RunRecord


class DeliveryContractTest(unittest.TestCase):
    def test_feishu_status_delivery_keeps_interactive_card_payload(self) -> None:
        session = make_session(chat_id='chat-1')
        run = make_run(source_message_id='msg-1', status_message_id='card-1')
        binding = binding_from_run(run, session)

        delivery = status_delivery(
            binding,
            text='running',
            card={'header': {'template': 'blue'}},
            render_hash='hash-1',
            remote_message_ref='card-1',
        )

        self.assertEqual(binding.channel, 'feishu')
        self.assertTrue(delivery_needs_queue(delivery))
        self.assertEqual(delivery.kind, 'status_card')
        self.assertEqual(delivery.payload['action'], 'update')
        self.assertEqual(delivery.payload['chat_id'], 'chat-1')
        self.assertEqual(delivery.payload['message_id'], 'card-1')

    def test_web_final_delivery_is_state_not_feishu_outbox(self) -> None:
        session = make_session(chat_id='web')
        run = make_run(source_message_id='web-1')
        binding = binding_from_run(run, session)

        delivery = final_reply_delivery(binding, text='done')

        self.assertEqual(binding.channel, 'web')
        self.assertFalse(delivery_needs_queue(delivery))
        self.assertEqual(delivery.kind, 'final_state')
        self.assertEqual(delivery.payload['text'], 'done')

    def test_durable_binding_decides_channel_without_message_id_prefix(self) -> None:
        session = make_session(chat_id='browser-1')
        run = make_run(source_message_id='msg-1')
        durable = ChannelBindingRecord(
            id=1,
            session_id=session.id,
            channel='web',
            conversation_ref='browser-1',
            thread_ref='',
            root_message_ref='',
            metadata={},
            created_at=100,
            updated_at=100,
        )
        binding = binding_from_run(run, session, durable)

        delivery = final_reply_delivery(binding, text='done')

        self.assertEqual(binding.channel, 'web')
        self.assertEqual(delivery.kind, 'final_state')
        self.assertEqual(delivery.dedupe_key, 'run:1:final_state')

    def test_wecom_status_delivery_degrades_to_text(self) -> None:
        session = make_session(chat_id='wecom:room-1')
        run = make_run(source_message_id='wecom-msg-1')
        binding = binding_from_run(run, session)

        delivery = status_delivery(binding, text='running', render_hash='hash-1')

        self.assertEqual(binding.channel, 'wecom')
        self.assertFalse(delivery_needs_queue(delivery))
        self.assertEqual(delivery.kind, 'status_text')
        self.assertEqual(delivery.payload['text'], 'running')


def make_session(*, chat_id: str = 'chat-1') -> AgentSession:
    return AgentSession(
        id=1,
        kind='main',
        chat_id=chat_id,
        thread_id=None,
        root_message_id=None,
        codex_thread_id=None,
        cwd='/workspace',
    )


def make_run(*, source_message_id: str, status_message_id: str = '') -> RunRecord:
    return RunRecord(
        id=1,
        session_id=1,
        source_message_id=source_message_id,
        prompt='hello',
        state='running',
        status_phase='running',
        status='running',
        status_message_id=status_message_id,
        codex_thread_id='',
        turn_id='',
        subject='Codex',
        display_title='Run',
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
    )


if __name__ == '__main__':
    unittest.main()
