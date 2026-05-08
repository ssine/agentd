from __future__ import annotations

import unittest

from agentd.title import TITLE_DISPLAY_WIDTH, display_width, normalize_title


class TitleTest(unittest.TestCase):
    def test_english_counts_as_half_width_of_chinese(self) -> None:
        self.assertEqual(display_width('abcd'), 4)
        self.assertEqual(display_width('标题'), 4)

    def test_normalize_title_keeps_longer_english_title(self) -> None:
        title = normalize_title('Inspect Feishu card title truncation')

        self.assertEqual(display_width(title), TITLE_DISPLAY_WIDTH)
        self.assertEqual(title, 'Inspect Feishu card title tru...')

    def test_normalize_title_keeps_more_chinese_title(self) -> None:
        title = normalize_title('检查飞书卡片标题截断是否过短需要调整')

        self.assertEqual(display_width(title), 31)
        self.assertEqual(title, '检查飞书卡片标题截断是否过短...')


if __name__ == '__main__':
    unittest.main()
