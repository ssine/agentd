from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agentd.daemon import AgentDaemon
from agentd.models import IncomingMessage, MessageAttachment


class AttachmentDownloadTest(unittest.TestCase):
    def test_feishu_attachment_is_downloaded_into_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            daemon = object.__new__(AgentDaemon)
            daemon.config = SimpleNamespace(state_dir=root)
            daemon.feishu = FakeFeishuDownloader()
            daemon.log = logging.getLogger('test')
            message = IncomingMessage(
                chat_id='chat-1',
                message_id='msg/1',
                text='[image]',
                attachments=(MessageAttachment(kind='image', key='img_1', name='photo'),),
            )

            updated = daemon._prepare_message_attachments(message)

            self.assertEqual(len(updated.attachments), 1)
            local_path = Path(updated.attachments[0].local_path)
            self.assertTrue(local_path.is_file())
            self.assertEqual(local_path.read_bytes(), b'image-bytes')
            self.assertIn('attachments', local_path.parts)
            self.assertEqual(local_path.name, '01-photo.jpg')
            self.assertEqual(daemon.feishu.last_call[:3], ('msg/1', 'img_1', 'image'))

    def test_feishu_media_attachment_uses_file_resource_type(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            daemon = object.__new__(AgentDaemon)
            daemon.config = SimpleNamespace(state_dir=root)
            daemon.feishu = FakeFeishuDownloader()
            daemon.log = logging.getLogger('test')
            message = IncomingMessage(
                chat_id='chat-1',
                message_id='msg-2',
                text='[media]',
                attachments=(
                    MessageAttachment(
                        kind='media',
                        key='file_video_1',
                        name='',
                        mime_type='video/mp4',
                    ),
                ),
            )

            updated = daemon._prepare_message_attachments(message)

            self.assertEqual(daemon.feishu.last_call[:3], ('msg-2', 'file_video_1', 'file'))
            local_path = Path(updated.attachments[0].local_path)
            self.assertTrue(local_path.is_file())
            self.assertEqual(local_path.name, '01-media-1.mp4')


class FakeFeishuDownloader:
    def download_message_resource(self, message_id: str, file_key: str, resource_type: str, destination: Path) -> Path:
        self.last_call = (message_id, file_key, resource_type, destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b'image-bytes')
        return destination


if __name__ == '__main__':
    unittest.main()
