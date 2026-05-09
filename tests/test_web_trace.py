from __future__ import annotations

import json
import shlex
import tempfile
import unittest
from pathlib import Path

import zstandard

from agentd.registry import Registry
from agentd.web_gateway import build_state
from agentd.web_trace import build_responses_trace, exchange_detail, load_exchange


class WebTraceTest(unittest.TestCase):
    def test_builds_request_summaries_with_usage(self) -> None:
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

            self.assertEqual(trace['root']['children'], [])
            self.assertEqual(trace['exchanges'][0]['model'], 'gpt-test')
            self.assertEqual(trace['exchanges'][0]['input_tokens'], 3)
            self.assertEqual(trace['exchanges'][0]['output_tokens'], 2)
            self.assertEqual(trace['exchanges'][0]['total_tokens'], 5)
            self.assertEqual(trace['exchanges'][0]['response_preview'], 'hi')

            state = build_state(registry, selected_run_id=run.id)
            self.assertEqual(state['selected_run']['id'], run.id)
            self.assertEqual(state['trace']['exchanges'][0]['codex_turn_id'], 'turn-1')

    def test_build_state_filters_trace_to_selected_turn(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            registry = Registry(root / 'agentd.sqlite')
            session = registry.get_main_session('web', str(root))
            first = registry.create_run(
                session_id=session.id,
                source_message_id='web-1',
                prompt='first',
                host='host',
                subject='Codex',
                display_title='first',
            )
            second = registry.create_run(
                session_id=session.id,
                source_message_id='web-2',
                prompt='second',
                host='host',
                subject='Codex',
                display_title='second',
            )
            registry.update_run(first.id, codex_thread_id='thread', turn_id='turn-1')
            registry.update_run(second.id, codex_thread_id='thread', turn_id='turn-2')
            first_request, first_response = write_exchange_files(root, request_text='first', response_text='one')
            second_request, second_response = write_exchange_files(root, request_text='second', response_text='two')
            insert_exchange(
                registry,
                exchange_id='exchange-1',
                session_id=session.id,
                turn_id='turn-1',
                request_path=first_request,
                response_path=first_response,
            )
            insert_exchange(
                registry,
                exchange_id='exchange-2',
                session_id=session.id,
                turn_id='turn-2',
                request_path=second_request,
                response_path=second_response,
            )

            state = build_state(registry, selected_run_id=first.id)

            self.assertEqual([item['codex_turn_id'] for item in state['trace']['exchanges']], ['turn-1'])
            self.assertEqual(
                state['selected_run']['codex_resume_command'],
                f'cd {shlex.quote(str(root))} && codex resume thread',
            )

    def test_decodes_zstd_compressed_request_body(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            registry = Registry(root / 'agentd.sqlite')
            session = registry.get_main_session('web', str(root))
            request_path, response_path = write_exchange_files(
                root,
                request_text='compressed hello',
                response_text='compressed hi',
                request_content_encoding='zstd',
            )
            insert_exchange(
                registry,
                session_id=session.id,
                turn_id='turn-zstd',
                request_path=request_path,
                response_path=response_path,
            )

            row = registry.list_model_http_exchanges(session_id=session.id)[0]
            detail = exchange_detail(load_exchange(row))

            self.assertEqual(detail['response_text'], 'compressed hi')
            self.assertEqual(detail['codex_turn_id'], 'turn-zstd')
            self.assertEqual(detail['request_input_items'][1]['content'], 'compressed hello')

    def test_exchange_detail_uses_non_empty_content_for_function_calls(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            registry = Registry(root / 'agentd.sqlite')
            session = registry.get_main_session('web', str(root))
            request_path, response_path = write_exchange_files(
                root,
                request_text='hello',
                response_text='done',
                extra_input=[
                    {'type': 'function_call', 'name': 'shell', 'arguments': '{"cmd":"pwd"}', 'call_id': 'call-1'},
                    {'type': 'function_call_output', 'call_id': 'call-1', 'output': '/tmp'},
                ],
            )
            insert_exchange(
                registry,
                session_id=session.id,
                turn_id='turn-tools',
                request_path=request_path,
                response_path=response_path,
            )

            row = registry.list_model_http_exchanges(session_id=session.id)[0]
            detail = exchange_detail(load_exchange(row))
            contents = [item['content'] for item in detail['request_input_items']]

            self.assertTrue(all(content for content in contents))
            self.assertTrue(any('pwd' in content for content in contents))
            self.assertTrue(any('/tmp' in content for content in contents))


def write_exchange_files(
    root: Path,
    *,
    request_text: str,
    response_text: str,
    request_content_encoding: str = '',
    extra_input: list[dict[str, object]] | None = None,
) -> tuple[Path, Path]:
    capture_dir = root / 'captures' / 'responses' / '2026-W19'
    capture_dir.mkdir(parents=True, exist_ok=True)
    request_body = {
        'model': 'gpt-test',
        'stream': True,
        'input': [
            {'role': 'developer', 'content': 'rules'},
            {'role': 'user', 'content': [{'type': 'input_text', 'text': request_text}]},
            *(extra_input or []),
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
    request_body_bytes = json.dumps(request_body).encode('utf-8')
    request_headers = 'Content-Type: application/json\r\n'
    if request_content_encoding == 'zstd':
        request_body_bytes = zstandard.ZstdCompressor().compress(request_body_bytes)
        request_headers += 'Content-Encoding: zstd\r\n'
    request_path = capture_dir / 'exchange-request.http'
    response_path = capture_dir / 'exchange-response.http'
    request_path.write_bytes(
        f'POST /v1/responses HTTP/1.1\r\n{request_headers}\r\n'.encode('utf-8') + request_body_bytes
    )
    response_path.write_bytes(
        b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n' + f'data: {json.dumps(completed)}\n\n'.encode()
    )
    return request_path, response_path


def insert_exchange(
    registry: Registry,
    *,
    exchange_id: str = 'exchange',
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
                exchange_id,
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
