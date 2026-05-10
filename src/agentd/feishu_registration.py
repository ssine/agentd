from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

APP_REGISTRATION_PATH = '/oauth/v1/app/registration'


@dataclass(frozen=True)
class FeishuAppRegistrationBegin:
    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class FeishuAppRegistrationResult:
    app_id: str
    app_secret: str
    tenant_brand: str = 'feishu'
    open_id: str = ''


class FeishuAppRegistrationError(RuntimeError):
    pass


class FeishuAppRegistrationClient:
    def __init__(
        self,
        *,
        brand: str = 'feishu',
        timeout_seconds: int = 300,
        request_form: Callable[[str, dict[str, str]], dict[str, Any]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.brand = 'lark' if brand == 'lark' else 'feishu'
        self.timeout_seconds = timeout_seconds
        self._request_form = request_form or self._request_form_default
        self._sleep = sleep

    def begin(self) -> FeishuAppRegistrationBegin:
        data = self._request_form(
            self._accounts_base('feishu') + APP_REGISTRATION_PATH,
            {
                'action': 'begin',
                'archetype': 'PersonalAgent',
                'auth_method': 'client_secret',
                'request_user_info': 'open_id tenant_brand',
            },
        )
        user_code = _str(data, 'user_code')
        if not user_code:
            raise FeishuAppRegistrationError('app registration begin response is missing user_code')
        return FeishuAppRegistrationBegin(
            device_code=_required(data, 'device_code'),
            user_code=user_code,
            verification_url=self._verification_url(user_code),
            expires_in=_int(data, 'expires_in', 300),
            interval=max(1, _int(data, 'interval', 5)),
        )

    def poll(self, begin: FeishuAppRegistrationBegin) -> FeishuAppRegistrationResult:
        result = self._poll_brand('feishu', begin)
        if not result.app_secret and result.tenant_brand == 'lark':
            result = self._poll_brand('lark', begin)
        if not result.app_id or not result.app_secret:
            raise FeishuAppRegistrationError('app registration succeeded but app_id/app_secret is missing')
        return result

    def _poll_brand(self, brand: str, begin: FeishuAppRegistrationBegin) -> FeishuAppRegistrationResult:
        deadline = time.monotonic() + min(begin.expires_in, self.timeout_seconds)
        interval = begin.interval
        attempts = 0
        while time.monotonic() < deadline and attempts < 200:
            attempts += 1
            self._sleep(interval)
            data = self._request_form(
                self._accounts_base(brand) + APP_REGISTRATION_PATH,
                {'action': 'poll', 'device_code': begin.device_code},
            )
            err = _str(data, 'error')
            app_id = _str(data, 'client_id')
            if not err and app_id:
                user_info = data.get('user_info') if isinstance(data.get('user_info'), dict) else {}
                return FeishuAppRegistrationResult(
                    app_id=app_id,
                    app_secret=_str(data, 'client_secret'),
                    tenant_brand=_str(user_info, 'tenant_brand') or brand,
                    open_id=_str(user_info, 'open_id'),
                )
            if err == 'authorization_pending':
                continue
            if err == 'slow_down':
                interval = min(interval + 5, 60)
                continue
            if err == 'access_denied':
                raise FeishuAppRegistrationError('app registration was denied by user')
            if err in {'expired_token', 'invalid_grant'}:
                raise FeishuAppRegistrationError('device code expired; please retry')
            description = _str(data, 'error_description') or err or 'unknown error'
            raise FeishuAppRegistrationError(f'app registration failed: {description}')
        raise FeishuAppRegistrationError('app registration timed out; please retry')

    def _verification_url(self, user_code: str) -> str:
        query = urllib.parse.urlencode({'user_code': user_code, 'from': 'agentd'})
        return f'{self._open_base(self.brand)}/page/cli?{query}'

    @staticmethod
    def _accounts_base(brand: str) -> str:
        if brand == 'lark':
            return 'https://accounts.larksuite.com'
        return 'https://accounts.feishu.cn'

    @staticmethod
    def _open_base(brand: str) -> str:
        if brand == 'lark':
            return 'https://open.larksuite.com'
        return 'https://open.feishu.cn'

    @staticmethod
    def _request_form_default(url: str, form: dict[str, str]) -> dict[str, Any]:
        body = urllib.parse.urlencode(form).encode('utf-8')
        req = urllib.request.Request(
            url,
            data=body,
            method='POST',
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
            status = resp.status
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FeishuAppRegistrationError(f'app registration returned non-JSON response: HTTP {status}') from exc
        if status >= 400 or data.get('error'):
            description = _str(data, 'error_description') or _str(data, 'error') or f'HTTP {status}'
            raise FeishuAppRegistrationError(f'app registration failed: {description}')
        if not isinstance(data, dict):
            raise FeishuAppRegistrationError('app registration returned invalid JSON response')
        return data


def _str(data: object, key: str) -> str:
    if not isinstance(data, dict):
        return ''
    value = data.get(key)
    return value if isinstance(value, str) else ''


def _required(data: dict[str, Any], key: str) -> str:
    value = _str(data, key)
    if not value:
        raise FeishuAppRegistrationError(f'app registration response is missing {key}')
    return value


def _int(data: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(data.get(key) or default)
    except (TypeError, ValueError):
        return default
