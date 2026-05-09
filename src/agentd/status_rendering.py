from __future__ import annotations

import time
from typing import Any

from .models import AgentSession
from .run_projection import RunIteration, RunView, compact
from .title import normalize_title


def format_status_text(active: RunView) -> str:
    elapsed = elapsed_seconds(active)
    subject = active.run.subject or 'Agent'
    lines = [
        f'{subject} {phase_label(active.run.status_phase)}: {active.run.status}',
        run_line(active, elapsed),
        toggle_state_line(active),
    ]
    if active.run.status_phase == 'failed' and active.run.error:
        lines.append(f'错误信息: {compact(active.run.error, 1200)}')
    iterations = visible_iterations(active)
    offset = max(0, len(active.iterations) - len(iterations))
    for index, iteration in enumerate(iterations, start=offset + 1):
        lines.extend(iteration_lines(active, index, iteration))
    return '\n'.join(lines)


def build_status_card(active: RunView) -> dict[str, Any]:
    elapsed = elapsed_seconds(active)
    template = {
        'running': 'blue',
        'done': 'green',
        'stopped': 'orange',
        'failed': 'red',
    }.get(active.run.status_phase, 'blue')

    return {
        'config': {'wide_screen_mode': True, 'update_multi': True},
        'header': {
            'template': template,
            'title': {'tag': 'plain_text', 'content': status_title(active)},
        },
        'elements': [
            {'tag': 'div', 'text': {'tag': 'lark_md', 'content': run_line(active, elapsed)}},
            *error_elements(active),
            {'tag': 'hr'},
            *view_elements(active),
            {'tag': 'action', 'actions': card_actions(active)},
        ],
    }


def run_line(active: RunView, elapsed: int) -> str:
    return f'{session_label(active.session)} · {active.run.host} · {active.session.cwd} · {format_elapsed(elapsed)}'


def view_elements(active: RunView) -> list[dict[str, Any]]:
    return [{'tag': 'div', 'text': {'tag': 'lark_md', 'content': iterations_view(active)}}]


def error_elements(active: RunView) -> list[dict[str, Any]]:
    if active.run.status_phase != 'failed' or not active.run.error:
        return []
    content = '**错误信息**\n' + escape_lark_md(compact(active.run.error, 1800))
    return [{'tag': 'div', 'text': {'tag': 'lark_md', 'content': content}}]


def iterations_view(active: RunView) -> str:
    iterations = visible_iterations(active)
    if not iterations:
        return f'等待 {active.run.subject or "Agent"} 产生可见输出。'

    offset = max(0, len(active.iterations) - len(iterations))
    lines: list[str] = []
    for index, iteration in enumerate(iterations, start=offset + 1):
        lines.extend(iteration_lines(active, index, iteration))
    if len(active.iterations) > len(iterations):
        lines.append(f'已隐藏更早 {len(active.iterations) - len(iterations)} 步，点“早期：隐藏”切换。')
    return '\n'.join(lines)


def tools_view(active: RunView) -> str:
    totals: dict[str, int] = {}
    failures: dict[str, int] = {}
    for iteration in active.iterations:
        for tool, count in iteration.tool_counts.items():
            totals[tool] = totals.get(tool, 0) + count
        for tool, count in iteration.failed_tool_counts.items():
            failures[tool] = failures.get(tool, 0) + count

    lines = ['**工具详情**']
    if totals:
        lines.append('总计：' + ', '.join(f'{tool} x{count}' for tool, count in totals.items()))
    if failures:
        lines.append('失败：' + ', '.join(f'{tool} x{count}' for tool, count in failures.items()))
    if active.running_tools:
        running = {}
        for tool in active.running_tools.values():
            running[tool] = running.get(tool, 0) + 1
        lines.append('进行中：' + ', '.join(f'{tool} x{count}' for tool, count in running.items()))
    if active.tool_details:
        lines.append('')
        lines.append('最近工具调用：')
        lines.extend(f'- {escape_lark_md(item)}' for item in active.tool_details[-12:])
    if len(lines) == 1:
        lines.append('还没有工具调用。')
    return '\n'.join(lines)


def output_view(active: RunView) -> str:
    if not active.model_outputs:
        return '**模型输出**\n还没有模型输出。'
    parts = []
    for index, text in enumerate(active.model_outputs[-8:], start=max(1, len(active.model_outputs) - 7)):
        parts.append(f'{index}. {escape_lark_md(compact(text, 500))}')
    return '**模型输出**\n' + '\n\n'.join(parts)


def visible_iterations(active: RunView) -> list[RunIteration]:
    if active.run.hide_early_iterations:
        return active.iterations[-6:]
    return active.iterations


def iteration_lines(active: RunView, index: int, iteration: RunIteration) -> list[str]:
    message = display_text(active, iteration.message, truncated_limit=150, expanded_limit=4000)
    lines = [f'{index}. 💬 {escape_lark_md(message)}']
    tools = format_tool_counts(iteration)
    if tools:
        lines.append(f'   🛠 {escape_lark_md(tools)}')
    if active.run.show_tool_details and iteration.tool_details:
        for detail in iteration.tool_details:
            text = display_text(active, detail, truncated_limit=180, expanded_limit=4000)
            lines.append(f'   🔧 {escape_lark_md(text)}')
    return lines


def display_text(active: RunView, text: object, *, truncated_limit: int, expanded_limit: int) -> str:
    return compact(text, truncated_limit if active.run.truncate_content else expanded_limit)


def elapsed_seconds(active: RunView) -> int:
    end = active.run.finished_at if active.run.finished_at is not None else int(time.time())
    return max(0, int(end - active.run.started_at))


def toggle_state_line(active: RunView) -> str:
    early = '隐藏' if active.run.hide_early_iterations else '显示'
    tools = '展开' if active.run.show_tool_details else '摘要'
    truncate = '开' if active.run.truncate_content else '关'
    return f'早期：{early} · 工具：{tools} · 截断：{truncate}'


def card_actions(active: RunView) -> list[dict[str, Any]]:
    actions: list[tuple[str, str, str]] = []
    if active.run.status_phase == 'running':
        actions.append(('停止', 'danger', 'stop'))
    actions.extend(
        [
            (f'早期：{"隐藏" if active.run.hide_early_iterations else "显示"}', 'default', 'toggle_early'),
            (f'工具：{"展开" if active.run.show_tool_details else "摘要"}', 'default', 'toggle_tools'),
            (f'截断：{"开" if active.run.truncate_content else "关"}', 'default', 'toggle_truncate'),
        ]
    )
    return [
        {
            'tag': 'button',
            'text': {'tag': 'plain_text', 'content': label},
            'type': style,
            'value': {
                'action': action,
                'session_id': active.session.id,
                'message_id': active.run.status_message_id,
                'chat_id': active.session.chat_id,
            },
        }
        for label, style, action in actions
    ]


def phase_label(phase: str) -> str:
    return {
        'running': '工作中',
        'done': '已完成',
        'stopped': '已停止',
        'failed': '失败',
    }.get(phase, '工作中')


def view_label(view: str) -> str:
    return {
        'live': '实时视图',
        'history': '完整过程',
        'tools': '工具详情',
        'output': '模型输出',
    }.get(view, view)


def session_label(session: AgentSession) -> str:
    if session.kind == 'main':
        return '主会话'
    if session.kind == 'child':
        return '话题会话'
    if session.kind == 'schedule':
        return '定时会话'
    return session.kind or '会话'


def status_title(active: RunView) -> str:
    icon = '🌿' if active.session.kind == 'child' else '🧵'
    title = active.run.display_title or ('子任务' if active.session.kind == 'child' else '主任务')
    return f'{icon} {normalize_title(title, fallback="任务")}'


def format_tool_counts(iteration: RunIteration) -> str:
    parts = [f'{tool} x{count}' for tool, count in iteration.tool_counts.items()]
    if iteration.failed_tool_counts:
        failed = ', '.join(f'{tool} x{count}' for tool, count in iteration.failed_tool_counts.items())
        parts.append(f'失败：{failed}')
    if iteration.running_tools:
        running = ', '.join(f'{tool} x{count}' for tool, count in iteration.running_tools.items())
        parts.append(f'进行中：{running}')
    return ', '.join(parts)


def format_elapsed(seconds: int) -> str:
    if seconds < 60:
        return f'{seconds}s'
    minutes, rem = divmod(seconds, 60)
    if minutes < 60:
        return f'{minutes}m{rem:02d}s'
    hours, minutes = divmod(minutes, 60)
    return f'{hours}h{minutes:02d}m'


def escape_lark_md(text: str) -> str:
    return text.replace('\\', '\\\\').replace('`', '\\`')
