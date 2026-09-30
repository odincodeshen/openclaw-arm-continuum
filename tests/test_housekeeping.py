import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from openclaw_runtime.housekeeping import run_cleanup, write_status

DAY = 86400.0
NOW = 1_800_000_000.0


def _file(path: Path, size: int, age_days: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (NOW - age_days * DAY, NOW - age_days * DAY))
    return path


class CleanupTest(unittest.TestCase):
    def test_only_old_short_lived_files_go(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            old_voice = _file(ws / "inbox/audio/telegram/old.oga", 10, 20)
            new_voice = _file(ws / "inbox/audio/telegram/new.oga", 10, 3)
            old_export = _file(ws / ".openclaw/exports/words.txt", 5, 9)
            staged = _file(ws / "inbox/.staging/telegram/a.pdf", 7, 8)
            for week in (1, 2, 3):
                _file(ws / f"inbox/english_bot/wk{week}/episode.mp3", 100, 1)
            doc = _file(ws / "inbox/categories/x/doc.pdf", 50, 400)
            night = _file(ws / ".openclaw/night/1/2026-01-01.json", 5, 400)

            result = run_cleanup(ws, now=NOW)

            self.assertFalse(old_voice.exists())
            self.assertTrue(new_voice.exists())
            self.assertFalse(old_export.exists())
            self.assertFalse(staged.exists())
            self.assertEqual(sorted(p.name for p in (ws / "inbox/english_bot").iterdir()), ["wk2", "wk3"])
            self.assertTrue(doc.exists() and night.exists())
            self.assertEqual((result.files, result.bytes), (4, 122))
            self.assertEqual(result.by_area, {"voice": 10, "exports": 5, "staging": 7, "english audio": 100})
            write_status(ws, result, now=NOW)
            status = json.loads((ws / ".openclaw/housekeeping.json").read_text())
            self.assertEqual(status["bytes"], 122)

    def test_missing_folders_are_fine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run_cleanup(Path(tmp), now=NOW).files, 0)


class HousekeepingDueTest(unittest.TestCase):
    def test_once_a_day_after_the_time(self) -> None:
        import openclaw_telegram_gateway as gateway

        tz = ZoneInfo("Europe/London")
        self.assertFalse(gateway.housekeeping_due(datetime(2026, 9, 30, 3, 0, tzinfo=tz), ""))
        self.assertTrue(gateway.housekeeping_due(datetime(2026, 9, 30, 3, 31, tzinfo=tz), "2026-09-29"))
        self.assertFalse(gateway.housekeeping_due(datetime(2026, 9, 30, 9, 0, tzinfo=tz), "2026-09-30"))


if __name__ == "__main__":
    unittest.main()
