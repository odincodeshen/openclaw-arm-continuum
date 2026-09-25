import unittest
from pathlib import Path

from openclaw_runtime.whisper_paths import to_whisper_path

from tests.support import build_settings


class ToWhisperPathTest(unittest.TestCase):
    def test_no_workspace_root_configured_leaves_path_unchanged(self) -> None:
        settings = build_settings(whisper_workspace_root="")
        result = to_whisper_path(Path("/workspace/inbox/english_bot/wk1/episode.mp3"), settings)
        self.assertEqual(result, Path("/workspace/inbox/english_bot/wk1/episode.mp3"))

    def test_translates_workspace_prefix_to_persona_namespaced_root(self) -> None:
        settings = build_settings(whisper_workspace_root="/profiles/lc9_dgx4_en/workspace")
        result = to_whisper_path(Path("/workspace/inbox/english_bot/wk1/episode.mp3"), settings)
        self.assertEqual(result, Path("/profiles/lc9_dgx4_en/workspace/inbox/english_bot/wk1/episode.mp3"))

    def test_path_not_under_local_workspace_root_is_left_unchanged(self) -> None:
        """Defensive: if some caller ever passes a path that isn't under the
        expected /workspace mount, don't silently mangle it into something
        wrong -- leave it alone so a real bug is visible instead of hidden
        behind an incorrect translated path."""
        settings = build_settings(whisper_workspace_root="/profiles/lc9_dgx4_en/workspace")
        result = to_whisper_path(Path("/tmp/elsewhere/file.mp3"), settings)
        self.assertEqual(result, Path("/tmp/elsewhere/file.mp3"))


if __name__ == "__main__":
    unittest.main()
