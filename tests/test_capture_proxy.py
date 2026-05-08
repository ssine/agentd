from __future__ import annotations

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
                    self.assertEqual(Path(row['request_body_raw_path']).read_bytes(), raw_body)
                    self.assertEqual(
                        json.loads(Path(row['request_body_decoded_path']).read_text(encoding='utf-8')),
                        request_body,
                    )
                    self.assertEqual(Path(row['response_body_raw_path']).read_bytes(), response_body)
                    headers = json.loads(Path(row['request_headers_path']).read_text(encoding='utf-8'))
                    self.assertEqual(headers['Authorization'], '<redacted>')
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
