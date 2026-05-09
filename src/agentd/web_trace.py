from __future__ import annotations

import gzip
import io
import json
import sqlite3
import tarfile
import zlib
from pathlib import Path
from typing import Any


def build_responses_trace(rows: list[sqlite3.Row]) -> dict[str, Any]:
    exchanges: list[dict[str, Any]] = []
    for row in rows:
        exchange = load_exchange(row)
        exchanges.append(exchange_summary(exchange))
    return {
        'root': {
            'id': 'root',
            'generated_by': 'unknown',
            'role': 'system',
            'content': '',
            'message': {'role': 'system', 'content': ''},
            'request': None,
            'children': [],
        },
        'exchanges': exchanges,
    }


def load_exchange(row: sqlite3.Row) -> dict[str, Any]:
    request_bytes = read_exchange_member(row, 'request')
    response_bytes = read_exchange_member(row, 'response')
    request_body = http_body(request_bytes)
    response_body = http_body(response_bytes)
    request_json = parse_json_bytes(request_body)
    response = parse_response_body(response_body)
    return {
        'id': row_value(row, 'id'),
        'created_at': row_value(row, 'created_at', 0),
        'completed_at': row_value(row, 'completed_at'),
        'session_id': row_value(row, 'session_id'),
        'codex_thread_id': row_value(row, 'codex_thread_id'),
        'codex_turn_id': row_value(row, 'codex_turn_id'),
        'request_path': row_value(row, 'request_path'),
        'upstream_url': row_value(row, 'upstream_url'),
        'provider_id': row_value(row, 'provider_id'),
        'model': row_value(row, 'model') or model_from_request(request_json),
        'status_code': row_value(row, 'status_code'),
        'storage_state': row_value(row, 'storage_state'),
        'period_key': row_value(row, 'period_key'),
        'archive_path': row_value(row, 'archive_path'),
        'archive_member_request': row_value(row, 'archive_member_request'),
        'archive_member_response': row_value(row, 'archive_member_response'),
        'request_json': request_json,
        'response_text': response['text'],
        'response_message': {'role': 'assistant', 'content': response['text']},
        'usage': response['usage'],
        'error': row_value(row, 'error'),
        'has_request_capture': bool(request_bytes),
        'has_response_capture': bool(response_bytes),
    }


def exchange_detail(exchange: dict[str, Any]) -> dict[str, Any]:
    request_json = exchange.get('request_json')
    input_items = normalize_input_items(request_json.get('input')) if isinstance(request_json, dict) else []
    return {
        **exchange_summary(exchange),
        'upstream_url': exchange.get('upstream_url'),
        'provider_id': exchange.get('provider_id'),
        'request_json': request_json,
        'request_input_count': len(input_items),
        'request_input_items': [
            request_input_item_detail(item, index) for index, item in enumerate(input_items) if isinstance(item, dict)
        ],
        'response_text': exchange.get('response_text') or '',
    }


def read_exchange_member(row: sqlite3.Row, kind: str) -> bytes:
    state = str(row_value(row, 'storage_state') or '')
    if state == 'archived':
        archive_path = str(row_value(row, 'archive_path') or '')
        member_name = str(row_value(row, f'archive_member_{kind}') or '')
        if archive_path and member_name:
            return read_tar_zst_member(Path(archive_path), member_name)

    path = str(row_value(row, f'{kind}_capture_path') or '')
    if not path:
        path = str(row_value(row, 'request_body_raw_path' if kind == 'request' else 'response_body_raw_path') or '')
    if not path:
        return b''
    capture_path = Path(path)
    if not capture_path.exists():
        return b''
    return capture_path.read_bytes()


def read_tar_zst_member(archive_path: Path, member_name: str) -> bytes:
    if not archive_path.exists():
        return b''
    try:
        import zstandard
    except ModuleNotFoundError:
        return b''

    decompressor = zstandard.ZstdDecompressor()
    with (
        archive_path.open('rb') as raw_file,
        decompressor.stream_reader(raw_file, closefd=False) as decompressed,
        tarfile.open(fileobj=decompressed, mode='r|') as tar,
    ):
        for member in tar:
            if member.name != member_name:
                continue
            extracted = tar.extractfile(member)
            return extracted.read() if extracted is not None else b''
    return b''


def http_body(raw: bytes) -> bytes:
    if not raw:
        return b''
    headers, body = split_http_message(raw)
    decoded = decode_http_body(body, headers.get('content-encoding', ''))
    return decoded if decoded is not None else body


def split_http_message(raw: bytes) -> tuple[dict[str, str], bytes]:
    for marker in (b'\r\n\r\n', b'\n\n'):
        if marker in raw:
            head, body = raw.split(marker, 1)
            return parse_http_headers(head), body
    return {}, raw


def parse_http_headers(head: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    lines = head.decode('utf-8', errors='replace').splitlines()
    for line in lines[1:]:
        if ':' not in line:
            continue
        key, value = line.split(':', 1)
        headers[key.strip().lower()] = value.strip()
    return headers


def decode_http_body(body: bytes, content_encoding: str) -> bytes | None:
    encodings = [part.strip().lower() for part in content_encoding.split(',') if part.strip()]
    if not encodings or encodings == ['identity']:
        return body

    decoded = body
    for encoding in reversed(encodings):
        if encoding == 'identity':
            continue
        if encoding == 'gzip':
            decoded = gzip.decompress(decoded)
        elif encoding == 'deflate':
            decoded = zlib.decompress(decoded)
        elif encoding == 'zstd':
            try:
                import zstandard
            except ModuleNotFoundError:
                return None
            with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(decoded)) as reader:
                decoded = reader.read()
        else:
            return None
    return decoded


def parse_json_bytes(raw: bytes) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def normalize_input_items(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [{'role': 'user', 'content': value}]
    return []


def parse_response_body(raw: bytes) -> dict[str, Any]:
    body = raw.strip()
    if not body:
        return {'text': '', 'usage': {}}
    if body.startswith(b'{'):
        payload = parse_json_bytes(body)
        return {'text': extract_response_text(payload), 'usage': normalize_usage(find_usage(payload))}

    text_parts: list[str] = []
    usage: dict[str, Any] = {}
    completed_text = ''
    for payload in iter_sse_payloads(raw):
        event_type = str(payload.get('type') or '')
        delta = payload.get('delta')
        if isinstance(delta, str) and 'output_text.delta' in event_type:
            text_parts.append(delta)
        response = payload.get('response') if isinstance(payload.get('response'), dict) else None
        if response:
            completed_text = extract_response_text(response) or completed_text
            usage = normalize_usage(find_usage(response)) or usage
        usage = normalize_usage(find_usage(payload)) or usage
    text = completed_text or ''.join(text_parts)
    return {'text': text, 'usage': usage}


def iter_sse_payloads(raw: bytes) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for line in raw.decode('utf-8', errors='replace').splitlines():
        line = line.strip()
        if not line.startswith('data:'):
            continue
        data = line.removeprefix('data:').strip()
        if not data or data == '[DONE]':
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def exchange_summary(exchange: dict[str, Any]) -> dict[str, Any]:
    usage = exchange.get('usage') if isinstance(exchange.get('usage'), dict) else {}
    response_text = str(exchange.get('response_text') or '')
    return {
        'id': exchange.get('id'),
        'created_at': exchange.get('created_at'),
        'completed_at': exchange.get('completed_at'),
        'session_id': exchange.get('session_id'),
        'codex_thread_id': exchange.get('codex_thread_id'),
        'codex_turn_id': exchange.get('codex_turn_id'),
        'request_path': exchange.get('request_path'),
        'model': exchange.get('model'),
        'status_code': exchange.get('status_code'),
        'storage_state': exchange.get('storage_state'),
        'period_key': exchange.get('period_key'),
        'usage': usage,
        'input_tokens': usage.get('input_tokens'),
        'output_tokens': usage.get('output_tokens'),
        'total_tokens': usage.get('total_tokens'),
        'has_request_capture': exchange.get('has_request_capture'),
        'has_response_capture': exchange.get('has_response_capture'),
        'error': exchange.get('error'),
        'response_preview': compact_text(response_text, 180),
    }


def request_input_item_detail(item: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        'index': index,
        'role': str(item.get('role') or item.get('type') or 'item'),
        'type': str(item.get('type') or ''),
        'content': display_request_item_content(item),
    }


def display_request_item_content(item: dict[str, Any]) -> str:
    content_text = content_to_text(item.get('content')).strip()
    if content_text:
        return content_text

    fields: dict[str, Any] = {}
    for key in ('name', 'call_id', 'arguments', 'output', 'summary', 'result', 'error', 'text'):
        value = item.get(key)
        if value not in (None, '', [], {}):
            fields[key] = value
    if fields:
        return json.dumps(fields, ensure_ascii=False, sort_keys=True)

    fallback = {
        key: value
        for key, value in sorted(item.items())
        if key not in {'id', 'type', 'role', 'status', 'content'} and value not in (None, '', [], {})
    }
    return json.dumps(fallback, ensure_ascii=False, sort_keys=True) if fallback else ''


def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [content_to_text(item) for item in content]
        return '\n'.join(part for part in parts if part)
    if isinstance(content, dict):
        for key in ('text', 'content', 'value'):
            if key in content:
                return content_to_text(content[key])
        return json.dumps(content, ensure_ascii=False, sort_keys=True)
    if content is None:
        return ''
    return str(content)


def compact_text(value: str, limit: int) -> str:
    text = ' '.join(str(value or '').split())
    return text if len(text) <= limit else text[: limit - 6] + ' ...(截断)'


def extract_response_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ''
    output = payload.get('output')
    if isinstance(output, list):
        texts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get('content')
            if isinstance(content, list):
                texts.extend(content_to_text(part) for part in content)
            elif content is not None:
                texts.append(content_to_text(content))
        return ''.join(texts).strip()
    return content_to_text(payload.get('content')).strip()


def find_usage(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        usage = payload.get('usage')
        if isinstance(usage, dict):
            return usage
        for value in payload.values():
            found = find_usage(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = find_usage(value)
            if found:
                return found
    return {}


def normalize_usage(usage: dict[str, Any]) -> dict[str, int]:
    if not usage:
        return {}

    def integer(*keys: str) -> int | None:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int):
                return value
        return None

    result: dict[str, int] = {}
    input_tokens = integer('input_tokens', 'prompt_tokens')
    output_tokens = integer('output_tokens', 'completion_tokens')
    total_tokens = integer('total_tokens')
    if input_tokens is not None:
        result['input_tokens'] = input_tokens
    if output_tokens is not None:
        result['output_tokens'] = output_tokens
    if total_tokens is not None:
        result['total_tokens'] = total_tokens
    elif input_tokens is not None and output_tokens is not None:
        result['total_tokens'] = input_tokens + output_tokens
    return result


def model_from_request(request: dict[str, Any] | None) -> str:
    if not isinstance(request, dict):
        return ''
    model = request.get('model')
    return model if isinstance(model, str) else ''


def row_value(row: sqlite3.Row, key: str, default: Any = '') -> Any:
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value
