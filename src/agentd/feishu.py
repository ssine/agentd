from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import FeishuConfig
from .models import CardAction, IncomingMessage, MessageAttachment

FEISHU_API_BASE = 'https://open.feishu.cn/open-apis'
MESSAGE_TEXT_LIMIT = 4000
SHORT_FINAL_MARKDOWN_CARD_LIMIT = 50
TRUNCATED_SUFFIX = '\n...(已截断)'


def obj_get(obj: Any, *keys: str) -> Any:
    current = obj
    for key in keys:
        current = current.get(key) if isinstance(current, dict) else getattr(current, key, None)
        if current is None:
            return None
    return current


def message_id_from_result(result: dict[str, Any]) -> str:
    data = result.get('data') if isinstance(result.get('data'), dict) else result
    value = data.get('message_id') if isinstance(data, dict) else None
    return value if isinstance(value, str) else ''


def thread_id_from_result(result: dict[str, Any]) -> str:
    data = result.get('data') if isinstance(result.get('data'), dict) else result
    value = data.get('thread_id') if isinstance(data, dict) else None
    return value if isinstance(value, str) else ''


def message_text(message: Any) -> str:
    msg_type, content, raw = _message_content(message)
    if not isinstance(content, dict):
        return str(raw or '').strip()

    if msg_type == 'post':
        body = content.get('zh_cn') or content.get('en_us') or content
        parts: list[str] = []
        for line in body.get('content', []):
            for node in line:
                tag = node.get('tag')
                if tag in {'text', 'md'}:
                    parts.append(node.get('text', ''))
                elif tag == 'a':
                    parts.append(node.get('text') or node.get('href') or '')
                elif tag == 'at':
                    parts.append(f'@{node.get("user_name") or node.get("user_id") or ""}')
                else:
                    attachment = _attachment_from_node(node)
                    if attachment is not None:
                        parts.append(f'[{attachment.kind}]')
        return ''.join(parts).strip()

    if msg_type in {'image', 'file', 'audio', 'media'}:
        return f'[{msg_type}]'
    return str(content.get('text') or '').strip()


def message_attachments(message: Any) -> tuple[MessageAttachment, ...]:
    msg_type, content, _raw = _message_content(message)
    if not isinstance(content, dict):
        return ()

    if msg_type in {'image', 'file', 'audio', 'media'}:
        attachment = _attachment_from_mapping(content, kind=msg_type)
        return (attachment,) if attachment is not None else ()
    if msg_type != 'post':
        return ()

    body = content.get('zh_cn') or content.get('en_us') or content
    attachments: list[MessageAttachment] = []
    for line in body.get('content', []):
        if not isinstance(line, list):
            continue
        for node in line:
            if isinstance(node, dict):
                attachment = _attachment_from_node(node)
                if attachment is not None:
                    attachments.append(attachment)
    return tuple(attachments)


def _message_content(message: Any) -> tuple[str, Any, str]:
    raw = obj_get(message, 'content')
    msg_type = str(obj_get(message, 'message_type') or obj_get(message, 'msg_type') or '')
    try:
        content = json.loads(raw or '{}')
    except json.JSONDecodeError:
        return msg_type, None, str(raw or '')
    return msg_type, content, str(raw or '')


def _attachment_from_node(node: dict[str, Any]) -> MessageAttachment | None:
    tag = str(node.get('tag') or '')
    if tag in {'img', 'image'}:
        return _attachment_from_mapping(node, kind='image')
    if tag in {'file', 'audio', 'media', 'video'}:
        return _attachment_from_mapping(node, kind='media' if tag == 'video' else tag)
    if node.get('file_key'):
        return _attachment_from_mapping(node, kind='file')
    if node.get('image_key'):
        return _attachment_from_mapping(node, kind='image')
    return None


def _attachment_from_mapping(data: dict[str, Any], *, kind: str) -> MessageAttachment | None:
    key = _attachment_key(data, kind=kind)
    if not key:
        return None
    size = _attachment_size(data.get('file_size') or data.get('size'))
    return MessageAttachment(
        kind=kind,
        key=key,
        name=str(data.get('file_name') or data.get('name') or data.get('title') or ''),
        mime_type=str(data.get('mime_type') or data.get('file_type') or ''),
        size=size,
    )


def _attachment_key(data: dict[str, Any], *, kind: str) -> str:
    if kind == 'image':
        return str(data.get('image_key') or data.get('key') or data.get('file_key') or '')
    if kind in {'file', 'audio', 'media'}:
        return str(data.get('file_key') or data.get('key') or data.get('image_key') or '')
    return str(data.get('key') or data.get('file_key') or data.get('image_key') or '')


def _attachment_size(value: Any) -> int | None:
    if value in (None, ''):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def sender_name(event: Any, sender_open_id: str) -> str:
    return obj_get(event, 'sender', 'sender_name') or obj_get(event, 'sender', 'name') or sender_open_id or 'unknown'


def parse_incoming(data: Any) -> IncomingMessage | None:
    event = obj_get(data, 'event') or data
    message = obj_get(event, 'message')
    sender = obj_get(event, 'sender')
    if not message or not sender:
        return None

    text = message_text(message)
    attachments = message_attachments(message)
    if not text and not attachments:
        return None

    sender_open_id = str(obj_get(sender, 'sender_id', 'open_id') or '')
    return IncomingMessage(
        chat_id=str(obj_get(message, 'chat_id') or ''),
        message_id=str(obj_get(message, 'message_id') or ''),
        text=text,
        sender_open_id=sender_open_id,
        sender_name=sender_name(event, sender_open_id),
        sender_type=str(obj_get(sender, 'sender_type') or ''),
        thread_id=str(obj_get(message, 'thread_id') or ''),
        chat_type=str(obj_get(message, 'chat_type') or ''),
        attachments=attachments,
    )


class FeishuApi:
    def __init__(self, config: FeishuConfig) -> None:
        self.config = config
        self._token = ''
        self._token_expires_at = 0

    def send_text(
        self,
        chat_id: str,
        text: str,
        *,
        at_open_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        msg_type, content = build_text_content(text, at_open_ids=at_open_ids)
        return self.send_message(chat_id, msg_type, content)

    def send_interactive(self, chat_id: str, card: dict[str, Any]) -> dict[str, Any]:
        return self.send_message(chat_id, 'interactive', json.dumps(card, ensure_ascii=False))

    def send_markdown(
        self,
        chat_id: str,
        markdown: str,
        *,
        at_open_ids: list[str] | None = None,
        width_mode: str | None = 'fill',
    ) -> dict[str, Any]:
        return self.send_interactive(
            chat_id, build_markdown_card(markdown, at_open_ids=at_open_ids, width_mode=width_mode)
        )

    def reply_interactive(
        self,
        message_id: str,
        card: dict[str, Any],
        *,
        reply_in_thread: bool = False,
    ) -> dict[str, Any]:
        return self.reply_message(
            message_id,
            'interactive',
            json.dumps(card, ensure_ascii=False),
            reply_in_thread=reply_in_thread,
        )

    def send_message(
        self, receive_id: str, msg_type: str, content: str, receive_id_type: str = 'chat_id'
    ) -> dict[str, Any]:
        if not self.config.app_id or not self.config.app_secret:
            raise RuntimeError('missing Feishu app_id/app_secret')
        return self._request_json(
            'POST',
            f'{FEISHU_API_BASE}/im/v1/messages?receive_id_type={urllib.parse.quote(receive_id_type)}',
            {
                'receive_id': receive_id,
                'msg_type': msg_type,
                'content': content,
            },
            {'Authorization': f'Bearer {self._tenant_access_token()}'},
        )

    def update_interactive(self, message_id: str, card: dict[str, Any]) -> dict[str, Any]:
        if not self.config.app_id or not self.config.app_secret:
            raise RuntimeError('missing Feishu app_id/app_secret')
        return self._request_json(
            'PATCH',
            f'{FEISHU_API_BASE}/im/v1/messages/{urllib.parse.quote(message_id)}',
            {'content': json.dumps(card, ensure_ascii=False)},
            {'Authorization': f'Bearer {self._tenant_access_token()}'},
        )

    def reply_text(
        self,
        message_id: str,
        text: str,
        *,
        reply_in_thread: bool = False,
        at_open_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        msg_type, content = build_text_content(text, at_open_ids=at_open_ids)
        return self.reply_message(message_id, msg_type, content, reply_in_thread=reply_in_thread)

    def reply_markdown(
        self,
        message_id: str,
        markdown: str,
        *,
        reply_in_thread: bool = False,
        at_open_ids: list[str] | None = None,
        width_mode: str | None = 'fill',
    ) -> dict[str, Any]:
        return self.reply_interactive(
            message_id,
            build_markdown_card(markdown, at_open_ids=at_open_ids, width_mode=width_mode),
            reply_in_thread=reply_in_thread,
        )

    def reply_message(
        self,
        message_id: str,
        msg_type: str,
        content: str,
        *,
        reply_in_thread: bool = False,
    ) -> dict[str, Any]:
        if not self.config.app_id or not self.config.app_secret:
            raise RuntimeError('missing Feishu app_id/app_secret')
        payload: dict[str, Any] = {'msg_type': msg_type, 'content': content}
        if reply_in_thread:
            payload['reply_in_thread'] = True
        return self._request_json(
            'POST',
            f'{FEISHU_API_BASE}/im/v1/messages/{urllib.parse.quote(message_id)}/reply',
            payload,
            {'Authorization': f'Bearer {self._tenant_access_token()}'},
        )

    def download_message_resource(
        self,
        message_id: str,
        file_key: str,
        resource_type: str,
        destination: Path,
    ) -> Path:
        if not self.config.app_id or not self.config.app_secret:
            raise RuntimeError('missing Feishu app_id/app_secret')
        destination.parent.mkdir(parents=True, exist_ok=True)
        message_id_path = urllib.parse.quote(message_id, safe='')
        file_key_path = urllib.parse.quote(file_key, safe='')
        resource_type_query = urllib.parse.quote(resource_type, safe='')
        url = (
            f'{FEISHU_API_BASE}/im/v1/messages/{message_id_path}/resources/'
            f'{file_key_path}?type={resource_type_query}'
        )
        raw = self._request_binary('GET', url, {'Authorization': f'Bearer {self._tenant_access_token()}'})
        tmp = destination.with_name(destination.name + '.tmp')
        tmp.write_bytes(raw)
        tmp.replace(destination)
        return destination

    def _tenant_access_token(self) -> str:
        now = int(time.time())
        if self._token and self._token_expires_at > now + 60:
            return self._token
        result = self._post_json(
            f'{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal',
            {'app_id': self.config.app_id, 'app_secret': self.config.app_secret},
        )
        token = result.get('tenant_access_token')
        if not isinstance(token, str) or not token:
            raise RuntimeError('tenant_access_token missing from Feishu response')
        expire = int(result.get('expire') or 7200)
        self._token = token
        self._token_expires_at = now + expire
        return token

    @staticmethod
    def _request_json(
        method: str,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        request_headers = {'Content-Type': 'application/json; charset=utf-8'}
        if headers:
            request_headers.update(headers)
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            result = {}
        code = result.get('code') or result.get('StatusCode')
        if code not in (None, 0):
            raise RuntimeError(f'Feishu API returned code={code!r}: {raw[:500]}')
        return result

    @classmethod
    def _post_json(cls, url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
        return cls._request_json('POST', url, payload, headers)

    @staticmethod
    def _request_binary(method: str, url: str, headers: dict[str, str] | None = None) -> bytes:
        request_headers: dict[str, str] = {}
        if headers:
            request_headers.update(headers)
        req = urllib.request.Request(url, headers=request_headers, method=method)
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            content_type = resp.headers.get('Content-Type', '')
        if 'json' in content_type.lower():
            try:
                result = json.loads(raw.decode('utf-8', errors='replace'))
            except json.JSONDecodeError:
                result = {}
            code = result.get('code') or result.get('StatusCode')
            if code not in (None, 0):
                raise RuntimeError(f'Feishu API returned code={code!r}: {raw[:500]!r}')
        return raw


class FeishuListener:
    def __init__(self, config: FeishuConfig) -> None:
        self.config = config

    def start(
        self,
        on_message: Callable[[IncomingMessage], None],
        on_card_action: Callable[[CardAction], str] | None = None,
    ) -> None:
        if not self.config.app_id or not self.config.app_secret:
            raise RuntimeError('missing Feishu app_id/app_secret')
        try:
            import lark_oapi as lark
            from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
            from lark_oapi.event.callback.model.p2_card_action_trigger import (
                P2CardActionTrigger,
                P2CardActionTriggerResponse,
            )
        except ModuleNotFoundError as exc:
            raise RuntimeError('missing dependency: lark-oapi is not installed in agentd venv') from exc

        log = logging.getLogger('agentd.feishu')

        def handle(data: P2ImMessageReceiveV1) -> None:
            message = parse_incoming(data)
            if message is None:
                return
            on_message(message)

        def ignore_event(_data: Any) -> None:
            return None

        def handle_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
            action = parse_card_action(data)
            if on_card_action is not None and action is not None:
                threading.Thread(
                    target=dispatch_card_action,
                    args=(action,),
                    name='agentd-card-action',
                    daemon=True,
                ).start()
            return P2CardActionTriggerResponse()

        def dispatch_card_action(action: CardAction) -> None:
            try:
                if on_card_action is not None:
                    on_card_action(action)
            except Exception:
                log.exception('failed to handle Feishu card action: %s', action)

        handler = (
            lark.EventDispatcherHandler.builder('', '')
            .register_p2_im_message_receive_v1(handle)
            .register_p2_im_message_message_read_v1(ignore_event)
            .register_p2_card_action_trigger(handle_card_action)
            .build()
        )
        ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )
        ws_client.start()


def build_text_content(text: str, at_open_ids: list[str] | None = None) -> tuple[str, str]:
    text = trim_message_text(text)

    if at_open_ids:
        elements: list[dict[str, str]] = []
        for open_id in at_open_ids:
            elements.append({'tag': 'at', 'user_id': open_id})
        elements.append({'tag': 'text', 'text': ' '})
        elements.append({'tag': 'text', 'text': text})
        return 'post', json.dumps({'zh_cn': {'title': '', 'content': [elements]}}, ensure_ascii=False)

    return 'text', json.dumps({'text': text}, ensure_ascii=False)


def build_markdown_card(
    markdown: str,
    at_open_ids: list[str] | None = None,
    *,
    width_mode: str | None = 'fill',
) -> dict[str, Any]:
    markdown = trim_message_text(markdown)
    if at_open_ids:
        mentions = ' '.join(f'<at id={open_id}></at>' for open_id in at_open_ids)
        markdown = f'{mentions} {markdown}' if markdown else mentions
    card: dict[str, Any] = {
        'schema': '2.0',
        'body': {
            'elements': [
                {
                    'tag': 'markdown',
                    'content': markdown,
                }
            ]
        },
    }
    if width_mode:
        card['config'] = {'width_mode': width_mode}
    return card


def final_message_card_width_mode(markdown: str) -> str | None:
    if len(trim_message_text(markdown)) <= SHORT_FINAL_MARKDOWN_CARD_LIMIT:
        return None
    return 'fill'


def trim_message_text(text: str) -> str:
    text = text.strip()
    if len(text) > MESSAGE_TEXT_LIMIT:
        text = text[: MESSAGE_TEXT_LIMIT - len(TRUNCATED_SUFFIX)] + TRUNCATED_SUFFIX
    return text


def parse_card_action(data: Any) -> CardAction | None:
    event = obj_get(data, 'event')
    value = obj_get(event, 'action', 'value')
    if not isinstance(value, dict):
        return None
    action = str(value.get('action') or '').strip()
    if not action:
        return None
    session_id_raw = value.get('session_id')
    try:
        session_id = int(session_id_raw) if session_id_raw is not None else None
    except (TypeError, ValueError):
        session_id = None
    return CardAction(
        action=action,
        session_id=session_id,
        message_id=str(value.get('message_id') or obj_get(event, 'context', 'open_message_id') or ''),
        chat_id=str(value.get('chat_id') or obj_get(event, 'context', 'open_chat_id') or ''),
    )
