"""Tests for audio clipping.

`clip_audio_segment` and `safe_audio_write_path` live in
app/openclaw_whisper_service.py, which -- like the rest of that service --
needs `av` and `faster_whisper` to even import. Per scripts/ci_validate.py's
own policy, whisper-dependent modules are excluded from the standard import
check and covered by their own image build/test instead, so the class below
skips cleanly (not fails) wherever `av` isn't installed, same pattern as the
Qdrant-backed L2 scenarios skipping without a live Qdrant. AudioClipClient
(the stdlib-only caller side, used by the gateway) is fully testable here
regardless, since it never imports av itself.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openclaw_runtime.audio_clip_client import AudioClipClient

from tests.support import build_settings

try:
    import av  # noqa: F401

    AV_AVAILABLE = True
except ImportError:
    AV_AVAILABLE = False

if AV_AVAILABLE:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
    from openclaw_whisper_service import clip_audio_segment, safe_audio_write_path  # noqa: E402


class AudioClipClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = build_settings(whisper_base_url="http://whisper-test", whisper_timeout=45)

    @patch("openclaw_runtime.audio_clip_client.request_json")
    def test_posts_clip_request_with_expected_fields(self, request_json) -> None:
        request_json.return_value = {"ok": True, "output_path": "/workspace/x.mp3", "duration": 42.5}
        client = AudioClipClient(self.settings)

        duration = client.clip(Path("/workspace/in.mp3"), 180.0, 240.0, Path("/workspace/out.mp3"))

        self.assertEqual(duration, 42.5)
        args, kwargs = request_json.call_args
        method, url, payload = args
        self.assertEqual(method, "POST")
        self.assertEqual(url, "http://whisper-test/clip")
        self.assertEqual(payload["path"], "/workspace/in.mp3")
        self.assertEqual(payload["output_path"], "/workspace/out.mp3")
        self.assertEqual(payload["start_seconds"], 180.0)
        self.assertEqual(payload["end_seconds"], 240.0)
        self.assertEqual(kwargs["timeout"], 45)

    @patch("openclaw_runtime.audio_clip_client.request_json")
    def test_missing_duration_in_response_defaults_to_zero(self, request_json) -> None:
        request_json.return_value = {"ok": True}
        client = AudioClipClient(self.settings)
        duration = client.clip(Path("/workspace/in.mp3"), 0.0, 10.0, Path("/workspace/out.mp3"))
        self.assertEqual(duration, 0.0)


@unittest.skipUnless(AV_AVAILABLE, "av is not installed in this environment")
class ClipAudioSegmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.input_path = self.root / "input.mp3"
        self._write_silent_mp3(self.input_path, seconds=5)

    @staticmethod
    def _write_silent_mp3(path: Path, seconds: int) -> None:
        container = av.open(str(path), mode="w")
        stream = container.add_stream("libmp3lame", rate=44100)
        frame = av.AudioFrame(format="s16", layout="mono", samples=44100)
        frame.rate = 44100
        for plane in frame.planes:
            plane.update(bytes(plane.buffer_size))
        for _ in range(seconds):
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
        container.close()

    def test_clip_produces_a_file_within_requested_duration(self) -> None:
        output_path = self.root / "clip.mp3"
        duration = clip_audio_segment(self.input_path, 1.0, 3.0, output_path)
        self.assertTrue(output_path.exists())
        self.assertGreater(output_path.stat().st_size, 0)
        self.assertLessEqual(duration, 2.5)

    def test_end_before_start_raises(self) -> None:
        with self.assertRaises(ValueError):
            clip_audio_segment(self.input_path, 3.0, 1.0, self.root / "bad.mp3")

    def test_safe_audio_write_path_rejects_outside_workspace(self) -> None:
        import openclaw_whisper_service as svc

        original_root = svc.WORKSPACE_ROOT
        svc.WORKSPACE_ROOT = self.root
        try:
            with self.assertRaises(ValueError):
                safe_audio_write_path("/etc/passwd")
        finally:
            svc.WORKSPACE_ROOT = original_root

    def test_safe_audio_write_path_allows_new_file_inside_workspace(self) -> None:
        import openclaw_whisper_service as svc

        original_root = svc.WORKSPACE_ROOT
        svc.WORKSPACE_ROOT = self.root
        try:
            result = safe_audio_write_path(str(self.root / "does" / "not" / "exist.mp3"))
            self.assertTrue(result.parent.exists())
        finally:
            svc.WORKSPACE_ROOT = original_root


if __name__ == "__main__":
    unittest.main()
