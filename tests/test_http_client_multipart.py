import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from openclaw_runtime.http_client import post_multipart_file


class PostMultipartFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audio_path = Path(self.tmp.name) / "clip.mp3"
        self.audio_path.write_bytes(b"\xff\xfb\x90\x00fake-mp3-bytes")

    @patch("openclaw_runtime.http_client.urllib.request.urlopen")
    def test_sends_multipart_body_with_fields_and_file(self, urlopen) -> None:
        response = MagicMock()
        response.read.return_value = json.dumps({"ok": True}).encode("utf-8")
        response.__enter__.return_value = response
        urlopen.return_value = response

        result = post_multipart_file(
            "https://api.telegram.org/botXYZ/sendAudio",
            {"chat_id": "12345", "caption": "shadowing clip"},
            "audio",
            self.audio_path,
        )

        self.assertEqual(result, {"ok": True})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.telegram.org/botXYZ/sendAudio")
        self.assertTrue(request.headers["Content-type"].startswith("multipart/form-data; boundary="))
        body = request.data
        self.assertIn(b'name="chat_id"', body)
        self.assertIn(b"12345", body)
        self.assertIn(b'name="caption"', body)
        self.assertIn(b"shadowing clip", body)
        self.assertIn(b'name="audio"; filename="clip.mp3"', body)
        self.assertIn(b"fake-mp3-bytes", body)

    @patch("openclaw_runtime.http_client.urllib.request.urlopen")
    def test_content_type_guessed_from_filename(self, urlopen) -> None:
        response = MagicMock()
        response.read.return_value = b"{}"
        response.__enter__.return_value = response
        urlopen.return_value = response

        post_multipart_file("https://example.com/upload", {}, "audio", self.audio_path)

        request = urlopen.call_args.args[0]
        self.assertIn(b"Content-Type: audio/mpeg", request.data)


if __name__ == "__main__":
    unittest.main()
