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


class SendVoiceAndDocumentTest(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_settings = gateway.settings
        gateway.settings = build_settings(telegram_bot_token="test-token-123", request_timeout=30)
        self.addCleanup(setattr, gateway, "settings", self._orig_settings)

    @patch("openclaw_telegram_gateway.post_multipart_file")
    def test_voice_uses_send_voice_and_the_voice_field(self, post_multipart_file) -> None:
        gateway.send_voice_file(555, Path("clip.ogg"), caption="Listen")
        url, fields, file_field, file_path = post_multipart_file.call_args.args
        self.assertEqual(url, "https://api.telegram.org/bottest-token-123/sendVoice")
        self.assertEqual((fields, file_field), ({"chat_id": "555", "caption": "Listen"}, "voice"))

    @patch("openclaw_telegram_gateway.post_multipart_file")
    def test_document_uses_send_document(self, post_multipart_file) -> None:
        gateway.send_document_file(555, Path("words.txt"))
        url, fields, file_field, _ = post_multipart_file.call_args.args
        self.assertEqual(url, "https://api.telegram.org/bottest-token-123/sendDocument")
        self.assertEqual(file_field, "document")

    def test_english_bot_sends_ogg_as_voice_and_mp3_as_audio(self) -> None:
        calls = []
        with patch.object(gateway, "send_voice_file", lambda chat, path, caption: calls.append(("voice", path.name))), \
                patch.object(gateway, "send_audio_file", lambda chat, path, caption: calls.append(("audio", path.name))):
            gateway._english_bot_send_audio("1", Path("monday_clip.ogg"), "c")
            gateway._english_bot_send_audio("1", Path("tuesday_clip.mp3"), "c")
        self.assertEqual(calls, [("voice", "monday_clip.ogg"), ("audio", "tuesday_clip.mp3")])


if __name__ == "__main__":
    unittest.main()
