from __future__ import annotations

import io
import unittest
import urllib.error
from unittest import mock

from agentd.feishu_registration import (
    FeishuAppRegistrationBegin,
    FeishuAppRegistrationClient,
    FeishuAppRegistrationError,
)


class FeishuAppRegistrationClientTest(unittest.TestCase):
    def test_begin_builds_feishu_verification_url(self) -> None:
        calls: list[tuple[str, dict[str, str]]] = []

        def request(url: str, form: dict[str, str]) -> dict[str, object]:
            calls.append((url, form))
            return {
                'device_code': 'dev',
                'user_code': 'user',
                'expires_in': 300,
                'interval': 5,
            }

        begin = FeishuAppRegistrationClient(request_form=request, sleep=lambda _: None).begin()

        self.assertEqual(calls[0][0], 'https://accounts.feishu.cn/oauth/v1/app/registration')
        self.assertEqual(calls[0][1]['archetype'], 'PersonalAgent')
        self.assertEqual(calls[0][1]['auth_method'], 'client_secret')
        self.assertEqual(begin.verification_url, 'https://open.feishu.cn/page/cli?user_code=user&from=agentd')

    def test_poll_returns_app_credentials(self) -> None:
        responses = [
            {'error': 'authorization_pending'},
            {
                'client_id': 'cli_test',
                'client_secret': 'secret-value',
                'user_info': {'tenant_brand': 'feishu', 'open_id': 'ou_test'},
            },
        ]

        def request(_url: str, _form: dict[str, str]) -> dict[str, object]:
            return responses.pop(0)

        begin = FeishuAppRegistrationBegin(
            device_code='dev',
            user_code='user',
            verification_url='https://open.feishu.cn/page/cli?user_code=user',
            expires_in=300,
            interval=1,
        )

        result = FeishuAppRegistrationClient(request_form=request, sleep=lambda _: None).poll(begin)

        self.assertEqual(result.app_id, 'cli_test')
        self.assertEqual(result.app_secret, 'secret-value')
        self.assertEqual(result.open_id, 'ou_test')

    def test_poll_retries_lark_endpoint_when_secret_is_missing_for_lark_tenant(self) -> None:
        calls: list[str] = []
        responses = [
            {'client_id': 'cli_test', 'client_secret': '', 'user_info': {'tenant_brand': 'lark'}},
            {'client_id': 'cli_test', 'client_secret': 'secret-value', 'user_info': {'tenant_brand': 'lark'}},
        ]

        def request(url: str, _form: dict[str, str]) -> dict[str, object]:
            calls.append(url)
            return responses.pop(0)

        begin = FeishuAppRegistrationBegin(
            device_code='dev',
            user_code='user',
            verification_url='https://open.larksuite.com/page/cli?user_code=user',
            expires_in=300,
            interval=1,
        )

        result = FeishuAppRegistrationClient(brand='lark', request_form=request, sleep=lambda _: None).poll(begin)

        self.assertEqual(result.app_secret, 'secret-value')
        self.assertEqual(calls[0], 'https://accounts.feishu.cn/oauth/v1/app/registration')
        self.assertEqual(calls[1], 'https://accounts.larksuite.com/oauth/v1/app/registration')

    def test_poll_surfaces_denied_registration(self) -> None:
        def request(_url: str, _form: dict[str, str]) -> dict[str, object]:
            return {'error': 'access_denied'}

        begin = FeishuAppRegistrationBegin(
            device_code='dev',
            user_code='user',
            verification_url='https://open.feishu.cn/page/cli?user_code=user',
            expires_in=300,
            interval=1,
        )

        with self.assertRaisesRegex(FeishuAppRegistrationError, 'denied'):
            FeishuAppRegistrationClient(request_form=request, sleep=lambda _: None).poll(begin)

    def test_default_request_form_returns_poll_errors_from_http_error_body(self) -> None:
        body = io.BytesIO(b'{"error":"authorization_pending"}')
        error = urllib.error.HTTPError('https://accounts.feishu.cn', 400, 'Bad Request', {}, body)

        with mock.patch('agentd.feishu_registration.urllib.request.urlopen', side_effect=error):
            data = FeishuAppRegistrationClient._request_form_default(
                'https://accounts.feishu.cn/oauth/v1/app/registration',
                {'action': 'poll', 'device_code': 'dev'},
            )

        self.assertEqual(data, {'error': 'authorization_pending'})


if __name__ == '__main__':
    unittest.main()
