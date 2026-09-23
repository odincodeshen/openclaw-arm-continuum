import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openclaw_telegram_gateway as gateway

from tests.support import build_settings


class SendAudioFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_settings = gateway.settings
        gateway.settings = build_settings(telegram_bot_token="test-token-123", request_timeout=30)
        self.addCleanup(setattr, gateway, "settings", self._orig_settings)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audio_path = Path(self.tmp.name) / "shadowing.mp3"
        self.audio_path.write_bytes(b"fake-audio")

    @patch("openclaw_telegram_gateway.post_multipart_file")
    def test_calls_send_audio_endpoint_with_chat_id_and_file(self, post_multipart_file) -> None:
        gateway.send_audio_file(555, self.audio_path)

        args, kwargs = post_multipart_file.call_args
        url, fields, file_field, file_path = args
        self.assertEqual(url, "https://api.telegram.org/bottest-token-123/sendAudio")
        self.assertEqual(fields["chat_id"], "555")
        self.assertNotIn("caption", fields)
        self.assertEqual(file_field, "audio")
        self.assertEqual(file_path, self.audio_path)
        self.assertEqual(kwargs["timeout"], 30)

    @patch("openclaw_telegram_gateway.post_multipart_file")
    def test_caption_included_when_given(self, post_multipart_file) -> None:
        gateway.send_audio_file(555, self.audio_path, caption="Shadowing clip for today")

        fields = post_multipart_file.call_args.args[1]
        self.assertEqual(fields["caption"], "Shadowing clip for today")


if __name__ == "__main__":
    unittest.main()
