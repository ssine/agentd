from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .context import ContextConfig, load_context_config
from .schedule import ScheduleConfig, load_schedule_config

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEGACY_CONFIG_PATH = PROJECT_ROOT / '.agents/config/agentd.toml'


@dataclass(frozen=True)
class FeishuConfig:
    app_id: str = ''
    app_secret: str = ''
    ignore_bot_messages: bool = True
    main_reply_in_thread: bool = False
    child_reply_in_thread: bool = True


@dataclass(frozen=True)
class CodexConfig:
    command: str = ''
    model: str = ''
    model_provider: str = ''
    sandbox: str = 'danger-full-access'
    approval_policy: str = 'never'
    turn_timeout_seconds: int = 1800
    startup_timeout_seconds: int = 60


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
    codex: CodexConfig

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
    home_config = default_home_dir() / 'agentd.toml'
    if home_config.exists() or os.environ.get('AGENTD_HOME'):
        return home_config
    if LEGACY_CONFIG_PATH.exists():
        return LEGACY_CONFIG_PATH
    return home_config


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


def _command(value: Any, base: Path) -> str:
    raw = str(value or (base / 'bin/acodex'))
    parts = shlex.split(raw)
    if not parts:
        return str(base / 'bin/acodex')

    executable = Path(parts[0]).expanduser()
    if not executable.is_absolute() and '/' in parts[0]:
        parts[0] = str((base / executable).resolve())
    elif executable.is_absolute():
        parts[0] = str(executable)
    return shlex.join(parts)


def load_config(path: str | Path | None = None) -> AgentdConfig:
    raw_config_path = path or os.environ.get('AGENTD_CONFIG')
    config_path = Path(raw_config_path).expanduser() if raw_config_path else default_config_path()
    if not config_path.is_absolute():
        config_path = ((Path.cwd() if raw_config_path else PROJECT_ROOT) / config_path).resolve()

    raw = _load_toml(config_path)
    agentd_raw = raw.get('agentd') if isinstance(raw.get('agentd'), dict) else {}
    context_raw = raw.get('context') if isinstance(raw.get('context'), dict) else {}
    feishu_raw = raw.get('feishu') if isinstance(raw.get('feishu'), dict) else {}
    codex_raw = raw.get('codex') if isinstance(raw.get('codex'), dict) else {}

    home_dir = _as_path(agentd_raw.get('home_dir') or config_path.parent, config_path.parent)
    source_dir = (
        _as_path(agentd_raw.get('source_dir'), config_path.parent)
        if agentd_raw.get('source_dir') is not None
        else PROJECT_ROOT
    )
    executable = _as_path(agentd_raw.get('executable', '.venv/bin/agentd'), source_dir)
    workspace = _as_path(agentd_raw.get('workspace', '.'), source_dir)

    if agentd_raw.get('state_dir') is not None:
        state_dir = _as_path(agentd_raw.get('state_dir'), home_dir)
    elif agentd_raw.get('runtime_dir') is not None:
        state_dir = _as_path(agentd_raw.get('runtime_dir'), workspace)
    else:
        state_dir = home_dir / 'state'

    context_dir = _as_path(
        context_raw.get('context_dir')
        or context_raw.get('dir')
        or agentd_raw.get('context_dir')
        or default_context_dir(),
        home_dir,
    )
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
        app_id=_env_first('AGENTD_FEISHU_APP_ID', 'CODEX_FEISHU_APP_ID') or str(feishu_raw.get('app_id') or ''),
        app_secret=_env_first('AGENTD_FEISHU_APP_SECRET', 'CODEX_FEISHU_APP_SECRET')
        or str(feishu_raw.get('app_secret') or ''),
        ignore_bot_messages=bool(feishu_raw.get('ignore_bot_messages', True)),
        main_reply_in_thread=bool(feishu_raw.get('main_reply_in_thread', False)),
        child_reply_in_thread=bool(feishu_raw.get('child_reply_in_thread', True)),
    )

    codex = CodexConfig(
        command=_command(codex_raw.get('command'), source_dir),
        model=str(codex_raw.get('model') or ''),
        model_provider=str(codex_raw.get('model_provider') or ''),
        sandbox=str(codex_raw.get('sandbox') or 'danger-full-access'),
        approval_policy=str(codex_raw.get('approval_policy') or 'never'),
        turn_timeout_seconds=int(codex_raw.get('turn_timeout_seconds') or 1800),
        startup_timeout_seconds=int(codex_raw.get('startup_timeout_seconds') or 60),
    )

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
        codex=codex,
    )
