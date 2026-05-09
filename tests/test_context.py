from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentd.context import ContextResolver, load_context_config


class ContextPromptFilesTest(unittest.TestCase):
    def test_profile_can_load_all_skills(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            skills_dir = root / 'skills'
            (skills_dir / 'alpha').mkdir(parents=True)
            (skills_dir / 'beta').mkdir()
            (skills_dir / 'alpha' / 'SKILL.md').write_text(
                '---\nname: alpha\n---\nalpha body\n',
                encoding='utf-8',
            )
            (skills_dir / 'beta' / 'SKILL.md').write_text(
                '---\nname: beta\n---\nbeta body\n',
                encoding='utf-8',
            )
            (root / 'context.toml').write_text(
                '\n'.join(
                    [
                        '[context]',
                        'skill_roots = ["skills"]',
                        '',
                        '[profiles.default]',
                        'skills = ["*"]',
                        '',
                    ]
                ),
                encoding='utf-8',
            )

            config = load_context_config(root / 'context.toml', root)
            resolved = ContextResolver(config, root).resolve()

            self.assertEqual([skill.name for skill in resolved.skills], ['agentd-ops', 'alpha', 'beta'])
            self.assertEqual(resolved.missing_skills, ())

    def test_agentd_ops_is_forced_into_empty_profile(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            (root / 'context.toml').write_text(
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

            config = load_context_config(root / 'context.toml', root)
            resolved = ContextResolver(config, root).resolve()

            self.assertEqual([skill.name for skill in resolved.skills], ['agentd-ops'])
            self.assertEqual(resolved.skills[0].path.name, 'SKILL.md')
            self.assertEqual(resolved.skills[0].path.parent.name, 'agentd-ops')
            self.assertEqual(resolved.skills[0].path.parent.parent.name, 'skills')

    def test_default_prompt_files_are_loaded_from_context_dir(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            memory_dir = root / 'memory'
            memory_dir.mkdir()
            (root / 'CONTEXT.md').write_text('general context\n', encoding='utf-8')
            (memory_dir / 'MEMORY.md').write_text('memory index\n', encoding='utf-8')
            (root / 'context.toml').write_text(
                '\n'.join(
                    [
                        '[context]',
                        'memory_dir = "memory"',
                        'skill_roots = []',
                        '',
                        '[profiles.default]',
                        'skills = []',
                        '',
                    ]
                ),
                encoding='utf-8',
            )

            config = load_context_config(root / 'context.toml', root)
            resolved = ContextResolver(config, root).resolve()

            self.assertEqual([file.label for file in resolved.prompt_files], ['CONTEXT.md', 'memory/MEMORY.md'])
            self.assertEqual([file.text for file in resolved.prompt_files], ['general context', 'memory index'])

    def test_prompt_files_can_be_limited(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            (root / 'context.md').write_text('abcdef', encoding='utf-8')
            (root / 'context.toml').write_text(
                '\n'.join(
                    [
                        '[context]',
                        'skill_roots = []',
                        'prompt_files = ["context.md"]',
                        'prompt_file_max_bytes = 3',
                        '',
                    ]
                ),
                encoding='utf-8',
            )

            config = load_context_config(root / 'context.toml', root)
            resolved = ContextResolver(config, root).resolve()

            self.assertEqual(len(resolved.prompt_files), 1)
            self.assertTrue(resolved.prompt_files[0].truncated)
            self.assertIn('abc', resolved.prompt_files[0].text)
            self.assertIn('truncated after 3 bytes', resolved.prompt_files[0].text)


if __name__ == '__main__':
    unittest.main()
