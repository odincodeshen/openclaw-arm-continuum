import tempfile
import unittest
from pathlib import Path

from openclaw_runtime.vision_client import VisionClient, VisionError

from tests.support import build_settings


class VisionClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.image = Path(self.tmp.name) / "x.jpg"
        self.image.write_bytes(b"\xff\xd8\xff\xd9")
        self.settings = build_settings(vlm_base_url="http://vision.local/v1", vlm_model="test-vl")

    def test_uses_vlm_settings_and_returns_cleaned_text(self) -> None:
        seen = {}

        def fake_completion(payload):
            seen["payload"] = payload
            return {"choices": [{"message": {"content": "<think>hmm</think>A red door with the sign EXIT."}}]}

        client = VisionClient(self.settings)
        client._completion = fake_completion

        out = client.describe_image(self.image, "describe it")

        self.assertEqual(out, "A red door with the sign EXIT.")
        self.assertEqual(seen["payload"]["model"], "test-vl")
        content = seen["payload"]["messages"][0]["content"]
        self.assertEqual(content[0]["text"], "describe it")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_empty_description_raises(self) -> None:
        client = VisionClient(self.settings)
        client._completion = lambda payload: {"choices": [{"message": {"content": ""}}]}
        with self.assertRaises(VisionError):
            client.describe_image(self.image)


if __name__ == "__main__":
    unittest.main()
