import unittest
from datetime import datetime

from openclaw_runtime.cron_jobs import is_due


def daily_job(time_text: str = "07:30", created_at: int = 1000) -> dict:
    return {
        "id": "morning-brief",
        "enabled": True,
        "created_at": created_at,
        "schedule": {"type": "daily", "time": time_text},
    }


def weekly_job(weekday: int = 0, time_text: str = "08:00", created_at: int = 1000) -> dict:
    return {
        "id": "weekly-report",
        "enabled": True,
        "created_at": created_at,
        "schedule": {"type": "weekly", "weekday": weekday, "time": time_text},
    }


def monthly_job(day, time_text: str = "18:00", created_at: int = 1000) -> dict:
    return {
        "id": "monthly-review",
        "enabled": True,
        "created_at": created_at,
        "schedule": {"type": "monthly", "day": day, "time": time_text},
    }


def interval_job(seconds: int = 3600, created_at: int = 0) -> dict:
    return {
        "id": "every-hour",
        "enabled": True,
        "created_at": created_at,
        "schedule": {"type": "interval", "seconds": seconds},
    }


class DailyDueWindowTest(unittest.TestCase):
    def test_missed_window_does_not_catch_up(self) -> None:
        # container was down all day and restarts at 20:00, never ran today
        job = daily_job("07:30")
        self.assertFalse(is_due(job, datetime(2026, 7, 4, 20, 0), {}))

    def test_within_window_runs(self) -> None:
        job = daily_job("07:30")
        self.assertTrue(is_due(job, datetime(2026, 7, 4, 7, 35), {}))

    def test_exact_due_time_runs(self) -> None:
        job = daily_job("07:30")
        self.assertTrue(is_due(job, datetime(2026, 7, 4, 7, 30), {}))

    def test_past_default_window_does_not_run(self) -> None:
        job = daily_job("07:30")
        self.assertFalse(is_due(job, datetime(2026, 7, 4, 7, 46), {}))

    def test_before_due_time_does_not_run(self) -> None:
        job = daily_job("07:30")
        self.assertFalse(is_due(job, datetime(2026, 7, 4, 7, 0), {}))

    def test_does_not_run_twice_same_day(self) -> None:
        job = daily_job("07:30")
        state: dict = {}
        now = datetime(2026, 7, 4, 7, 35)
        self.assertTrue(is_due(job, now, state))
        state["job_last_runs"][job["id"]] = {"date": now.strftime("%Y-%m-%d")}
        self.assertFalse(is_due(job, datetime(2026, 7, 4, 7, 40), state))

    def test_custom_window_minutes(self) -> None:
        job = daily_job("07:30")
        now = datetime(2026, 7, 4, 8, 0)
        self.assertFalse(is_due(job, now, {}, window_minutes=15))
        self.assertTrue(is_due(job, now, {}, window_minutes=60))

    def test_disabled_job_never_runs(self) -> None:
        job = daily_job("07:30")
        job["enabled"] = False
        self.assertFalse(is_due(job, datetime(2026, 7, 4, 7, 30), {}))


class WeeklyDueWindowTest(unittest.TestCase):
    def test_wrong_weekday_does_not_run(self) -> None:
        job = weekly_job(weekday=0)  # Monday
        tuesday = datetime(2026, 7, 7, 8, 5)  # 2026-07-07 is a Tuesday
        self.assertFalse(is_due(job, tuesday, {}))

    def test_missed_window_on_correct_weekday_does_not_catch_up(self) -> None:
        job = weekly_job(weekday=0, time_text="08:00")
        monday_evening = datetime(2026, 7, 6, 20, 0)  # 2026-07-06 is a Monday
        self.assertFalse(is_due(job, monday_evening, {}))

    def test_within_window_on_correct_weekday_runs(self) -> None:
        job = weekly_job(weekday=0, time_text="08:00")
        monday_morning = datetime(2026, 7, 6, 8, 10)
        self.assertTrue(is_due(job, monday_morning, {}))


class MonthlyDueWindowTest(unittest.TestCase):
    def test_wrong_day_does_not_run(self) -> None:
        job = monthly_job(day=1, time_text="18:00")
        self.assertFalse(is_due(job, datetime(2026, 7, 15, 18, 5), {}))

    def test_missed_window_does_not_catch_up(self) -> None:
        job = monthly_job(day=1, time_text="18:00")
        self.assertFalse(is_due(job, datetime(2026, 7, 1, 23, 0), {}))

    def test_within_window_runs(self) -> None:
        job = monthly_job(day=1, time_text="18:00")
        self.assertTrue(is_due(job, datetime(2026, 7, 1, 18, 5), {}))

    def test_last_day_of_month_resolves_correctly(self) -> None:
        job = monthly_job(day="last", time_text="18:00")
        self.assertTrue(is_due(job, datetime(2026, 7, 31, 18, 5), {}))
        self.assertFalse(is_due(job, datetime(2026, 7, 30, 18, 5), {}))


class IntervalJobTest(unittest.TestCase):
    def test_not_due_before_interval_elapses(self) -> None:
        job = interval_job(seconds=3600)
        state = {"job_last_runs": {job["id"]: {"timestamp": int(datetime(2026, 7, 4, 8, 0).timestamp())}}}
        self.assertFalse(is_due(job, datetime(2026, 7, 4, 8, 30), state))

    def test_due_after_interval_elapses(self) -> None:
        job = interval_job(seconds=3600)
        state = {"job_last_runs": {job["id"]: {"timestamp": int(datetime(2026, 7, 4, 8, 0).timestamp())}}}
        self.assertTrue(is_due(job, datetime(2026, 7, 4, 9, 1), state))


if __name__ == "__main__":
    unittest.main()
