from __future__ import annotations

import json
import unittest

from agentd.feishu import (
    MESSAGE_TEXT_LIMIT,
    TRUNCATED_SUFFIX,
    build_markdown_card,
    build_text_content,
    final_message_card_width_mode,
)


class FeishuMessageBuilderTest(unittest.TestCase):
    def test_build_markdown_card_uses_markdown_component(self) -> None:
        markdown = '**Done**\n- item\n\nUse `agentd`.'
        card = build_markdown_card(markdown)

        self.assertEqual(card['schema'], '2.0')
        self.assertEqual(card['config'], {'width_mode': 'fill'})
        self.assertEqual(card['body']['elements'], [{'tag': 'markdown', 'content': markdown}])

    def test_build_markdown_card_can_prefix_mentions(self) -> None:
        card = build_markdown_card('hello', at_open_ids=['ou_123', 'ou_456'])

        self.assertEqual(card['body']['elements'][0]['content'], '<at id=ou_123></at> <at id=ou_456></at> hello')

    def test_build_markdown_card_can_omit_width_mode(self) -> None:
        card = build_markdown_card('short final', width_mode=None)

        self.assertNotIn('config', card)

    def test_final_message_card_width_mode_omits_fill_for_short_messages(self) -> None:
        self.assertIsNone(final_message_card_width_mode('x' * 50))
        self.assertEqual(final_message_card_width_mode('x' * 51), 'fill')

    def test_build_text_content_keeps_plain_text_message_type(self) -> None:
        msg_type, content = build_text_content('**not markdown**')

        self.assertEqual(msg_type, 'text')
        self.assertEqual(json.loads(content), {'text': '**not markdown**'})

    def test_builders_truncate_to_existing_limit(self) -> None:
        card = build_markdown_card('x' * (MESSAGE_TEXT_LIMIT + 1))

        content = card['body']['elements'][0]['content']
        self.assertEqual(len(content), MESSAGE_TEXT_LIMIT)
        self.assertTrue(content.endswith(TRUNCATED_SUFFIX))


if __name__ == '__main__':
    unittest.main()
