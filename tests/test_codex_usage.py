from __future__ import annotations

import unittest

from agentd.codex_usage import format_codex_usage, snapshot_to_dict, usage_snapshot_from_result


class CodexUsageTest(unittest.TestCase):
    def test_parses_app_server_rate_limit_snapshot(self) -> None:
        snapshot = usage_snapshot_from_result(sample_app_server_result(), queried_at=1778401473)

        self.assertEqual(snapshot.rate_limits.limit_id, 'codex')
        self.assertEqual(snapshot.rate_limits.plan_type, 'pro')
        self.assertTrue(snapshot.rate_limits.is_allowed())
        self.assertFalse(snapshot.rate_limits.is_limit_reached())
        self.assertIsNotNone(snapshot.rate_limits.primary)
        self.assertIsNotNone(snapshot.rate_limits.secondary)
        if snapshot.rate_limits.primary is None or snapshot.rate_limits.secondary is None:
            self.fail('expected primary and secondary windows')
        self.assertEqual(snapshot.rate_limits.primary.window_duration_mins, 300)
        self.assertEqual(snapshot.rate_limits.primary.used_percent, 6)
        self.assertEqual(snapshot.rate_limits.primary.remaining_percent, 94)
        self.assertEqual(snapshot.rate_limits.secondary.window_duration_mins, 10080)
        self.assertEqual(snapshot.rate_limits_by_limit_id['codex_bengalfox'].limit_name, 'GPT-5.3-Codex-Spark')

    def test_formats_usage_for_humans(self) -> None:
        text = format_codex_usage(usage_snapshot_from_result(sample_app_server_result(), queried_at=1778401473))

        self.assertIn('当前计划：Pro，当前未触发限额。', text)
        self.assertIn('5 小时窗口：已用 6%，剩余 94%', text)
        self.assertIn('一周窗口：已用 17%，剩余 83%', text)
        self.assertIn('额外限额 GPT-5.3-Codex-Spark：5 小时剩余 100%，一周剩余 100%。', text)
        self.assertIn('官方接口只返回百分比，不返回绝对消息数。', text)

    def test_snapshot_to_dict_includes_inferred_allowed_state(self) -> None:
        data = snapshot_to_dict(usage_snapshot_from_result(sample_app_server_result(), queried_at=1778401473))

        self.assertTrue(data['rate_limits']['allowed'])
        self.assertFalse(data['rate_limits']['limit_reached'])


def sample_app_server_result() -> dict[str, object]:
    return {
        'rateLimits': {
            'limitId': 'codex',
            'limitName': None,
            'primary': {'usedPercent': 6, 'windowDurationMins': 300, 'resetsAt': 1778410334},
            'secondary': {'usedPercent': 17, 'windowDurationMins': 10080, 'resetsAt': 1778857837},
            'credits': {'hasCredits': False, 'unlimited': False, 'balance': '0'},
            'planType': 'pro',
            'rateLimitReachedType': None,
        },
        'rateLimitsByLimitId': {
            'codex_bengalfox': {
                'limitId': 'codex_bengalfox',
                'limitName': 'GPT-5.3-Codex-Spark',
                'primary': {'usedPercent': 0, 'windowDurationMins': 300, 'resetsAt': 1778420114},
                'secondary': {'usedPercent': 0, 'windowDurationMins': 10080, 'resetsAt': 1779006914},
                'credits': None,
                'planType': 'pro',
                'rateLimitReachedType': None,
            },
            'codex': {
                'limitId': 'codex',
                'limitName': None,
                'primary': {'usedPercent': 6, 'windowDurationMins': 300, 'resetsAt': 1778410334},
                'secondary': {'usedPercent': 17, 'windowDurationMins': 10080, 'resetsAt': 1778857837},
                'credits': {'hasCredits': False, 'unlimited': False, 'balance': '0'},
                'planType': 'pro',
                'rateLimitReachedType': None,
            },
        },
    }


if __name__ == '__main__':
    unittest.main()
