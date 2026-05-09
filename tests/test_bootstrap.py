from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentd.bootstrap import BootstrapOptions, init_agentd


class BootstrapTest(unittest.TestCase):
    def test_init_agentd_creates_config_and_context_skeleton(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            options = BootstrapOptions(
                config_path=root / '.agentd' / 'agentd.toml',
                home_dir=root / '.agentd',
                context_dir=root / 'agent-context',
                source_dir=root / 'agentd',
            )

            result = init_agentd(options)

            self.assertIn(options.config_path, result.created)
            self.assertTrue((options.context_dir / 'context.toml').is_file())
            self.assertTrue((options.context_dir / 'CONTEXT.md').is_file())
            self.assertTrue((options.context_dir / 'memory' / 'MEMORY.md').is_file())
            self.assertTrue((options.context_dir / 'memory' / 'projects').is_dir())
            self.assertTrue((options.context_dir / 'skills' / 'README.md').is_file())
            text = options.config_path.read_text(encoding='utf-8')
            self.assertIn(f'source_dir = "{options.source_dir}"', text)
            self.assertIn('[runner]\n# Supported values: "codex" and "claude_code".\nkind = "codex"', text)
            self.assertIn('[claude]\ncommand = "aclaude"\nmodel = "sonnet"', text)

    def test_init_agentd_does_not_overwrite_existing_files_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            context_dir = root / 'agent-context'
            context_dir.mkdir()
            context_file = context_dir / 'CONTEXT.md'
            context_file.write_text('custom\n', encoding='utf-8')
            options = BootstrapOptions(
                config_path=root / '.agentd' / 'agentd.toml',
                home_dir=root / '.agentd',
                context_dir=context_dir,
                source_dir=root / 'agentd',
            )

            result = init_agentd(options)

            self.assertIn(context_file, result.skipped)
            self.assertEqual(context_file.read_text(encoding='utf-8'), 'custom\n')


if __name__ == '__main__':
    unittest.main()
