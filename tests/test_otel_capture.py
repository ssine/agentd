from __future__ import annotations

import datetime as dt
import json
import sqlite3
import tempfile
import unittest
import urllib.request
from pathlib import Path

from agentd.capture_proxy import read_tar_zst_member_names
from agentd.otel_capture import (
    OtelCaptureContext,
    OtelCaptureServer,
    otel_config_overrides,
)


class OtelCaptureTest(unittest.TestCase):
    def test_server_captures_json_logs_export(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            server = OtelCaptureServer(
                capture_dir=root / 'captures',
                db_path=root / 'agentd.sqlite',
                context=OtelCaptureContext(session_id=42, codex_thread_id='thread-1', codex_turn_id='turn-1'),
            )
            server.start()
            try:
                body = {
                    'resourceLogs': [
                        {
                            'scopeLogs': [
                                {
                                    'logRecords': [
                                        {'body': {'stringValue': 'one'}},
                                        {'body': {'stringValue': 'two'}},
                                    ]
                                }
                            ]
                        }
                    ]
                }
                raw_body = json.dumps(body).encode('utf-8')
                request = urllib.request.Request(
                    f'{server.endpoint_url("logs")}?batch=1',
                    data=raw_body,
                    method='POST',
                    headers={
                        'Content-Type': 'application/json',
                        'Authorization': 'Bearer secret',
                    },
                )

                with urllib.request.urlopen(request, timeout=5) as response:
                    response_body = response.read()

                self.assertEqual(response.status, 200)
                self.assertEqual(response_body, b'{}')
                row = self._export(root / 'agentd.sqlite')
                self.assertEqual(row['session_id'], 42)
                self.assertEqual(row['codex_thread_id'], 'thread-1')
                self.assertEqual(row['codex_turn_id'], 'turn-1')
                self.assertEqual(row['signal'], 'logs')
                self.assertEqual(row['request_path'], '/v1/logs?batch=1')
                self.assertEqual(row['protocol'], 'json')
                self.assertEqual(row['record_count'], 2)
                self.assertEqual(row['storage_state'], 'live')
                self.assertIsNone(row['decoded_body_path'])

                capture_path = Path(row['capture_path'])
                self.assertTrue(capture_path.name.endswith('-logs.otlp.jsonl'))
                capture_text = capture_path.read_text(encoding='utf-8')
                self.assertTrue(capture_text.endswith('\n'))
                self.assertEqual(json.loads(capture_text), body)
                self.assertNotIn('Bearer secret', capture_text)
            finally:
                server.stop()

    def test_server_captures_binary_payload(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            server = OtelCaptureServer(
                capture_dir=root / 'captures',
                db_path=root / 'agentd.sqlite',
                context=OtelCaptureContext(session_id=42),
            )
            server.start()
            try:
                raw_body = b'\x08\x01\x12\x02ok'
                request = urllib.request.Request(
                    server.endpoint_url('traces'),
                    data=raw_body,
                    method='POST',
                    headers={'Content-Type': 'application/x-protobuf'},
                )

                with urllib.request.urlopen(request, timeout=5) as response:
                    response_body = response.read()

                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers['Content-Type'], 'application/x-protobuf')
                self.assertEqual(response_body, b'')
                row = self._export(root / 'agentd.sqlite')
                self.assertEqual(row['signal'], 'traces')
                self.assertEqual(row['protocol'], 'binary')
                self.assertIsNone(row['record_count'])
                capture_path = Path(row['capture_path'])
                self.assertTrue(capture_path.name.endswith('-traces.otlp.pb'))
                self.assertEqual(capture_path.read_bytes(), raw_body)
            finally:
                server.stop()

    def test_otel_config_overrides_use_codex_cli_dotted_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            server = OtelCaptureServer(
                capture_dir=root / 'captures',
                db_path=root / 'agentd.sqlite',
                context=OtelCaptureContext(session_id=1),
            )
            server.start()
            try:
                overrides = otel_config_overrides(
                    server,
                    environment='agentd-test',
                    protocol='json',
                    log_user_prompt=False,
                    logs=True,
                    traces=True,
                    metrics=False,
                )

                self.assertIn('otel.environment="agentd-test"', overrides)
                self.assertIn('otel.log_user_prompt=false', overrides)
                self.assertIn('otel.exporter="otlp-http"', overrides)
                self.assertIn(f'otel.exporter.otlp-http.endpoint="{server.endpoint_url("logs")}"', overrides)
                self.assertIn('otel.exporter.otlp-http.protocol="json"', overrides)
                self.assertIn('otel.trace_exporter="otlp-http"', overrides)
                self.assertIn(f'otel.trace_exporter.otlp-http.endpoint="{server.endpoint_url("traces")}"', overrides)
                self.assertIn('otel.metrics_exporter="none"', overrides)
            finally:
                server.stop()

    def test_archives_finished_otel_period_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            server = OtelCaptureServer(
                capture_dir=root / 'captures',
                db_path=root / 'agentd.sqlite',
                context=OtelCaptureContext(session_id=1),
                zstd_level=3,
            )
            period_dir = root / 'captures' / 'otel' / '2026-W18'
            period_dir.mkdir(parents=True)
            capture_path = period_dir / 'abc-logs.otlp.jsonl'
            capture_path.write_text('{}\n', encoding='utf-8')
            server.store.insert_export(
                {
                    'id': 'abc',
                    'created_at': 1,
                    'completed_at': 2,
                    'session_id': 1,
                    'codex_thread_id': 'thread',
                    'codex_turn_id': 'turn',
                    'signal': 'logs',
                    'request_path': '/v1/logs',
                    'content_type': 'application/json',
                    'content_encoding': '',
                    'protocol': 'json',
                    'body_bytes': 2,
                    'record_count': 0,
                    'status_code': 200,
                    'capture_path': str(capture_path),
                    'decoded_body_path': None,
                    'storage_state': 'live',
                    'period_key': '',
                    'archive_path': None,
                    'archive_member_capture': None,
                    'archive_member_decoded': None,
                    'archive_format': 'tar.zst',
                    'error': None,
                }
            )

            server.archive_finished_periods(now=dt.datetime(2026, 5, 9, 12, 0, tzinfo=dt.timezone.utc))

            archive_path = root / 'captures' / 'otel' / '2026-W18.tar.zst'
            self.assertFalse(period_dir.exists())
            self.assertEqual(read_tar_zst_member_names(archive_path), {'abc-logs.otlp.jsonl'})
            row = self._export(root / 'agentd.sqlite')
            self.assertEqual(row['storage_state'], 'archived')
            self.assertEqual(row['period_key'], '2026-W18')
            self.assertEqual(row['archive_path'], str(archive_path))
            self.assertEqual(row['archive_member_capture'], 'abc-logs.otlp.jsonl')
            self.assertIsNone(row['archive_member_decoded'])

    def _export(self, db_path: Path) -> sqlite3.Row:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute('select * from otel_exports').fetchone()
            self.assertIsNotNone(row)
            return row
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
