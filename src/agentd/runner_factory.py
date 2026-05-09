from __future__ import annotations

from .config import AgentdConfig
from .runners import AgentRunner, ClaudeCodeRunner, CodexRunner


def create_runner(config: AgentdConfig) -> AgentRunner:
    kind = config.runner.kind
    if kind == 'codex':
        return CodexRunner(config.codex, config.log_dir)
    if kind == 'claude_code':
        return ClaudeCodeRunner(config.claude, config.log_dir)
    raise ValueError(f'unsupported runner kind: {kind}')
