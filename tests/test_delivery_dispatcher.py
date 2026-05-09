from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from agentd.channels.delivery import binding_from_run, final_reply_delivery, status_delivery
from agentd.delivery_dispatcher import DeliveryDispatcher
from agentd.registry import Registry


class DeliveryDispatcherTest(unittest.TestCase):
    def test_web_delivery_is_recorded_without_feishu_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            registry = Registry(root / 'agentd.sqlite')
            session = registry.get_main_session('browser-1', str(root), channel='web', conversation_ref='browser-1')
            run = registry.create_run(
                session_id=session.id,
                source_message_id='msg-1',
                prompt='hello',
                host='host-a',
                subject='Codex',
                display_title='Web run',
            )
            binding = binding_from_run(run, session, registry.get_channel_binding(session.id))
            dispatcher = DeliveryDispatcher(
                registry=registry,
                feishu=FakeFeishu(),
                log=logging.getLogger('test'),
            )

            dispatcher.dispatch(final_reply_delivery(binding, text='done'), run_id=run.id, replace_sent=False)

            self.assertEqual(registry.claim_pending_outbox(), [])
            deliveries = registry.list_deliveries(run_id=run.id)
            self.assertEqual(len(deliveries), 1)
            self.assertEqual(deliveries[0].channel, 'web')
            self.assertEqual(deliveries[0].kind, 'final_state')
            self.assertEqual(deliveries[0].state, 'sent')
            updated = registry.get_run(run.id)
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertIsNotNone(updated.final_message_sent_at)

    def test_feishu_status_delivery_uses_outbox_and_marks_delivery_sent(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            registry = Registry(root / 'agentd.sqlite')
            session = registry.get_main_session('chat-1', str(root))
            run = registry.create_run(
                session_id=session.id,
                source_message_id='msg-1',
                prompt='hello',
                host='host-a',
                subject='Codex',
                display_title='Feishu run',
                status_message_id='card-1',
            )
            binding = binding_from_run(run, session, registry.get_channel_binding(session.id))
            fake = FakeFeishu()
            dispatcher = DeliveryDispatcher(
                registry=registry,
                feishu=fake,
                log=logging.getLogger('test'),
            )

            dispatcher.dispatch(
                status_delivery(
                    binding,
                    text='running',
                    card={'header': {'template': 'blue'}},
                    render_hash='hash-1',
                    remote_message_ref='card-1',
                ),
                run_id=run.id,
            )
            dispatcher.drain_feishu_outbox()

            self.assertEqual(fake.updated, [('card-1', {'header': {'template': 'blue'}})])
            deliveries = registry.list_deliveries(run_id=run.id)
            self.assertEqual(len(deliveries), 1)
            self.assertEqual(deliveries[0].channel, 'feishu')
            self.assertEqual(deliveries[0].state, 'sent')
            self.assertEqual(deliveries[0].external_ref, 'card-1')


class FakeFeishu:
    def __init__(self) -> None:
        self.updated: list[tuple[str, dict[str, object]]] = []

    def update_interactive(self, message_id: str, card: dict[str, object]) -> dict[str, object]:
        self.updated.append((message_id, card))
        return {'data': {'message_id': message_id}}


if __name__ == '__main__':
    unittest.main()
