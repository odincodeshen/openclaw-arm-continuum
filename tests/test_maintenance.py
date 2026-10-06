import importlib.util
import tarfile
import tempfile
import unittest
from pathlib import Path

_path = Path(__file__).resolve().parent.parent / "scripts" / "openclaw_maintenance.py"
_spec = importlib.util.spec_from_file_location("openclaw_maintenance", _path)
maintenance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(maintenance)


class SnapshotSelectionTest(unittest.TestCase):
    def test_throwaway_collections_are_not_backed_up(self) -> None:
        names = ["apa_tracker_memory", "verify_1791000000_ab12cd_knowledge", "e2e_1234_cat_x", "perf_9_tracker",
                 "evalcopy_1_0", "oc_cat_trip"]
        snapshotted = []

        def fake_qdrant(method, path):
            if path == "/collections":
                return {"result": {"collections": [{"name": n} for n in names]}}
            if method == "POST":
                snapshotted.append(path.split("/")[2])
                return {"result": {"name": "s"}}
            return {}

        class Body:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, *args):
                return b""

        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp, patch.object(maintenance, "_qdrant", fake_qdrant), \
                patch.object(maintenance.urllib.request, "urlopen", lambda *a, **k: Body()):
            kept = maintenance.snapshot_collections(Path(tmp))
        self.assertEqual(kept, ["apa_tracker_memory", "oc_cat_trip"])
        self.assertEqual(snapshotted, kept)


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


class VerifyRunTest(unittest.TestCase):
    def run_verify(self, returncode, stdout):
        from unittest.mock import patch
        import subprocess as sp
        alerts = []
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(maintenance, "VERIFY_STATUS", Path(tmp) / "status.json"), \
                patch.object(maintenance, "_alert", alerts.append), \
                patch.object(maintenance.subprocess, "run",
                             lambda *a, **k: sp.CompletedProcess(a[0], returncode, stdout, "")):
            code = maintenance.verify("full", tmp)
            status = __import__("json").loads((Path(tmp) / "status.json").read_text())
        return code, status, alerts

    def test_pass_records_status_without_alert(self):
        code, status, alerts = self.run_verify(0, "   PASS help_menu  0.0s\nAll passed -- report: x\n")
        self.assertEqual((code, status["ok"], alerts), (0, True, []))

    def test_failure_alerts_with_the_failed_scenarios(self):
        out = "   FAIL photo_text  25.8s  step 1: missing\n   PASS help_menu  0.0s\nFAILED -- report: x\n"
        code, status, alerts = self.run_verify(1, out)
        self.assertEqual(code, 1)
        self.assertEqual(status["failed"], ["photo_text"])
        self.assertIn("photo_text", alerts[0])

    def test_watchdog_summary_shows_it(self):
        import importlib.util as iu
        spec = iu.spec_from_file_location("wd", Path(__file__).resolve().parent.parent / "scripts" / "openclaw_watchdog.py")
        wd = iu.module_from_spec(spec)
        spec.loader.exec_module(wd)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".cache").mkdir()
            (Path(tmp) / ".cache" / "openclaw-verify-status.json").write_text(
                '{"at": 1, "ok": false, "mode": "full", "failed": ["photo_text"], "warnings": ["x"]}')
            lines = wd.maintenance_lines(Path(tmp), now=2)
        self.assertIn("Verify (full): FAILED -- photo_text · 1 warning(s)", lines)
