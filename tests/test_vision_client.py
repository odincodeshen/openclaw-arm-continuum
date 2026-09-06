import tempfile
import unittest
from pathlib import Path

from openclaw_runtime.vision_client import DEFAULT_DESCRIBE_INSTRUCTION, VisionClient, VisionError


class FakeLlm:
    endpoint_id = "vision"

    def __init__(self, answer="", raises=None):
        self.answer = answer
        self.raises = raises
        self.calls = []

    def chat_with_image(self, image_path, prompt, *, max_tokens=None):
        self.calls.append({"path": image_path, "prompt": prompt, "max_tokens": max_tokens})
        if self.raises:
            raise self.raises
        return self.answer

    def is_reachable(self):
        return True


class VisionClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.image = Path(self.tmp.name) / "x.jpg"
        self.image.write_bytes(b"\xff\xd8\xff\xd9")

    def test_delegates_to_backing_client_with_default_instruction(self) -> None:
        llm = FakeLlm(answer="A red door with the sign EXIT.")
        out = VisionClient(llm).describe_image(self.image)
        self.assertEqual(out, "A red door with the sign EXIT.")
        self.assertEqual(llm.calls[0]["prompt"], DEFAULT_DESCRIBE_INSTRUCTION)

    def test_custom_instruction_is_passed_through(self) -> None:
        llm = FakeLlm(answer="ok")
        VisionClient(llm).describe_image(self.image, "just OCR", max_tokens=123)
        self.assertEqual(llm.calls[0]["prompt"], "just OCR")
        self.assertEqual(llm.calls[0]["max_tokens"], 123)

    def test_empty_description_raises_vision_error(self) -> None:
        with self.assertRaises(VisionError):
            VisionClient(FakeLlm(answer="   ")).describe_image(self.image)

    def test_backend_failure_is_wrapped(self) -> None:
        with self.assertRaises(VisionError):
            VisionClient(FakeLlm(raises=RuntimeError("endpoint down"))).describe_image(self.image)

    def test_endpoint_id_is_exposed(self) -> None:
        self.assertEqual(VisionClient(FakeLlm()).endpoint_id, "vision")


if __name__ == "__main__":
    unittest.main()
