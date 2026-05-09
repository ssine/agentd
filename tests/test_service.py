from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from os import environ
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agentd.service import (
    clear_deferred_service_command,
    clear_startup_notice,
    defer_service_command,
    deferred_service_request_path,
    read_deferred_service_command,
    read_startup_notice,
    service_notice_chat_id,
    startup_notice_path,
    systemd_unit,
    write_deferred_service_command,
    write_startup_notice,
)


class ServiceRequestTest(unittest.TestCase):
    def test_deferred_service_command_round_trips_request(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            runtime_dir = Path(raw_dir)
            config = SimpleNamespace(runtime_dir=runtime_dir)
            payload = {
                'command': 'restart',
                'backend': 'process',
                'not_before': 123.5,
                'timeout_seconds': 7,
            }

            write_deferred_service_command(config, payload)

            self.assertEqual(read_deferred_service_command(config), payload)
            self.assertTrue(deferred_service_request_path(config).exists())

            clear_deferred_service_command(config)

            self.assertIsNone(read_deferred_service_command(config))

    def test_startup_notice_round_trips_request(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            runtime_dir = Path(raw_dir)
            config = SimpleNamespace(runtime_dir=runtime_dir)
            payload = {'chat_id': 'chat-1', 'text': 'started', 'created_at': 123.5}

            write_startup_notice(config, payload)

            self.assertEqual(read_startup_notice(config), payload)
            self.assertTrue(startup_notice_path(config).exists())

            clear_startup_notice(config)

            self.assertIsNone(read_startup_notice(config))

    def test_service_notice_chat_id_uses_agentd_context(self) -> None:
        with patch.dict(environ, {'AGENTD_CHAT_ID': 'chat-1'}, clear=False):
            self.assertEqual(service_notice_chat_id(), 'chat-1')

    def test_deferred_restart_records_notify_chat_id(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            runtime_dir = Path(raw_dir)
            config = SimpleNamespace(runtime_dir=runtime_dir)

            with (
                patch.dict(environ, {'AGENTD_CHAT_ID': 'chat-1'}, clear=False),
                patch('agentd.service.select_backend', return_value='process'),
                patch('agentd.service.service_running', return_value=True),
                redirect_stdout(StringIO()),
            ):
                result = defer_service_command(config, 'auto', 'restart', 5, timeout_seconds=7)

            self.assertEqual(result, 0)
            request = read_deferred_service_command(config)
            self.assertIsNotNone(request)
            assert request is not None
            self.assertEqual(request['notify_chat_id'], 'chat-1')

    def test_deferred_restart_writes_request_even_when_daemon_is_older_than_code(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            runtime_dir = Path(raw_dir)
            config = SimpleNamespace(runtime_dir=runtime_dir)
            stdout = StringIO()

            with (
                patch('agentd.service.select_backend', return_value='process'),
                patch('agentd.service.service_running', return_value=True),
                patch('agentd.service.launch_service_command') as launch_service,
                redirect_stdout(stdout),
            ):
                result = defer_service_command(config, 'auto', 'restart', 5, timeout_seconds=7)

            self.assertEqual(result, 0)
            launch_service.assert_not_called()
            request = read_deferred_service_command(config)
            self.assertIsNotNone(request)
            assert request is not None
            self.assertEqual(request['command'], 'restart')
            self.assertEqual(request['backend'], 'process')
            self.assertEqual(request['timeout_seconds'], 7)
            self.assertIn('scheduled agentd service restart after active runs finish', stdout.getvalue())

    def test_systemd_unit_description_is_runner_neutral(self) -> None:
        config = SimpleNamespace(
            executable=Path('/tmp/agentd'),
            config_path=Path('/tmp/agentd.toml'),
            workspace=Path('/tmp/workspace'),
        )

        unit = systemd_unit(config)

        self.assertIn('Description=agentd IM-to-agent control plane', unit)
        self.assertNotIn('Feishu to Codex bridge', unit)


if __name__ == '__main__':
    unittest.main()
