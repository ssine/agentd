from __future__ import annotations

import unicodedata

TITLE_DISPLAY_WIDTH = 32


def title_from_text(text: object, *, fallback: str) -> str:
    value = ' '.join(str(text or '').split())
    if not value:
        return normalize_title(fallback, fallback=fallback)
    prefixes = [
        '在 ',
        '开个子任务',
        '开子任务',
        '开 thread',
        '这个在 thread 里跑',
        '帮我',
        '请',
    ]
    for prefix in prefixes:
        if value.startswith(prefix):
            value = value[len(prefix) :].strip(' ：:，,')
            break
    return normalize_title(value, fallback=fallback)


def normalize_title(text: object, *, fallback: str = '任务') -> str:
    value = ' '.join(str(text or '').split())
    if not value:
        value = fallback
    return compact_display_width(value, TITLE_DISPLAY_WIDTH)


def compact_display_width(text: object, limit: int, *, suffix: str = '...') -> str:
    value = ' '.join(str(text or '').split())
    if display_width(value) <= limit:
        return value
    suffix_width = display_width(suffix)
    budget = max(0, limit - suffix_width)
    result: list[str] = []
    used = 0
    for char in value:
        width = char_display_width(char)
        if used + width > budget:
            break
        result.append(char)
        used += width
    return ''.join(result).rstrip() + suffix


def display_width(text: str) -> int:
    return sum(char_display_width(char) for char in text)


def char_display_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    if unicodedata.east_asian_width(char) in {'F', 'W'}:
        return 2
    return 1
