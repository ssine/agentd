from __future__ import annotations

import datetime as dt
import gzip
import http.client
import io
import json
import os
import re
import shutil
import sqlite3
import tarfile
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
CAPTURE_PERIOD_RE = re.compile(r'^\d{4}-(?:\d{2}-\d{2}|W\d{2}|\d{2})$')


@dataclass
class CaptureContext:
    session_id: int
    codex_thread_id: str = ''
    codex_turn_id: str = ''
    provider_id: str = CAPTURE_PROVIDER_ID
    model: str = ''


@dataclass(frozen=True)
class CapturePaths:
    period_key: str
    period_dir: Path
    inprogress_path: Path
    request_capture_path: Path
    response_capture_path: Path


class CaptureStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init()

    def _init(self) -> None:
        with closing(self._connect()) as conn:
            ensure_model_http_exchanges_schema(conn)
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
                    :storage_state,
                    :period_key,
                    :request_capture_path,
                    :response_capture_path,
                    :archive_path,
                    :archive_member_request,
                    :archive_member_response,
                    :archive_format,
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
                select id, request_capture_path, response_capture_path, request_body_raw_path, response_body_raw_path
                from model_http_exchanges
                where storage_state = 'live'
                  and (
                    period_key = ?
                    or request_capture_path like ?
                    or response_capture_path like ?
                    or request_body_raw_path like ?
                    or response_body_raw_path like ?
                  )
                """,
                (period_key, legacy_path_prefix, legacy_path_prefix, legacy_path_prefix, legacy_path_prefix),
            ).fetchall()
            for row in rows:
                request_path = (
                    row['request_capture_path'] or row['request_body_raw_path'] or f'{row["id"]}-request.http'
                )
                response_path = (
                    row['response_capture_path'] or row['response_body_raw_path'] or f'{row["id"]}-response.http'
                )
                conn.execute(
                    """
                    update model_http_exchanges
                    set storage_state = 'archived',
                        period_key = ?,
                        archive_path = ?,
                        archive_member_request = ?,
                        archive_member_response = ?,
                        archive_format = ?
                    where id = ?
                    """,
                    (
                        period_key,
                        str(archive_path),
                        Path(str(request_path)).name,
                        Path(str(response_path)).name,
                        archive_format,
                        row['id'],
                    ),
                )
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
        archive_period: str = 'week',
        archive_format: str = 'tar.zst',
        zstd_level: int = 10,
    ) -> None:
        self.capture_dir = capture_dir
        self.db_path = db_path
        self.upstream_mode = upstream_mode or 'codex-default'
        self.upstream_url = upstream_url
        self.context = context
        self.save_sensitive_headers = save_sensitive_headers
        self.archive_period = normalize_archive_period(archive_period)
        self.archive_format = normalize_archive_format(archive_format)
        self.zstd_level = normalize_zstd_level(zstd_level)
        self.store = CaptureStore(db_path)
        self._context_lock = threading.Lock()
        self._archive_lock = threading.Lock()
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
        self.archive_finished_periods()

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

    def exchange_dir(self) -> tuple[str, Path]:
        self.archive_finished_periods()
        period_key = capture_period_key(dt.datetime.now(dt.timezone.utc), self.archive_period)
        path = self.capture_dir / 'responses' / period_key
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        with suppress(OSError):
            os.chmod(path, 0o700)
        return period_key, path

    def archive_finished_periods(self, now: dt.datetime | None = None) -> None:
        with self._archive_lock:
            responses_root = self.capture_dir / 'responses'
            responses_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            with suppress(OSError):
                os.chmod(responses_root, 0o700)

            current_period = capture_period_key(now or dt.datetime.now(dt.timezone.utc), self.archive_period)
            for period_dir in sorted(path for path in responses_root.iterdir() if path.is_dir()):
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

        try:
            decoded_body = decode_body(raw_body, header_value(request_headers, 'content-encoding'))
            request_json = parse_json(decoded_body)
            metadata = parse_metadata_header(header_value(request_headers, 'x-codex-turn-metadata'))
            correlated = correlate(context, metadata)
            model = model_from_request(request_json) or context.model
            stream = stream_from_request(request_json)

            write_http_request(
                paths.request_capture_path,
                'POST',
                request_path,
                request_headers,
                raw_body,
                self.proxy.save_sensitive_headers,
            )
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
                    'request_headers_path': str(paths.request_capture_path),
                    'request_body_raw_path': str(paths.request_capture_path),
                    'request_body_decoded_path': None,
                    'response_headers_path': None,
                    'response_body_raw_path': str(paths.response_capture_path),
                    'storage_state': 'live',
                    'period_key': paths.period_key,
                    'request_capture_path': str(paths.request_capture_path),
                    'response_capture_path': str(paths.response_capture_path),
                    'archive_path': None,
                    'archive_member_request': None,
                    'archive_member_response': None,
                    'archive_format': self.proxy.archive_format,
                    'error': None,
                }
            )
            self._forward(exchange_id, upstream_url, request_headers, raw_body, paths)
        except Exception as exc:  # pragma: no cover - exercised by integration failures
            self.proxy.store.update_exchange(
                exchange_id,
                completed_at=int(time.time()),
                error=str(exc),
                response_body_raw_path=str(paths.response_capture_path),
            )
            if not getattr(self, '_agentd_response_started', False):
                self.send_error(502, f'agentd capture proxy upstream error: {exc}')
            else:
                self.close_connection = True
        finally:
            with suppress(FileNotFoundError):
                paths.inprogress_path.unlink()

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
            response_headers = response.getheaders()
            self.proxy.store.update_exchange(
                exchange_id,
                status_code=response.status,
                response_headers_path=str(paths.response_capture_path),
                response_body_raw_path=str(paths.response_capture_path),
                response_capture_path=str(paths.response_capture_path),
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

            with paths.response_capture_path.open('wb') as body_file:
                body_file.write(
                    http_response_head(
                        response.status,
                        response.reason,
                        response_headers,
                        self.proxy.save_sensitive_headers,
                    )
                )
                body_file.flush()
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
                response_headers_path=str(paths.response_capture_path),
                response_body_raw_path=str(paths.response_capture_path),
                response_capture_path=str(paths.response_capture_path),
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
        period_key, root = self.proxy.exchange_dir()
        inprogress_path = root / f'{exchange_id}.inprogress'
        inprogress_path.write_text(str(time.time()) + '\n', encoding='utf-8')
        return CapturePaths(
            period_key=period_key,
            period_dir=root,
            inprogress_path=inprogress_path,
            request_capture_path=root / f'{exchange_id}-request.http',
            response_capture_path=root / f'{exchange_id}-response.http',
        )

    def log_message(self, format: str, *args: Any) -> None:
        return


def normalize_archive_period(value: str) -> str:
    raw = (value or 'week').strip().lower()
    aliases = {'daily': 'day', 'weekly': 'week', 'monthly': 'month'}
    period = aliases.get(raw, raw)
    if period not in {'day', 'week', 'month'}:
        raise ValueError('archive_period must be day, week, or month')
    return period


def normalize_archive_format(value: str) -> str:
    archive_format = (value or 'tar.zst').strip().lower()
    if archive_format != 'tar.zst':
        raise ValueError('archive_format currently supports only tar.zst')
    return archive_format


def normalize_zstd_level(value: int) -> int:
    level = int(value or 10)
    if level < 1 or level > 22:
        raise ValueError('zstd_level must be between 1 and 22')
    return level


def capture_period_key(moment: dt.datetime, archive_period: str) -> str:
    local = moment.astimezone()
    period = normalize_archive_period(archive_period)
    if period == 'day':
        return local.strftime('%Y-%m-%d')
    if period == 'month':
        return local.strftime('%Y-%m')
    year, week, _ = local.isocalendar()
    return f'{year}-W{week:02d}'


def is_capture_period_key(value: str) -> bool:
    return bool(CAPTURE_PERIOD_RE.match(value))


def write_http_request(
    path: Path,
    method: str,
    request_path: str,
    headers: dict[str, str],
    raw_body: bytes,
    save_sensitive_headers: bool,
) -> None:
    stored_headers = redact_headers(headers, save_sensitive_headers)
    path.write_bytes(http_message_head(f'{method} {request_path} HTTP/1.1', stored_headers.items()) + raw_body)


def http_response_head(
    status: int,
    reason: str,
    headers: list[tuple[str, str]],
    save_sensitive_headers: bool,
) -> bytes:
    stored_headers = [
        (key, '<redacted>' if not save_sensitive_headers and key.lower() in SENSITIVE_HEADERS else value)
        for key, value in headers
    ]
    return http_message_head(f'HTTP/1.1 {status} {reason}', stored_headers)


def http_message_head(start_line: str, headers: Any) -> bytes:
    lines = [start_line]
    lines.extend(f'{key}: {value}' for key, value in headers)
    return ('\r\n'.join(lines) + '\r\n\r\n').encode('utf-8')


def write_tar_zst_archive(source_dir: Path, archive_path: Path, *, level: int) -> None:
    try:
        import zstandard
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency installation issue
        raise RuntimeError('zstandard is required to write codex capture archives') from exc

    archive_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    compressor = zstandard.ZstdCompressor(level=level)
    with (
        archive_path.open('wb') as raw_file,
        compressor.stream_writer(raw_file, closefd=False) as compressed,
        tarfile.open(fileobj=compressed, mode='w|') as tar,
    ):
        for child in sorted(source_dir.iterdir()):
            if child.is_file() and not child.name.startswith('.') and not child.name.endswith('.inprogress'):
                tar.add(child, arcname=child.name, recursive=False)


def read_tar_zst_member_names(archive_path: Path) -> set[str]:
    try:
        import zstandard
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency installation issue
        raise RuntimeError('zstandard is required to read codex capture archives') from exc

    members: set[str] = set()
    decompressor = zstandard.ZstdDecompressor()
    with (
        archive_path.open('rb') as raw_file,
        decompressor.stream_reader(raw_file, closefd=False) as decompressed,
        tarfile.open(fileobj=decompressed, mode='r|') as tar,
    ):
        for member in tar:
            members.add(member.name)
    return members


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
        with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw_body)) as reader:
            return reader.read()
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


def ensure_model_http_exchanges_schema(conn: sqlite3.Connection) -> None:
    conn.execute(MODEL_HTTP_EXCHANGES_SCHEMA)
    columns = {str(row[1]) for row in conn.execute('pragma table_info(model_http_exchanges)')}
    for column, definition in MODEL_HTTP_EXCHANGES_ADDED_COLUMNS.items():
        if column not in columns:
            conn.execute(f'alter table model_http_exchanges add column {column} {definition}')


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
    storage_state text not null default 'live',
    period_key text not null default '',
    request_capture_path text,
    response_capture_path text,
    archive_path text,
    archive_member_request text,
    archive_member_response text,
    archive_format text,
    error text
);
"""

MODEL_HTTP_EXCHANGES_ADDED_COLUMNS = {
    'storage_state': "text not null default 'live'",
    'period_key': "text not null default ''",
    'request_capture_path': 'text',
    'response_capture_path': 'text',
    'archive_path': 'text',
    'archive_member_request': 'text',
    'archive_member_response': 'text',
    'archive_format': 'text',
}
