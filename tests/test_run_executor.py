from __future__ import annotations

import logging
import threading
import unittest
from dataclasses import replace

from agentd.active_run import ActiveRun
from agentd.models import AgentSession, RunRecord
from agentd.run_executor import RunExecutor
from agentd.runners import AgentTurnResult


class RunExecutorTest(unittest.TestCase):
    def test_handle_runner_event_updates_run_and_projects_tool(self) -> None:
        owner = FakeOwner()
        active = make_active(owner.session)
        executor = RunExecutor(owner)

        executor.handle_runner_event(active, {'type': 'tool_started', 'tool': 'functions.exec_command', 'item_id': 'tool-1'})

        self.assertEqual(owner.registry.run.status, '调用 Shell')
        self.assertEqual(owner.tool_events, [(active.run_id, 'Shell', 'tool-1', '')])
        self.assertEqual(owner.published, [(active.run_id, False, True)])
        self.assertEqual(owner.registry.touched, [active.run_id])

    def test_run_turn_worker_marks_success_and_clears_active_run(self) -> None:
        owner = FakeOwner(
            runner=FakeRunner(AgentTurnResult(session_ref='runner-session-1', turn_ref='turn-1', final_text='done', status='completed'))
        )
        active = make_active(owner.session)
        owner._active_runs[owner.session.id] = active
        executor = RunExecutor(owner)

        executor.run_turn_worker(active)

        self.assertEqual(owner.registry.session_ref, 'runner-session-1')
        self.assertEqual(owner.registry.run.state, 'succeeded')
        self.assertEqual(owner.registry.run.status_phase, 'done')
        self.assertEqual(owner.registry.run.runner_session_ref, 'runner-session-1')
        self.assertEqual(owner.registry.run.runner_turn_ref, 'turn-1')
        self.assertEqual(owner.model_messages, [(active.run_id, 'done', 'final_answer')])
        self.assertEqual(owner.final_replies, [(active.run_id, 'done')])
        self.assertTrue(active.done.is_set())
        self.assertNotIn(owner.session.id, owner._active_runs)


class FakeOwner:
    def __init__(self, *, runner: object | None = None) -> None:
        self.session = make_session()
        self.registry = FakeRegistry(make_run(self.session.id))
        self.runner = runner or FakeRunner(AgentTurnResult(session_ref='', turn_ref='', final_text='', status='completed'))
        self.runner_context_builder = FakeRunnerContextBuilder()
        self.log = logging.getLogger('test')
        self.host = 'host-a'
        self._active_lock = threading.Lock()
        self._active_runs: dict[int, ActiveRun] = {}
        self.model_messages: list[tuple[int, str, str]] = []
        self.tool_events: list[tuple[int, str, str, str]] = []
        self.published: list[tuple[int, bool, bool]] = []
        self.replays: list[int] = []
        self.final_replies: list[tuple[int, str]] = []

    def _last_model_output(self, run_id: int) -> str:
        return ''

    def _add_model_message(self, active: ActiveRun, text: str, *, phase: str) -> None:
        self.model_messages.append((active.run_id, text, phase))

    def _add_tool(self, active: ActiveRun, tool: str, *, item_id: str = '', detail: str = '') -> None:
        self.tool_events.append((active.run_id, tool, item_id, detail))

    def _finish_tool(self, active: ActiveRun, item_id: str, *, failed: bool = False) -> None:
        pass

    def _publish_status(self, active: ActiveRun, *, force: bool = False, create: bool = True) -> None:
        self.published.append((active.run_id, force, create))

    def _schedule_terminal_status_replay(self, run_id: int) -> None:
        self.replays.append(run_id)

    def _queue_final_once(self, active: ActiveRun, final_text: str) -> bool:
        self.final_replies.append((active.run_id, final_text))
        return True


class FakeRegistry:
    def __init__(self, run: RunRecord) -> None:
        self.run = run
        self.session_ref = ''
        self.touched: list[int] = []

    def get_run(self, run_id: int) -> RunRecord | None:
        return self.run if self.run.id == run_id else None

    def update_run(self, run_id: int, **fields: object) -> None:
        self.run = replace(self.run, **fields)

    def update_run_and_mark_card_dirty(self, run_id: int, **fields: object) -> None:
        self.update_run(run_id, **fields)

    def update_runner_session(self, session_id: int, runner_session_ref: str, *, runner_kind: str = '') -> None:
        self.session_ref = runner_session_ref

    def touch_run_lease(self, run_id: int) -> None:
        self.touched.append(run_id)


class FakeRunner:
    kind = 'fake'
    label = 'Fake'

    def __init__(self, result: AgentTurnResult) -> None:
        self.result = result

    def start_turn(self, request, *, event_sink=None, control=None):  # type: ignore[no-untyped-def]
        return self.result


class FakeResolvedContext:
    def codex_config_overrides(self) -> list[str]:
        return []


class FakeRunnerContextBuilder:
    def build(self, *, active: ActiveRun, run: RunRecord) -> tuple[FakeResolvedContext, dict[str, str], str]:
        return FakeResolvedContext(), {'AGENTD_SESSION_ID': str(active.session.id)}, 'developer instructions'


class FakeControl:
    def set_thread_name(self, name: str) -> tuple[bool, str]:
        return True, 'ok'


def make_active(session: AgentSession) -> ActiveRun:
    return ActiveRun(run_id=1, session=session, control=FakeControl())  # type: ignore[arg-type]


def make_session() -> AgentSession:
    return AgentSession(
        id=7,
        kind='main',
        chat_id='chat-1',
        thread_id=None,
        root_message_id=None,
        codex_thread_id=None,
        cwd='/workspace',
    )


def make_run(session_id: int) -> RunRecord:
    return RunRecord(
        id=1,
        session_id=session_id,
        source_message_id='msg-1',
        prompt='hello',
        state='running',
        status_phase='running',
        status='启动',
        status_message_id='card-1',
        codex_thread_id='',
        turn_id='',
        subject='Fake',
        display_title='Run',
        host='host-a',
        status_reply_in_thread=False,
        context_profile='default',
        skills=(),
        hide_early_iterations=True,
        show_tool_details=False,
        truncate_content=True,
        final_message_text='',
        final_message_sent_at=None,
        error='',
        handoff_child_session_id=None,
        started_at=100,
        finished_at=None,
        heartbeat_at=100,
        lease_until=130,
        created_at=100,
        updated_at=100,
    )


if __name__ == '__main__':
    unittest.main()
