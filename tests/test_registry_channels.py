from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentd.registry import Registry


class RegistryChannelPersistenceTest(unittest.TestCase):
    def test_main_session_creates_feishu_channel_binding_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            registry = Registry(root / 'agentd.sqlite')

            session = registry.get_main_session('chat-1', str(root))
            binding = registry.get_channel_binding(session.id)

            self.assertIsNotNone(binding)
            assert binding is not None
            self.assertEqual(binding.channel, 'feishu')
            self.assertEqual(binding.conversation_ref, 'chat-1')
            self.assertEqual(binding.thread_ref, '')

    def test_channel_binding_can_override_legacy_chat_id_shape(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            registry = Registry(root / 'agentd.sqlite')

            session = registry.get_main_session(
                'browser-1',
                str(root),
                channel='web',
                conversation_ref='browser-1',
            )
            run = registry.create_run(
                session_id=session.id,
                source_message_id='msg-1',
                prompt='hello',
                host='host-a',
                subject='Codex',
                display_title='Web run',
            )
            binding = registry.get_channel_binding(session.id)

            self.assertIsNotNone(binding)
            assert binding is not None
            self.assertEqual(binding.channel, 'web')
            self.assertEqual(binding.conversation_ref, 'browser-1')
            self.assertEqual(run.source_message_id, 'msg-1')

    def test_wecom_legacy_chat_id_unwraps_conversation_ref(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            registry = Registry(root / 'agentd.sqlite')

            session = registry.get_main_session('wecom:room-1', str(root), channel='wecom')
            binding = registry.get_channel_binding(session.id)

            self.assertIsNotNone(binding)
            assert binding is not None
            self.assertEqual(binding.channel, 'wecom')
            self.assertEqual(binding.conversation_ref, 'room-1')

    def test_delivery_ledger_survives_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            registry = Registry(root / 'agentd.sqlite')

            delivery_id = registry.upsert_delivery(
                channel='web',
                destination_ref='browser-1',
                kind='final_state',
                dedupe_key='run:1:final_state',
                payload={'text': 'done'},
                run_id=1,
                state='pending',
            )
            registry.mark_delivery_sent(delivery_id, external_ref='web-state')

            reopened = Registry(root / 'agentd.sqlite')
            delivery = reopened.get_delivery_by_dedupe_key('run:1:final_state')

            self.assertIsNotNone(delivery)
            assert delivery is not None
            self.assertEqual(delivery.channel, 'web')
            self.assertEqual(delivery.state, 'sent')
            self.assertEqual(delivery.external_ref, 'web-state')
            self.assertEqual(delivery.payload['text'], 'done')

    def test_runner_session_ref_is_persisted_with_legacy_codex_alias(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            registry = Registry(root / 'agentd.sqlite')
            session = registry.get_main_session('chat-1', str(root))
            run = registry.create_run(
                session_id=session.id,
                source_message_id='msg-1',
                prompt='hello',
                host='host-a',
                subject='Claude Code',
                display_title='Runner refs',
                runner_kind='claude_code',
            )

            registry.update_runner_session(session.id, 'claude-session-1', runner_kind='claude_code')
            registry.update_run(
                run.id,
                runner_kind='claude_code',
                runner_session_ref='claude-session-1',
                runner_turn_ref='claude-turn-1',
                codex_thread_id='claude-session-1',
                turn_id='claude-turn-1',
            )

            updated_session = registry.get_session(session.id)
            updated_run = registry.get_run(run.id)

            self.assertIsNotNone(updated_session)
            self.assertIsNotNone(updated_run)
            assert updated_session is not None
            assert updated_run is not None
            self.assertEqual(updated_session.runner_kind, 'claude_code')
            self.assertEqual(updated_session.agent_session_ref, 'claude-session-1')
            self.assertEqual(updated_session.codex_thread_id, 'claude-session-1')
            self.assertEqual(updated_run.runner_kind, 'claude_code')
            self.assertEqual(updated_run.agent_session_ref, 'claude-session-1')
            self.assertEqual(updated_run.agent_turn_ref, 'claude-turn-1')


if __name__ == '__main__':
    unittest.main()
