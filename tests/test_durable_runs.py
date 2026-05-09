from __future__ import annotations

import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from agentd.codex_app_server import CodexRunControl
from agentd.config import AgentdConfig, CodexConfig, FeishuConfig, WebConfig
from agentd.context import ContextConfig, ContextProfile
from agentd.daemon import ActiveRun, AgentDaemon
from agentd.models import SpawnRequest
from agentd.registry import Registry
from agentd.schedule import ScheduleConfig
from agentd.service import (
    read_deferred_service_command,
    read_startup_notice,
    write_deferred_service_command,
    write_startup_notice,
)


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

    def test_requeued_status_outbox_resets_attempt_budget(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            registry = Registry(root / 'agentd.sqlite')

            for index in range(12):
                registry.upsert_outbox(
                    kind='status_card',
                    dedupe_key='run:1:status_card',
                    run_id=1,
                    replace_sent=True,
                    payload={'card': {'version': index}},
                )

                claimed = registry.claim_pending_outbox(max_attempts=10)
                self.assertEqual(len(claimed), 1)
                self.assertEqual(claimed[0].attempts, 1)
                registry.finish_outbox(claimed[0].id, sent=True)

                with registry.connect() as conn:
                    row = conn.execute(
                        'select state, attempts from feishu_outbox where id = ?',
                        (claimed[0].id,),
                    ).fetchone()
                self.assertIsNotNone(row)
                assert row is not None
                self.assertEqual(row['state'], 'sent')
                self.assertEqual(row['attempts'], 0)

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

    def test_idle_work_count_tracks_runs_cards_and_outbox(self) -> None:
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

            self.assertGreaterEqual(registry.idle_work_count(), 1)

            registry.update_run(
                run.id,
                state='succeeded',
                status_phase='done',
                status='完成',
                finished_at=int(time.time()),
            )
            self.assertEqual(registry.idle_work_count(), 1)

            registry.mark_card_sent(run.id, remote_message_id='card-1', render_hash='hash-1')
            self.assertEqual(registry.idle_work_count(), 0)

            registry.update_run_and_mark_card_dirty(run.id, status='完成')
            self.assertEqual(registry.idle_work_count(), 1)

            registry.mark_card_sent(run.id, remote_message_id='card-1', render_hash='hash-2')
            self.assertEqual(registry.idle_work_count(), 0)

            outbox_id = registry.upsert_outbox(
                kind='status_card',
                dedupe_key=f'run:{run.id}:status_card',
                run_id=run.id,
                payload={'card': {'version': 1}},
            )
            self.assertEqual(registry.idle_work_count(), 1)
            self.assertEqual([item.id for item in registry.claim_pending_outbox()], [outbox_id])
            self.assertEqual(registry.idle_work_count(), 1)

            registry.finish_outbox(outbox_id, sent=True)
            self.assertEqual(registry.idle_work_count(), 0)

    def test_sending_outbox_can_be_reset_immediately_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            registry = Registry(root / 'agentd.sqlite')
            outbox_id = registry.upsert_outbox(
                kind='status_card',
                dedupe_key='run:1:status_card',
                run_id=1,
                payload={'card': {'version': 1}},
            )

            self.assertEqual([item.id for item in registry.claim_pending_outbox()], [outbox_id])
            self.assertEqual(registry.claim_pending_outbox(), [])

            registry.reset_stuck_outbox(older_than_seconds=0)

            self.assertEqual([item.id for item in registry.claim_pending_outbox()], [outbox_id])

    def test_recently_finished_runs_are_detected_for_restart_grace(self) -> None:
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
                display_title='Finished run',
            )
            now = int(time.time())
            registry.update_run(run.id, state='succeeded', status_phase='done', status='完成', finished_at=now)

            self.assertTrue(registry.has_recently_finished_run(within_seconds=5, now=now + 4))
            self.assertFalse(registry.has_recently_finished_run(within_seconds=5, now=now + 6))


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

    def test_recovery_finishes_stale_run_when_final_reply_was_sent(self) -> None:
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
            daemon.registry.update_run(
                run.id,
                final_message_text='done',
                final_message_sent_at=int(time.time()),
            )

            daemon._recover_stale_runs()

            recovered = daemon.registry.get_run(run.id)
            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertEqual(recovered.state, 'succeeded')
            self.assertEqual(recovered.status_phase, 'done')
            self.assertEqual(recovered.status, '完成')
            self.assertEqual(daemon.registry.list_run_events(run.id), [])

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
            session = daemon.registry.get_main_session(
                'browser-1',
                str(root),
                channel='web',
                conversation_ref='browser-1',
            )
            run = daemon.registry.create_run(
                session_id=session.id,
                source_message_id='msg-1',
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
            deliveries = daemon.registry.list_deliveries(run_id=run.id)
            self.assertEqual({delivery.channel for delivery in deliveries}, {'web'})
            self.assertEqual({delivery.state for delivery in deliveries}, {'sent'})

    def test_retryable_codex_error_keeps_status_card_running(self) -> None:
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
                status_message_id='status-card',
            )
            active = ActiveRun(run_id=run.id, session=session, control=CodexRunControl())
            payload = {
                'error': {'message': 'Reconnecting... 1/5'},
                'willRetry': True,
                'threadId': 'thread-1',
                'turnId': 'turn-1',
            }

            with redirect_stdout(StringIO()):
                daemon._handle_codex_event(active, {'type': 'error', 'text': json.dumps(payload)})

            updated = daemon.registry.get_run(run.id)
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated.state, 'running')
            self.assertEqual(updated.status_phase, 'running')
            self.assertEqual(updated.status, 'Codex 正在重连')
            self.assertEqual(updated.error, '')
            self.assertIsNone(updated.finished_at)
            events = daemon.registry.list_run_events(run.id)
            self.assertEqual(events, [])

            with daemon.registry.connect() as conn:
                row = conn.execute(
                    """
                    select payload_json
                    from feishu_outbox
                    where run_id = ? and kind = 'status_card'
                    """,
                    (run.id,),
                ).fetchone()
            self.assertIsNotNone(row)
            assert row is not None
            card = json.loads(str(row['payload_json']))['card']
            self.assertEqual(card['header']['template'], 'blue')
            self.assertNotIn('错误信息', json.dumps(card, ensure_ascii=False))

    def test_status_message_target_change_resends_same_render_hash(self) -> None:
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
                status_message_id='old-card',
            )
            daemon.registry.update_run_and_mark_card_dirty(
                run.id,
                state='succeeded',
                status_phase='done',
                status='完成',
                finished_at=int(time.time()),
            )

            with redirect_stdout(StringIO()):
                daemon._publish_status_for_run(run.id, force=True)

            projection = daemon.registry.get_card_projection(run.id)
            self.assertIsNotNone(projection)
            assert projection is not None
            first_hash = str(projection['last_render_hash'])
            self.assertTrue(first_hash)

            daemon.registry.update_run(run.id, status_message_id='new-card')
            projection = daemon.registry.get_card_projection(run.id)
            self.assertIsNotNone(projection)
            assert projection is not None
            self.assertEqual(projection['dirty'], 1)
            self.assertEqual(projection['last_render_hash'], '')

            with redirect_stdout(StringIO()):
                daemon._publish_status_for_run(run.id, force=True)

            with daemon.registry.connect() as conn:
                row = conn.execute(
                    """
                    select payload_json
                    from feishu_outbox
                    where run_id = ? and kind = 'status_card'
                    """,
                    (run.id,),
                ).fetchone()
            self.assertIsNotNone(row)
            assert row is not None
            payload = json.loads(str(row['payload_json']))
            self.assertEqual(payload['message_id'], 'new-card')
            self.assertEqual(payload['card']['header']['template'], 'green')

    def test_terminal_status_replay_resends_same_render_hash(self) -> None:
        class FakeFeishu:
            def __init__(self) -> None:
                self.updated: list[tuple[str, dict[str, object]]] = []

            def update_interactive(self, message_id: str, card: dict[str, object]) -> dict[str, object]:
                self.updated.append((message_id, card))
                return {'data': {'message_id': message_id}}

        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            config = make_config(root)
            daemon = AgentDaemon(config, dry_send=False)
            fake = FakeFeishu()
            daemon.feishu = fake  # type: ignore[assignment]
            session = daemon.registry.get_main_session('chat-1', str(root))
            run = daemon.registry.create_run(
                session_id=session.id,
                source_message_id='msg-1',
                prompt='hello',
                host='host-a',
                subject='Codex',
                display_title='Durable run',
                status_message_id='card-1',
            )
            daemon.registry.update_run_and_mark_card_dirty(
                run.id,
                state='succeeded',
                status_phase='done',
                status='完成',
                finished_at=int(time.time()),
            )

            daemon._publish_status_for_run(run.id, force=True)
            daemon._last_feishu_send_at = 0
            daemon._replay_terminal_status_card(run.id)

            self.assertEqual([call[0] for call in fake.updated], ['card-1', 'card-1'])
            self.assertEqual(fake.updated[-1][1]['header']['template'], 'green')

    def test_deferred_restart_waits_for_terminal_replay_grace(self) -> None:
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
                display_title='Finished run',
                status_message_id='card-1',
            )
            daemon.registry.update_run(
                run.id,
                state='succeeded',
                status_phase='done',
                status='完成',
                finished_at=int(time.time()),
            )
            daemon.registry.mark_card_sent(run.id, remote_message_id='card-1', render_hash='hash-1')
            write_deferred_service_command(
                config,
                {'command': 'restart', 'backend': 'process', 'not_before': 0, 'timeout_seconds': 7},
            )

            with patch('agentd.service.launch_service_command') as launch:
                daemon._maybe_run_deferred_service_command()

            launch.assert_not_called()
            self.assertIsNotNone(read_deferred_service_command(config))

    def test_spawned_child_uses_thread_intro_message_as_status_card(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            daemon = AgentDaemon(make_config(root), dry_send=True)
            parent_session = daemon.registry.get_main_session('chat-1', str(root))
            parent_run = daemon.registry.create_run(
                session_id=parent_session.id,
                source_message_id='user-message',
                prompt='delegate',
                host='host-a',
                subject='Codex',
                display_title='Parent run',
                status_message_id='parent-card',
            )
            parent_active = ActiveRun(run_id=parent_run.id, session=parent_session, control=CodexRunControl())
            with daemon._active_lock:
                daemon._active_runs[parent_session.id] = parent_active
            request = SpawnRequest(
                id=1,
                parent_session_id=parent_session.id,
                parent_status_message_id='parent-card',
                parent_source_message_id='user-message',
                chat_id='chat-1',
                cwd=str(root),
                title='Child run',
                prompt='do the child work',
                context_profile='',
                skills=(),
                state='claimed',
                sender_open_id='ou_user',
            )

            with (
                patch.object(daemon, '_status_ticker', return_value=None),
                patch.object(daemon, '_run_turn_worker', return_value=None),
                redirect_stdout(StringIO()),
            ):
                daemon._handle_spawn_request(request)

            parent_after = daemon.registry.get_run(parent_run.id)
            self.assertIsNotNone(parent_after)
            assert parent_after is not None
            self.assertEqual(parent_after.state, 'interrupted')
            self.assertEqual(parent_after.status_phase, 'stopped')
            self.assertEqual(parent_after.status, '已移交子任务')

            child_sessions = [session for session in daemon.registry.list_sessions() if session.kind == 'child']
            self.assertEqual(len(child_sessions), 1)
            child_session = child_sessions[0]
            self.assertEqual(child_session.root_message_id, 'dry-thread-message-1')
            child_runs = daemon.registry.list_runs(session_id=child_session.id)
            self.assertEqual(len(child_runs), 1)
            child_run = child_runs[0]
            self.assertEqual(child_run.source_message_id, 'dry-thread-message-1')
            self.assertEqual(child_run.status_message_id, 'dry-thread-message-1')

    def test_spawn_branch_keeps_parent_running(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            daemon = AgentDaemon(make_config(root), dry_send=True)
            parent_session = daemon.registry.get_main_session('chat-1', str(root))
            parent_run = daemon.registry.create_run(
                session_id=parent_session.id,
                source_message_id='user-message',
                prompt='do parent work',
                host='host-a',
                subject='Codex',
                display_title='Parent run',
                status_message_id='parent-card',
            )
            parent_active = ActiveRun(run_id=parent_run.id, session=parent_session, control=CodexRunControl())
            with daemon._active_lock:
                daemon._active_runs[parent_session.id] = parent_active
            request = SpawnRequest(
                id=2,
                parent_session_id=parent_session.id,
                parent_status_message_id='parent-card',
                parent_source_message_id='user-message',
                chat_id='chat-1',
                cwd=str(root),
                title='Parallel run',
                prompt='do parallel work',
                context_profile='',
                skills=(),
                state='claimed',
                sender_open_id='ou_user',
                mode='branch',
            )

            with (
                patch.object(daemon, '_status_ticker', return_value=None),
                patch.object(daemon, '_run_turn_worker', return_value=None),
                redirect_stdout(StringIO()),
            ):
                daemon._handle_spawn_request(request)

            parent_after = daemon.registry.get_run(parent_run.id)
            self.assertIsNotNone(parent_after)
            assert parent_after is not None
            self.assertEqual(parent_after.state, 'running')
            self.assertEqual(parent_after.status_phase, 'running')
            self.assertFalse(parent_active.done.is_set())
            self.assertIs(daemon._active_for(parent_session.id), parent_active)

            child_sessions = [session for session in daemon.registry.list_sessions() if session.kind == 'child']
            self.assertEqual(len(child_sessions), 1)
            child_runs = daemon.registry.list_runs(session_id=child_sessions[0].id)
            self.assertEqual(len(child_runs), 1)
            self.assertEqual(child_runs[0].status_message_id, 'dry-thread-message-2')

    def test_child_session_cannot_spawn_nested_child(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            daemon = AgentDaemon(make_config(root), dry_send=True)
            child_session = daemon.registry.bind_child_session(
                'chat-1', 'thread-1', str(root), root_message_id='card-1'
            )
            child_run = daemon.registry.create_run(
                session_id=child_session.id,
                source_message_id='card-1',
                prompt='child work',
                host='host-a',
                subject='子任务',
                display_title='Child run',
                status_message_id='card-1',
            )
            child_active = ActiveRun(run_id=child_run.id, session=child_session, control=CodexRunControl())
            with daemon._active_lock:
                daemon._active_runs[child_session.id] = child_active
            request = SpawnRequest(
                id=3,
                parent_session_id=child_session.id,
                parent_status_message_id='card-1',
                parent_source_message_id='card-1',
                chat_id='chat-1',
                cwd=str(root),
                title='Nested run',
                prompt='do nested work',
                context_profile='',
                skills=(),
                state='claimed',
                sender_open_id='ou_user',
                mode='branch',
            )

            with redirect_stdout(StringIO()):
                daemon._handle_spawn_request(request)

            child_after = daemon.registry.get_run(child_run.id)
            self.assertIsNotNone(child_after)
            assert child_after is not None
            self.assertEqual(child_after.status, '子任务内不支持再创建子任务')
            self.assertEqual(
                len([session for session in daemon.registry.list_sessions() if session.kind == 'child']), 1
            )

    def test_feishu_outbox_send_slot_is_throttled(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            daemon = AgentDaemon(make_config(root), dry_send=False)
            daemon._last_feishu_send_at = time.monotonic()

            with patch('agentd.delivery_dispatcher.time.sleep') as sleep:
                daemon._wait_for_feishu_send_slot()

            sleep.assert_called_once()
            self.assertGreater(sleep.call_args.args[0], 0.9)
            self.assertLessEqual(sleep.call_args.args[0], 1)

    def test_startup_notice_is_cleared_in_dry_send_mode(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            daemon = AgentDaemon(make_config(root), dry_send=True)
            write_startup_notice(
                daemon.config,
                {'chat_id': 'chat-1', 'text': 'started', 'created_at': time.time()},
            )

            daemon._send_startup_notice_if_needed()

            self.assertIsNone(read_startup_notice(daemon.config))

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

    def test_developer_instructions_include_context_prompt_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            memory_dir = root / 'memory'
            memory_dir.mkdir()
            (root / 'CONTEXT.md').write_text('general context', encoding='utf-8')
            (memory_dir / 'MEMORY.md').write_text('memory index', encoding='utf-8')
            context = ContextConfig(
                path=root / 'context.toml',
                context_dir=root,
                memory_dir=memory_dir,
                prompt_files=(root / 'CONTEXT.md', memory_dir / 'MEMORY.md'),
                profiles={'default': ContextProfile(name='default')},
            )
            config = replace(make_config(root), context=context)
            daemon = AgentDaemon(config, dry_send=True)
            session = daemon.registry.get_main_session('chat-1', str(root))
            active = ActiveRun(run_id=1, session=session, control=CodexRunControl())

            text = daemon._developer_instructions(active, daemon.context_resolver.resolve())

            self.assertIn(f'config_path: {config.config_path}', text)
            self.assertIn(f'home_dir: {config.home_dir}', text)
            self.assertIn(f'source_dir: {config.source_dir}', text)
            self.assertIn(f'state_dir: {config.state_dir}', text)
            self.assertIn(f'workspace: {config.workspace}', text)
            self.assertIn(f'context_dir: {config.context.context_dir}', text)
            self.assertIn('prompt_files: CONTEXT.md, memory/MEMORY.md', text)
            self.assertIn('general context', text)
            self.assertIn('memory index', text)
            self.assertNotIn('Follow repo guidance from AGENTS.md', text)


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
