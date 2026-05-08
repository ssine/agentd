from __future__ import annotations

import unittest
from types import SimpleNamespace

from agentd.capture_proxy import CAPTURE_PROVIDER_ID
from agentd.codex_app_server import CodexAppServer
from agentd.models import AgentSession


class ResumeThreadTest(unittest.TestCase):
    def test_resume_thread_does_not_require_experimental_exclude_turns(self) -> None:
        server = RecordingCodexAppServer()
        session = AgentSession(
            id=5,
            kind='main',
            chat_id='chat',
            thread_id=None,
            root_message_id=None,
            codex_thread_id='existing-thread',
            cwd='/tmp',
        )

        thread_id = server._start_or_resume_thread(None, None, session)

        self.assertEqual(thread_id, 'existing-thread')
        self.assertEqual(server.requests[0][0], 'thread/resume')
        self.assertNotIn('excludeTurns', server.requests[0][1])
        self.assertEqual([method for method, _ in server.requests], ['thread/resume'])

    def test_capture_provider_override_wins_for_thread_start(self) -> None:
        server = RecordingCodexAppServer(model_provider='user-provider')
        session = AgentSession(
            id=5,
            kind='main',
            chat_id='chat',
            thread_id=None,
            root_message_id=None,
            codex_thread_id=None,
            cwd='/tmp',
        )

        thread_id = server._start_or_resume_thread(None, None, session, model_provider_override=CAPTURE_PROVIDER_ID)

        self.assertEqual(thread_id, 'new-thread')
        self.assertEqual(server.requests[0][0], 'thread/start')
        self.assertEqual(server.requests[0][1]['modelProvider'], CAPTURE_PROVIDER_ID)

    def test_start_turn_attaches_responses_metadata(self) -> None:
        server = RecordingCodexAppServer()
        metadata = {
            'agentd_session_id': '5',
            'agentd_request_id': '5:123',
            'agentd_codex_thread_id': 'thread-1',
        }

        turn_id = server._start_turn(None, None, 'thread-1', 'hello', metadata)

        self.assertEqual(turn_id, 'turn-1')
        self.assertEqual(server.requests[0][0], 'turn/start')
        self.assertEqual(server.requests[0][1]['responsesapiClientMetadata'], metadata)


class RecordingCodexAppServer(CodexAppServer):
    def __init__(self, *, model_provider: str = '') -> None:
        super().__init__(
            SimpleNamespace(
                approval_policy='never',
                sandbox='danger-full-access',
                model='',
                model_provider=model_provider,
                startup_timeout_seconds=1,
            ),
            SimpleNamespace(mkdir=lambda **_: None),
        )
        self.requests: list[tuple[str, dict[str, object]]] = []

    def _request(self, proc, log, method, params, **kwargs):  # type: ignore[no-untyped-def]
        self.requests.append((method, params))
        if method == 'thread/resume':
            return {'thread': {'id': str(params['threadId'])}}
        if method == 'thread/start':
            return {'thread': {'id': 'new-thread'}}
        if method == 'turn/start':
            return {'turn': {'id': 'turn-1'}}
        raise AssertionError(f'unexpected request: {method}')


if __name__ == '__main__':
    unittest.main()
