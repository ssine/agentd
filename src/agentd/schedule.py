from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .context import split_skill_names

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib


@dataclass(frozen=True)
class ScheduleJob:
    id: str
    name: str
    enabled: bool
    session: str
    chat_id: str
    prompt: str
    title: str
    context_profile: str
    skills: tuple[str, ...]
    kind: str
    timezone: str
    time: str = ''
    interval_seconds: int = 0
    at: str = ''


@dataclass(frozen=True)
class ScheduleConfig:
    path: Path
    jobs: tuple[ScheduleJob, ...]


def load_schedule_config(path: Path) -> ScheduleConfig:
    raw = _load_toml(path)
    jobs_raw = raw.get('jobs') if isinstance(raw.get('jobs'), list) else []
    jobs: list[ScheduleJob] = []
    for index, value in enumerate(jobs_raw, start=1):
        if not isinstance(value, dict):
            continue
        job_id = str(value.get('id') or value.get('name') or f'job-{index}').strip()
        if not job_id:
            continue
        schedule_raw = value.get('schedule') if isinstance(value.get('schedule'), dict) else {}
        kind = str(schedule_raw.get('kind') or value.get('kind') or 'daily')
        session = _normalize_session(value.get('session') or value.get('target_session') or 'schedule')
        jobs.append(
            ScheduleJob(
                id=job_id,
                name=str(value.get('name') or job_id),
                enabled=bool(value.get('enabled', True)),
                session=session,
                chat_id=str(value.get('chat_id') or ''),
                prompt=str(value.get('prompt') or value.get('message') or ''),
                title=str(value.get('title') or value.get('name') or job_id),
                context_profile=str(value.get('profile') or value.get('context_profile') or ''),
                skills=split_skill_names(value.get('skills')),
                kind=kind,
                timezone=str(schedule_raw.get('timezone') or value.get('timezone') or 'Asia/Shanghai'),
                time=str(schedule_raw.get('time') or value.get('time') or ''),
                interval_seconds=int(schedule_raw.get('seconds') or value.get('interval_seconds') or 0),
                at=str(schedule_raw.get('at') or value.get('at') or ''),
            )
        )
    return ScheduleConfig(path=path, jobs=tuple(jobs))


def _normalize_session(value: object) -> str:
    session = str(value or '').strip().lower()
    if session in {'main', 'primary'}:
        return 'main'
    return 'schedule'


def due_run_key(job: ScheduleJob, now: datetime | None = None) -> str:
    if not job.enabled:
        return ''
    now = now or datetime.now(tz=ZoneInfo('UTC'))
    zone = _zone(job.timezone)

    if job.kind == 'daily':
        hour, minute = _parse_time(job.time)
        if hour is None:
            return ''
        local = now.astimezone(zone)
        target = local.replace(hour=hour, minute=minute or 0, second=0, microsecond=0)
        return local.strftime('%Y-%m-%d') if local >= target else ''

    if job.kind == 'interval':
        if job.interval_seconds <= 0:
            return ''
        return str(int(now.timestamp()) // job.interval_seconds)

    if job.kind == 'once':
        run_at = _parse_datetime(job.at, zone)
        if run_at is None:
            return ''
        return run_at.isoformat() if now >= run_at else ''

    return ''


def _parse_time(value: str) -> tuple[int | None, int | None]:
    parts = value.strip().split(':')
    if len(parts) < 2:
        return None, None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None, None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None, None
    return hour, minute


def _parse_datetime(value: str, zone: ZoneInfo) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo('UTC')


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open('rb') as fh:
        data = tomllib.load(fh)
    return data if isinstance(data, dict) else {}
