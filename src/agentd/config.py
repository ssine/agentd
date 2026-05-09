from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .context import ContextConfig, load_context_config
from .schedule import ScheduleConfig, load_schedule_config

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class FeishuConfig:
    app_id: str = ''
    app_secret: str = ''
    ignore_bot_messages: bool = True
    main_reply_in_thread: bool = False
    child_reply_in_thread: bool = True


@dataclass(frozen=True)
class WebConfig:
    enabled: bool = True
    host: str = '127.0.0.1'
    port: int = 8765


@dataclass(frozen=True)
class RunnerConfig:
    kind: str = 'codex'


@dataclass(frozen=True)
class CodexCaptureConfig:
    enabled: bool = False
    upstream_mode: str = 'codex-default'
    upstream_url: str = ''
    capture_dir: Path = Path()
    db_path: Path = Path()
    save_sensitive_headers: bool = False
    archive_period: str = 'week'
    archive_format: str = 'tar.zst'
    zstd_level: int = 10


@dataclass(frozen=True)
class CodexOtelConfig:
    enabled: bool = False
    capture_dir: Path = Path()
    db_path: Path = Path()
    environment: str = 'agentd'
    protocol: str = 'json'
    log_user_prompt: bool = False
    logs: bool = True
    traces: bool = True
    metrics: bool = True
    archive_period: str = 'week'
    archive_format: str = 'tar.zst'
    zstd_level: int = 10


@dataclass(frozen=True)
class CodexConfig:
    command: str = ''
    model: str = ''
    model_provider: str = ''
    sandbox: str = 'danger-full-access'
    approval_policy: str = 'never'
    turn_timeout_seconds: int | None = None
    startup_timeout_seconds: int = 60
    capture: CodexCaptureConfig = field(default_factory=CodexCaptureConfig)
    otel: CodexOtelConfig = field(default_factory=CodexOtelConfig)


@dataclass(frozen=True)
class ClaudeCodeConfig:
    command: str = 'aclaude'
    model: str = 'sonnet'
    permission_mode: str = 'bypassPermissions'
    use_login_shell: bool = True
    turn_timeout_seconds: int | None = None
    extra_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentdConfig:
    config_path: Path
    home_dir: Path
    executable: Path
    source_dir: Path
    state_dir: Path
    workspace: Path
    log_level: str
    context: ContextConfig
    schedules: ScheduleConfig
    feishu: FeishuConfig
    web: WebConfig
    codex: CodexConfig
    runner: RunnerConfig = field(default_factory=RunnerConfig)
    claude: ClaudeCodeConfig = field(default_factory=ClaudeCodeConfig)

    @property
    def runtime_dir(self) -> Path:
        return self.state_dir

    @property
    def db_path(self) -> Path:
        return self.state_dir / 'agentd.sqlite'

    @property
    def log_dir(self) -> Path:
        return self.state_dir / 'logs'


def default_home_dir() -> Path:
    return Path(os.environ.get('AGENTD_HOME') or '~/.agentd').expanduser().resolve()


def default_context_dir() -> Path:
    return Path(os.environ.get('AGENTD_CONTEXT_HOME') or '~/agent-context').expanduser().resolve()


def default_config_path() -> Path:
    return default_home_dir() / 'agentd.toml'


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open('rb') as fh:
        data = tomllib.load(fh)
    if not isinstance(data, dict):
        return {}
    return data


def _as_path(value: Any, base: Path) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _env_first(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ''


def _capture_archive_period(value: Any) -> str:
    raw = str(value or 'week').strip().lower()
    aliases = {
        'daily': 'day',
        'weekly': 'week',
        'monthly': 'month',
    }
    period = aliases.get(raw, raw)
    if period not in {'day', 'week', 'month'}:
        raise ValueError('codex.capture.archive_period must be day, week, or month')
    return period


def _capture_archive_format(value: Any) -> str:
    archive_format = str(value or 'tar.zst').strip().lower()
    if archive_format != 'tar.zst':
        raise ValueError('codex.capture.archive_format currently supports only tar.zst')
    return archive_format


def _zstd_level(value: Any) -> int:
    level = int(value or 10)
    if level < 1 or level > 22:
        raise ValueError('codex.capture.zstd_level must be between 1 and 22')
    return level


def _optional_timeout_seconds(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    timeout = int(value)
    if timeout <= 0:
        return None
    return timeout


def _otel_protocol(value: Any) -> str:
    protocol = str(value or 'json').strip().lower()
    if protocol not in {'json', 'binary'}:
        raise ValueError('codex.otel.protocol must be json or binary')
    return protocol


def _command(value: Any, base: Path) -> str:
    raw = str(value or 'codex')
    parts = shlex.split(raw)
    if not parts:
        return 'codex'

    executable = Path(parts[0]).expanduser()
    if not executable.is_absolute() and '/' in parts[0]:
        parts[0] = str((base / executable).resolve())
    elif executable.is_absolute():
        parts[0] = str(executable)
    return shlex.join(parts)


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(shlex.split(value))
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return (str(value),)


def _runner_kind(value: Any) -> str:
    kind = str(value or 'codex').strip().lower().replace('-', '_')
    aliases = {
        'claude': 'claude_code',
        'claude-code': 'claude_code',
        'claudecode': 'claude_code',
    }
    return aliases.get(kind, kind)


def load_config(path: str | Path | None = None) -> AgentdConfig:
    raw_config_path = path or os.environ.get('AGENTD_CONFIG')
    config_path = Path(raw_config_path).expanduser() if raw_config_path else default_config_path()
    if not config_path.is_absolute():
        config_path = ((Path.cwd() if raw_config_path else PROJECT_ROOT) / config_path).resolve()

    raw = _load_toml(config_path)
    agentd_raw = raw.get('agentd') if isinstance(raw.get('agentd'), dict) else {}
    context_raw = raw.get('context') if isinstance(raw.get('context'), dict) else {}
    feishu_raw = raw.get('feishu') if isinstance(raw.get('feishu'), dict) else {}
    web_raw = raw.get('web') if isinstance(raw.get('web'), dict) else {}
    runner_raw = raw.get('runner') if isinstance(raw.get('runner'), dict) else {}
    codex_raw = raw.get('codex') if isinstance(raw.get('codex'), dict) else {}
    claude_raw = raw.get('claude') if isinstance(raw.get('claude'), dict) else {}
    if not claude_raw:
        claude_raw = raw.get('claude_code') if isinstance(raw.get('claude_code'), dict) else {}
    codex_capture_raw = codex_raw.get('capture') if isinstance(codex_raw.get('capture'), dict) else {}
    codex_otel_raw = codex_raw.get('otel') if isinstance(codex_raw.get('otel'), dict) else {}

    home_dir = _as_path(agentd_raw.get('home_dir') or config_path.parent, config_path.parent)
    source_dir = (
        _as_path(agentd_raw.get('source_dir'), config_path.parent)
        if agentd_raw.get('source_dir') is not None
        else PROJECT_ROOT
    )
    executable = _as_path(agentd_raw.get('executable', '.venv/bin/agentd'), source_dir)
    context_dir = _as_path(
        context_raw.get('context_dir')
        or context_raw.get('dir')
        or agentd_raw.get('context_dir')
        or default_context_dir(),
        home_dir,
    )
    workspace = (
        _as_path(agentd_raw.get('workspace'), source_dir) if agentd_raw.get('workspace') is not None else context_dir
    )

    if agentd_raw.get('state_dir') is not None:
        state_dir = _as_path(agentd_raw.get('state_dir'), home_dir)
    elif agentd_raw.get('runtime_dir') is not None:
        state_dir = _as_path(agentd_raw.get('runtime_dir'), workspace)
    else:
        state_dir = home_dir / 'state'

    if agentd_raw.get('context_profiles') is not None:
        context_config_path = _as_path(agentd_raw.get('context_profiles'), workspace)
        context_base = workspace
    else:
        context_config_path = _as_path(
            context_raw.get('config') or context_raw.get('profiles') or 'context.toml', context_dir
        )
        context_base = context_dir
    if agentd_raw.get('schedules') is not None:
        schedules_path = _as_path(agentd_raw.get('schedules'), workspace)
    else:
        schedules_path = _as_path(context_raw.get('schedules') or 'schedules.toml', context_dir)

    context = load_context_config(context_config_path, context_base)
    schedules = load_schedule_config(schedules_path)

    feishu = FeishuConfig(
        app_id=_env_first('AGENTD_FEISHU_APP_ID') or str(feishu_raw.get('app_id') or ''),
        app_secret=_env_first('AGENTD_FEISHU_APP_SECRET') or str(feishu_raw.get('app_secret') or ''),
        ignore_bot_messages=bool(feishu_raw.get('ignore_bot_messages', True)),
        main_reply_in_thread=bool(feishu_raw.get('main_reply_in_thread', False)),
        child_reply_in_thread=bool(feishu_raw.get('child_reply_in_thread', True)),
    )
    web = WebConfig(
        enabled=bool(web_raw.get('enabled', True)),
        host=str(web_raw.get('host') or '127.0.0.1'),
        port=int(web_raw.get('port') or 8765),
    )

    capture_dir = _as_path(
        codex_capture_raw.get('dir') or codex_capture_raw.get('capture_dir') or 'captures',
        state_dir,
    )
    capture_db_path = _as_path(codex_capture_raw.get('db_path') or state_dir / 'agentd.sqlite', state_dir)
    otel_capture_dir = _as_path(
        codex_otel_raw.get('dir')
        or codex_otel_raw.get('capture_dir')
        or codex_capture_raw.get('dir')
        or codex_capture_raw.get('capture_dir')
        or 'captures',
        state_dir,
    )
    otel_db_path = _as_path(codex_otel_raw.get('db_path') or capture_db_path, state_dir)
    capture_archive_period = _capture_archive_period(codex_capture_raw.get('archive_period'))
    capture_archive_format = _capture_archive_format(codex_capture_raw.get('archive_format'))
    capture_zstd_level = _zstd_level(codex_capture_raw.get('zstd_level'))
    codex = CodexConfig(
        command=_command(codex_raw.get('command'), source_dir),
        model=str(codex_raw.get('model') or ''),
        model_provider=str(codex_raw.get('model_provider') or ''),
        sandbox=str(codex_raw.get('sandbox') or 'danger-full-access'),
        approval_policy=str(codex_raw.get('approval_policy') or 'never'),
        turn_timeout_seconds=_optional_timeout_seconds(codex_raw.get('turn_timeout_seconds')),
        startup_timeout_seconds=int(codex_raw.get('startup_timeout_seconds') or 60),
        capture=CodexCaptureConfig(
            enabled=bool(codex_capture_raw.get('enabled', False)),
            upstream_mode=str(codex_capture_raw.get('upstream_mode') or 'codex-default'),
            upstream_url=str(codex_capture_raw.get('upstream_url') or ''),
            capture_dir=capture_dir,
            db_path=capture_db_path,
            save_sensitive_headers=bool(codex_capture_raw.get('save_sensitive_headers', False)),
            archive_period=capture_archive_period,
            archive_format=capture_archive_format,
            zstd_level=capture_zstd_level,
        ),
        otel=CodexOtelConfig(
            enabled=bool(codex_otel_raw.get('enabled', False)),
            capture_dir=otel_capture_dir,
            db_path=otel_db_path,
            environment=str(codex_otel_raw.get('environment') or 'agentd'),
            protocol=_otel_protocol(codex_otel_raw.get('protocol')),
            log_user_prompt=bool(codex_otel_raw.get('log_user_prompt', False)),
            logs=bool(codex_otel_raw.get('logs', True)),
            traces=bool(codex_otel_raw.get('traces', True)),
            metrics=bool(codex_otel_raw.get('metrics', True)),
            archive_period=_capture_archive_period(codex_otel_raw.get('archive_period') or capture_archive_period),
            archive_format=_capture_archive_format(codex_otel_raw.get('archive_format') or capture_archive_format),
            zstd_level=_zstd_level(codex_otel_raw.get('zstd_level') or capture_zstd_level),
        ),
    )
    claude_turn_timeout = (
        _optional_timeout_seconds(claude_raw.get('turn_timeout_seconds'))
        if 'turn_timeout_seconds' in claude_raw
        else codex.turn_timeout_seconds
    )
    claude = ClaudeCodeConfig(
        command=_command(claude_raw.get('command') or 'aclaude', source_dir),
        model=str(claude_raw.get('model') or 'sonnet'),
        permission_mode=str(claude_raw.get('permission_mode') or 'bypassPermissions'),
        use_login_shell=bool(claude_raw.get('use_login_shell', True)),
        turn_timeout_seconds=claude_turn_timeout,
        extra_args=_string_tuple(claude_raw.get('extra_args')),
    )
    runner = RunnerConfig(kind=_runner_kind(runner_raw.get('kind') or agentd_raw.get('runner') or 'codex'))

    return AgentdConfig(
        config_path=config_path,
        home_dir=home_dir,
        executable=executable,
        source_dir=source_dir,
        state_dir=state_dir,
        workspace=workspace,
        log_level=str(agentd_raw.get('log_level') or 'INFO'),
        context=context,
        schedules=schedules,
        feishu=feishu,
        web=web,
        codex=codex,
        runner=runner,
        claude=claude,
    )
