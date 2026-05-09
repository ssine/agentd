from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib


BUILTIN_SKILL_ROOTS = (Path(__file__).resolve().parent / 'skills',)
FORCED_SKILLS = ('agentd-ops',)


@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str
    path: Path


@dataclass(frozen=True)
class ContextPromptFile:
    path: Path
    label: str
    text: str
    truncated: bool = False


@dataclass(frozen=True)
class ContextProfile:
    name: str
    skills: tuple[str, ...] = ()
    memory: str = 'rg'


@dataclass(frozen=True)
class ContextConfig:
    path: Path
    context_dir: Path
    memory_dir: Path
    default_profile: str = 'default'
    default_child_profile: str = 'default'
    skill_roots: tuple[Path, ...] = ()
    prompt_files: tuple[Path, ...] = ()
    prompt_file_max_bytes: int = 65536
    profiles: dict[str, ContextProfile] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedContext:
    profile: ContextProfile
    skills: tuple[SkillInfo, ...]
    missing_skills: tuple[str, ...]
    memory_dir: Path
    prompt_files: tuple[ContextPromptFile, ...] = ()

    def codex_config_overrides(self) -> list[str]:
        entries = ','.join('{path=' + json.dumps(str(skill.path)) + ',enabled=true}' for skill in self.skills)
        return [f'skills.config=[{entries}]']


class ContextResolver:
    def __init__(self, config: ContextConfig, workspace: Path) -> None:
        self.config = config
        self.workspace = workspace
        self.memory_dir = config.memory_dir
        self.builtin_skills = scan_skills(BUILTIN_SKILL_ROOTS)
        configured_skills = scan_skills(config.skill_roots)
        self.skills = {**self.builtin_skills, **configured_skills}
        self.forced_skills = tuple(
            skill for name in FORCED_SKILLS if (skill := self.builtin_skills.get(name)) is not None
        )

    def resolve(self, profile_name: str = '', extra_skills: tuple[str, ...] = ()) -> ResolvedContext:
        profile_name = profile_name or self.config.default_profile
        profile = self.config.profiles.get(profile_name) or ContextProfile(name=profile_name)
        requested = expand_skill_names((*profile.skills, *extra_skills), self.skills)
        found: list[SkillInfo] = list(self.forced_skills)
        seen = {skill.name for skill in found}
        missing: list[str] = []
        for name in requested:
            if name in seen:
                continue
            skill = self.skills.get(name)
            if skill is None:
                missing.append(name)
            else:
                found.append(skill)
                seen.add(name)
        return ResolvedContext(
            profile=profile,
            skills=tuple(found),
            missing_skills=tuple(missing),
            memory_dir=self.memory_dir,
            prompt_files=load_prompt_files(
                self.config.prompt_files,
                context_dir=self.config.context_dir,
                max_bytes=self.config.prompt_file_max_bytes,
            ),
        )


def load_context_config(path: Path, context_dir: Path) -> ContextConfig:
    raw = _load_toml(path)
    context_raw = raw.get('context') if isinstance(raw.get('context'), dict) else {}
    profiles_raw = raw.get('profiles') if isinstance(raw.get('profiles'), dict) else {}

    configured_context_dir = context_raw.get('context_dir') or context_raw.get('dir')
    if configured_context_dir:
        context_dir = _as_path(configured_context_dir, context_dir)

    memory_dir = _as_path(context_raw.get('memory_dir') or 'memory', context_dir)

    skill_roots_raw = context_raw.get('skill_roots')
    if skill_roots_raw is None:
        skill_roots_raw = ['skills', '~/.codex/skills']
    skill_roots = tuple(_as_path(item, context_dir) for item in _as_list(skill_roots_raw))
    prompt_files_raw = context_raw.get('prompt_files')
    if prompt_files_raw is None:
        prompt_files_raw = ['CONTEXT.md', 'memory/MEMORY.md']
    prompt_files = tuple(_as_path(item, context_dir) for item in _as_list(prompt_files_raw))
    prompt_file_max_bytes = int(context_raw.get('prompt_file_max_bytes') or 65536)

    profiles: dict[str, ContextProfile] = {}
    for name, value in profiles_raw.items():
        if not isinstance(value, dict):
            continue
        profile_name = str(name)
        profiles[profile_name] = ContextProfile(
            name=profile_name,
            skills=split_skill_names(value.get('skills')),
            memory=str(value.get('memory') or 'rg'),
        )

    if 'default' not in profiles:
        profiles['default'] = ContextProfile(name='default')

    return ContextConfig(
        path=path,
        context_dir=context_dir,
        memory_dir=memory_dir,
        default_profile=str(context_raw.get('default_profile') or 'default'),
        default_child_profile=str(context_raw.get('default_child_profile') or 'default'),
        skill_roots=skill_roots,
        prompt_files=prompt_files,
        prompt_file_max_bytes=prompt_file_max_bytes,
        profiles=profiles,
    )


def load_prompt_files(paths: tuple[Path, ...], *, context_dir: Path, max_bytes: int) -> tuple[ContextPromptFile, ...]:
    if max_bytes <= 0:
        return ()
    loaded: list[ContextPromptFile] = []
    for path in paths:
        try:
            if not path.is_file():
                continue
            with path.open('rb') as fh:
                data = fh.read(max_bytes + 1)
        except OSError:
            continue
        truncated = len(data) > max_bytes
        if truncated:
            data = data[:max_bytes]
        text = data.decode('utf-8', errors='replace').strip()
        if not text:
            continue
        if truncated:
            text += f'\n\n[agentd: truncated after {max_bytes} bytes]'
        loaded.append(
            ContextPromptFile(
                path=path,
                label=prompt_file_label(path, context_dir),
                text=text,
                truncated=truncated,
            )
        )
    return tuple(loaded)


def prompt_file_label(path: Path, context_dir: Path) -> str:
    try:
        return str(path.relative_to(context_dir))
    except ValueError:
        return str(path)


def scan_skills(roots: tuple[Path, ...]) -> dict[str, SkillInfo]:
    skills: dict[str, SkillInfo] = {}
    duplicates: dict[str, list[Path]] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob('SKILL.md')):
            if any(part.startswith('.') for part in path.relative_to(root).parts[:-1]):
                continue
            skill = read_skill_info(path)
            if skill is None:
                continue
            if skill.name in skills:
                duplicates.setdefault(skill.name, [skills[skill.name].path]).append(path)
                continue
            skills[skill.name] = skill

    if duplicates:
        lines = []
        for name, paths in sorted(duplicates.items()):
            joined = ', '.join(str(path) for path in paths)
            lines.append(f'{name}: {joined}')
        raise RuntimeError('duplicate skill names: ' + '; '.join(lines))
    return skills


def read_skill_info(path: Path) -> SkillInfo | None:
    try:
        text = path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        text = path.read_text(encoding='utf-8', errors='replace')
    frontmatter = parse_frontmatter(text)
    name = str(frontmatter.get('name') or path.parent.name).strip()
    description = str(frontmatter.get('description') or '').strip()
    if not name:
        return None
    return SkillInfo(name=name, description=description, path=path.resolve())


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith('---'):
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---':
        return {}
    data: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == '---':
            break
        if ':' not in line:
            continue
        key, value = line.split(':', 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            data[key] = value
    return data


def split_skill_names(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw = value.replace(',', ' ').split()
        return unique_names(raw)
    if isinstance(value, list):
        return unique_names(str(item).strip() for item in value)
    return ()


def expand_skill_names(requested: tuple[str, ...], available: dict[str, SkillInfo]) -> tuple[str, ...]:
    if not any(is_all_skills_marker(name) for name in requested):
        return unique_names(requested)
    names = [name for name in sorted(available) if name]
    names.extend(name for name in requested if not is_all_skills_marker(name))
    return unique_names(names)


def is_all_skills_marker(name: str) -> bool:
    return name.strip().lower() in {'*', 'all'}


def unique_names(values: tuple[str, ...] | list[str] | Any) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for item in values:
        name = str(item).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(name)
    return tuple(result)


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open('rb') as fh:
        data = tomllib.load(fh)
    return data if isinstance(data, dict) else {}


def _as_path(value: Any, base: Path) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]
