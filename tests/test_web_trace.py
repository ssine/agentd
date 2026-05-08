from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentd.registry import Registry
from agentd.web_gateway import build_state
from agentd.web_trace import build_responses_trace


class WebTraceTest(unittest.TestCase):
    def test_builds_deduplicated_responses_tree_with_usage(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            registry = Registry(root / 'agentd.sqlite')
            session = registry.get_main_session('web', str(root))
            run = registry.create_run(
                session_id=session.id,
                source_message_id='web-1',
                prompt='hello',
                host='host',
                subject='Codex',
                display_title='hello',
            )
            request_path, response_path = write_exchange_files(root, request_text='hello', response_text='hi')
            insert_exchange(
                registry,
                session_id=session.id,
                turn_id='turn-1',
                request_path=request_path,
                response_path=response_path,
            )

            rows = registry.list_model_http_exchanges(session_id=session.id)
            trace = build_responses_trace(rows)
            assistant = trace['root']['children'][0]['children'][0]['children'][0]

            self.assertEqual(assistant['role'], 'assistant')
            self.assertEqual(assistant['content'], 'hi')
            self.assertEqual(assistant['request']['model'], 'gpt-test')
            self.assertEqual(assistant['request']['input_tokens'], 3)
            self.assertEqual(assistant['request']['output_tokens'], 2)
            self.assertEqual(assistant['request']['total_tokens'], 5)

            state = build_state(registry, selected_run_id=run.id)
            self.assertEqual(state['selected_run']['id'], run.id)
            self.assertEqual(state['trace']['exchanges'][0]['codex_turn_id'], 'turn-1')


def write_exchange_files(root: Path, *, request_text: str, response_text: str) -> tuple[Path, Path]:
    capture_dir = root / 'captures' / 'responses' / '2026-W19'
    capture_dir.mkdir(parents=True)
    request_body = {
        'model': 'gpt-test',
        'stream': True,
        'input': [
            {'role': 'developer', 'content': 'rules'},
            {'role': 'user', 'content': [{'type': 'input_text', 'text': request_text}]},
        ],
    }
    completed = {
        'type': 'response.completed',
        'response': {
            'output': [
                {
                    'type': 'message',
                    'role': 'assistant',
                    'content': [{'type': 'output_text', 'text': response_text}],
                }
            ],
            'usage': {'input_tokens': 3, 'output_tokens': 2, 'total_tokens': 5},
        },
    }
    request_path = capture_dir / 'exchange-request.http'
    response_path = capture_dir / 'exchange-response.http'
    request_path.write_bytes(
        b'POST /v1/responses HTTP/1.1\r\nContent-Type: application/json\r\n\r\n'
        + json.dumps(request_body).encode('utf-8')
    )
    response_path.write_bytes(
        b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n' + f'data: {json.dumps(completed)}\n\n'.encode()
    )
    return request_path, response_path


def insert_exchange(
    registry: Registry,
    *,
    session_id: int,
    turn_id: str,
    request_path: Path,
    response_path: Path,
) -> None:
    with registry.connect() as conn:
        conn.execute(
            """
            insert into model_http_exchanges(
                id,
                created_at,
                completed_at,
                session_id,
                codex_thread_id,
                codex_turn_id,
                method,
                request_path,
                upstream_url,
                provider_id,
                model,
                stream,
                status_code,
                request_headers_path,
                request_body_raw_path,
                request_body_decoded_path,
                response_headers_path,
                response_body_raw_path,
                storage_state,
                period_key,
                request_capture_path,
                response_capture_path,
                archive_path,
                archive_member_request,
                archive_member_response,
                archive_format,
                error
            )
            values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'exchange',
                1,
                2,
                session_id,
                'thread',
                turn_id,
                'POST',
                '/v1/responses',
                'https://example.test/v1/responses',
                'agentd-capture',
                'gpt-test',
                1,
                200,
                str(request_path),
                str(request_path),
                None,
                str(response_path),
                str(response_path),
                'live',
                '2026-W19',
                str(request_path),
                str(response_path),
                None,
                None,
                None,
                'tar.zst',
                None,
            ),
        )


if __name__ == '__main__':
    unittest.main()
