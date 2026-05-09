from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agentd.cli import main


class CliTest(unittest.TestCase):
    def test_service_restart_defer_defaults_to_ten_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            config = SimpleNamespace(state_dir=root / 'state', log_dir=root / 'logs', log_level='CRITICAL')

            with (
                patch('agentd.cli.load_config', return_value=config),
                patch('agentd.service.service_command', return_value=0) as service_command,
            ):
                result = main(['--config', str(root / 'agentd.toml'), 'service', 'restart', '--defer'])

            self.assertEqual(result, 0)
            args = service_command.call_args.args[1]
            self.assertEqual(args.defer, 10.0)

    def test_service_restart_defer_accepts_explicit_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            config = SimpleNamespace(state_dir=root / 'state', log_dir=root / 'logs', log_level='CRITICAL')

            with (
                patch('agentd.cli.load_config', return_value=config),
                patch('agentd.service.service_command', return_value=0) as service_command,
            ):
                result = main(['--config', str(root / 'agentd.toml'), 'service', 'restart', '--defer', '30'])

            self.assertEqual(result, 0)
            args = service_command.call_args.args[1]
            self.assertEqual(args.defer, 30.0)


if __name__ == '__main__':
    unittest.main()
