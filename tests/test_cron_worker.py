import unittest
from datetime import datetime

import openclaw_cron_worker as cron_worker

from tests.support import build_settings


class ShouldRunDueWindowTest(unittest.TestCase):
    def test_missed_window_does_not_catch_up(self) -> None:
        # Container was down all day and restarts at 20:00, never ran today.
        settings = build_settings(cron_daily_report_time="07:00")
        self.assertFalse(cron_worker.should_run(datetime(2026, 7, 4, 20, 0), settings, {}))

    def test_within_window_runs(self) -> None:
        settings = build_settings(cron_daily_report_time="07:00")
        self.assertTrue(cron_worker.should_run(datetime(2026, 7, 4, 7, 5), settings, {}))

    def test_exact_due_time_runs(self) -> None:
        settings = build_settings(cron_daily_report_time="07:00")
        self.assertTrue(cron_worker.should_run(datetime(2026, 7, 4, 7, 0), settings, {}))

    def test_past_default_window_does_not_run(self) -> None:
        settings = build_settings(cron_daily_report_time="07:00")
        self.assertFalse(cron_worker.should_run(datetime(2026, 7, 4, 7, 20), settings, {}))

    def test_before_due_time_does_not_run(self) -> None:
        settings = build_settings(cron_daily_report_time="07:00")
        self.assertFalse(cron_worker.should_run(datetime(2026, 7, 4, 6, 59), settings, {}))

    def test_does_not_run_twice_same_day(self) -> None:
        settings = build_settings(cron_daily_report_time="07:00")
        state = {"last_daily_report_date": "2026-07-04"}
        self.assertFalse(cron_worker.should_run(datetime(2026, 7, 4, 7, 5), settings, state))

    def test_runs_again_next_day(self) -> None:
        settings = build_settings(cron_daily_report_time="07:00")
        state = {"last_daily_report_date": "2026-07-04"}
        self.assertTrue(cron_worker.should_run(datetime(2026, 7, 5, 7, 5), settings, state))

    def test_custom_window_minutes(self) -> None:
        settings = build_settings(cron_daily_report_time="07:00", cron_due_window_minutes=60)
        self.assertTrue(cron_worker.should_run(datetime(2026, 7, 4, 7, 45), settings, {}))

    def test_run_on_start_bypasses_window_once(self) -> None:
        settings = build_settings(cron_daily_report_time="07:00", cron_run_on_start=True)
        self.assertTrue(cron_worker.should_run(datetime(2026, 7, 4, 20, 0), settings, {}))

    def test_run_on_start_does_not_repeat_after_startup_done(self) -> None:
        settings = build_settings(cron_daily_report_time="07:00", cron_run_on_start=True)
        state = {"startup_run_done": True}
        self.assertFalse(cron_worker.should_run(datetime(2026, 7, 4, 20, 0), settings, state))


if __name__ == "__main__":
    unittest.main()
