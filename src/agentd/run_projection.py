from __future__ import annotations

from dataclasses import dataclass, field

from .models import AgentSession, RunEvent, RunRecord
from .registry import Registry


@dataclass
class RunIteration:
    message: str = ''
    phase: str = ''
    tool_counts: dict[str, int] = field(default_factory=dict)
    failed_tool_counts: dict[str, int] = field(default_factory=dict)
    running_tools: dict[str, int] = field(default_factory=dict)
    tool_details: list[str] = field(default_factory=list)


@dataclass
class RunView:
    run: RunRecord
    session: AgentSession
    iterations: list[RunIteration]
    running_tools: dict[str, str]
    tool_details: list[str]
    model_outputs: list[str]


def load_run_view(registry: Registry, run_id: int) -> RunView | None:
    run = registry.get_run(run_id)
    if run is None:
        return None
    session = registry.get_session(run.session_id)
    if session is None:
        return None
    iterations, running_tools, tool_details, model_outputs = project_run_events(registry.list_run_events(run_id))
    return RunView(
        run=run,
        session=session,
        iterations=iterations,
        running_tools=running_tools,
        tool_details=tool_details,
        model_outputs=model_outputs,
    )


def project_run_events(events: list[RunEvent]) -> tuple[list[RunIteration], dict[str, str], list[str], list[str]]:
    iterations: list[RunIteration] = []
    running_tools: dict[str, str] = {}
    tool_details: list[str] = []
    model_outputs: list[str] = []

    def current_iteration() -> RunIteration:
        if not iterations:
            iterations.append(RunIteration(message='准备中', phase='system'))
        return iterations[-1]

    for event in events:
        payload = event.payload
        if event.event_type == 'agent_message':
            text = str(payload.get('text') or '').strip()
            if text:
                iterations.append(RunIteration(message=text, phase=str(payload.get('phase') or 'commentary')))
                model_outputs.append(text)
        elif event.event_type == 'tool_started':
            tool = str(payload.get('tool') or 'Tool')
            item_id = str(payload.get('item_id') or '')
            detail = str(payload.get('detail') or '')
            iteration = current_iteration()
            iteration.tool_counts[tool] = iteration.tool_counts.get(tool, 0) + 1
            if item_id:
                running_tools[item_id] = tool
                iteration.running_tools[tool] = iteration.running_tools.get(tool, 0) + 1
            if detail:
                tool_detail = f'{tool}: {detail}'
                iteration.tool_details.append(tool_detail)
                tool_details.append(compact(tool_detail, 220))
                tool_details = tool_details[-80:]
        elif event.event_type == 'tool_completed':
            item_id = str(payload.get('item_id') or '')
            tool = running_tools.pop(item_id, '') if item_id else ''
            if not tool:
                continue
            for iteration in reversed(iterations):
                if iteration.running_tools.get(tool, 0) > 0:
                    iteration.running_tools[tool] -= 1
                    if iteration.running_tools[tool] <= 0:
                        iteration.running_tools.pop(tool, None)
                    if payload.get('failed'):
                        iteration.failed_tool_counts[tool] = iteration.failed_tool_counts.get(tool, 0) + 1
                    break
    return iterations, running_tools, tool_details, model_outputs


def last_model_output(registry: Registry, run_id: int) -> str:
    for event in reversed(registry.list_run_events(run_id)):
        if event.event_type == 'agent_message':
            return str(event.payload.get('text') or '').strip()
    return ''


def compact(text: object, limit: int) -> str:
    value = ' '.join(str(text or '').split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 6)] + ' ...(截断)'
