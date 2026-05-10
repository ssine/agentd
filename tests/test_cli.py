from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agentd.cli import main
from agentd.codex_usage import CodexLimitSnapshot, CodexUsageSnapshot, UsageWindow


class CliTest(unittest.TestCase):
    def test_init_creates_config_and_context_skeleton(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            config_path = root / '.agentd' / 'agentd.toml'
            context_dir = root / 'agent-context'
            source_dir = root / 'agentd'

            with redirect_stdout(StringIO()):
                result = main(
                    [
                        '--config',
                        str(config_path),
                        'init',
                        '--home-dir',
                        str(root / '.agentd'),
                        '--context-dir',
                        str(context_dir),
                        '--source-dir',
                        str(source_dir),
                    ]
                )

            self.assertEqual(result, 0)
            self.assertTrue(config_path.is_file())
            self.assertTrue((context_dir / 'context.toml').is_file())
            self.assertTrue((context_dir / 'memory' / 'MEMORY.md').is_file())

    def test_init_uses_home_dir_config_path_when_config_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            home_dir = root / 'home'
            source_dir = root / 'agentd'

            with redirect_stdout(StringIO()):
                result = main(
                    [
                        'init',
                        '--home-dir',
                        str(home_dir),
                        '--source-dir',
                        str(source_dir),
                        '--runner-kind',
                        'claude_code',
                    ]
                )

            self.assertEqual(result, 0)
            config_path = home_dir / 'agentd.toml'
            self.assertTrue(config_path.is_file())
            self.assertTrue((home_dir / 'context' / 'context.toml').is_file())
            self.assertIn('kind = "claude_code"', config_path.read_text(encoding='utf-8'))

    def test_init_create_feishu_app_writes_credentials_without_printing_secret(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            config_path = root / '.agentd' / 'agentd.toml'
            context_dir = root / 'context'
            stdout = StringIO()

            with (
                patch('agentd.cli.create_feishu_app', return_value=('cli_test', 'secret-value')) as create_app,
                redirect_stdout(stdout),
            ):
                result = main(
                    [
                        '--config',
                        str(config_path),
                        'init',
                        '--context-dir',
                        str(context_dir),
                        '--create-feishu-app',
                    ]
                )

            self.assertEqual(result, 0)
            create_app.assert_called_once()
            text = config_path.read_text(encoding='utf-8')
            self.assertIn('app_id = "cli_test"', text)
            self.assertIn('app_secret = "secret-value"', text)
            self.assertIn('created Feishu app and saved credentials', stdout.getvalue())
            self.assertNotIn('secret-value', stdout.getvalue())

    def test_init_create_feishu_app_refuses_existing_credentials_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            config_path = root / '.agentd' / 'agentd.toml'
            config_path.parent.mkdir()
            config_path.write_text(
                '\n'.join(['[feishu]', 'app_id = "cli_existing"', 'app_secret = "existing"']),
                encoding='utf-8',
            )

            with patch('agentd.cli.create_feishu_app') as create_app, redirect_stderr(StringIO()):
                result = main(['--config', str(config_path), 'init', '--create-feishu-app'])

            self.assertEqual(result, 2)
            create_app.assert_not_called()

    def test_init_create_feishu_app_updates_empty_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            config_path = root / '.agentd' / 'agentd.toml'
            context_dir = root / 'context'
            context_dir.mkdir()
            config_path.parent.mkdir()
            config_path.write_text(
                '\n'.join(
                    [
                        '[context]',
                        f'dir = "{context_dir}"',
                        '',
                        '[feishu]',
                        'app_id = ""',
                        'app_secret = ""',
                        '',
                    ]
                ),
                encoding='utf-8',
            )

            with (
                patch('agentd.cli.create_feishu_app', return_value=('cli_test', 'secret-value')),
                redirect_stdout(StringIO()),
            ):
                result = main(['--config', str(config_path), 'init', '--create-feishu-app'])

            self.assertEqual(result, 0)
            text = config_path.read_text(encoding='utf-8')
            self.assertIn('app_id = "cli_test"', text)
            self.assertIn('app_secret = "secret-value"', text)

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

    def test_codex_usage_command_prints_human_output(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            config = SimpleNamespace(state_dir=root / 'state', log_dir=root / 'logs', log_level='CRITICAL')

            stdout = StringIO()
            with (
                patch('agentd.cli.load_config', return_value=config),
                patch('agentd.cli.read_codex_usage', return_value=make_usage_snapshot()),
                redirect_stdout(stdout),
            ):
                result = main(['--config', str(root / 'agentd.toml'), 'codex-usage'])

            self.assertEqual(result, 0)
            self.assertIn('当前计划：Pro，当前未触发限额。', stdout.getvalue())
            self.assertIn('5 小时窗口：已用 5%，剩余 95%', stdout.getvalue())

    def test_codex_usage_command_can_print_json(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            config = SimpleNamespace(state_dir=root / 'state', log_dir=root / 'logs', log_level='CRITICAL')

            stdout = StringIO()
            with (
                patch('agentd.cli.load_config', return_value=config),
                patch('agentd.cli.read_codex_usage', return_value=make_usage_snapshot()),
                redirect_stdout(stdout),
            ):
                result = main(['--config', str(root / 'agentd.toml'), 'codex-usage', '--json'])

            self.assertEqual(result, 0)
            self.assertIn('"plan_type": "pro"', stdout.getvalue())
            self.assertIn('"remaining_percent": 95.0', stdout.getvalue())

def make_usage_snapshot() -> CodexUsageSnapshot:
    main = CodexLimitSnapshot(
        limit_id='codex',
        limit_name='',
        plan_type='pro',
        primary=UsageWindow(used_percent=5, window_duration_mins=300, resets_at=1778410334),
        secondary=UsageWindow(used_percent=17, window_duration_mins=10080, resets_at=1778857837),
    )
    return CodexUsageSnapshot(queried_at=1778401473, rate_limits=main, rate_limits_by_limit_id={'codex': main})


if __name__ == '__main__':
    unittest.main()
