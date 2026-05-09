from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentd.active_run import ActiveRun
from agentd.config import AgentdConfig, CodexConfig, FeishuConfig, WebConfig
from agentd.context import ContextConfig, ContextProfile, ContextPromptFile, ResolvedContext, SkillInfo
from agentd.models import AgentSession, IncomingMessage, RunRecord
from agentd.run_context import RunContextBuilder, RunnerContextBuilder
from agentd.runners import AgentRunControl
from agentd.schedule import ScheduleConfig


class RunContextBuilderTest(unittest.TestCase):
    def test_message_prompt_uses_channel_label(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            builder = make_builder(root, runner_kind='codex', runner_label='Codex')
            session = make_session()
            message = IncomingMessage(
                chat_id='browser',
                message_id='web-msg-1',
                text='hello from web',
                sender_open_id='user-1',
                sender_name='Sine',
                channel='web',
            )

            prompt = builder.message_prompt(message, session)

            self.assertIn('[Web Message]', prompt)
            self.assertIn('- agentd_session_id: 7', prompt)
            self.assertIn('Sine: hello from web', prompt)

    def test_runner_context_builds_env_and_developer_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            resolver = FakeContextResolver(root)
            config = make_config(root)
            builder = RunContextBuilder(
                config=config,
                context_resolver=resolver,
                runner_kind='claude_code',
                runner_label='Claude Code',
            )
            runner_builder = RunnerContextBuilder(builder)
            session = make_session(kind='child', runner_session_ref='claude-session-1')
            active = ActiveRun(run_id=3, session=session, control=AgentRunControl())
            run = make_run(session.id)

            resolved, env, instructions = runner_builder.build(active=active, run=run)

            self.assertIs(resolved, resolver.resolved)
            self.assertEqual(resolver.calls, [('work', ('agentd-ops',))])
            self.assertEqual(env['AGENTD_RUNNER_KIND'], 'claude_code')
            self.assertEqual(env['AGENTD_RUNNER_SESSION_REF'], 'claude-session-1')
            self.assertEqual(env['AGENTD_CONTEXT_PROFILE'], 'work')
            self.assertEqual(env['AGENTD_CONTEXT_SKILLS'], 'agentd-ops')
            self.assertIn('Feishu-to-Claude Code control plane', instructions)
            self.assertIn('This is already a Feishu child thread', instructions)
            self.assertIn('missing_skills: missing-skill', instructions)
            self.assertIn('## CONTEXT.md', instructions)
            self.assertIn('context text', instructions)


class FakeContextResolver:
    def __init__(self, root: Path) -> None:
        self.memory_dir = root / 'memory'
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.resolved = ResolvedContext(
            profile=ContextProfile(name='work', skills=('agentd-ops',)),
            skills=(SkillInfo(name='agentd-ops', description='Agentd ops', path=root / 'skills' / 'agentd-ops' / 'SKILL.md'),),
            missing_skills=('missing-skill',),
            memory_dir=self.memory_dir,
            prompt_files=(ContextPromptFile(path=root / 'CONTEXT.md', label='CONTEXT.md', text='context text'),),
        )

    def resolve(self, profile_name: str = '', extra_skills: tuple[str, ...] = ()) -> ResolvedContext:
        self.calls.append((profile_name, extra_skills))
        return self.resolved


def make_builder(root: Path, *, runner_kind: str, runner_label: str) -> RunContextBuilder:
    return RunContextBuilder(
        config=make_config(root),
        context_resolver=FakeContextResolver(root),
        runner_kind=runner_kind,
        runner_label=runner_label,
    )


def make_config(root: Path) -> AgentdConfig:
    context = ContextConfig(
        path=root / 'context.toml',
        context_dir=root,
        memory_dir=root / 'memory',
        profiles={'default': ContextProfile(name='default'), 'work': ContextProfile(name='work')},
    )
    return AgentdConfig(
        config_path=root / 'agentd.toml',
        home_dir=root,
        executable=root / 'agentd',
        source_dir=root / 'src',
        state_dir=root / 'state',
        workspace=root / 'workspace',
        log_level='INFO',
        context=context,
        schedules=ScheduleConfig(path=root / 'schedules.toml', jobs=()),
        feishu=FeishuConfig(),
        web=WebConfig(enabled=False),
        codex=CodexConfig(command='codex'),
    )


def make_session(*, kind: str = 'main', runner_session_ref: str = '') -> AgentSession:
    return AgentSession(
        id=7,
        kind=kind,
        chat_id='chat-1',
        thread_id='thread-1' if kind == 'child' else None,
        root_message_id='root-1' if kind == 'child' else None,
        codex_thread_id='legacy-session',
        cwd='/workspace',
        runner_kind='claude_code' if runner_session_ref else '',
        runner_session_ref=runner_session_ref or None,
    )


def make_run(session_id: int) -> RunRecord:
    return RunRecord(
        id=3,
        session_id=session_id,
        source_message_id='source-1',
        prompt='prompt',
        state='running',
        status_phase='running',
        status='启动',
        status_message_id='status-1',
        codex_thread_id='legacy-session',
        turn_id='turn-1',
        subject='Claude Code',
        display_title='Run',
        host='host-a',
        status_reply_in_thread=False,
        context_profile='work',
        skills=('agentd-ops',),
        hide_early_iterations=True,
        show_tool_details=False,
        truncate_content=True,
        final_message_text='',
        final_message_sent_at=None,
        error='',
        handoff_child_session_id=None,
        started_at=100,
        finished_at=None,
        heartbeat_at=100,
        lease_until=130,
        created_at=100,
        updated_at=100,
    )


if __name__ == '__main__':
    unittest.main()
