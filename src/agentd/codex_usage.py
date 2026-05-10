from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .codex_app_server import CodexAppServer, CodexAppServerError, codex_app_server_config_overrides
from .config import AgentdConfig


class CodexUsageError(RuntimeError):
    pass


@dataclass(frozen=True)
class UsageWindow:
    used_percent: float
    window_duration_mins: int
    resets_at: int

    @property
    def remaining_percent(self) -> float:
        return max(0.0, 100.0 - self.used_percent)


@dataclass(frozen=True)
class CodexLimitSnapshot:
    limit_id: str
    limit_name: str
    plan_type: str
    primary: UsageWindow | None
    secondary: UsageWindow | None
    rate_limit_reached_type: str | None = None
    allowed: bool | None = None
    limit_reached: bool | None = None
    credits: dict[str, Any] | None = None

    def is_limit_reached(self) -> bool:
        if self.limit_reached is not None:
            return self.limit_reached
        return bool(self.rate_limit_reached_type)

    def is_allowed(self) -> bool:
        if self.allowed is not None:
            return self.allowed
        return not self.is_limit_reached()


@dataclass(frozen=True)
class CodexUsageSnapshot:
    queried_at: int
    rate_limits: CodexLimitSnapshot
    rate_limits_by_limit_id: dict[str, CodexLimitSnapshot]


def read_codex_usage(config: AgentdConfig, *, cwd: str | Path | None = None, timeout: int | None = None) -> CodexUsageSnapshot:
    query_cwd = str(Path(cwd or config.workspace).expanduser().resolve())
    config.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = config.log_dir / f'codex-usage-{int(time.time())}.log'
    argv = [*shlex.split(config.codex.command), 'app-server']
    for override in codex_app_server_config_overrides():
        argv.extend(['-c', override])
    argv.extend(['--listen', 'stdio://'])

    server = CodexAppServer(config.codex, config.log_dir)
    env = os.environ.copy()
    try:
        with log_path.open('a', encoding='utf-8') as log:
            proc = subprocess.Popen(
                argv,
                cwd=query_cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            try:
                server._initialize(proc, log)
                result = server._request(
                    proc,
                    log,
                    'account/rateLimits/read',
                    {},
                    timeout=timeout or config.codex.startup_timeout_seconds,
                )
            finally:
                server._terminate(proc)
    except FileNotFoundError as exc:
        raise CodexUsageError(f'Codex command not found: {shlex.split(config.codex.command)[0]}') from exc
    except CodexAppServerError as exc:
        raise CodexUsageError(str(exc)) from exc
    except OSError as exc:
        raise CodexUsageError(f'failed to start Codex app-server: {exc}') from exc

    try:
        return usage_snapshot_from_result(result, queried_at=int(time.time()))
    except (TypeError, ValueError, KeyError) as exc:
        compact = json.dumps(result, ensure_ascii=False, sort_keys=True)[:1000]
        raise CodexUsageError(f'unexpected Codex usage response: {compact}') from exc


def usage_snapshot_from_result(result: dict[str, Any], *, queried_at: int | None = None) -> CodexUsageSnapshot:
    raw_main = _mapping(result.get('rateLimits')) or _mapping(result.get('rate_limit'))
    raw_by_id = _mapping(result.get('rateLimitsByLimitId')) or {}

    parsed_by_id: dict[str, CodexLimitSnapshot] = {}
    for limit_id, raw_limit in raw_by_id.items():
        parsed = _parse_limit(_mapping(raw_limit), fallback_limit_id=str(limit_id))
        if parsed is not None:
            parsed_by_id[parsed.limit_id or str(limit_id)] = parsed

    main = _parse_limit(raw_main, fallback_limit_id='codex')
    if main is None:
        main = parsed_by_id.get('codex')
    if main is None:
        raise ValueError('missing rateLimits')
    if main.limit_id and main.limit_id not in parsed_by_id:
        parsed_by_id = {**parsed_by_id, main.limit_id: main}

    return CodexUsageSnapshot(
        queried_at=queried_at or int(time.time()),
        rate_limits=main,
        rate_limits_by_limit_id=parsed_by_id,
    )


def snapshot_to_dict(snapshot: CodexUsageSnapshot) -> dict[str, Any]:
    data = asdict(snapshot)
    data['rate_limits']['allowed'] = snapshot.rate_limits.is_allowed()
    data['rate_limits']['limit_reached'] = snapshot.rate_limits.is_limit_reached()
    _add_window_remaining(data['rate_limits'])
    for limit_id, limit in snapshot.rate_limits_by_limit_id.items():
        data['rate_limits_by_limit_id'][limit_id]['allowed'] = limit.is_allowed()
        data['rate_limits_by_limit_id'][limit_id]['limit_reached'] = limit.is_limit_reached()
        _add_window_remaining(data['rate_limits_by_limit_id'][limit_id])
    return data


def format_codex_usage(snapshot: CodexUsageSnapshot) -> str:
    main = snapshot.rate_limits
    plan = _display_plan(main.plan_type)
    status = '当前未触发限额' if main.is_allowed() else f'已触发限额：{main.rate_limit_reached_type or "unknown"}'
    lines = [
        f'查询时间：{_format_timestamp(snapshot.queried_at)}',
        '',
        f'当前计划：{plan or "unknown"}，{status}。',
        '',
    ]
    if main.primary is not None:
        lines.append(_format_window('5 小时窗口', main.primary))
    if main.secondary is not None:
        lines.append(_format_window('一周窗口', main.secondary))

    additional = [
        limit
        for limit_id, limit in sorted(snapshot.rate_limits_by_limit_id.items())
        if limit_id != main.limit_id and limit.limit_name
    ]
    if additional:
        lines.append('')
        for limit in additional:
            parts = []
            if limit.primary is not None:
                parts.append(f'5 小时剩余 {_format_percent(limit.primary.remaining_percent)}')
            if limit.secondary is not None:
                parts.append(f'一周剩余 {_format_percent(limit.secondary.remaining_percent)}')
            lines.append(f'额外限额 {limit.limit_name}：{"，".join(parts)}。')

    lines.append('')
    lines.append('官方接口只返回百分比，不返回绝对消息数。')
    return '\n'.join(lines)


def format_codex_usage_error(error: Exception) -> str:
    return f'无法读取 Codex 额度：{error}'


def _parse_limit(raw: dict[str, Any], *, fallback_limit_id: str) -> CodexLimitSnapshot | None:
    if not raw:
        return None
    primary = _parse_window(_mapping(raw.get('primary')) or _mapping(raw.get('primary_window')))
    secondary = _parse_window(_mapping(raw.get('secondary')) or _mapping(raw.get('secondary_window')))
    rate_limit_reached_type = _optional_str(raw.get('rateLimitReachedType') or raw.get('rate_limit_reached_type'))
    return CodexLimitSnapshot(
        limit_id=str(raw.get('limitId') or raw.get('limit_id') or fallback_limit_id),
        limit_name=str(raw.get('limitName') or raw.get('limit_name') or ''),
        plan_type=str(raw.get('planType') or raw.get('plan_type') or ''),
        primary=primary,
        secondary=secondary,
        rate_limit_reached_type=rate_limit_reached_type,
        allowed=_optional_bool(raw.get('allowed')),
        limit_reached=_optional_bool(raw.get('limitReached') if 'limitReached' in raw else raw.get('limit_reached')),
        credits=_mapping(raw.get('credits')) or None,
    )


def _add_window_remaining(limit_data: dict[str, Any]) -> None:
    for key in ('primary', 'secondary'):
        window = limit_data.get(key)
        if isinstance(window, dict):
            used_percent = float(window.get('used_percent') or 0)
            window['remaining_percent'] = max(0.0, 100.0 - used_percent)


def _parse_window(raw: dict[str, Any]) -> UsageWindow | None:
    if not raw:
        return None
    used_percent = float(raw.get('usedPercent') if 'usedPercent' in raw else raw.get('used_percent', 0))
    duration_mins = _duration_mins(raw)
    resets_at = int(raw.get('resetsAt') if 'resetsAt' in raw else raw.get('reset_at', 0))
    return UsageWindow(used_percent=used_percent, window_duration_mins=duration_mins, resets_at=resets_at)


def _duration_mins(raw: dict[str, Any]) -> int:
    if raw.get('windowDurationMins') is not None:
        return int(raw['windowDurationMins'])
    if raw.get('window_duration_mins') is not None:
        return int(raw['window_duration_mins'])
    if raw.get('limit_window_seconds') is not None:
        return int(raw['limit_window_seconds']) // 60
    return 0


def _format_window(label: str, window: UsageWindow) -> str:
    actual_label = label if window.window_duration_mins in {300, 10080} else f'{window.window_duration_mins} 分钟窗口'
    return (
        f'{actual_label}：已用 {_format_percent(window.used_percent)}，'
        f'剩余 {_format_percent(window.remaining_percent)}，重置时间 {_format_timestamp(window.resets_at)}'
    )


def _format_percent(value: float) -> str:
    if value == int(value):
        return f'{int(value)}%'
    return f'{value:.1f}%'


def _display_plan(plan_type: str) -> str:
    if not plan_type:
        return ''
    if plan_type.lower() == 'api':
        return 'API'
    return plan_type[:1].upper() + plan_type[1:]


def _format_timestamp(epoch_seconds: int) -> str:
    if epoch_seconds <= 0:
        return 'unknown'
    return datetime.fromtimestamp(epoch_seconds).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _optional_str(value: Any) -> str | None:
    if value in (None, ''):
        return None
    return str(value)
