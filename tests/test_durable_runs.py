from __future__ import annotations

import tempfile
import time
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path

from agentd.codex_app_server import CodexRunControl
from agentd.config import AgentdConfig, CodexConfig, FeishuConfig, WebConfig
from agentd.context import ContextConfig, ContextProfile
from agentd.daemon import ActiveRun, AgentDaemon
from agentd.registry import Registry
from agentd.schedule import ScheduleConfig


class DurableRunRegistryTest(unittest.TestCase):
    def test_run_events_and_final_outbox_survive_reopen(self) -> None:
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
                display_title='Durable run',
            )
            registry.append_run_event(run.id, 'agent_message', {'text': 'working', 'phase': 'commentary'})
            registry.upsert_outbox(
                kind='final_reply',
                dedupe_key=f'run:{run.id}:final',
                run_id=run.id,
                replace_sent=False,
                payload={'text': 'done'},
            )

            reopened = Registry(root / 'agentd.sqlite')
            persisted = reopened.get_run(run.id)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(persisted.prompt, 'hello')
            self.assertEqual(reopened.list_run_events(run.id)[0].payload['text'], 'working')

            claimed = reopened.claim_pending_outbox()
            self.assertEqual(len(claimed), 1)
            reopened.finish_outbox(claimed[0].id, sent=True)
            reopened.upsert_outbox(
                kind='final_reply',
                dedupe_key=f'run:{run.id}:final',
                run_id=run.id,
                replace_sent=False,
                payload={'text': 'done again'},
            )
            self.assertEqual(reopened.claim_pending_outbox(), [])

    def test_stale_active_runs_are_discoverable_by_lease(self) -> None:
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
                display_title='Durable run',
                lease_seconds=-1,
            )

            stale = registry.list_stale_active_runs(now=int(time.time()))

            self.assertEqual([item.id for item in stale], [run.id])


class DurableRunProjectionTest(unittest.TestCase):
    def test_recovery_marks_stale_running_run_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            config = make_config(root)
            daemon = AgentDaemon(config, dry_send=True)
            session = daemon.registry.get_main_session('chat-1', str(root))
            run = daemon.registry.create_run(
                session_id=session.id,
                source_message_id='msg-1',
                prompt='hello',
                host='host-a',
                subject='Codex',
                display_title='Durable run',
                lease_seconds=-1,
            )

            daemon._recover_stale_runs()

            recovered = daemon.registry.get_run(run.id)
            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertEqual(recovered.state, 'interrupted')
            self.assertEqual(recovered.status_phase, 'stopped')
            self.assertIn('无法重新附着', daemon.registry.list_run_events(run.id)[0].payload['text'])

    def test_status_projection_rebuilds_from_database_without_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            config = make_config(root)
            daemon = AgentDaemon(config, dry_send=True)
            session = daemon.registry.get_main_session('chat-1', str(root))
            run = daemon.registry.create_run(
                session_id=session.id,
                source_message_id='msg-1',
                prompt='hello',
                host='host-a',
                subject='Codex',
                display_title='Durable run',
            )
            daemon.registry.append_run_event(run.id, 'agent_message', {'text': 'working', 'phase': 'commentary'})
            daemon.registry.append_run_event(run.id, 'tool_started', {'tool': 'Bash', 'item_id': 'tool-1'})

            with redirect_stdout(StringIO()):
                daemon._publish_status_for_run(run.id)

            reopened = AgentDaemon(config, dry_send=True)
            view = reopened._load_run_view(run.id)
            self.assertIsNotNone(view)
            assert view is not None
            self.assertEqual(view.model_outputs, ['working'])
            self.assertEqual(view.iterations[-1].running_tools, {'Bash': 1})
            projected = reopened.registry.get_run(run.id)
            self.assertIsNotNone(projected)
            assert projected is not None
            self.assertEqual(projected.status_message_id, 'dry-run-status')

    def test_web_run_does_not_enqueue_feishu_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            config = make_config(root)
            daemon = AgentDaemon(config, dry_send=False)
            session = daemon.registry.get_main_session('web', str(root))
            run = daemon.registry.create_run(
                session_id=session.id,
                source_message_id='web-1',
                prompt='hello',
                host='host-a',
                subject='Codex',
                display_title='Web run',
            )
            active = ActiveRun(run_id=run.id, session=session, control=CodexRunControl())

            self.assertTrue(daemon._queue_final_once(active, 'done'))
            daemon._publish_status(active, force=True, create=True)

            self.assertEqual(daemon.registry.claim_pending_outbox(), [])
            updated = daemon.registry.get_run(run.id)
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated.final_message_text, 'done')

    def test_web_gateway_starts_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            config = replace(
                make_config(root),
                web=WebConfig(enabled=True, host='127.0.0.1', port=0),
            )
            daemon = AgentDaemon(config, dry_send=True)

            daemon._ensure_web_gateway()

            gateway = daemon._web_gateway
            self.assertIsNotNone(gateway)
            assert gateway is not None
            self.assertIsNotNone(gateway.server)
            assert gateway.server is not None
            self.assertGreater(gateway.server.server_address[1], 0)
            gateway.server.shutdown()
            gateway.server.server_close()


def make_config(root: Path) -> AgentdConfig:
    context = ContextConfig(
        path=root / 'context.toml',
        context_dir=root,
        memory_dir=root / 'memory',
        profiles={'default': ContextProfile(name='default')},
    )
    return AgentdConfig(
        config_path=root / 'agentd.toml',
        home_dir=root,
        executable=root / 'agentd',
        source_dir=root,
        state_dir=root / 'state',
        workspace=root,
        log_level='INFO',
        context=context,
        schedules=ScheduleConfig(path=root / 'schedules.toml', jobs=()),
        feishu=FeishuConfig(),
        web=WebConfig(enabled=False),
        codex=CodexConfig(command='codex'),
    )


if __name__ == '__main__':
    unittest.main()
