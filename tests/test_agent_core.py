from __future__ import annotations

import unittest
from types import SimpleNamespace

from agentd.active_run import ActiveRun
from agentd.agent_core import AgentCore
from agentd.channels import ControlCommand
from agentd.models import AgentSession, IncomingMessage


class AgentCoreTest(unittest.TestCase):
    def test_unknown_control_command_is_rejected(self) -> None:
        core = AgentCore(SimpleNamespace())

        result = core.handle_control_command(
            ControlCommand(
                command_type='unknown',
                channel='web',
                conversation_ref='browser',
                message_ref='msg-1',
            )
        )

        self.assertEqual(result, 'unsupported control command: unknown')

    def test_live_status_command_only_publishes_status(self) -> None:
        owner = FakeOwner()
        core = AgentCore(owner)
        active = make_active(control=FakeControl())

        result = core.handle_live_input(active, make_message('/status'))

        self.assertEqual(result, 'status')
        self.assertEqual(owner.published, [(active.run_id, True, True)])
        self.assertEqual(owner.registry.updates, [])

    def test_live_input_steers_active_run_and_records_control_event(self) -> None:
        owner = FakeOwner()
        core = AgentCore(owner)
        active = make_active(control=FakeControl(steer_result=(True, 'appended')))

        result = core.handle_live_input(active, make_message('continue'))

        self.assertEqual(result, 'appended')
        self.assertEqual(owner.registry.updates, [(active.run_id, {'status': '已追加指令'})])
        self.assertEqual(owner.model_messages, [(active.run_id, '用户追加指令：continue', 'control')])
        self.assertEqual(owner.published, [(active.run_id, True, True)])


class FakeOwner:
    def __init__(self) -> None:
        self.registry = FakeRegistry()
        self.run_context_builder = FakeRunContextBuilder()
        self.published: list[tuple[int, bool, bool]] = []
        self.model_messages: list[tuple[int, str, str]] = []

    def _publish_status(self, active: ActiveRun, *, force: bool = False, create: bool = True) -> None:
        self.published.append((active.run_id, force, create))

    def _add_model_message(self, active: ActiveRun, text: str, *, phase: str) -> None:
        self.model_messages.append((active.run_id, text, phase))


class FakeRegistry:
    def __init__(self) -> None:
        self.updates: list[tuple[int, dict[str, object]]] = []

    def update_run(self, run_id: int, **fields: object) -> None:
        self.updates.append((run_id, fields))


class FakeRunContextBuilder:
    def message_prompt(self, message: IncomingMessage, session: AgentSession) -> str:
        return f'prompt: {message.text}'


class FakeControl:
    def __init__(
        self,
        *,
        steer_result: tuple[bool, str] = (False, 'unsupported'),
        interrupt_result: tuple[bool, str] = (False, 'unsupported'),
    ) -> None:
        self.steer_result = steer_result
        self.interrupt_result = interrupt_result

    def steer(self, text: str) -> tuple[bool, str]:
        return self.steer_result

    def interrupt(self) -> tuple[bool, str]:
        return self.interrupt_result


def make_active(*, control: FakeControl) -> ActiveRun:
    return ActiveRun(
        run_id=42,
        session=AgentSession(
            id=7,
            kind='main',
            chat_id='chat-1',
            thread_id=None,
            root_message_id=None,
            codex_thread_id=None,
            cwd='/workspace',
        ),
        control=control,  # type: ignore[arg-type]
    )


def make_message(text: str) -> IncomingMessage:
    return IncomingMessage(chat_id='chat-1', message_id='msg-1', text=text)


if __name__ == '__main__':
    unittest.main()
