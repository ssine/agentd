from __future__ import annotations

import json
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
    feishu_app_id: str = ''
    feishu_app_secret: str = ''
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
    if options.config_path in result.created:
        options.config_path.chmod(0o600)
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
app_id = {_toml_string(options.feishu_app_id)}
app_secret = {_toml_string(options.feishu_app_secret)}
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


def write_feishu_credentials(
    config_path: Path,
    *,
    app_id: str,
    app_secret: str,
    overwrite: bool = False,
) -> list[str]:
    if not app_id or not app_secret:
        return []
    text = config_path.read_text(encoding='utf-8') if config_path.exists() else ''
    lines = text.splitlines()
    updated: list[str] = []
    seen_feishu = False
    in_feishu = False
    found_app_id = False
    found_app_secret = False
    insert_at = len(lines)
    out: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            if in_feishu and insert_at == len(lines):
                insert_at = len(out)
            in_feishu = stripped == '[feishu]'
            seen_feishu = seen_feishu or in_feishu
        if in_feishu and stripped.startswith('app_id') and '=' in stripped:
            found_app_id = True
            line, changed = _credential_line(line, 'app_id', app_id, overwrite=overwrite)
            if changed:
                updated.append('app_id')
        elif in_feishu and stripped.startswith('app_secret') and '=' in stripped:
            found_app_secret = True
            line, changed = _credential_line(line, 'app_secret', app_secret, overwrite=overwrite)
            if changed:
                updated.append('app_secret')
        out.append(line)

    if seen_feishu:
        additions: list[str] = []
        if not found_app_id:
            additions.append(f'app_id = {_toml_string(app_id)}')
            updated.append('app_id')
        if not found_app_secret:
            additions.append(f'app_secret = {_toml_string(app_secret)}')
            updated.append('app_secret')
        if additions:
            out[insert_at:insert_at] = additions
    else:
        if out and out[-1].strip():
            out.append('')
        out.extend(['[feishu]', f'app_id = {_toml_string(app_id)}', f'app_secret = {_toml_string(app_secret)}'])
        updated.extend(['app_id', 'app_secret'])

    if updated:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text('\n'.join(out).rstrip() + '\n', encoding='utf-8')
        config_path.chmod(0o600)
    return updated


def _credential_line(line: str, key: str, value: str, *, overwrite: bool) -> tuple[str, bool]:
    prefix, current = line.split('=', 1)
    current = current.strip()
    if current not in {'""', "''"} and not overwrite:
        return line, False
    return f'{prefix.rstrip()} = {_toml_string(value)}', True


def _toml_string(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=False)


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
