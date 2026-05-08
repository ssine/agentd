from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentd.config import load_config


class ConfigTest(unittest.TestCase):
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
            self.assertEqual(config.workspace, workspace)
            self.assertEqual(config.state_dir, home / 'state')
            self.assertEqual(config.runtime_dir, home / 'state')
            self.assertEqual(config.context.context_dir, context_dir)
            self.assertEqual(config.context.path, context_dir / 'context.toml')
            self.assertEqual(config.context.memory_dir, context_dir / 'memory')
            self.assertEqual(config.context.skill_roots, (context_dir / 'skills',))
            self.assertEqual(config.schedules.path, context_dir / 'schedules.toml')

    def test_workspace_relative_context_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            workspace = Path(raw_dir)
            config_dir = workspace / '.agents/config'
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
                        'runtime_dir = ".agents/runtime"',
                        'context_profiles = ".agents/config/context-profiles.toml"',
                        'schedules = ".agents/config/schedules.toml"',
                        '',
                    ]
                ),
                encoding='utf-8',
            )

            config = load_config(config_path)

            self.assertEqual(config.workspace, workspace)
            self.assertEqual(config.runtime_dir, workspace / '.agents/runtime')
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
                        '[codex.capture]',
                        'enabled = true',
                        'upstream_mode = "chatgpt"',
                        'upstream_url = "https://example.test/responses"',
                        'save_sensitive_headers = true',
                        'archive_period = "month"',
                        'archive_format = "tar.zst"',
                        'zstd_level = 12',
                        '',
                    ]
                ),
                encoding='utf-8',
            )

            config = load_config(config_path)

            self.assertEqual(config.codex.command, 'codex')
            self.assertTrue(config.codex.capture.enabled)
            self.assertEqual(config.codex.capture.upstream_mode, 'chatgpt')
            self.assertEqual(config.codex.capture.upstream_url, 'https://example.test/responses')
            self.assertEqual(config.codex.capture.capture_dir, home / 'state' / 'captures')
            self.assertEqual(config.codex.capture.db_path, home / 'state' / 'agentd.sqlite')
            self.assertTrue(config.codex.capture.save_sensitive_headers)
            self.assertEqual(config.codex.capture.archive_period, 'month')
            self.assertEqual(config.codex.capture.archive_format, 'tar.zst')
            self.assertEqual(config.codex.capture.zstd_level, 12)


if __name__ == '__main__':
    unittest.main()
