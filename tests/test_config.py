from __future__ import annotations

import tempfile
import unittest
from os import environ
from pathlib import Path
from unittest.mock import patch

from agentd.config import default_config_path, default_context_dir, load_config


class ConfigTest(unittest.TestCase):
    def test_default_config_path_uses_agentd_home_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            home = root / '.agentd'
            repo_local_config = root / 'repo-local-config'
            repo_local_config.mkdir()
            (repo_local_config / 'agentd.toml').write_text('[agentd]\n', encoding='utf-8')

            with patch.dict(environ, {'AGENTD_HOME': str(home)}):
                self.assertEqual(default_config_path(), home / 'agentd.toml')

    def test_default_context_dir_lives_under_agentd_home(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            home = Path(raw_dir) / '.agentd'

            with patch.dict(environ, {'AGENTD_HOME': str(home), 'AGENTD_CONTEXT_HOME': ''}, clear=False):
                self.assertEqual(default_context_dir(), home / 'context')

    def test_feishu_legacy_codex_env_vars_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            home = root / '.agentd'
            context_dir = root / 'agent-context'
            home.mkdir()
            context_dir.mkdir()
            (context_dir / 'context.toml').write_text(
                '\n'.join(
                    [
                        '[context]',
                        'skill_roots = []',
                        '',
                        '[profiles.default]',
                        'skills = []',
                        '',
                    ]
                ),
                encoding='utf-8',
            )
            config_path = home / 'agentd.toml'
            config_path.write_text(
                '\n'.join(
                    [
                        '[context]',
                        f'dir = "{context_dir}"',
                        '',
                    ]
                ),
                encoding='utf-8',
            )

            with patch.dict(
                environ,
                {
                    'AGENTD_FEISHU_APP_ID': '',
                    'AGENTD_FEISHU_APP_SECRET': '',
                    'CODEX_' + 'FEISHU_APP_ID': 'legacy-id',
                    'CODEX_' + 'FEISHU_APP_SECRET': 'legacy-secret',
                },
            ):
                config = load_config(config_path)

            self.assertEqual(config.feishu.app_id, '')
            self.assertEqual(config.feishu.app_secret, '')

    def test_home_state_and_external_context_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            home = root / '.agentd'
            context_dir = root / 'agent-context'
            source_dir = root / 'agentd-src'
            workspace = root / 'workspace'
            home.mkdir()
            context_dir.mkdir()
            source_dir.mkdir()
            workspace.mkdir()
            (context_dir / 'skills').mkdir()
            (context_dir / 'context.toml').write_text(
                '\n'.join(
                    [
                        '[context]',
                        'memory_dir = "memory"',
                        'skill_roots = ["skills"]',
                        'prompt_files = ["CONTEXT.md", "memory/MEMORY.md"]',
                        'prompt_file_max_bytes = 1234',
                        '',
                        '[profiles.default]',
                        'skills = []',
                        '',
                    ]
                ),
                encoding='utf-8',
            )
            config_path = home / 'agentd.toml'
            config_path.write_text(
                '\n'.join(
                    [
                        '[agentd]',
                        f'source_dir = "{source_dir}"',
                        f'workspace = "{workspace}"',
                        'state_dir = "state"',
                        '',
                        '[context]',
                        f'dir = "{context_dir}"',
                        'config = "context.toml"',
                        'schedules = "schedules.toml"',
                        '',
                    ]
                ),
                encoding='utf-8',
            )

            config = load_config(config_path)

            self.assertEqual(config.home_dir, home)
            self.assertEqual(config.source_dir, source_dir)
            self.assertEqual(config.claude.model, 'sonnet')
            self.assertEqual(config.workspace, workspace)
            self.assertEqual(config.state_dir, home / 'state')
            self.assertEqual(config.runtime_dir, home / 'state')
            self.assertEqual(config.context.context_dir, context_dir)
            self.assertEqual(config.context.path, context_dir / 'context.toml')
            self.assertEqual(config.context.memory_dir, context_dir / 'memory')
            self.assertEqual(config.context.skill_roots, (context_dir / 'skills',))
            self.assertEqual(
                config.context.prompt_files,
                (context_dir / 'CONTEXT.md', context_dir / 'memory/MEMORY.md'),
            )
            self.assertEqual(config.context.prompt_file_max_bytes, 1234)
            self.assertEqual(config.schedules.path, context_dir / 'schedules.toml')

    def test_workspace_defaults_to_context_dir(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            home = root / '.agentd'
            context_dir = root / 'agent-context'
            source_dir = root / 'agentd-src'
            home.mkdir()
            context_dir.mkdir()
            source_dir.mkdir()
            config_path = home / 'agentd.toml'
            config_path.write_text(
                '\n'.join(
                    [
                        '[agentd]',
                        f'source_dir = "{source_dir}"',
                        '',
                        '[context]',
                        f'dir = "{context_dir}"',
                        '',
                    ]
                ),
                encoding='utf-8',
            )

            config = load_config(config_path)

            self.assertEqual(config.workspace, context_dir)
            self.assertEqual(config.source_dir, source_dir)
            self.assertEqual(config.state_dir, home / 'state')

    def test_workspace_relative_context_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            workspace = Path(raw_dir)
            config_dir = workspace / 'config'
            config_dir.mkdir(parents=True)
            (workspace / 'skills').mkdir()
            (config_dir / 'context-profiles.toml').write_text(
                '\n'.join(
                    [
                        '[context]',
                        'skill_roots = ["skills"]',
                        '',
                        '[profiles.default]',
                        'skills = []',
                        '',
                    ]
                ),
                encoding='utf-8',
            )
            config_path = config_dir / 'agentd.toml'
            config_path.write_text(
                '\n'.join(
                    [
                        '[agentd]',
                        f'workspace = "{workspace}"',
                        'runtime_dir = ".agentd-runtime"',
                        'context_profiles = "config/context-profiles.toml"',
                        'schedules = "config/schedules.toml"',
                        '',
                    ]
                ),
                encoding='utf-8',
            )

            config = load_config(config_path)

            self.assertEqual(config.workspace, workspace)
            self.assertEqual(config.runtime_dir, workspace / '.agentd-runtime')
            self.assertEqual(config.context.path, config_dir / 'context-profiles.toml')
            self.assertEqual(config.context.skill_roots, (workspace / 'skills',))

    def test_codex_defaults_and_capture_config(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            home = root / '.agentd'
            source_dir = root / 'agentd-src'
            workspace = root / 'workspace'
            home.mkdir()
            source_dir.mkdir()
            workspace.mkdir()
            config_path = home / 'agentd.toml'
            config_path.write_text(
                '\n'.join(
                    [
                        '[agentd]',
                        f'source_dir = "{source_dir}"',
                        f'workspace = "{workspace}"',
                        'state_dir = "state"',
                        '',
                        '[web]',
                        'enabled = false',
                        'host = "0.0.0.0"',
                        'port = 9999',
                        '',
                        '[codex.capture]',
                        'enabled = true',
                        'upstream_mode = "chatgpt"',
                        'upstream_url = "https://example.test/responses"',
                        'save_sensitive_headers = true',
                        'archive_period = "month"',
                        'archive_format = "tar.zst"',
                        'zstd_level = 12',
                        '',
                        '[codex.otel]',
                        'enabled = true',
                        'environment = "local"',
                        'protocol = "json"',
                        'log_user_prompt = true',
                        'logs = true',
                        'traces = false',
                        'metrics = true',
                        'archive_period = "day"',
                        'zstd_level = 9',
                        '',
                    ]
                ),
                encoding='utf-8',
            )

            config = load_config(config_path)

            self.assertEqual(config.codex.command, 'codex')
            self.assertIsNone(config.codex.turn_timeout_seconds)
            self.assertIsNone(config.claude.turn_timeout_seconds)
            self.assertFalse(config.web.enabled)
            self.assertEqual(config.web.host, '0.0.0.0')
            self.assertEqual(config.web.port, 9999)
            self.assertTrue(config.codex.capture.enabled)
            self.assertEqual(config.codex.capture.upstream_mode, 'chatgpt')
            self.assertEqual(config.codex.capture.upstream_url, 'https://example.test/responses')
            self.assertEqual(config.codex.capture.capture_dir, home / 'state' / 'captures')
            self.assertEqual(config.codex.capture.db_path, home / 'state' / 'agentd.sqlite')
            self.assertTrue(config.codex.capture.save_sensitive_headers)
            self.assertEqual(config.codex.capture.archive_period, 'month')
            self.assertEqual(config.codex.capture.archive_format, 'tar.zst')
            self.assertEqual(config.codex.capture.zstd_level, 12)
            self.assertTrue(config.codex.otel.enabled)
            self.assertEqual(config.codex.otel.capture_dir, home / 'state' / 'captures')
            self.assertEqual(config.codex.otel.db_path, home / 'state' / 'agentd.sqlite')
            self.assertEqual(config.codex.otel.environment, 'local')
            self.assertEqual(config.codex.otel.protocol, 'json')
            self.assertTrue(config.codex.otel.log_user_prompt)
            self.assertTrue(config.codex.otel.logs)
            self.assertFalse(config.codex.otel.traces)
            self.assertTrue(config.codex.otel.metrics)
            self.assertEqual(config.codex.otel.archive_period, 'day')
            self.assertEqual(config.codex.otel.archive_format, 'tar.zst')
            self.assertEqual(config.codex.otel.zstd_level, 9)

    def test_runner_and_claude_code_config(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            home = root / '.agentd'
            source_dir = root / 'agentd-src'
            workspace = root / 'workspace'
            home.mkdir()
            source_dir.mkdir()
            workspace.mkdir()
            config_path = home / 'agentd.toml'
            config_path.write_text(
                '\n'.join(
                    [
                        '[agentd]',
                        f'source_dir = "{source_dir}"',
                        f'workspace = "{workspace}"',
                        '',
                        '[runner]',
                        'kind = "claude"',
                        '',
                        '[claude]',
                        'command = "aclaude"',
                        'model = "sonnet"',
                        'permission_mode = "bypassPermissions"',
                        'use_login_shell = true',
                        'turn_timeout_seconds = 120',
                        'extra_args = ["--max-budget-usd", "1"]',
                        '',
                    ]
                ),
                encoding='utf-8',
            )

            config = load_config(config_path)

            self.assertEqual(config.runner.kind, 'claude_code')
            self.assertEqual(config.claude.command, 'aclaude')
            self.assertEqual(config.claude.model, 'sonnet')
            self.assertTrue(config.claude.use_login_shell)
            self.assertEqual(config.claude.turn_timeout_seconds, 120)
            self.assertEqual(config.claude.extra_args, ('--max-budget-usd', '1'))

    def test_codex_turn_timeout_is_only_set_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            home = root / '.agentd'
            source_dir = root / 'agentd-src'
            workspace = root / 'workspace'
            home.mkdir()
            source_dir.mkdir()
            workspace.mkdir()
            config_path = home / 'agentd.toml'
            config_path.write_text(
                '\n'.join(
                    [
                        '[agentd]',
                        f'source_dir = "{source_dir}"',
                        f'workspace = "{workspace}"',
                        '',
                        '[codex]',
                        'turn_timeout_seconds = 120',
                        '',
                    ]
                ),
                encoding='utf-8',
            )

            config = load_config(config_path)

            self.assertEqual(config.codex.turn_timeout_seconds, 120)
            self.assertEqual(config.claude.turn_timeout_seconds, 120)


if __name__ == '__main__':
    unittest.main()
