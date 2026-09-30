import importlib.util
import tarfile
import tempfile
import unittest
from pathlib import Path

_path = Path(__file__).resolve().parent.parent / "scripts" / "openclaw_maintenance.py"
_spec = importlib.util.spec_from_file_location("openclaw_maintenance", _path)
maintenance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(maintenance)


class BackupFilesTest(unittest.TestCase):
    def test_archive_keeps_data_and_skips_rebuildable_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "profiles/bot/workspace"
            for rel in ("inbox/categories/x/a.pdf", ".openclaw/night/1/2026-09-28.json", "inbox/tracker/t.md",
                        "dictionary/ecdict.sqlite", "inbox/english_bot/wk2/episode.mp3",
                        "inbox/audio/telegram/v.oga", "inbox/.staging/telegram/s.pdf"):
                (ws / rel).parent.mkdir(parents=True, exist_ok=True)
                (ws / rel).write_text("x")
            (root / "profiles/bot/.env").write_text("TOKEN=x")
            (root / "gateway-data/state").mkdir(parents=True)
            (root / "gateway-data/state/openclaw.sqlite").write_text("db")
            target = root / "files.tar.gz"
            maintenance.archive_files(target, root)
            names = set(tarfile.open(target).getnames())
        for kept in ("profiles/bot/workspace/inbox/categories/x/a.pdf",
                     "profiles/bot/workspace/.openclaw/night/1/2026-09-28.json",
                     "profiles/bot/.env", "gateway-data/state/openclaw.sqlite"):
            self.assertIn(kept, names)
        for skipped in ("dictionary", "english_bot", "audio", ".staging"):
            self.assertFalse(any(f"/{skipped}/" in name or name.endswith(f"/{skipped}") for name in names), skipped)

    def test_prune_keeps_the_newest_and_ignores_other_folders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for name in ("2026-09-27_0315", "2026-09-28_0315", "2026-09-29_0315", "my-manual-backup"):
                (folder / name).mkdir()
            removed = maintenance.prune_backups(folder, 2)
            self.assertEqual(removed, ["2026-09-27_0315"])
            self.assertEqual(sorted(p.name for p in folder.iterdir()),
                             ["2026-09-28_0315", "2026-09-29_0315", "my-manual-backup"])


if __name__ == "__main__":
    unittest.main()
