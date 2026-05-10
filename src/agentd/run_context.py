from __future__ import annotations

from .active_run import ActiveRun
from .channel_utils import channel_from_message, channel_label
from .config import AgentdConfig
from .context import ContextResolver, ResolvedContext
from .models import AgentSession, IncomingMessage, RunRecord, SpawnRequest
from .schedule import ScheduleJob


class RunContextBuilder:
    def __init__(
        self,
        *,
        config: AgentdConfig,
        context_resolver: ContextResolver,
        runner_kind: str,
        runner_label: str,
    ) -> None:
        self.config = config
        self.context_resolver = context_resolver
        self.runner_kind = runner_kind
        self.runner_label = runner_label

    def message_prompt(self, message: IncomingMessage, session: AgentSession) -> str:
        sender = message.sender_name or message.sender_open_id or 'unknown'
        thread_id = message.thread_id or 'none'
        channel = channel_from_message(message)
        lines = [
            f'[{channel_label(channel)} Message]',
            f'- sender: {sender} (open_id: {message.sender_open_id or "unknown"})',
            f'- chat_id: {message.chat_id}',
            f'- message_id: {message.message_id}',
            f'- thread_id: {thread_id}',
            f'- agentd_session_id: {session.id}',
            '',
            f'{sender}: {message.text}',
        ]
        lines.extend(message_attachment_prompt_lines(message))
        return '\n'.join(lines)

    def live_input_prompt(self, message: IncomingMessage) -> str:
        lines = [message.text.strip()]
        lines.extend(message_attachment_prompt_lines(message))
        return '\n'.join(line for line in lines if line)

    def child_prompt(self, request: SpawnRequest, session: AgentSession, source_message_id: str) -> str:
        return '\n'.join(
            [
                '[Feishu Child Task]',
                f'- parent_session_id: {request.parent_session_id}',
                f'- agentd_session_id: {session.id}',
                f'- chat_id: {session.chat_id}',
                f'- root_message_id: {session.root_message_id or request.parent_status_message_id}',
                f'- thread_id: {session.thread_id or "unknown"}',
                f'- source_message_id: {source_message_id}',
                f'- cwd: {session.cwd}',
                f'- initial_title: {request.title or ""}',
                '',
                request.prompt,
            ]
        )

    def scheduled_prompt(self, job: ScheduleJob, session: AgentSession, run_key: str) -> str:
        return '\n'.join(
            [
                '[Scheduled Task]',
                f'- job_id: {job.id}',
                f'- job_name: {job.name}',
                f'- run_key: {run_key}',
                f'- agentd_session_id: {session.id}',
                f'- chat_id: {session.chat_id}',
                f'- cwd: {session.cwd}',
                '',
                job.prompt,
            ]
        )

    def resolve_context(self, run: RunRecord) -> ResolvedContext:
        return self.context_resolver.resolve(run.context_profile, run.skills)

    def developer_instructions(self, active: ActiveRun, resolved: ResolvedContext) -> str:
        injected_skills = ', '.join(skill.name for skill in resolved.skills) or 'none'
        prompt_files = ', '.join(file.label for file in resolved.prompt_files) or 'none'
        config = self.config
        lines = [
            f'You are running inside agentd, a Feishu-to-{self.runner_label} control plane.',
            '',
            'Runtime context:',
            f'- session_kind: {active.session.kind}',
            f'- agentd_session_id: {active.session.id}',
            f'- config_path: {config.config_path}',
            f'- home_dir: {config.home_dir}',
            f'- source_dir: {config.source_dir}',
            f'- state_dir: {config.state_dir}',
            f'- workspace: {config.workspace}',
            f'- cwd: {active.session.cwd}',
            f'- context_dir: {config.context.context_dir}',
            f'- context_profile: {resolved.profile.name}',
            f'- context_config_path: {config.context.path}',
            f'- profiles_available: {", ".join(sorted(config.context.profiles))}',
            f'- memory_dir: {resolved.memory_dir}',
            f'- prompt_files: {prompt_files}',
            f'- injected_skills: {injected_skills}',
            '',
            'Agentd contract:',
            '- Agentd sends your final answer back to Feishu. Do not call Feishu send/reply commands yourself unless the user explicitly asks you to send an additional proactive message.',
            '- For agentd service status, logs, health checks, start, stop, or restart, use `"$AGENTD_CLI" service ...`.',
            '- Agentd persists run, card, and final-reply state across restarts. Use `"$AGENTD_CLI" service restart --defer` when you want to avoid interrupting the current turn.',
            '- If you will handle substantial work in this session, set a concise task title once early with `"$AGENTD_CLI" set-title "<title>"`.',
        ]
        if active.session.kind == 'child':
            lines.extend(
                [
                    '- This is already a Feishu child thread. Feishu does not support nested child threads.',
                    '- Do not call `spawn-child` or `spawn-branch` from this session. If the user asks for separate work, tell them to start it from the main chat.',
                ]
            )
        else:
            lines.extend(
                [
                    '- Use `spawn-child` only for handoff: create a child thread, stop the current turn, and let the child take over the same work.',
                    '- Use `spawn-branch` for parallel work: create a child thread without interrupting the current turn.',
                    '- If the user asks to open a child task/thread for work that should become the active discussion of the current topic, prefer `spawn-child` handoff over `spawn-branch`. For example, finding a wiki entry and continuing the discussion there is handoff, not parallel background work.',
                    '- If live user input is unrelated to the current task or explicitly asks for a new task, use `spawn-branch` instead of steering this turn or handing off.',
                    '- If you delegate or branch, do not call `set-title` in the parent session. Pipe the full child task to `"$AGENTD_CLI" spawn-child ...` or `"$AGENTD_CLI" spawn-branch ...`, then stop only for handoff.',
                    '- Example handoff: `printf %s "$child_task" | "$AGENTD_CLI" spawn-child --cwd /path/to/work --title "short title" --skills bookkeeping,calendar`.',
                    '- Example parallel branch: `printf %s "$child_task" | "$AGENTD_CLI" spawn-branch --cwd /path/to/work --title "short title"`.',
                ]
            )
        lines.extend(
            [
                '',
                'Context policy:',
                '- Treat the injected agentd context files below as persistent user-managed context.',
                '- The current user request takes precedence over older context or memory if they conflict.',
                '- MEMORY.md is injected as an index. For deeper prior work, preferences, decisions, dates, people, or todos, search memory files with `rg` first and load only relevant snippets.',
                f'- Only the injected skills are enabled in this {self.runner_label} run. Read a SKILL.md only when its description clearly matches the task.',
                '- To use another profile or skill set, delegate with `--profile <profile>` or `--skills a,b`.',
            ]
        )
        if resolved.missing_skills:
            lines.append(f'- missing_skills: {", ".join(resolved.missing_skills)}')
        if resolved.prompt_files:
            lines.extend(
                [
                    '',
                    'Agentd context files:',
                    'The following files are injected by agentd from the configured context directory.',
                ]
            )
            for file in resolved.prompt_files:
                lines.extend(
                    [
                        '',
                        f'## {file.label}',
                        f'Path: {file.path}',
                        '<agentd_context_file>',
                        file.text,
                        '</agentd_context_file>',
                    ]
                )
        return '\n'.join(lines)

    def runner_env(self, active: ActiveRun, run: RunRecord | None) -> dict[str, str]:
        context_profile = run.context_profile if run else ''
        context_skills = run.skills if run else ()
        return {
            'AGENTD_CLI': str(self.config.executable),
            'AGENTD_CONFIG': str(self.config.config_path),
            'AGENTD_SESSION_ID': str(active.session.id),
            'AGENTD_RUNNER_KIND': self.runner_kind,
            'AGENTD_RUNNER_SESSION_REF': active.session.agent_session_ref,
            'AGENTD_CHAT_ID': active.session.chat_id,
            'AGENTD_SOURCE_MESSAGE_ID': run.source_message_id if run else '',
            'AGENTD_STATUS_MESSAGE_ID': run.status_message_id if run else '',
            'AGENTD_SENDER_OPEN_ID': run.sender_open_id if run else '',
            'AGENTD_SESSION_KIND': active.session.kind,
            'AGENTD_CWD': active.session.cwd,
            'AGENTD_CONTEXT_PROFILE': context_profile,
            'AGENTD_CONTEXT_SKILLS': ','.join(context_skills),
            'AGENTD_MEMORY_DIR': str(self.context_resolver.memory_dir),
        }


def message_attachment_prompt_lines(message: IncomingMessage) -> list[str]:
    if not message.attachments:
        return []
    lines = [
        '',
        'Attachments:',
        'The sender attached file/image resources. Downloaded resources are available on the local filesystem.',
    ]
    for index, attachment in enumerate(message.attachments, start=1):
        detail = [f'- {index}. type={attachment.kind}']
        if attachment.name:
            detail.append(f'name={attachment.name}')
        if attachment.mime_type:
            detail.append(f'mime_type={attachment.mime_type}')
        if attachment.size is not None:
            detail.append(f'size={attachment.size}')
        if attachment.local_path:
            detail.append(f'local_path={attachment.local_path}')
        if attachment.download_error:
            detail.append(f'download_error={attachment.download_error}')
        elif not attachment.local_path:
            detail.append('local_path=unavailable')
        lines.append(' '.join(detail))
    return lines


class RunnerContextBuilder:
    def __init__(self, builder: RunContextBuilder) -> None:
        self.builder = builder

    def resolve_context(self, run: RunRecord) -> ResolvedContext:
        return self.builder.resolve_context(run)

    def extra_env(self, active: ActiveRun, run: RunRecord | None) -> dict[str, str]:
        return self.builder.runner_env(active, run)

    def developer_instructions(self, active: ActiveRun, resolved: ResolvedContext) -> str:
        return self.builder.developer_instructions(active, resolved)

    def build(self, *, active: ActiveRun, run: RunRecord) -> tuple[ResolvedContext, dict[str, str], str]:
        resolved = self.resolve_context(run)
        return resolved, self.extra_env(active, run), self.developer_instructions(active, resolved)
