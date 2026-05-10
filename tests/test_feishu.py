from __future__ import annotations

import json
import unittest

from agentd.feishu import (
    MESSAGE_TEXT_LIMIT,
    TRUNCATED_SUFFIX,
    build_markdown_card,
    build_text_content,
    final_message_card_width_mode,
    message_attachments,
    message_text,
    parse_incoming,
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

    def test_parse_image_message_keeps_attachment_key(self) -> None:
        message = parse_incoming(
            {
                'event': {
                    'sender': {'sender_id': {'open_id': 'ou_1'}, 'sender_type': 'user'},
                    'message': {
                        'chat_id': 'oc_1',
                        'message_id': 'om_1',
                        'message_type': 'image',
                        'content': json.dumps({'image_key': 'img_v2_1'}),
                    },
                }
            }
        )

        self.assertIsNotNone(message)
        if message is None:
            self.fail('expected image message')
        self.assertEqual(message.text, '[image]')
        self.assertEqual(len(message.attachments), 1)
        self.assertEqual(message.attachments[0].kind, 'image')
        self.assertEqual(message.attachments[0].key, 'img_v2_1')

    def test_parse_file_message_keeps_attachment_metadata(self) -> None:
        message = parse_incoming(
            {
                'event': {
                    'sender': {'sender_id': {'open_id': 'ou_1'}, 'sender_type': 'user'},
                    'message': {
                        'chat_id': 'oc_1',
                        'message_id': 'om_2',
                        'message_type': 'file',
                        'content': json.dumps(
                            {
                                'file_key': 'file_v2_1',
                                'file_name': 'report.pdf',
                                'file_size': '123',
                                'mime_type': 'application/pdf',
                            }
                        ),
                    },
                }
            }
        )

        self.assertIsNotNone(message)
        if message is None:
            self.fail('expected file message')
        self.assertEqual(message.text, '[file]')
        self.assertEqual(message.attachments[0].kind, 'file')
        self.assertEqual(message.attachments[0].key, 'file_v2_1')
        self.assertEqual(message.attachments[0].name, 'report.pdf')
        self.assertEqual(message.attachments[0].size, 123)
        self.assertEqual(message.attachments[0].mime_type, 'application/pdf')

    def test_parse_media_message_keeps_video_file_key(self) -> None:
        message = parse_incoming(
            {
                'event': {
                    'sender': {'sender_id': {'open_id': 'ou_1'}, 'sender_type': 'user'},
                    'message': {
                        'chat_id': 'oc_1',
                        'message_id': 'om_3',
                        'message_type': 'media',
                        'content': json.dumps(
                            {
                                'file_key': 'file_v2_video',
                                'image_key': 'img_v2_cover',
                                'file_name': 'demo.mp4',
                                'mime_type': 'video/mp4',
                            }
                        ),
                    },
                }
            }
        )

        self.assertIsNotNone(message)
        if message is None:
            self.fail('expected media message')
        self.assertEqual(message.text, '[media]')
        self.assertEqual(message.attachments[0].kind, 'media')
        self.assertEqual(message.attachments[0].key, 'file_v2_video')
        self.assertEqual(message.attachments[0].name, 'demo.mp4')
        self.assertEqual(message.attachments[0].mime_type, 'video/mp4')

    def test_rich_text_image_is_visible_in_text_and_attachments(self) -> None:
        raw_message = {
            'message_type': 'post',
            'content': json.dumps(
                {
                    'zh_cn': {
                        'content': [
                            [
                                {'tag': 'text', 'text': 'see '},
                                {'tag': 'img', 'image_key': 'img_v2_post'},
                            ]
                        ]
                    }
                }
            ),
        }

        self.assertEqual(message_text(raw_message), 'see [image]')
        attachments = message_attachments(raw_message)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].kind, 'image')
        self.assertEqual(attachments[0].key, 'img_v2_post')


if __name__ == '__main__':
    unittest.main()
