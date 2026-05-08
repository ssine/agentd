from __future__ import annotations

import datetime as dt
import gzip
import http.client
import json
import os
import sqlite3
import threading
import time
import uuid
import zlib
from contextlib import closing, suppress
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import ParseResult, urlparse

CAPTURE_PROVIDER_ID = 'agentd-capture'
CHATGPT_RESPONSES_URL = 'https://chatgpt.com/backend-api/codex/responses'
OPENAI_RESPONSES_URL = 'https://api.openai.com/v1/responses'
SENSITIVE_HEADERS = {
    'authorization',
    'cookie',
    'set-cookie',
    'chatgpt-account-id',
    'x-openai-api-key',
    'x-stainless-api-key',
}
HOP_BY_HOP_HEADERS = {
    'connection',
    'keep-alive',
    'proxy-authenticate',
    'proxy-authorization',
    'te',
    'trailer',
    'trailers',
    'transfer-encoding',
    'upgrade',
}


@dataclass
class CaptureContext:
    session_id: int
    codex_thread_id: str = ''
    codex_turn_id: str = ''
    provider_id: str = CAPTURE_PROVIDER_ID
    model: str = ''


@dataclass(frozen=True)
class CapturePaths:
    request_headers_path: Path
    request_body_raw_path: Path
    request_body_decoded_path: Path | None
    response_headers_path: Path
    response_body_raw_path: Path


class CaptureStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init()

    def _init(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(MODEL_HTTP_EXCHANGES_SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def insert_exchange(self, row: dict[str, Any]) -> None:
        with self._lock, closing(self._connect()) as conn:
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
                    error
                )
                values(
                    :id,
                    :created_at,
                    :completed_at,
                    :session_id,
                    :codex_thread_id,
                    :codex_turn_id,
                    :method,
                    :request_path,
                    :upstream_url,
                    :provider_id,
                    :model,
                    :stream,
                    :status_code,
                    :request_headers_path,
                    :request_body_raw_path,
                    :request_body_decoded_path,
                    :response_headers_path,
                    :response_body_raw_path,
                    :error
                )
                """,
                row,
            )
            conn.commit()

    def update_exchange(self, exchange_id: str, **updates: Any) -> None:
        if not updates:
            return
        assignments = ', '.join(f'{key} = ?' for key in updates)
        values = [*updates.values(), exchange_id]
        with self._lock, closing(self._connect()) as conn:
            conn.execute(f'update model_http_exchanges set {assignments} where id = ?', values)
            conn.commit()


class CaptureProxy:
    def __init__(
        self,
        *,
        capture_dir: Path,
        db_path: Path,
        upstream_mode: str,
        upstream_url: str = '',
        context: CaptureContext,
        save_sensitive_headers: bool = False,
    ) -> None:
        self.capture_dir = capture_dir
        self.db_path = db_path
        self.upstream_mode = upstream_mode or 'codex-default'
        self.upstream_url = upstream_url
        self.context = context
        self.save_sensitive_headers = save_sensitive_headers
        self.store = CaptureStore(db_path)
        self._context_lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError('capture proxy is not started')
        host, port = self._server.server_address
        return f'http://{host}:{port}/v1'

    def start(self) -> None:
        self.capture_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with suppress(OSError):
            os.chmod(self.capture_dir, 0o700)

        class Handler(CaptureProxyHandler):
            proxy = self

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name='agentd-capture-proxy', daemon=True)
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

    def snapshot_context(self) -> CaptureContext:
        with self._context_lock:
            return CaptureContext(
                session_id=self.context.session_id,
                codex_thread_id=self.context.codex_thread_id,
                codex_turn_id=self.context.codex_turn_id,
                provider_id=self.context.provider_id,
                model=self.context.model,
            )

    def upstream_for_headers(self, headers: dict[str, str]) -> str:
        return self.upstream_for_path(headers, '/v1/responses')

    def upstream_for_path(self, headers: dict[str, str], request_path: str) -> str:
        suffix = ''
        path = urlparse(request_path).path
        if path.startswith('/v1/responses/'):
            suffix = path.removeprefix('/v1/responses')
        if self.upstream_url:
            return f'{self.upstream_url.rstrip("/")}{suffix}'
        mode = self.upstream_mode
        if mode == 'chatgpt':
            return f'{CHATGPT_RESPONSES_URL}{suffix}'
        if mode == 'api':
            return f'{OPENAI_RESPONSES_URL}{suffix}'
        if any(key.lower() == 'chatgpt-account-id' for key in headers):
            return f'{CHATGPT_RESPONSES_URL}{suffix}'
        return f'{OPENAI_RESPONSES_URL}{suffix}'

    def exchange_dir(self) -> Path:
        today = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')
        path = self.capture_dir / 'responses' / today
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        with suppress(OSError):
            os.chmod(path, 0o700)
        return path


class CaptureProxyHandler(BaseHTTPRequestHandler):
    proxy: CaptureProxy
    protocol_version = 'HTTP/1.1'

    def do_POST(self) -> None:
        request_path = self.path
        parsed_path = urlparse(request_path).path
        if parsed_path != '/v1/responses':
            if parsed_path.startswith('/v1/responses/'):
                request_headers = headers_to_dict(self.headers)
                raw_body = self._read_request_body()
                upstream_url = self.proxy.upstream_for_path(request_headers, request_path)
                self._passthrough(upstream_url, request_headers, raw_body)
                return
            self.send_error(404, 'agentd capture proxy only handles POST /v1/responses')
            return

        exchange_id = uuid.uuid4().hex
        context = self.proxy.snapshot_context()
        request_headers = headers_to_dict(self.headers)
        raw_body = self._read_request_body()
        upstream_url = self.proxy.upstream_for_path(request_headers, request_path)
        paths = self._capture_paths(exchange_id)

        decoded_body = decode_body(raw_body, header_value(request_headers, 'content-encoding'))
        request_json = parse_json(decoded_body)
        request_decoded_path = write_decoded_request(paths.request_body_decoded_path, request_json)
        metadata = parse_metadata_header(header_value(request_headers, 'x-codex-turn-metadata'))
        correlated = correlate(context, metadata)
        model = model_from_request(request_json) or context.model
        stream = stream_from_request(request_json)

        write_json(paths.request_headers_path, redact_headers(request_headers, self.proxy.save_sensitive_headers))
        paths.request_body_raw_path.write_bytes(raw_body)
        self.proxy.store.insert_exchange(
            {
                'id': exchange_id,
                'created_at': int(time.time()),
                'completed_at': None,
                'session_id': correlated.session_id,
                'codex_thread_id': correlated.codex_thread_id,
                'codex_turn_id': correlated.codex_turn_id,
                'method': 'POST',
                'request_path': request_path,
                'upstream_url': upstream_url,
                'provider_id': correlated.provider_id,
                'model': model,
                'stream': int(stream) if stream is not None else None,
                'status_code': None,
                'request_headers_path': str(paths.request_headers_path),
                'request_body_raw_path': str(paths.request_body_raw_path),
                'request_body_decoded_path': str(request_decoded_path) if request_decoded_path else None,
                'response_headers_path': None,
                'response_body_raw_path': str(paths.response_body_raw_path),
                'error': None,
            }
        )

        try:
            self._forward(exchange_id, upstream_url, request_headers, raw_body, paths)
        except Exception as exc:  # pragma: no cover - exercised by integration failures
            self.proxy.store.update_exchange(
                exchange_id,
                completed_at=int(time.time()),
                error=str(exc),
                response_body_raw_path=str(paths.response_body_raw_path),
            )
            if not getattr(self, '_agentd_response_started', False):
                self.send_error(502, f'agentd capture proxy upstream error: {exc}')
            else:
                self.close_connection = True

    def _forward(
        self,
        exchange_id: str,
        upstream_url: str,
        request_headers: dict[str, str],
        raw_body: bytes,
        paths: CapturePaths,
    ) -> None:
        parsed = urlparse(upstream_url)
        conn = connection_for_url(parsed)
        try:
            target = upstream_target(parsed, self.path)
            headers = forward_request_headers(request_headers, parsed, len(raw_body))
            conn.request('POST', target, body=raw_body, headers=headers)
            response = conn.getresponse()
            response_headers = response_headers_to_dict(response.getheaders())
            write_json(paths.response_headers_path, response_headers)
            self.proxy.store.update_exchange(
                exchange_id,
                status_code=response.status,
                response_headers_path=str(paths.response_headers_path),
                response_body_raw_path=str(paths.response_body_raw_path),
            )
            self._agentd_response_started = True
            self.send_response(response.status, response.reason)
            sent_content_length = False
            for key, value in response.getheaders():
                if key.lower() in HOP_BY_HOP_HEADERS:
                    continue
                if key.lower() == 'content-length':
                    sent_content_length = True
                self.send_header(key, value)
            if not sent_content_length:
                self.send_header('Connection', 'close')
                self.close_connection = True
            self.end_headers()

            with paths.response_body_raw_path.open('wb') as body_file:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    body_file.write(chunk)
                    body_file.flush()
                    self.wfile.write(chunk)
                    self.wfile.flush()

            self.proxy.store.update_exchange(
                exchange_id,
                completed_at=int(time.time()),
                response_headers_path=str(paths.response_headers_path),
                response_body_raw_path=str(paths.response_body_raw_path),
            )
        finally:
            conn.close()

    def _passthrough(self, upstream_url: str, request_headers: dict[str, str], raw_body: bytes) -> None:
        parsed = urlparse(upstream_url)
        conn = connection_for_url(parsed)
        try:
            target = upstream_target(parsed, self.path)
            headers = forward_request_headers(request_headers, parsed, len(raw_body))
            conn.request('POST', target, body=raw_body, headers=headers)
            response = conn.getresponse()
            self._agentd_response_started = True
            self.send_response(response.status, response.reason)
            sent_content_length = False
            for key, value in response.getheaders():
                if key.lower() in HOP_BY_HOP_HEADERS:
                    continue
                if key.lower() == 'content-length':
                    sent_content_length = True
                self.send_header(key, value)
            if not sent_content_length:
                self.send_header('Connection', 'close')
                self.close_connection = True
            self.end_headers()
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception as exc:
            if not getattr(self, '_agentd_response_started', False):
                self.send_error(502, f'agentd capture proxy upstream error: {exc}')
            else:
                self.close_connection = True
        finally:
            conn.close()

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

    def _capture_paths(self, exchange_id: str) -> CapturePaths:
        root = self.proxy.exchange_dir()
        return CapturePaths(
            request_headers_path=root / f'{exchange_id}-request.headers.json',
            request_body_raw_path=root / f'{exchange_id}-request.raw',
            request_body_decoded_path=root / f'{exchange_id}-request.json',
            response_headers_path=root / f'{exchange_id}-response.headers.json',
            response_body_raw_path=root / f'{exchange_id}-response.sse',
        )

    def log_message(self, format: str, *args: Any) -> None:
        return


def capture_provider_overrides(base_url: str, provider_id: str = CAPTURE_PROVIDER_ID) -> list[str]:
    return [
        f'model_provider={json.dumps(provider_id)}',
        f'model_providers.{provider_id}.name="OpenAI"',
        f'model_providers.{provider_id}.base_url={json.dumps(base_url)}',
        f'model_providers.{provider_id}.wire_api="responses"',
        f'model_providers.{provider_id}.requires_openai_auth=true',
        f'model_providers.{provider_id}.supports_websockets=false',
    ]


def turn_client_metadata(*, session_id: int, request_id: str, codex_thread_id: str) -> dict[str, str]:
    return {
        'agentd_session_id': str(session_id),
        'agentd_request_id': request_id,
        'agentd_codex_thread_id': codex_thread_id,
    }


def headers_to_dict(headers: Any) -> dict[str, str]:
    return {str(key): str(value) for key, value in headers.items()}


def header_value(headers: dict[str, str], name: str) -> str:
    lower_name = name.lower()
    for key, value in headers.items():
        if key.lower() == lower_name:
            return value
    return ''


def response_headers_to_dict(headers: list[tuple[str, str]]) -> dict[str, list[str] | str]:
    result: dict[str, list[str] | str] = {}
    for key, value in headers:
        if key in result:
            existing = result[key]
            if isinstance(existing, list):
                existing.append(value)
            else:
                result[key] = [existing, value]
        else:
            result[key] = value
    return result


def redact_headers(headers: dict[str, str], save_sensitive_headers: bool) -> dict[str, str]:
    if save_sensitive_headers:
        return dict(headers)
    return {key: ('<redacted>' if key.lower() in SENSITIVE_HEADERS else value) for key, value in headers.items()}


def decode_body(raw_body: bytes, content_encoding: str) -> bytes | None:
    encoding = content_encoding.lower().strip()
    if not encoding or encoding == 'identity':
        return raw_body
    if encoding == 'gzip':
        return gzip.decompress(raw_body)
    if encoding == 'deflate':
        return zlib.decompress(raw_body)
    if encoding == 'zstd':
        try:
            import zstandard  # type: ignore[import-not-found]
        except ModuleNotFoundError:
            return None
        return zstandard.ZstdDecompressor().decompress(raw_body)
    return None


def parse_json(raw_body: bytes | None) -> dict[str, Any] | None:
    if raw_body is None:
        return None
    try:
        value = json.loads(raw_body.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def write_decoded_request(path: Path | None, body: dict[str, Any] | None) -> Path | None:
    if path is None or body is None:
        return None
    write_json(path, body)
    return path


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def parse_metadata_header(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def correlate(context: CaptureContext, metadata: dict[str, Any]) -> CaptureContext:
    session_id = context.session_id
    metadata_session_id = metadata.get('agentd_session_id')
    if metadata_session_id is not None:
        with suppress(ValueError):
            session_id = int(str(metadata_session_id))
    return CaptureContext(
        session_id=session_id,
        codex_thread_id=str(
            metadata.get('thread_id')
            or metadata.get('agentd_codex_thread_id')
            or metadata.get('codex_thread_id')
            or context.codex_thread_id
        ),
        codex_turn_id=str(
            metadata.get('turn_id')
            or metadata.get('agentd_codex_turn_id')
            or metadata.get('codex_turn_id')
            or context.codex_turn_id
        ),
        provider_id=context.provider_id,
        model=context.model,
    )


def model_from_request(body: dict[str, Any] | None) -> str:
    if not body:
        return ''
    model = body.get('model')
    return model if isinstance(model, str) else ''


def stream_from_request(body: dict[str, Any] | None) -> bool | None:
    if not body or 'stream' not in body:
        return None
    return bool(body.get('stream'))


def connection_for_url(parsed: ParseResult) -> http.client.HTTPConnection:
    port = parsed.port
    if parsed.scheme == 'https':
        return http.client.HTTPSConnection(parsed.hostname or '', port=port, timeout=60)
    if parsed.scheme == 'http':
        return http.client.HTTPConnection(parsed.hostname or '', port=port, timeout=60)
    raise ValueError(f'unsupported upstream URL scheme: {parsed.scheme}')


def upstream_target(upstream: ParseResult, original_path: str) -> str:
    target = upstream.path or '/'
    query = upstream.query
    original_query = urlparse(original_path).query
    if original_query:
        query = f'{query}&{original_query}' if query else original_query
    if query:
        target = f'{target}?{query}'
    return target


def forward_request_headers(headers: dict[str, str], upstream: ParseResult, content_length: int) -> dict[str, str]:
    result = {
        key: value
        for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() not in {'host', 'content-length'}
    }
    host = upstream.hostname or ''
    if upstream.port is not None:
        host = f'{host}:{upstream.port}'
    result['Host'] = host
    result['Content-Length'] = str(content_length)
    return result


MODEL_HTTP_EXCHANGES_SCHEMA = """
create table if not exists model_http_exchanges (
    id text primary key,
    created_at integer not null,
    completed_at integer,
    session_id integer,
    codex_thread_id text,
    codex_turn_id text,
    method text not null,
    request_path text not null,
    upstream_url text not null,
    provider_id text not null,
    model text,
    stream integer,
    status_code integer,
    request_headers_path text not null,
    request_body_raw_path text not null,
    request_body_decoded_path text,
    response_headers_path text,
    response_body_raw_path text,
    error text
);
"""
