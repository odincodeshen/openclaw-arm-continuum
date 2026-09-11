import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import openclaw_cron_worker as cron_worker
from openclaw_runtime.skills.base import SkillResult

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


class _FakeRouter:
    def __init__(self, result: SkillResult) -> None:
        self.result = result

    def route(self, prompt: str) -> SkillResult:
        return self.result


class RunDynamicJobTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = build_settings(cron_chat_ids={123}, inbox_path=Path(self.tmp.name))
        self.sent: list[tuple[int, str]] = []
        self._orig_send = cron_worker.send_message
        cron_worker.send_message = lambda settings, chat_id, text: self.sent.append((chat_id, text))
        self.addCleanup(setattr, cron_worker, "send_message", self._orig_send)

    def _job(self) -> dict:
        return {"id": "j1", "name": "Memory digest", "prompt": "/mem digest", "chat_id": 123}

    def test_normal_result_is_delivered(self) -> None:
        router = _FakeRouter(SkillResult("memory_write", "3 things due this week"))
        result = cron_worker.run_dynamic_job(self.settings, router, datetime(2026, 1, 1, 8, 0), self._job())
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["delivered"])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("3 things due this week", self.sent[0][1])

    def test_suppressed_result_is_recorded_but_not_delivered(self) -> None:
        router = _FakeRouter(
            SkillResult("memory_write", "You're all caught up.", suppress_if_routine=True)
        )
        result = cron_worker.run_dynamic_job(self.settings, router, datetime(2026, 1, 1, 8, 0), self._job())
        self.assertEqual(result["status"], "skipped")
        self.assertFalse(result["delivered"])
        self.assertEqual(self.sent, [])
        self.assertTrue(Path(result["path"]).is_file())

    def test_router_exception_is_an_error_not_a_skip(self) -> None:
        # An error still gets delivered (you want to know a job failed) --
        # only a routine "nothing new" result is suppressed.
        class _RaisingRouter:
            def route(self, prompt: str) -> SkillResult:
                raise RuntimeError("boom")

        result = cron_worker.run_dynamic_job(self.settings, _RaisingRouter(), datetime(2026, 1, 1, 8, 0), self._job())
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["delivered"])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Task failed: boom", self.sent[0][1])


class WriteGatewayRunbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = build_settings()
        self.captured: list[dict] = []
        self._orig = cron_worker.update_gateway_job_state
        cron_worker.update_gateway_job_state = lambda settings, job_id, state: self.captured.append(state)
        self.addCleanup(setattr, cron_worker, "update_gateway_job_state", self._orig)

    def _run(self, gateway_state: dict, result: dict) -> dict:
        job = {"id": "j1", "gateway_state": gateway_state}
        cron_worker.write_gateway_runback(self.settings, job, datetime(2026, 1, 1), result)
        return self.captured[-1]

    def test_error_increments_errors_and_resets_skipped(self) -> None:
        state = self._run(
            {"consecutiveErrors": 2, "consecutiveSkipped": 3},
            {"status": "error", "error": "boom", "delivered": False, "duration_ms": 5},
        )
        self.assertEqual(state["consecutiveErrors"], 3)
        self.assertEqual(state["consecutiveSkipped"], 0)
        self.assertEqual(state["lastDeliveryStatus"], "not-delivered")
        self.assertEqual(state["lastError"], "boom")

    def test_skipped_increments_skipped_and_resets_errors(self) -> None:
        state = self._run(
            {"consecutiveErrors": 2, "consecutiveSkipped": 1},
            {"status": "skipped", "error": None, "delivered": False, "duration_ms": 5},
        )
        self.assertEqual(state["consecutiveErrors"], 0)
        self.assertEqual(state["consecutiveSkipped"], 2)
        self.assertEqual(state["lastDeliveryStatus"], "skipped")
        self.assertEqual(state["lastRunStatus"], "skipped")
        self.assertIsNone(state["lastError"])

    def test_ok_resets_both_counters(self) -> None:
        state = self._run(
            {"consecutiveErrors": 2, "consecutiveSkipped": 3},
            {"status": "ok", "error": None, "delivered": True, "duration_ms": 5},
        )
        self.assertEqual(state["consecutiveErrors"], 0)
        self.assertEqual(state["consecutiveSkipped"], 0)
        self.assertEqual(state["lastDeliveryStatus"], "delivered")


if __name__ == "__main__":
    unittest.main()
