from __future__ import annotations

import json
import unittest

from agentd.models import AgentSession, RunEvent, RunRecord
from agentd.run_projection import RunView, project_run_events
from agentd.status_rendering import build_status_card, format_status_text, tools_view


class StatusRenderingTest(unittest.TestCase):
    def test_project_run_events_tracks_tool_lifecycle(self) -> None:
        iterations, running_tools, tool_details, model_outputs = project_run_events(
            [
                RunEvent(
                    id=1,
                    run_id=1,
                    event_type='agent_message',
                    payload={'text': 'working', 'phase': 'commentary'},
                    created_at=100,
                ),
                RunEvent(
                    id=2,
                    run_id=1,
                    event_type='tool_started',
                    payload={'tool': 'Shell', 'item_id': 'tool-1', 'detail': 'uv run ruff check'},
                    created_at=101,
                ),
                RunEvent(
                    id=3,
                    run_id=1,
                    event_type='tool_completed',
                    payload={'item_id': 'tool-1', 'failed': True},
                    created_at=102,
                ),
            ]
        )

        self.assertEqual(model_outputs, ['working'])
        self.assertEqual(running_tools, {})
        self.assertEqual(tool_details, ['Shell: uv run ruff check'])
        self.assertEqual(iterations[0].tool_counts, {'Shell': 1})
        self.assertEqual(iterations[0].failed_tool_counts, {'Shell': 1})

    def test_format_status_text_and_card_are_view_rendering_only(self) -> None:
        view = RunView(
            run=make_run(),
            session=make_session(),
            iterations=[],
            running_tools={},
            tool_details=[],
            model_outputs=[],
        )

        status_text = format_status_text(view)
        card = build_status_card(view)
        raw_card = json.dumps(card, ensure_ascii=False)

        self.assertIn('Codex 失败: 失败: systemError', status_text)
        self.assertIn('错误信息: upstream overloaded', status_text)
        self.assertIn('**错误信息**', raw_card)
        self.assertEqual(card['header']['template'], 'red')

    def test_tools_view_is_channel_neutral_text(self) -> None:
        iterations, running_tools, tool_details, _ = project_run_events(
            [
                RunEvent(
                    id=1,
                    run_id=1,
                    event_type='tool_started',
                    payload={'tool': 'Shell', 'item_id': 'tool-1', 'detail': 'date'},
                    created_at=100,
                )
            ]
        )
        view = RunView(
            run=make_run(status_phase='running', status='工作中', error=''),
            session=make_session(),
            iterations=iterations,
            running_tools=running_tools,
            tool_details=tool_details,
            model_outputs=[],
        )

        text = tools_view(view)

        self.assertIn('Shell x1', text)
        self.assertIn('最近工具调用', text)


def make_session() -> AgentSession:
    return AgentSession(
        id=1,
        kind='main',
        chat_id='chat-1',
        thread_id=None,
        root_message_id=None,
        codex_thread_id='thread-1',
        cwd='/workspace',
    )


def make_run(*, status_phase: str = 'failed', status: str = '失败: systemError', error: str = 'upstream overloaded') -> RunRecord:
    return RunRecord(
        id=1,
        session_id=1,
        source_message_id='msg-1',
        prompt='hello',
        state='failed',
        status_phase=status_phase,
        status=status,
        status_message_id='card-1',
        codex_thread_id='thread-1',
        turn_id='turn-1',
        subject='Codex',
        display_title='Status test',
        host='host',
        status_reply_in_thread=False,
        context_profile='default',
        skills=(),
        hide_early_iterations=True,
        show_tool_details=False,
        truncate_content=True,
        final_message_text='',
        final_message_sent_at=None,
        error=error,
        handoff_child_session_id=None,
        started_at=100,
        finished_at=110,
        heartbeat_at=100,
        lease_until=130,
        created_at=100,
        updated_at=110,
    )


if __name__ == '__main__':
    unittest.main()
