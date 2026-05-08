from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import closing, suppress
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .capture_proxy import (
    capture_period_key,
    decode_body,
    header_value,
    headers_to_dict,
    is_capture_period_key,
    normalize_archive_format,
    normalize_archive_period,
    normalize_zstd_level,
    parse_json,
    read_tar_zst_member_names,
    write_tar_zst_archive,
)

OTEL_HTTP_SIGNALS = {'logs', 'traces', 'metrics'}
OTEL_SIGNAL_BY_PATH = {
    '/v1/logs': 'logs',
    '/v1/traces': 'traces',
    '/v1/metrics': 'metrics',
}


@dataclass
class OtelCaptureContext:
    session_id: int
    codex_thread_id: str = ''
    codex_turn_id: str = ''
    model: str = ''


@dataclass(frozen=True)
class OtelCapturePaths:
    period_key: str
    period_dir: Path
    inprogress_path: Path
    capture_path: Path


class OtelCaptureStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init()

    def _init(self) -> None:
        with closing(self._connect()) as conn:
            ensure_otel_exports_schema(conn)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def insert_export(self, row: dict[str, Any]) -> None:
        with self._lock, closing(self._connect()) as conn:
            conn.execute(
                """
                insert into otel_exports(
                    id,
                    created_at,
                    completed_at,
                    session_id,
                    codex_thread_id,
                    codex_turn_id,
                    signal,
                    request_path,
                    content_type,
                    content_encoding,
                    protocol,
                    body_bytes,
                    record_count,
                    status_code,
                    capture_path,
                    decoded_body_path,
                    storage_state,
                    period_key,
                    archive_path,
                    archive_member_capture,
                    archive_member_decoded,
                    archive_format,
                    error
                )
                values(
                    :id,
                    :created_at,
                    :completed_at,
                    :session_id,
                    :codex_thread_id,
                    :codex_turn_id,
                    :signal,
                    :request_path,
                    :content_type,
                    :content_encoding,
                    :protocol,
                    :body_bytes,
                    :record_count,
                    :status_code,
                    :capture_path,
                    :decoded_body_path,
                    :storage_state,
                    :period_key,
                    :archive_path,
                    :archive_member_capture,
                    :archive_member_decoded,
                    :archive_format,
                    :error
                )
                """,
                row,
            )
            conn.commit()

    def update_export(self, export_id: str, **updates: Any) -> None:
        if not updates:
            return
        assignments = ', '.join(f'{key} = ?' for key in updates)
        values = [*updates.values(), export_id]
        with self._lock, closing(self._connect()) as conn:
            conn.execute(f'update otel_exports set {assignments} where id = ?', values)
            conn.commit()

    def mark_period_archived(
        self,
        period_key: str,
        archive_path: Path,
        archive_format: str,
        period_dir: Path,
    ) -> None:
        with self._lock, closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            legacy_path_prefix = f'{period_dir}{os.sep}%'
            rows = conn.execute(
                """
                select id, capture_path, decoded_body_path
                from otel_exports
                where storage_state = 'live'
                  and (
                    period_key = ?
                    or capture_path like ?
                    or decoded_body_path like ?
                  )
                """,
                (period_key, legacy_path_prefix, legacy_path_prefix),
            ).fetchall()
            for row in rows:
                capture_path = str(row['capture_path'] or f'{row["id"]}.http')
                decoded_body_path = str(row['decoded_body_path'] or '')
                conn.execute(
                    """
                    update otel_exports
                    set storage_state = 'archived',
                        period_key = ?,
                        archive_path = ?,
                        archive_member_capture = ?,
                        archive_member_decoded = ?,
                        archive_format = ?
                    where id = ?
                    """,
                    (
                        period_key,
                        str(archive_path),
                        Path(capture_path).name,
                        Path(decoded_body_path).name if decoded_body_path else None,
                        archive_format,
                        row['id'],
                    ),
                )
            conn.commit()


class OtelCaptureServer:
    def __init__(
        self,
        *,
        capture_dir: Path,
        db_path: Path,
        context: OtelCaptureContext,
        signals: set[str] | None = None,
        archive_period: str = 'week',
        archive_format: str = 'tar.zst',
        zstd_level: int = 10,
    ) -> None:
        self.capture_dir = capture_dir
        self.db_path = db_path
        self.context = context
        self.signals = {'logs', 'traces'} if signals is None else set(signals)
        self.archive_period = normalize_archive_period(archive_period)
        self.archive_format = normalize_archive_format(archive_format)
        self.zstd_level = normalize_zstd_level(zstd_level)
        self.store = OtelCaptureStore(db_path)
        self._context_lock = threading.Lock()
        self._archive_lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError('OTel capture server is not started')
        host, port = self._server.server_address
        return f'http://{host}:{port}'

    def endpoint_url(self, signal: str) -> str:
        if signal not in OTEL_HTTP_SIGNALS:
            raise ValueError(f'unsupported OTel signal: {signal}')
        return f'{self.base_url}/v1/{signal}'

    def start(self) -> None:
        self.capture_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with suppress(OSError):
            os.chmod(self.capture_dir, 0o700)
        self.archive_finished_periods()

        class Handler(OtelCaptureHandler):
            proxy = self

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name='agentd-otel-capture', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None

    def update_context(self, *, codex_thread_id: str = '', codex_turn_id: str = '') -> None:
        with self._context_lock:
            if codex_thread_id:
                self.context.codex_thread_id = codex_thread_id
            if codex_turn_id:
                self.context.codex_turn_id = codex_turn_id

    def snapshot_context(self) -> OtelCaptureContext:
        with self._context_lock:
            return OtelCaptureContext(
                session_id=self.context.session_id,
                codex_thread_id=self.context.codex_thread_id,
                codex_turn_id=self.context.codex_turn_id,
                model=self.context.model,
            )

    def export_dir(self) -> tuple[str, Path]:
        self.archive_finished_periods()
        period_key = capture_period_key(dt.datetime.now(dt.timezone.utc), self.archive_period)
        path = self.capture_dir / 'otel' / period_key
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        with suppress(OSError):
            os.chmod(path, 0o700)
        return period_key, path

    def archive_finished_periods(self, now: dt.datetime | None = None) -> None:
        with self._archive_lock:
            otel_root = self.capture_dir / 'otel'
            otel_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            with suppress(OSError):
                os.chmod(otel_root, 0o700)

            current_period = capture_period_key(now or dt.datetime.now(dt.timezone.utc), self.archive_period)
            for period_dir in sorted(path for path in otel_root.iterdir() if path.is_dir()):
                if period_dir.name == current_period or not is_capture_period_key(period_dir.name):
                    continue
                self._archive_period_dir(period_dir)

    def _archive_period_dir(self, period_dir: Path) -> None:
        if any(period_dir.glob('*.inprogress')):
            return
        files = [path for path in sorted(period_dir.iterdir()) if path.is_file() and not path.name.startswith('.')]
        if not files:
            shutil.rmtree(period_dir)
            return

        archive_path = period_dir.parent / f'{period_dir.name}.{self.archive_format}'
        if not archive_path.exists():
            tmp_path = period_dir.parent / f'.{archive_path.name}.{uuid.uuid4().hex}.tmp'
            with suppress(FileNotFoundError):
                tmp_path.unlink()
            write_tar_zst_archive(period_dir, tmp_path, level=self.zstd_level)
            os.replace(tmp_path, archive_path)
            with suppress(OSError):
                os.chmod(archive_path, 0o600)

        archived_members = read_tar_zst_member_names(archive_path)
        live_file_names = {path.name for path in files}
        if not live_file_names.issubset(archived_members):
            return

        self.store.mark_period_archived(period_dir.name, archive_path, self.archive_format, period_dir)
        shutil.rmtree(period_dir)


class OtelCaptureHandler(BaseHTTPRequestHandler):
    proxy: OtelCaptureServer
    protocol_version = 'HTTP/1.1'

    def do_POST(self) -> None:
        request_path = self.path
        signal = OTEL_SIGNAL_BY_PATH.get(urlparse(request_path).path)
        if signal is None or signal not in self.proxy.signals:
            self.send_error(404, 'agentd OTel capture only handles enabled OTLP HTTP signal endpoints')
            return

        export_id = uuid.uuid4().hex
        context = self.proxy.snapshot_context()
        headers = headers_to_dict(self.headers)
        raw_body = self._read_request_body()
        paths: OtelCapturePaths | None = None

        try:
            decoded_body = decode_body(raw_body, header_value(headers, 'content-encoding'))
            payload = decoded_body if decoded_body is not None else raw_body
            protocol = otel_protocol_from_headers(headers)
            body_json = parse_json(payload)
            record_count: int | None = None
            if body_json is not None:
                record_count = count_otel_records(signal, body_json)
            paths = self._capture_paths(export_id, signal, protocol)
            write_otel_payload(paths.capture_path, protocol, payload, body_json)

            self.proxy.store.insert_export(
                {
                    'id': export_id,
                    'created_at': int(time.time()),
                    'completed_at': int(time.time()),
                    'session_id': context.session_id,
                    'codex_thread_id': context.codex_thread_id,
                    'codex_turn_id': context.codex_turn_id,
                    'signal': signal,
                    'request_path': request_path,
                    'content_type': header_value(headers, 'content-type'),
                    'content_encoding': header_value(headers, 'content-encoding'),
                    'protocol': protocol,
                    'body_bytes': len(payload),
                    'record_count': record_count,
                    'status_code': 200,
                    'capture_path': str(paths.capture_path),
                    'decoded_body_path': None,
                    'storage_state': 'live',
                    'period_key': paths.period_key,
                    'archive_path': None,
                    'archive_member_capture': None,
                    'archive_member_decoded': None,
                    'archive_format': self.proxy.archive_format,
                    'error': None,
                }
            )
            self._send_success(protocol)
        except Exception as exc:  # pragma: no cover - exercised by filesystem failures
            self.proxy.store.update_export(export_id, completed_at=int(time.time()), error=str(exc), status_code=500)
            self.send_error(500, f'agentd OTel capture error: {exc}')
        finally:
            if paths is not None:
                with suppress(FileNotFoundError):
                    paths.inprogress_path.unlink()

    def _send_success(self, protocol: str) -> None:
        body = b'' if protocol == 'binary' else b'{}'
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-protobuf' if protocol == 'binary' else 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)
        self.wfile.flush()

    def _read_request_body(self) -> bytes:
        if self.headers.get('Transfer-Encoding', '').lower() == 'chunked':
            return self._read_chunked_body()
        length = int(self.headers.get('Content-Length') or 0)
        return self.rfile.read(length) if length else b''

    def _read_chunked_body(self) -> bytes:
        chunks: list[bytes] = []
        while True:
            line = self.rfile.readline()
            size_raw = line.split(b';', 1)[0].strip()
            size = int(size_raw, 16)
            if size == 0:
                while True:
                    trailer = self.rfile.readline()
                    if trailer in (b'\r\n', b'\n', b''):
                        break
                break
            chunks.append(self.rfile.read(size))
            self.rfile.read(2)
        return b''.join(chunks)

    def _capture_paths(self, export_id: str, signal: str, protocol: str = '') -> OtelCapturePaths:
        period_key, root = self.proxy.export_dir()
        inprogress_path = root / f'{export_id}.inprogress'
        inprogress_path.write_text(str(time.time()) + '\n', encoding='utf-8')
        suffix = '.otlp.jsonl' if protocol == 'json' else '.otlp.pb' if protocol == 'binary' else '.otlp'
        return OtelCapturePaths(
            period_key=period_key,
            period_dir=root,
            inprogress_path=inprogress_path,
            capture_path=root / f'{export_id}-{signal}{suffix}',
        )

    def log_message(self, format: str, *args: Any) -> None:
        return


def otel_config_overrides(
    server: OtelCaptureServer,
    *,
    environment: str,
    protocol: str,
    log_user_prompt: bool,
    logs: bool,
    traces: bool,
    metrics: bool,
) -> list[str]:
    overrides = [
        f'otel.environment={json.dumps(environment)}',
        f'otel.log_user_prompt={toml_bool(log_user_prompt)}',
    ]
    if logs:
        overrides.extend(
            [
                'otel.exporter="otlp-http"',
                f'otel.exporter.otlp-http.endpoint={json.dumps(server.endpoint_url("logs"))}',
                f'otel.exporter.otlp-http.protocol={json.dumps(protocol)}',
            ]
        )
    else:
        overrides.append('otel.exporter="none"')

    if traces:
        overrides.extend(
            [
                'otel.trace_exporter="otlp-http"',
                f'otel.trace_exporter.otlp-http.endpoint={json.dumps(server.endpoint_url("traces"))}',
                f'otel.trace_exporter.otlp-http.protocol={json.dumps(protocol)}',
            ]
        )
    else:
        overrides.append('otel.trace_exporter="none"')

    if metrics:
        overrides.extend(
            [
                'otel.metrics_exporter="otlp-http"',
                f'otel.metrics_exporter.otlp-http.endpoint={json.dumps(server.endpoint_url("metrics"))}',
                f'otel.metrics_exporter.otlp-http.protocol={json.dumps(protocol)}',
            ]
        )
    else:
        overrides.append('otel.metrics_exporter="none"')

    return overrides


def toml_bool(value: bool) -> str:
    return 'true' if value else 'false'


def write_otel_payload(path: Path, protocol: str, payload: bytes, body_json: dict[str, Any] | None) -> None:
    if protocol == 'json' and body_json is not None:
        line = json.dumps(body_json, ensure_ascii=False, separators=(',', ':'))
        path.write_text(f'{line}\n', encoding='utf-8')
        return
    path.write_bytes(payload)


def otel_protocol_from_headers(headers: dict[str, str]) -> str:
    content_type = header_value(headers, 'content-type').lower()
    if 'json' in content_type:
        return 'json'
    if 'protobuf' in content_type or 'proto' in content_type:
        return 'binary'
    return ''


def count_otel_records(signal: str, body: dict[str, Any]) -> int | None:
    if signal == 'logs':
        return sum_nested_records(body, 'resourceLogs', 'scopeLogs', 'logRecords')
    if signal == 'traces':
        return sum_nested_records(body, 'resourceSpans', 'scopeSpans', 'spans')
    if signal == 'metrics':
        return sum_nested_records(body, 'resourceMetrics', 'scopeMetrics', 'metrics')
    return None


def sum_nested_records(body: dict[str, Any], resource_key: str, scope_key: str, records_key: str) -> int:
    count = 0
    resources = body.get(resource_key)
    if not isinstance(resources, list):
        return 0
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        scopes = resource.get(scope_key)
        if not isinstance(scopes, list):
            continue
        for scope in scopes:
            if not isinstance(scope, dict):
                continue
            records = scope.get(records_key)
            if isinstance(records, list):
                count += len(records)
    return count


def ensure_otel_exports_schema(conn: sqlite3.Connection) -> None:
    conn.execute(OTEL_EXPORTS_SCHEMA)
    columns = {str(row[1]) for row in conn.execute('pragma table_info(otel_exports)')}
    for column, definition in OTEL_EXPORTS_ADDED_COLUMNS.items():
        if column not in columns:
            conn.execute(f'alter table otel_exports add column {column} {definition}')


OTEL_EXPORTS_SCHEMA = """
create table if not exists otel_exports (
    id text primary key,
    created_at integer not null,
    completed_at integer,
    session_id integer,
    codex_thread_id text,
    codex_turn_id text,
    signal text not null,
    request_path text not null,
    content_type text,
    content_encoding text,
    protocol text,
    body_bytes integer,
    record_count integer,
    status_code integer,
    capture_path text not null,
    decoded_body_path text,
    storage_state text not null default 'live',
    period_key text not null default '',
    archive_path text,
    archive_member_capture text,
    archive_member_decoded text,
    archive_format text,
    error text
);
"""

OTEL_EXPORTS_ADDED_COLUMNS = {
    'storage_state': "text not null default 'live'",
    'period_key': "text not null default ''",
    'archive_path': 'text',
    'archive_member_capture': 'text',
    'archive_member_decoded': 'text',
    'archive_format': 'text',
}
