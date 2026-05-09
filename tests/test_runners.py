from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agentd.config import ClaudeCodeConfig
from agentd.models import AgentSession
from agentd.runners import AgentTurnRequest
from agentd.runners.claude_code import ClaudeCodeRunner


class ClaudeCodeRunnerTest(unittest.TestCase):
    def test_invokes_aclaude_through_login_shell_and_normalizes_result(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            runner = ClaudeCodeRunner(
                ClaudeCodeConfig(
                    command='aclaude',
                    model='sonnet',
                    permission_mode='bypassPermissions',
                    use_login_shell=True,
                    turn_timeout_seconds=10,
                    extra_args=('--max-budget-usd', '1'),
                ),
                root / 'logs',
            )
            session = AgentSession(
                id=7,
                kind='main',
                chat_id='web',
                thread_id=None,
                root_message_id=None,
                codex_thread_id='legacy-codex-alias',
                cwd=str(root),
                runner_kind='claude_code',
                runner_session_ref='claude-session-before',
            )
            events: list[dict[str, object]] = []

            with patch('agentd.runners.claude_code.subprocess.run') as run:
                run.return_value = SimpleNamespace(
                    returncode=0,
                    stdout='{"type":"result","result":"done","session_id":"claude-session-after"}\n',
                    stderr='',
                )

                result = runner.start_turn(
                    AgentTurnRequest(
                        session=session,
                        prompt='do it',
                        developer_instructions='system rules',
                        extra_env={'AGENTD_SESSION_ID': '7'},
                    ),
                    event_sink=events.append,
                )

            self.assertEqual(result.session_ref, 'claude-session-after')
            self.assertEqual(result.final_text, 'done')
            self.assertEqual(result.status, 'completed')
            argv = run.call_args.args[0]
            self.assertEqual(argv[:2], ['zsh', '-lic'])
            self.assertIn('aclaude', argv[2])
            self.assertIn('--print', argv[2])
            self.assertIn('--resume claude-session-before', argv[2])
            self.assertIn('--append-system-prompt-file', argv[2])
            system_prompt_files = list((root / 'logs').glob('claude-code-system-*.md'))
            self.assertEqual(len(system_prompt_files), 1)
            self.assertEqual(system_prompt_files[0].read_text(encoding='utf-8'), 'system rules')
            self.assertEqual(run.call_args.kwargs['input'], 'do it')
            self.assertEqual(run.call_args.kwargs['env']['AGENTD_SESSION_ID'], '7')
            self.assertEqual([event['type'] for event in events], ['thread_ready', 'turn_started', 'agent_message', 'final_answer_ready', 'turn_completed'])

    def test_failed_process_returns_failed_turn(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            runner = ClaudeCodeRunner(ClaudeCodeConfig(command='claude', use_login_shell=False), root / 'logs')
            session = AgentSession(
                id=7,
                kind='main',
                chat_id='web',
                thread_id=None,
                root_message_id=None,
                codex_thread_id=None,
                cwd=str(root),
            )

            with patch('agentd.runners.claude_code.subprocess.run') as run:
                run.return_value = SimpleNamespace(returncode=1, stdout='', stderr='auth failed')

                result = runner.start_turn(AgentTurnRequest(session=session, prompt='hello'))

            self.assertEqual(result.status, 'failed')
            self.assertEqual(result.final_text, 'auth failed')


if __name__ == '__main__':
    unittest.main()
