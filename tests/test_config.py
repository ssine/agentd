from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentd.config import load_config


class ConfigTest(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
