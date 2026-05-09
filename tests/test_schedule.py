from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from agentd.config import AgentdConfig, CodexConfig, FeishuConfig, WebConfig
from agentd.context import ContextConfig, ContextProfile
from agentd.daemon import AgentDaemon
from agentd.schedule import ScheduleConfig, load_schedule_config


class ScheduleReloadTest(unittest.TestCase):
    def test_scheduler_starts_even_when_no_jobs_are_enabled_initially(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            (root / 'schedules.toml').write_text('', encoding='utf-8')
            daemon = AgentDaemon(make_config(root), dry_send=True)

            with patch('agentd.daemon.threading.Thread') as thread:
                daemon._ensure_scheduler()

            self.assertTrue(daemon._scheduler_started)
            thread.assert_called_once()
            self.assertEqual(thread.call_args.kwargs['name'], 'agentd-scheduler')
            thread.return_value.start.assert_called_once()

    def test_schedule_config_reloads_when_file_content_changes_without_mtime_change(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            path = root / 'schedules.toml'
            path.write_text(interval_job_toml('example', enabled=False, prompt='first'), encoding='utf-8')
            daemon = AgentDaemon(make_config(root), dry_send=True)
            original_stat = path.stat()

            path.write_text(interval_job_toml('example', enabled=False, prompt='later'), encoding='utf-8')
            os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

            self.assertTrue(daemon._reload_schedule_config_if_changed())
            jobs = daemon._schedule_jobs_snapshot()
            self.assertEqual(len(jobs), 1)
            self.assertFalse(jobs[0].enabled)
            self.assertEqual(jobs[0].prompt, 'later')

    def test_schedule_config_parses_main_session_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            path = root / 'schedules.toml'
            path.write_text(daily_job_toml('daily', time='09:00', session='main'), encoding='utf-8')

            jobs = load_schedule_config(path).jobs

            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].session, 'main')

    def test_disabled_reloaded_job_does_not_keep_triggering_old_job(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            path = root / 'schedules.toml'
            path.write_text(interval_job_toml('interval', enabled=True), encoding='utf-8')
            daemon = AgentDaemon(make_config(root), dry_send=True)
            now = datetime(2026, 5, 9, 1, 0, tzinfo=ZoneInfo('UTC'))

            with patch.object(daemon, '_start_scheduled_job') as start:
                daemon._maybe_start_scheduled_job(daemon._schedule_jobs_snapshot()[0], now=now)
                self.assertEqual(start.call_count, 1)

                path.write_text(interval_job_toml('interval', enabled=False), encoding='utf-8')
                self.assertTrue(daemon._reload_schedule_config_if_changed(now=now))
                start.reset_mock()

                for job in daemon._schedule_jobs_snapshot():
                    daemon._maybe_start_scheduled_job(job, now=now + timedelta(seconds=60))

            start.assert_not_called()

    def test_deleted_reloaded_job_does_not_keep_triggering_old_job(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            path = root / 'schedules.toml'
            path.write_text(interval_job_toml('interval', enabled=True), encoding='utf-8')
            daemon = AgentDaemon(make_config(root), dry_send=True)

            path.write_text('', encoding='utf-8')
            self.assertTrue(daemon._reload_schedule_config_if_changed())
            self.assertEqual(daemon._schedule_jobs_snapshot(), ())

            with patch.object(daemon, '_start_scheduled_job') as start:
                for job in daemon._schedule_jobs_snapshot():
                    daemon._maybe_start_scheduled_job(job, now=datetime(2026, 5, 9, 1, 0, tzinfo=ZoneInfo('UTC')))

            start.assert_not_called()

    def test_new_daily_job_past_todays_time_does_not_backfill_on_reload(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            path = root / 'schedules.toml'
            path.write_text('', encoding='utf-8')
            daemon = AgentDaemon(make_config(root), dry_send=True)
            today_after_time = datetime(2026, 5, 9, 10, 0, tzinfo=ZoneInfo('Asia/Shanghai'))

            path.write_text(daily_job_toml('daily', time='09:00'), encoding='utf-8')
            self.assertTrue(daemon._reload_schedule_config_if_changed(now=today_after_time))

            with patch.object(daemon, '_start_scheduled_job') as start:
                daemon._maybe_start_scheduled_job(daemon._schedule_jobs_snapshot()[0], now=today_after_time)
                start.assert_not_called()

                daemon._maybe_start_scheduled_job(
                    daemon._schedule_jobs_snapshot()[0],
                    now=today_after_time + timedelta(days=1),
                )

            start.assert_called_once()
            self.assertEqual(start.call_args.args[1], '2026-05-10')

    def test_main_session_schedule_queues_while_busy_and_starts_pending_when_idle(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            path = root / 'schedules.toml'
            path.write_text(daily_job_toml('daily', time='23:30', session='main'), encoding='utf-8')
            daemon = AgentDaemon(make_config(root), dry_send=True)
            job = daemon._schedule_jobs_snapshot()[0]
            session = daemon.registry.get_main_session('chat-1', str(root))
            active_run = daemon.registry.create_run(
                session_id=session.id,
                source_message_id='msg-1',
                prompt='busy',
                host='host-a',
                subject='Codex',
                display_title='Busy run',
            )
            due_time = datetime(2026, 5, 9, 23, 31, tzinfo=ZoneInfo('Asia/Shanghai'))

            with patch.object(daemon, '_start_scheduled_job') as start:
                daemon._maybe_start_scheduled_job(job, now=due_time)
                start.assert_not_called()

            self.assertEqual(daemon.registry.get_pending_schedule_run(job.id), '2026-05-09')

            daemon.registry.update_run(
                active_run.id,
                state='succeeded',
                status_phase='done',
                status='完成',
                finished_at=1,
            )
            after_midnight = datetime(2026, 5, 10, 0, 5, tzinfo=ZoneInfo('Asia/Shanghai'))

            with patch.object(daemon, '_start_scheduled_job') as start:
                daemon._maybe_start_scheduled_job(job, now=after_midnight)

            start.assert_called_once()
            started_job, run_key, started_session = start.call_args.args
            self.assertEqual(started_job.id, 'daily')
            self.assertEqual(run_key, '2026-05-09')
            self.assertEqual(started_session.kind, 'main')
            self.assertEqual(daemon.registry.get_pending_schedule_run(job.id), '')


def make_config(root: Path) -> AgentdConfig:
    context = ContextConfig(
        path=root / 'context.toml',
        context_dir=root,
        memory_dir=root / 'memory',
        profiles={'default': ContextProfile(name='default')},
    )
    schedules_path = root / 'schedules.toml'
    schedules = load_schedule_config(schedules_path) if schedules_path.exists() else ScheduleConfig(schedules_path, ())
    return AgentdConfig(
        config_path=root / 'agentd.toml',
        home_dir=root,
        executable=root / 'agentd',
        source_dir=root,
        state_dir=root / 'state',
        workspace=root,
        log_level='INFO',
        context=context,
        schedules=schedules,
        feishu=FeishuConfig(),
        web=WebConfig(enabled=False),
        codex=CodexConfig(command='codex'),
    )


def interval_job_toml(job_id: str, *, enabled: bool, prompt: str = 'run interval') -> str:
    enabled_value = 'true' if enabled else 'false'
    return '\n'.join(
        [
            '[[jobs]]',
            f'id = "{job_id}"',
            f'enabled = {enabled_value}',
            'chat_id = "chat-1"',
            f'prompt = "{prompt}"',
            '',
            '[jobs.schedule]',
            'kind = "interval"',
            'seconds = 60',
            '',
        ]
    )


def daily_job_toml(job_id: str, *, time: str, session: str = 'schedule') -> str:
    return '\n'.join(
        [
            '[[jobs]]',
            f'id = "{job_id}"',
            'enabled = true',
            f'session = "{session}"',
            'chat_id = "chat-1"',
            'prompt = "run daily"',
            '',
            '[jobs.schedule]',
            'kind = "daily"',
            'timezone = "Asia/Shanghai"',
            f'time = "{time}"',
            '',
        ]
    )


if __name__ == '__main__':
    unittest.main()
