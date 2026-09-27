import unittest
from types import SimpleNamespace
from unittest.mock import patch

import openclaw_cron_worker as cron_worker
import openclaw_memory_watcher as watcher

from tests.support import build_settings


def _result(path, skipped, reason="", collection="c", chunks=1):
    return SimpleNamespace(path=path, skipped=skipped, reason=reason, collection=collection, chunks=chunks)


class WatcherSkipLoggingTest(unittest.TestCase):
    def test_each_skipped_file_is_logged_once_across_scans(self) -> None:
        logged: list[str] = []
        seen: set = set()
        scan = [_result("/inbox/a.jpg", True, "unsupported_suffix"), _result("/inbox/b.md", True, "unchanged")]
        with patch.object(watcher, "log", logged.append):
            for _ in range(5):
                watcher.report_results(scan, seen)
        self.assertEqual(logged, ["[watcher] skipped path=/inbox/a.jpg reason=unsupported_suffix"])

    def test_new_reason_for_the_same_file_is_logged_and_ingests_always_are(self) -> None:
        logged: list[str] = []
        seen: set = set()
        with patch.object(watcher, "log", logged.append):
            watcher.report_results([_result("/inbox/a.md", True, "empty")], seen)
            watcher.report_results([_result("/inbox/a.md", True, "too_large")], seen)
            watcher.report_results([_result("/inbox/a.md", False)], seen)
            watcher.report_results([_result("/inbox/a.md", False)], seen)
        self.assertEqual(len(logged), 4)
        self.assertTrue(logged[2].startswith("[watcher] ingested path=/inbox/a.md"))


class CronJobPollLoggingTest(unittest.TestCase):
    def setUp(self) -> None:
        cron_worker._last_job_poll_note = None
        self.addCleanup(setattr, cron_worker, "_last_job_poll_note", None)
        self.logged: list[str] = []
        patcher = patch.object(cron_worker, "log", self.logged.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = build_settings(cron_chat_ids={1})

    def test_only_changes_are_logged(self) -> None:
        polls = [[], [], [{"id": "j"}], [{"id": "j"}], []]
        with patch.object(cron_worker, "list_gateway_jobs", side_effect=polls), \
                patch.object(cron_worker, "gateway_job_to_runtime", lambda job, chat: job):
            for _ in polls:
                cron_worker.load_dynamic_jobs(self.settings)
        self.assertEqual(
            self.logged,
            [
                "[cron] loaded 0 Gateway dashboard job(s)",
                "[cron] loaded 1 Gateway dashboard job(s)",
                "[cron] loaded 0 Gateway dashboard job(s)",
            ],
        )

    def test_outage_is_logged_once_and_recovery_is_logged(self) -> None:
        outcomes = [RuntimeError("HTTP 404"), RuntimeError("HTTP 404"), RuntimeError("HTTP 404"), []]

        def fake_list(settings, include_disabled=True):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with patch.object(cron_worker, "list_gateway_jobs", fake_list), \
                patch.object(cron_worker, "load_jobs", lambda path: {"jobs": []}):
            for _ in range(4):
                cron_worker.load_dynamic_jobs(self.settings)
        self.assertEqual(len(self.logged), 2)
        self.assertIn("RPC unavailable", self.logged[0])
        self.assertEqual(self.logged[1], "[cron] loaded 0 Gateway dashboard job(s)")


if __name__ == "__main__":
    unittest.main()
