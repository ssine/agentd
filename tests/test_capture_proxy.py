from __future__ import annotations

import datetime as dt
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agentd.capture_proxy import (
    CHATGPT_RESPONSES_URL,
    OPENAI_RESPONSES_URL,
    CaptureContext,
    CaptureProxy,
    capture_period_key,
    capture_provider_overrides,
    read_tar_zst_member_names,
)


class CaptureProxyTest(unittest.TestCase):
    def test_proxy_captures_and_forwards_responses_exchange(self) -> None:
        upstream = RecordingUpstream()
        upstream.start()
        try:
            with tempfile.TemporaryDirectory() as raw_dir:
                root = Path(raw_dir)
                proxy = CaptureProxy(
                    capture_dir=root / 'captures',
                    db_path=root / 'agentd.sqlite',
                    upstream_mode='codex-default',
                    upstream_url=f'{upstream.url}/v1/responses',
                    context=CaptureContext(session_id=42, codex_thread_id='thread-before', codex_turn_id='turn-before'),
                )
                proxy.start()
                try:
                    request_body = {
                        'model': 'gpt-test',
                        'stream': True,
                        'input': [{'type': 'message', 'content': 'hello'}],
                    }
                    metadata = {
                        'agentd_session_id': '42',
                        'thread_id': 'codex-thread',
                        'turn_id': 'codex-turn',
                    }
                    raw_body = json.dumps(request_body).encode('utf-8')
                    request = urllib.request.Request(
                        f'{proxy.base_url}/responses?trace=1',
                        data=raw_body,
                        method='POST',
                        headers={
                            'Content-Type': 'application/json',
                            'Authorization': 'Bearer secret',
                            'x-codex-turn-metadata': json.dumps(metadata),
                        },
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        response_body = response.read()

                    self.assertEqual(response_body, b'data: one\n\ndata: two\n\n')
                    self.assertEqual(upstream.requests[0]['path'], '/v1/responses?trace=1')
                    self.assertEqual(upstream.requests[0]['body'], raw_body)
                    self.assertEqual(upstream.requests[0]['headers']['Authorization'], 'Bearer secret')

                    row = self._exchange(root / 'agentd.sqlite')
                    self.assertEqual(row['session_id'], 42)
                    self.assertEqual(row['codex_thread_id'], 'codex-thread')
                    self.assertEqual(row['codex_turn_id'], 'codex-turn')
                    self.assertEqual(row['model'], 'gpt-test')
                    self.assertEqual(row['stream'], 1)
                    self.assertEqual(row['status_code'], 200)
                    self.assertEqual(row['storage_state'], 'live')
                    self.assertRegex(row['period_key'], r'^\d{4}-W\d{2}$')
                    self.assertIsNone(row['archive_path'])
                    self.assertIsNone(row['request_body_decoded_path'])

                    request_capture = Path(row['request_capture_path'])
                    response_capture = Path(row['response_capture_path'])
                    self.assertEqual(request_capture, Path(row['request_body_raw_path']))
                    self.assertEqual(response_capture, Path(row['response_body_raw_path']))
                    self.assertTrue(request_capture.name.endswith('-request.http'))
                    self.assertTrue(response_capture.name.endswith('-response.http'))

                    request_bytes = request_capture.read_bytes()
                    self.assertTrue(request_bytes.startswith(b'POST /v1/responses?trace=1 HTTP/1.1\r\n'))
                    self.assertIn(b'Authorization: <redacted>\r\n', request_bytes)
                    self.assertNotIn(b'Bearer secret', request_bytes)
                    self.assertTrue(request_bytes.endswith(raw_body))

                    response_bytes = response_capture.read_bytes()
                    self.assertTrue(response_bytes.startswith(b'HTTP/1.1 200 OK\r\n'))
                    self.assertIn(b'Content-Type: text/event-stream\r\n', response_bytes)
                    self.assertTrue(response_bytes.endswith(response_body))
                finally:
                    proxy.stop()
        finally:
            upstream.stop()

    def test_codex_default_upstream_uses_chatgpt_account_header(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            proxy = CaptureProxy(
                capture_dir=root / 'captures',
                db_path=root / 'agentd.sqlite',
                upstream_mode='codex-default',
                context=CaptureContext(session_id=1),
            )
            self.assertEqual(proxy.upstream_for_headers({'ChatGPT-Account-ID': 'acct'}), CHATGPT_RESPONSES_URL)
            self.assertEqual(proxy.upstream_for_headers({'Authorization': 'Bearer sk-test'}), OPENAI_RESPONSES_URL)
            self.assertEqual(
                proxy.upstream_for_path({'ChatGPT-Account-ID': 'acct'}, '/v1/responses/compact'),
                f'{CHATGPT_RESPONSES_URL}/compact',
            )

    def test_proxy_passthrough_for_responses_subpaths(self) -> None:
        upstream = RecordingUpstream()
        upstream.start()
        try:
            with tempfile.TemporaryDirectory() as raw_dir:
                root = Path(raw_dir)
                proxy = CaptureProxy(
                    capture_dir=root / 'captures',
                    db_path=root / 'agentd.sqlite',
                    upstream_mode='codex-default',
                    upstream_url=f'{upstream.url}/v1/responses',
                    context=CaptureContext(session_id=42),
                )
                proxy.start()
                try:
                    raw_body = b'{"input":[]}'
                    request = urllib.request.Request(
                        f'{proxy.base_url}/responses/compact',
                        data=raw_body,
                        method='POST',
                        headers={'Content-Type': 'application/json'},
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        response.read()

                    self.assertEqual(upstream.requests[0]['path'], '/v1/responses/compact')
                    self.assertEqual(upstream.requests[0]['body'], raw_body)
                    with sqlite3.connect(root / 'agentd.sqlite') as conn:
                        count = conn.execute('select count(*) from model_http_exchanges').fetchone()[0]
                    self.assertEqual(count, 0)
                finally:
                    proxy.stop()
        finally:
            upstream.stop()

    def test_capture_provider_overrides_use_codex_cli_dotted_paths(self) -> None:
        overrides = capture_provider_overrides('http://127.0.0.1:1234/v1')

        self.assertIn('model_provider="agentd-capture"', overrides)
        self.assertIn('model_providers.agentd-capture.name="OpenAI"', overrides)
        self.assertIn('model_providers.agentd-capture.base_url="http://127.0.0.1:1234/v1"', overrides)

    def test_period_key_formats(self) -> None:
        moment = dt.datetime(2026, 5, 9, 12, 0, tzinfo=dt.timezone.utc)

        self.assertEqual(capture_period_key(moment, 'day'), '2026-05-09')
        self.assertEqual(capture_period_key(moment, 'week'), '2026-W19')
        self.assertEqual(capture_period_key(moment, 'month'), '2026-05')

    def test_archives_finished_period_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            proxy = CaptureProxy(
                capture_dir=root / 'captures',
                db_path=root / 'agentd.sqlite',
                upstream_mode='codex-default',
                context=CaptureContext(session_id=1),
                zstd_level=3,
            )
            period_dir = root / 'captures' / 'responses' / '2026-W18'
            period_dir.mkdir(parents=True)
            request_path = period_dir / 'abc-request.http'
            response_path = period_dir / 'abc-response.http'
            request_path.write_bytes(b'POST /v1/responses HTTP/1.1\r\n\r\n{}')
            response_path.write_bytes(b'HTTP/1.1 200 OK\r\n\r\ndata: done\n\n')
            proxy.store.insert_exchange(
                {
                    'id': 'abc',
                    'created_at': 1,
                    'completed_at': 2,
                    'session_id': 1,
                    'codex_thread_id': 'thread',
                    'codex_turn_id': 'turn',
                    'method': 'POST',
                    'request_path': '/v1/responses',
                    'upstream_url': 'https://example.test/v1/responses',
                    'provider_id': 'agentd-capture',
                    'model': 'gpt-test',
                    'stream': 1,
                    'status_code': 200,
                    'request_headers_path': str(request_path),
                    'request_body_raw_path': str(request_path),
                    'request_body_decoded_path': None,
                    'response_headers_path': str(response_path),
                    'response_body_raw_path': str(response_path),
                    'storage_state': 'live',
                    'period_key': '',
                    'request_capture_path': str(request_path),
                    'response_capture_path': str(response_path),
                    'archive_path': None,
                    'archive_member_request': None,
                    'archive_member_response': None,
                    'archive_format': 'tar.zst',
                    'error': None,
                }
            )

            proxy.archive_finished_periods(now=dt.datetime(2026, 5, 9, 12, 0, tzinfo=dt.timezone.utc))

            archive_path = root / 'captures' / 'responses' / '2026-W18.tar.zst'
            self.assertFalse(period_dir.exists())
            self.assertEqual(read_tar_zst_member_names(archive_path), {'abc-request.http', 'abc-response.http'})
            row = self._exchange(root / 'agentd.sqlite')
            self.assertEqual(row['storage_state'], 'archived')
            self.assertEqual(row['period_key'], '2026-W18')
            self.assertEqual(row['archive_path'], str(archive_path))
            self.assertEqual(row['archive_member_request'], 'abc-request.http')
            self.assertEqual(row['archive_member_response'], 'abc-response.http')
            self.assertEqual(row['archive_format'], 'tar.zst')

    def test_archive_skips_period_with_inprogress_marker(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            proxy = CaptureProxy(
                capture_dir=root / 'captures',
                db_path=root / 'agentd.sqlite',
                upstream_mode='codex-default',
                context=CaptureContext(session_id=1),
            )
            period_dir = root / 'captures' / 'responses' / '2026-W18'
            period_dir.mkdir(parents=True)
            (period_dir / 'abc.inprogress').write_text('active\n', encoding='utf-8')
            (period_dir / 'abc-request.http').write_bytes(b'request')

            proxy.archive_finished_periods(now=dt.datetime(2026, 5, 9, 12, 0, tzinfo=dt.timezone.utc))

            self.assertTrue(period_dir.exists())
            self.assertFalse((root / 'captures' / 'responses' / '2026-W18.tar.zst').exists())

    def _exchange(self, db_path: Path) -> sqlite3.Row:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute('select * from model_http_exchanges').fetchone()
            self.assertIsNotNone(row)
            return row
        finally:
            conn.close()


class RecordingUpstream:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        if self.server is None:
            raise RuntimeError('server is not started')
        host, port = self.server.server_address
        return f'http://{host}:{port}'

    def start(self) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_POST(self) -> None:
                length = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(length)
                owner.requests.append(
                    {
                        'path': self.path,
                        'headers': {str(key): str(value) for key, value in self.headers.items()},
                        'body': body,
                    }
                )
                response = b'data: one\n\ndata: two\n\n'
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('x-ratelimit-remaining-requests', '9')
                self.send_header('Content-Length', str(len(response)))
                self.end_headers()
                self.wfile.write(response)
                self.wfile.flush()

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.server is None:
            return
        self.server.shutdown()
        self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
