from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class BootstrapOptions:
    config_path: Path
    home_dir: Path
    context_dir: Path
    source_dir: Path
    executable: str = '.venv/bin/agentd'
    runner_kind: str = 'codex'
    codex_command: str = 'codex'
    claude_command: str = 'aclaude'
    claude_model: str = 'sonnet'
    overwrite: bool = False


@dataclass
class BootstrapResult:
    created: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)


def init_agentd(options: BootstrapOptions) -> BootstrapResult:
    result = BootstrapResult()
    options.home_dir.mkdir(parents=True, exist_ok=True)
    options.context_dir.mkdir(parents=True, exist_ok=True)
    (options.context_dir / 'memory' / 'projects').mkdir(parents=True, exist_ok=True)
    (options.context_dir / 'skills').mkdir(parents=True, exist_ok=True)

    write_file(
        options.config_path,
        agentd_toml(options),
        overwrite=options.overwrite,
        result=result,
    )
    write_file(
        options.context_dir / 'context.toml',
        context_toml(),
        overwrite=options.overwrite,
        result=result,
    )
    write_file(
        options.context_dir / 'schedules.toml',
        schedules_toml(),
        overwrite=options.overwrite,
        result=result,
    )
    write_file(
        options.context_dir / 'CONTEXT.md',
        context_md(),
        overwrite=options.overwrite,
        result=result,
    )
    write_file(
        options.context_dir / 'memory' / 'MEMORY.md',
        memory_index_md(),
        overwrite=options.overwrite,
        result=result,
    )
    write_file(
        options.context_dir / 'skills' / 'README.md',
        skills_readme_md(),
        overwrite=options.overwrite,
        result=result,
    )
    write_file(
        options.context_dir / '.gitignore',
        context_gitignore(),
        overwrite=options.overwrite,
        result=result,
    )
    write_file(
        options.context_dir / '.env.example',
        env_example(),
        overwrite=options.overwrite,
        result=result,
    )
    return result


def write_file(path: Path, text: str, *, overwrite: bool, result: BootstrapResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        result.skipped.append(path)
        return
    path.write_text(text.rstrip() + '\n', encoding='utf-8')
    result.created.append(path)


def agentd_toml(options: BootstrapOptions) -> str:
    return f"""
[agentd]
home_dir = "{options.home_dir}"
executable = "{options.executable}"
source_dir = "{options.source_dir}"
state_dir = "state"
workspace = "{options.context_dir}"
log_level = "INFO"

[context]
dir = "{options.context_dir}"
config = "context.toml"
schedules = "schedules.toml"

[feishu]
# app_id and app_secret can also be supplied by environment:
# AGENTD_FEISHU_APP_ID, AGENTD_FEISHU_APP_SECRET
app_id = ""
app_secret = ""
ignore_bot_messages = true
main_reply_in_thread = false
child_reply_in_thread = true

[web]
enabled = true
host = "127.0.0.1"
port = 8765

[runner]
# Supported values: "codex" and "claude_code".
kind = "{options.runner_kind}"

[codex]
command = "{options.codex_command}"
model = ""
model_provider = ""
sandbox = "danger-full-access"
approval_policy = "never"
startup_timeout_seconds = 60

[claude]
command = "{options.claude_command}"
model = "{options.claude_model}"
permission_mode = "bypassPermissions"
use_login_shell = true
"""


def context_toml() -> str:
    return """
[context]
default_profile = "default"
default_child_profile = "default"
memory_dir = "memory"
skill_roots = ["skills", "~/.codex/skills"]
prompt_files = ["CONTEXT.md", "memory/MEMORY.md"]
prompt_file_max_bytes = 65536

[profiles.default]
skills = []
memory = "rg"
"""


def schedules_toml() -> str:
    return """
# Lightweight agentd schedules. Jobs are disabled until chat_id and prompt are filled.

[[jobs]]
id = "example"
name = "Example"
enabled = false
chat_id = ""
prompt = ""
title = "Example"
# "schedule" keeps a separate scheduled-task session.
# "main" posts into the chat's main session and queues until it is idle.
session = "schedule"
profile = "default"
skills = []

[jobs.schedule]
kind = "daily"
timezone = "Asia/Shanghai"
time = "09:00"
"""


def context_md() -> str:
    return """
# CONTEXT.md

This is my private agent context repository.

- Runtime state belongs under `~/.agentd/state`, not here.
- Long-term searchable notes live under `memory/`.
- Context-local skills live under `skills/**/SKILL.md`.
- Keep this file small; put detailed notes in `memory/` and link them from `memory/MEMORY.md`.
"""


def memory_index_md() -> str:
    return """
# Memory Index

This directory stores long-term context for agents. Search it with `rg` before loading deeper memory files.

- Project notes: `memory/projects/`
"""


def skills_readme_md() -> str:
    return """
# Skills

Add context-local agentd skills under `skills/**/SKILL.md`.

Each `SKILL.md` should include frontmatter:

```markdown
---
name: example
description: Use when ...
---
```
"""


def context_gitignore() -> str:
    return """
.env
.venv/
__pycache__/
*.pyc
"""


def env_example() -> str:
    return """
# Copy to .env if your private skills need local environment values.
"""
